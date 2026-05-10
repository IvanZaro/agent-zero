from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from usr.plugins.autoresearch.coder.base import Coder, EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, run_eval
from usr.plugins.autoresearch.harness.judge import judge_score
from usr.plugins.autoresearch.harness.smoke import smoke_check
from usr.plugins.autoresearch.state._path_safety import EditablePathError, validate_editable_path
from usr.plugins.autoresearch.state.git_ops import apply_edit_patch, commit_experiment, revert_to_baseline, start_run_branch
from usr.plugins.autoresearch.state.runs import (
    ExperimentRecord,
    RunState,
    SuiteDriftError,
    compute_suite_hash,
    write_config,
)
import usr.plugins.autoresearch.worker.kill_handler as kill_handler
from usr.plugins.autoresearch.worker._artifacts import (
    git_diff_patch,
    write_atomic,
    write_experiment_artifacts,
)
from usr.plugins.autoresearch.worker._program_md import ProgramMd, ProgramMdError, parse_program_md
from usr.plugins.autoresearch.worker.cost_meter import BudgetExceeded, CostMeter

log = logging.getLogger(__name__)

_DEFAULT_SUITE = Path(__file__).parent.parent / "tests" / "fixtures" / "suite.json"


class EvalUnusableError(Exception):
    """Raised when all eval tasks crash — caller aborts the run."""

    def __init__(self, record: ExperimentRecord) -> None:
        self.record = record
        super().__init__(f"All eval tasks crashed in exp {record.n}")


class BaselineUnusableError(Exception):
    """Raised when the baseline eval is structurally broken (all tasks errored)."""

    def __init__(self, baseline: "EvalResult") -> None:
        self.baseline = baseline
        super().__init__(
            f"Baseline eval is structurally broken: 0/{baseline.total} passed, all tasks errored"
        )


@dataclass(frozen=True)
class RunContext:
    cfg: ProgramMd
    prose: str
    suite_path: Path
    suite_hash: str
    baseline_eval: EvalResult
    state: RunState
    target: Path               # resolved absolute path to prompt file
    profile_root: Path
    runs_root: Path
    program_md_text: str       # raw text snapshot for artifacts


def _find_repo_root(start: Path) -> Path:
    for parent in [start, *start.parents]:
        if (parent / "agent.py").exists():
            return parent
    return Path.cwd()


def _check_clean_worktree(repo_root: Path) -> None:
    """Raise RuntimeError if there are uncommitted changes in the worktree (E12)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()}")
    if result.stdout.strip():
        raise RuntimeError(
            f"Dirty worktree detected before starting run — commit or stash changes first.\n"
            f"git status output:\n{result.stdout}"
        )


async def _run_baseline_eval(
    target: Path,
    suite_path: Path,
    eval_model: str,
    parallelism: int,
) -> EvalResult:
    prompt_text = target.read_text(encoding="utf-8")
    return await run_eval(
        prompt_text=prompt_text,
        suite_path=suite_path,
        model=eval_model,
        parallelism=parallelism,
    )


def _resolve_eval_model(cfg: ProgramMd) -> str:
    import os
    if cfg.eval_model:
        return cfg.eval_model
    env_eval = os.environ.get("AUTORESEARCH_EVAL_MODEL")
    if env_eval:
        return env_eval
    # Final fallback: a cheap, stable model. Using gpt-4o-mini matches
    # the previous fallback so unconfigured runs don't change behaviour.
    return "openrouter/openai/gpt-4o-mini"


async def run_loop(
    run_id: str,
    program_md_path: Path,
    max_experiments_override: int | None = None,
    cost_cap_override: Decimal | None = None,
    coder: Coder | None = None,
    cost_meter: CostMeter | None = None,
    runs_root: Path | None = None,
) -> RunState:
    """Full experiment loop — orchestrates coder, eval, git, state, and artifacts."""
    import shutil

    repo_root = _find_repo_root(program_md_path.resolve())
    program_md_text = program_md_path.read_text(encoding="utf-8")

    # --- Parse program.md ---
    try:
        cfg, prose = parse_program_md(program_md_path)
    except ProgramMdError as exc:
        raise ValueError(f"program.md parse error: {exc}") from exc

    max_experiments = max_experiments_override if max_experiments_override is not None else cfg.max_experiments
    cost_cap_usd = cost_cap_override if cost_cap_override is not None else cfg.cost_cap_usd

    # --- Resolve paths ---
    profile_root = repo_root / "agents" / cfg.profile
    target_raw = profile_root / cfg.prompt_file

    try:
        target = validate_editable_path(target_raw, profile_root)
    except EditablePathError as exc:
        raise ValueError(f"Invalid prompt_file path: {exc}") from exc

    suite_path = Path(cfg.eval_suite)
    if not suite_path.is_absolute():
        suite_path = repo_root / suite_path

    # --- E12: dirty worktree check ---
    _check_clean_worktree(repo_root)

    # --- Eval model ---
    eval_model = _resolve_eval_model(cfg)

    # --- Run roots ---
    effective_runs_root = runs_root if runs_root is not None else repo_root / "usr" / "autoresearch" / "runs"
    run_dir = effective_runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- Suite hash (for E11 drift detection) ---
    suite_hash = compute_suite_hash(suite_path)

    # --- write_config (immutable per run) ---
    write_config(
        run_id=run_id,
        runs_root=effective_runs_root,
        suite_hash=suite_hash,
        profile=cfg.profile,
        suite_path=str(suite_path),
    )

    # --- Snapshot program.md ---
    write_atomic(run_dir / "program.md", program_md_text)

    # --- baseline_eval: run suite ONCE before any edit ---
    log.info("run %s: computing baseline eval", run_id)
    baseline_eval = await _run_baseline_eval(target, suite_path, eval_model, cfg.parallelism)
    from dataclasses import asdict
    write_atomic(
        run_dir / "baseline_eval.json",
        json.dumps(asdict(baseline_eval), indent=2),
    )

    # E5b: baseline structurally broken — abort before any experiments.
    # baseline_eval.json is already written above; preserve it for diagnostics.
    if (
        baseline_eval.total > 0
        and baseline_eval.passed == 0
        and all(t.error is not None for t in baseline_eval.per_task)
    ):
        abort_state = RunState(
            run_id=run_id,
            status="aborted",
            experiments=[],
            spend_usd=Decimal("0"),
            started_at=datetime.now(tz=timezone.utc),
            baseline_sha="",
            _run_dir=run_dir,
        )
        abort_state.save()
        log.error(
            "Baseline eval is structurally broken: 0/%d passed, all tasks errored. "
            "Aborting run before any experiments. First task error: %s",
            baseline_eval.total,
            baseline_eval.per_task[0].error,
        )
        return abort_state

    # --- Git branch ---
    baseline_sha = start_run_branch(run_id, repo_root)

    # --- Build coder if not injected ---
    if coder is None:
        if cost_meter is None:
            from usr.plugins.autoresearch.coder._cost_rates import load_rates
            cost_meter = CostMeter(cap_usd=cost_cap_usd, rates=load_rates())
        if cfg.backend == "claude_code":
            from usr.plugins.autoresearch.coder.claude_code_coder import ClaudeCodeCoder
            coder = ClaudeCodeCoder(model=cfg.coder_model or "claude-sonnet-4-5", cost_meter=cost_meter)
        else:
            from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder
            coder = LiteLLMCoder(model=cfg.coder_model, cost_meter=cost_meter)
    elif cost_meter is None:
        from usr.plugins.autoresearch.coder._cost_rates import load_rates
        cost_meter = CostMeter(cap_usd=cost_cap_usd, rates=load_rates())

    # --- RunState ---
    state = RunState(
        run_id=run_id,
        status="running",
        experiments=[],
        spend_usd=Decimal("0"),
        started_at=datetime.now(tz=timezone.utc),
        baseline_sha=baseline_sha,
        _run_dir=run_dir,
    )
    state.save()

    # --- Kill handler ---
    kill_handler.register(baseline_sha, repo_root)

    # Track mutable baseline (rotates on kept)
    current_baseline_eval = baseline_eval

    try:
        for n in range(1, max_experiments + 1):
            try:
                rec = await _run_experiment(
                    n=n,
                    state=state,
                    cfg=cfg,
                    prose=prose,
                    target=target,
                    suite_path=suite_path,
                    baseline_eval=current_baseline_eval,
                    eval_model=eval_model,
                    coder=coder,
                    repo_root=repo_root,
                    run_dir=run_dir,
                    program_md_text=program_md_text,
                    effective_runs_root=effective_runs_root,
                )
            except EvalUnusableError as exc:
                state.experiments.append(exc.record)
                state.status = "aborted"
                state.save()
                return state
            except BudgetExceeded:
                state.status = "cost_capped"
                state.save()
                return state
            except SuiteDriftError:
                state.status = "aborted"
                state.save()
                return state

            state.experiments.append(rec)
            state.spend_usd = coder.report_cost()
            state.save()

            # Rotate baseline on kept
            if rec.outcome == "kept" and rec.eval_result is not None:
                current_baseline_eval = rec.eval_result
                state.baseline_sha = rec.sha  # type: ignore[assignment]
                kill_handler.update_baseline(rec.sha)  # type: ignore[arg-type]
                state.save()

        state.status = "completed"
        state.save()
        return state

    except SuiteDriftError:
        state.status = "aborted"
        state.save()
        return state


async def _run_experiment(
    *,
    n: int,
    state: RunState,
    cfg: ProgramMd,
    prose: str,
    target: Path,
    suite_path: Path,
    baseline_eval: EvalResult,
    eval_model: str,
    coder: Coder,
    repo_root: Path,
    run_dir: Path,
    program_md_text: str,
    effective_runs_root: Path,
) -> ExperimentRecord:
    started_at = datetime.now(tz=timezone.utc)
    trace: list[str] = [f"[exp-{n:03d}] started at {started_at.isoformat()}"]
    exp_dir = run_dir / f"exp-{n:03d}"
    outcome = "crashed"
    sha: str | None = None
    rationale: str | None = None
    eval_result: EvalResult | None = None
    diff_patch: str | None = None
    judge: object | None = None

    # E11: suite drift check before each experiment
    state.verify_suite_hash(suite_path, runs_root=effective_runs_root)

    try:
        # --- 1. Coder ---
        trace.append(f"[exp-{n:03d}] calling coder ({coder.name})")
        try:
            patch = await coder.propose_edit(
                program_md=prose,
                file_text=target.read_text(encoding="utf-8"),
                recent_history=state.experiments[-5:],
            )
            # Assign correct target_path (coder returns __pending__)
            patch = EditPatch(
                target_path=target,
                old_text=patch.old_text,
                new_text=patch.new_text,
                rationale=patch.rationale,
            )
            rationale = patch.rationale
            trace.append(f"[exp-{n:03d}] coder proposed: {rationale!r}")
        except BudgetExceeded:
            # Propagate — caller handles
            raise
        except Exception as exc:
            trace.append(f"[exp-{n:03d}] coder_failed: {exc}")
            log.warning("exp %d coder_failed: %s", n, exc)
            outcome = "coder_failed"
            return _finalise(
                n, outcome, sha, started_at, rationale, eval_result, diff_patch, judge,
                trace, exp_dir, program_md_text, coder,
            )

        # --- 2. Apply patch (E2 uniqueness) ---
        apply_result = apply_edit_patch(patch, repo_root)
        if not apply_result.ok:
            trace.append(f"[exp-{n:03d}] patch_failed: {apply_result.error}")
            log.warning("exp %d patch_failed: %s", n, apply_result.error)
            outcome = "patch_failed"
            return _finalise(
                n, outcome, sha, started_at, rationale, eval_result, diff_patch, judge,
                trace, exp_dir, program_md_text, coder,
            )

        # --- 3. Smoke check (E3 — does NOT spend eval budget) ---
        smoke = smoke_check(target.parent.parent)  # profile_root
        trace.append(f"[exp-{n:03d}] smoke: ok={smoke.ok} took={smoke.took_ms}ms")
        if not smoke.ok:
            trace.append(f"[exp-{n:03d}] smoke_failed: {smoke.error}")
            revert_to_baseline(state.baseline_sha, repo_root)
            outcome = "smoke_failed"
            return _finalise(
                n, outcome, sha, started_at, rationale, eval_result, diff_patch, judge,
                trace, exp_dir, program_md_text, coder,
            )

        # --- 4. Eval ---
        eval_result = await run_eval(
            prompt_text=target.read_text(encoding="utf-8"),
            suite_path=suite_path,
            model=eval_model,
            parallelism=cfg.parallelism,
        )
        trace.append(
            f"[exp-{n:03d}] eval: passed={eval_result.passed}/{eval_result.total} "
            f"tokens={eval_result.total_tokens}"
        )

        # E5: all tasks crashed
        if eval_result.passed == 0 and all(t.error for t in eval_result.per_task):
            revert_to_baseline(state.baseline_sha, repo_root)
            outcome = "eval_unusable"
            rec = _finalise(
                n, outcome, sha, started_at, rationale, eval_result, diff_patch, judge,
                trace, exp_dir, program_md_text, coder,
            )
            raise EvalUnusableError(rec)

        # --- 5. Judge (observability only; never gates) ---
        if cfg.judge_model:
            try:
                judge = await judge_score(eval_result, baseline_eval, cfg.judge_model)
                trace.append(f"[exp-{n:03d}] judge score={getattr(judge, 'score', None)}")
            except Exception as exc:
                log.warning("exp %d judge failed: %s", n, exc)
                judge = None

        # --- 6. Keep or revert ---
        improved = (
            eval_result.passed >= baseline_eval.passed
            and eval_result.total_tokens <= baseline_eval.total_tokens * 1.10
        )
        if improved and eval_result.passed > baseline_eval.passed:
            # Strict improvement required (>= same pass rate AND strictly more passes)
            improved = True
        elif eval_result.passed > baseline_eval.passed:
            improved = True
        else:
            improved = False

        if improved:
            commit_msg = (
                f"autoresearch: exp-{n:03d} improved "
                f"({eval_result.passed}/{eval_result.total}) — {rationale}"
            )
            sha = commit_experiment(state.run_id, n, commit_msg, repo_root)
            prompt_file_rel = str(target.relative_to(repo_root))
            diff_patch = git_diff_patch(state.baseline_sha, prompt_file_rel, repo_root)
            outcome = "kept"
            trace.append(f"[exp-{n:03d}] kept: committed {sha}")
        else:
            revert_to_baseline(state.baseline_sha, repo_root)
            prompt_file_rel = str(target.relative_to(repo_root))
            diff_patch = git_diff_patch(state.baseline_sha, prompt_file_rel, repo_root)
            outcome = "reverted"
            trace.append(f"[exp-{n:03d}] reverted to {state.baseline_sha}")

    except (EvalUnusableError, BudgetExceeded, SuiteDriftError):
        raise
    except Exception as exc:
        log.error("exp %d unexpected error: %s", n, exc)
        try:
            revert_to_baseline(state.baseline_sha, repo_root)
        except Exception:
            pass
        outcome = "crashed"
        trace.append(f"[exp-{n:03d}] crashed: {exc}")

    return _finalise(
        n, outcome, sha, started_at, rationale, eval_result, diff_patch, judge,
        trace, exp_dir, program_md_text, coder,
    )


def _finalise(
    n: int,
    outcome: str,
    sha: str | None,
    started_at: datetime,
    rationale: str | None,
    eval_result: EvalResult | None,
    diff_patch: str | None,
    judge: object | None,
    trace: list[str],
    exp_dir: Path,
    program_md_text: str,
    coder: Coder,
) -> ExperimentRecord:
    finished_at = datetime.now(tz=timezone.utc)
    spend_usd = coder.report_cost()

    rec = ExperimentRecord(
        n=n,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,  # type: ignore[arg-type]
        eval_result=eval_result,
        judge_score=getattr(judge, "score", None) if judge is not None else None,
        spend_usd=spend_usd,
        sha=sha,
        rationale=rationale,
    )

    write_experiment_artifacts(
        exp_dir=exp_dir,
        n=n,
        outcome=outcome,
        sha=sha,
        started_at=started_at,
        finished_at=finished_at,
        spend_usd=spend_usd,
        rationale=rationale,
        eval_result=eval_result,
        diff_patch=diff_patch,
        program_md_text=program_md_text,
        trace_lines=trace,
        judge_score=judge,
    )

    return rec
