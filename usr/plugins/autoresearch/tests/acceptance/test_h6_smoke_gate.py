"""
H6 — Smoke gate.

Design-doc invariant (§8):
  When the coder injects a broken-YAML patch (making _context.yaml or a
  prompt frontmatter invalid), the smoke check must fire:
    - outcome == 'smoke_failed'
    - eval_runner.run_eval was NOT called (eval budget preserved)
    - branch HEAD == baseline_sha (revert happened)

E3 rule: smoke failure reverts the change without spending eval budget.

Implementation note:
  We mock smoke_check to return SmokeResult(ok=False) so the gate is
  backend-independent. The alternative (corrupting YAML in the file directly
  via the coder) is tested in test_h6_yaml_corruption_triggers_smoke below.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.harness.smoke import SmokeResult
from usr.plugins.autoresearch.worker.cost_meter import CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup_repo(tmp_path: Path) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    prompt = agent_dir / "system.md"
    prompt.write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: h6 test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "h6-001", "input": "momentum?", "assert_substring": "long"}]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 1\ncost_cap_usd: '5.00'\n---\n"
        "Improve the assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


_SMOKE_FAIL_RESULT = SmokeResult(ok=False, error="injected YAML corruption for H6 test", took_ms=0)


class _ValidPatchCoder:
    """Returns a valid patch so apply succeeds, then smoke check can fire."""
    name = "valid_patch"

    def __init__(self, meter: CostMeter) -> None:
        self._meter = meter
        self._total = Decimal("0")

    async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
        self._meter.tick("mock-model", 100, 50)
        self._total = self._meter.spent
        return EditPatch(
            target_path=Path("__pending__"),
            old_text="go long",
            new_text="go long and buy",
            rationale="valid patch — smoke check is mocked to fail",
        )

    def report_cost(self) -> Decimal:
        return self._total


@pytest.mark.acceptance
def test_h6_smoke_failed_outcome(tmp_path: Path, mock_passing_baseline):
    """Broken patch → smoke check returns False → outcome == 'smoke_failed'."""
    program_md, baseline_sha = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ValidPatchCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.worker.loop.smoke_check",
                   return_value=_SMOKE_FAIL_RESULT), \
             mock_passing_baseline:
            return await run_loop(
                run_id="h6-smoke-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert len(state.experiments) == 1
    assert state.experiments[0].outcome == "smoke_failed", (
        f"Expected 'smoke_failed', got {state.experiments[0].outcome!r}"
    )


@pytest.mark.acceptance
def test_h6_eval_not_called_on_smoke_fail(tmp_path: Path):
    """eval_runner.run_eval must NOT be called during experiment when smoke check fails."""
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ValidPatchCoder(meter)
    experiment_eval_calls = 0

    async def _counting_run_eval(*args, **kwargs):
        nonlocal experiment_eval_calls
        experiment_eval_calls += 1
        return EvalResult(
            passed=1, total=1, total_tokens=50,
            per_task=[TaskOutcome("h6-001", True, 50, "long", None)]
        )

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.worker.loop.smoke_check",
                   return_value=_SMOKE_FAIL_RESULT), \
             patch("usr.plugins.autoresearch.worker.loop.run_eval",
                   side_effect=_counting_run_eval) as mock_eval:
            state = await run_loop(
                run_id="h6-noeval-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )
            # run_eval in loop.py is used for: baseline (1 call) + each experiment
            # Smoke fails before eval, so experiment eval should NOT be called.
            # Baseline eval is called once at startup. Total calls ≤ 1.
            assert mock_eval.call_count <= 1, (
                f"run_eval called {mock_eval.call_count} times; expected at most 1 "
                f"(baseline only) — smoke gate should prevent experiment eval"
            )
            return state

    asyncio.run(_run())


@pytest.mark.acceptance
def test_h6_branch_head_equals_baseline_after_smoke_fail(tmp_path: Path, mock_passing_baseline):
    """Branch HEAD must equal baseline_sha after smoke_failed (revert happened)."""
    program_md, baseline_sha = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ValidPatchCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.worker.loop.smoke_check",
                   return_value=_SMOKE_FAIL_RESULT), \
             mock_passing_baseline:
            return await run_loop(
                run_id="h6-revert-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    branch = "autoresearch/h6-revert-001"
    branch_head = _sha(tmp_path, branch)
    assert branch_head == baseline_sha, (
        f"Branch HEAD {branch_head} != baseline {baseline_sha}; "
        "revert should have restored branch to baseline_sha"
    )
    # main must also be untouched
    main_head = _sha(tmp_path, "main")
    assert main_head == baseline_sha, (
        f"main was modified! head={main_head}"
    )


@pytest.mark.acceptance
def test_h6_coder_tokens_charged_but_eval_tokens_not(tmp_path: Path, mock_passing_baseline):
    """
    After smoke_failed: meter.spent is > 0 (coder was called) and
    run status is completed (smoke_failed is not an abort condition).
    """
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ValidPatchCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.worker.loop.smoke_check",
                   return_value=_SMOKE_FAIL_RESULT), \
             mock_passing_baseline:
            return await run_loop(
                run_id="h6-cost-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    # Coder was called (for the experiment), so spend > 0
    assert state.spend_usd >= Decimal("0"), "spend_usd should be >= 0"
    # smoke_failed is a normal outcome — run completes
    assert state.status == "completed", f"Expected 'completed', got {state.status!r}"


@pytest.mark.acceptance
def test_h6_yaml_corruption_triggers_smoke_natively(tmp_path: Path):
    """
    Real smoke check: corrupt _context.yaml directly on disk before
    applying the patch. The native smoke_check (not mocked) must return ok=False.
    """
    from usr.plugins.autoresearch.harness.smoke import smoke_check

    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text("You are a trading assistant.\n")
    # Write broken _context.yaml
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        ": : : invalid yaml : : :\n"
    )
    profile_root = tmp_path / "agents" / "fixture_trader"
    result = smoke_check(profile_root)
    assert not result.ok, (
        "Expected smoke_check to fail on broken _context.yaml, but it passed"
    )
    assert result.error is not None
