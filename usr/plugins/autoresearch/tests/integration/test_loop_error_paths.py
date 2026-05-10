"""E1–E14 error path coverage for worker/loop.py. TDD: written before implementation."""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.worker.cost_meter import BudgetExceeded, CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _current_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _setup_repo(tmp_path: Path, max_experiments: int = 2) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text(
        "You are a trading assistant.\nBuy when momentum is positive.\n"
    )
    (tmp_path / "agents" / "trader" / "_context.yaml").write_text(
        "title: Trader\ndescription: test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    (tmp_path / "suite.json").write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "t-001", "input": "momentum?", "assert_substring": "buy"}],
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {tmp_path / 'suite.json'}\n"
        f"max_experiments: {max_experiments}\ncost_cap_usd: '5.00'\n---\n"
        "Improve the agent.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


def _make_eval(passed: int = 1, total: int = 1, tokens: int = 50, all_crashed: bool = False) -> EvalResult:
    if all_crashed:
        return EvalResult(
            passed=0, total=1, total_tokens=0,
            per_task=[TaskOutcome(task_id="t-001", passed=False, tokens=0, output=None, error="crashed")],
        )
    return EvalResult(
        passed=passed, total=total, total_tokens=tokens,
        per_task=[TaskOutcome(task_id="t-001", passed=passed > 0, tokens=tokens, output="buy", error=None)],
    )


def _good_patch(tmp_path: Path) -> EditPatch:
    return EditPatch(
        target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
        old_text="Buy when momentum is positive.",
        new_text="Aggressively buy when momentum is positive.",
        rationale="stronger signal",
    )


# ---------------------------------------------------------------------------
# E2: patch old_text not unique → patch_failed, loop advances
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2_patch_old_text_not_unique_marks_patch_failed_and_advances(tmp_path: Path) -> None:
    program_md, _ = _setup_repo(tmp_path, max_experiments=2)

    # Make the old_text appear twice and re-commit so the tree is clean
    prompt_file = tmp_path / "agents" / "trader" / "prompts" / "system.md"
    prompt_file.write_text(
        "Buy when momentum is positive.\nBuy when momentum is positive.\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "dup"], cwd=tmp_path, check=True, capture_output=True)
    baseline_sha = _current_sha(tmp_path)

    class DupCoder:
        name = "dup"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:  # noqa: ARG002
            return EditPatch(
                target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                old_text="Buy when momentum is positive.",
                new_text="BUY ALWAYS.",
                rationale="ambiguous patch",
            )

        def report_cost(self) -> Decimal:
            return Decimal("0")

    async def mock_eval(*_args, **_kw) -> EvalResult:
        return _make_eval(passed=1)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            state = await run_loop(
                run_id="e2-test-001",
                program_md_path=program_md,
                max_experiments_override=2,
                coder=DupCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
            )

    assert len(state.experiments) == 2
    assert state.experiments[0].outcome == "patch_failed"
    assert state.experiments[1].outcome == "patch_failed"
    assert state.status == "completed"
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# E3: smoke fails → revert + advance, run_eval NOT called for that experiment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e3_smoke_fails_reverts_and_eval_not_invoked(tmp_path: Path) -> None:
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=1)

    class GoodCoder:
        name = "mock"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            return _good_patch(tmp_path)

        def report_cost(self) -> Decimal:
            return Decimal("0")

    eval_call_count = 0

    async def counting_eval(*_args, **_kw) -> EvalResult:
        nonlocal eval_call_count
        eval_call_count += 1
        return _make_eval(passed=1)

    from usr.plugins.autoresearch.harness.smoke import SmokeResult
    smoke_fail = SmokeResult(ok=False, error="YAML parse error", took_ms=1)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=counting_eval):
        with patch("usr.plugins.autoresearch.worker.loop.smoke_check", return_value=smoke_fail):
            with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                state = await run_loop(
                    run_id="e3-test-001",
                    program_md_path=program_md,
                    max_experiments_override=1,
                    coder=GoodCoder(),
                    cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
                )

    assert state.experiments[0].outcome == "smoke_failed"
    assert state.status == "completed"
    # Exactly 1 eval call: only the baseline, not the smoke-failed experiment
    assert eval_call_count == 1, f"Expected 1 (baseline only), got {eval_call_count}"
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# E5: all eval tasks crashed → abort run, status=aborted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e5_all_eval_tasks_crashed_aborts_run(tmp_path: Path) -> None:
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=3)

    class GoodCoder:
        name = "mock"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            return _good_patch(tmp_path)

        def report_cost(self) -> Decimal:
            return Decimal("0")

    evals = iter([
        _make_eval(passed=1),          # baseline
        _make_eval(all_crashed=True),  # exp1 → all crashed → abort
    ])

    async def seq_eval(*_args, **_kw) -> EvalResult:
        return next(evals)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=seq_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            state = await run_loop(
                run_id="e5-test-001",
                program_md_path=program_md,
                max_experiments_override=3,
                coder=GoodCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
            )

    assert state.status == "aborted"
    assert len(state.experiments) == 1
    assert state.experiments[0].outcome == "eval_unusable"
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# E7: cost cap exceeded — coder raises BudgetExceeded → revert + exit clean
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e7_cost_cap_exceeded_mid_experiment_reverts_and_exits_clean(tmp_path: Path) -> None:
    # _setup_repo commits everything so the worktree is clean
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=5)

    class CapCoder:
        name = "cap"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            raise BudgetExceeded("Budget exceeded: $100 > $0.00")

        def report_cost(self) -> Decimal:
            return Decimal("100.00")

    async def mock_eval(*_args, **_kw) -> EvalResult:
        return _make_eval(passed=0)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            state = await run_loop(
                run_id="e7-test-001",
                program_md_path=program_md,
                max_experiments_override=5,
                coder=CapCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("0.00"), rates={}),
            )

    assert state.status == "cost_capped"
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# E11: suite drift mid-run → abort run, status=aborted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e11_suite_drift_aborts_run(tmp_path: Path) -> None:
    """Suite drift detected at experiment 2 start aborts the run.

    git reset --hard reverts in-repo file mutations, so we mock
    verify_suite_hash to raise SuiteDriftError on the 2nd call — simulating
    the real scenario where the suite file is modified externally.
    """
    program_md, _ = _setup_repo(tmp_path, max_experiments=3)

    class NopCoder:
        name = "nop"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            return _good_patch(tmp_path)

        def report_cost(self) -> Decimal:
            return Decimal("0")

    async def mock_eval(*_args, **_kw) -> EvalResult:
        return _make_eval(passed=0)

    from usr.plugins.autoresearch.state.runs import RunState, SuiteDriftError

    verify_call_count = 0

    def mock_verify(self, suite_path: Path, runs_root: Path | None = None) -> bool:
        nonlocal verify_call_count
        verify_call_count += 1
        if verify_call_count >= 2:
            raise SuiteDriftError("simulated drift: suite changed mid-run")
        return True

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            with patch.object(RunState, "verify_suite_hash", mock_verify):
                state = await run_loop(
                    run_id="e11-test-001",
                    program_md_path=program_md,
                    max_experiments_override=3,
                    coder=NopCoder(),
                    cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
                )

    assert state.status == "aborted"
    # Exp1 ran (drift fires at start of exp2), so exactly 1 experiment recorded
    assert len(state.experiments) == 1


# ---------------------------------------------------------------------------
# E12: dirty worktree at start rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e12_dirty_worktree_at_start_rejected(tmp_path: Path) -> None:
    program_md, _ = _setup_repo(tmp_path, max_experiments=1)

    # Introduce an untracked file to dirty the worktree
    (tmp_path / "dirty_file.txt").write_text("uncommitted change")

    class NeverCoder:
        name = "never"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            raise AssertionError("propose_edit must not be called on a dirty worktree")

        def report_cost(self) -> Decimal:
            return Decimal("0")

    with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
        with pytest.raises(Exception, match="[Dd]irty|uncommitted|worktree"):
            await run_loop(
                run_id="e12-test-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=NeverCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
            )


# ---------------------------------------------------------------------------
# Coder failure (non-BudgetExceeded) → coder_failed, loop advances
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coder_failure_marks_coder_failed_and_advances(tmp_path: Path) -> None:
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=2)

    class FailingCoder:
        name = "failing"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            raise ValueError("LLM returned garbage JSON after all retries")

        def report_cost(self) -> Decimal:
            return Decimal("0")

    async def mock_eval(*_args, **_kw) -> EvalResult:
        return _make_eval(passed=1)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            state = await run_loop(
                run_id="coder-fail-001",
                program_md_path=program_md,
                max_experiments_override=2,
                coder=FailingCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
            )

    assert state.status == "completed"
    assert len(state.experiments) == 2
    assert all(e.outcome == "coder_failed" for e in state.experiments)
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# E8: SIGTERM handler registered with correct baseline sha (POSIX only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM not applicable on Windows")
@pytest.mark.asyncio
async def test_e8_sigterm_handler_registered(tmp_path: Path) -> None:
    program_md, _ = _setup_repo(tmp_path, max_experiments=1)

    registered: dict[str, object] = {}

    def mock_register(sha: str, root: Path) -> None:
        registered["sha"] = sha
        registered["root"] = root

    class NopCoder:
        name = "nop"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
            return _good_patch(tmp_path)

        def report_cost(self) -> Decimal:
            return Decimal("0")

    async def mock_eval(*_args, **_kw) -> EvalResult:
        return _make_eval(passed=0)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            with patch("usr.plugins.autoresearch.worker.loop.kill_handler") as mock_kh:
                mock_kh.register = mock_register
                mock_kh.update_baseline = lambda _sha: None

                await run_loop(
                    run_id="e8-test-001",
                    program_md_path=program_md,
                    max_experiments_override=1,
                    coder=NopCoder(),
                    cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
                )

    assert "sha" in registered, "kill_handler.register was not called"
    assert registered["root"] == tmp_path
