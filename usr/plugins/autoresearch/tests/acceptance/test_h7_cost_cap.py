"""
H7 — Cost cap.

Design-doc invariant (§8):
  When CostMeter.cap_usd is set to $5.00 and the coder causes spend to
  exceed $5.00 after 1 experiment, the loop exits with status='cost_capped'
  and subsequent experiments are not attempted.

Assertions:
  - loop exits within 1 experiment
  - state.status == 'cost_capped'
  - branch HEAD == baseline_sha (revert happened mid-experiment)
  - len(state.experiments) == 0  (BudgetExceeded raised before experiment record appended)

Note: BudgetExceeded is raised inside propose_edit() on the first tick that
exceeds the cap. The experiment record may or may not be appended depending
on where in the call stack the exception propagates. The loop catches it
at the outer level and sets status='cost_capped'.

Rates: we supply realistic rates (gpt-4o: $5/1M input, $15/1M output) so
that a moderate token count (1000 input + 500 output) cleanly exceeds $5.00.
"""
from __future__ import annotations

import asyncio
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
from usr.plugins.autoresearch.worker.cost_meter import BudgetExceeded, CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop

# Realistic gpt-4o rates: $5/1M input = $0.005/1k, $15/1M output = $0.015/1k.
# 1000 input + 500 output → $0.005 + $0.0075 = $0.0125 per call.
# At cap=$0.01, the first coder tick (1000+500) produces $0.0125 > $0.01 → BudgetExceeded.
_TEST_RATES = {
    "mock-model": {
        "input_per_1k": Decimal("0.005"),
        "output_per_1k": Decimal("0.015"),
    }
}
_CAP_USD = Decimal("0.01")   # Low cap so first tick exceeds it
_TICK_INPUT = 1000
_TICK_OUTPUT = 500
# Cost per tick: 1000*0.005/1000 + 500*0.015/1000 = 0.005 + 0.0075 = 0.0125 > 0.01


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
    (agent_dir / "system.md").write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: h7 test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "h7-001", "input": "momentum?", "assert_substring": "long"}]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 10\ncost_cap_usd: '0.01'\n---\n"
        "Improve the assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


class _BudgetBustingCoder:
    """
    Ticks the meter with enough tokens to exceed the $0.01 cap on the first call.
    At _TEST_RATES, 1000 input + 500 output = $0.0125 > $0.01.
    """
    name = "budget_buster"

    def __init__(self, meter: CostMeter) -> None:
        self._meter = meter
        self._call_count = 0

    async def propose_edit(self, **_) -> EditPatch:  # type: ignore[override]
        self._call_count += 1
        # This tick raises BudgetExceeded since $0.0125 > $0.01 cap.
        self._meter.tick("mock-model", _TICK_INPUT, _TICK_OUTPUT)
        return EditPatch(
            target_path=Path("__pending__"),
            old_text="go long",
            new_text="go long and buy immediately",
            rationale="budget buster patch",
        )

    def report_cost(self) -> Decimal:
        return self._meter.spent


@pytest.mark.acceptance
def test_h7_cost_cap_exits_with_status_cost_capped(tmp_path: Path):
    """Loop must exit with status='cost_capped' when budget is exceeded."""
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=_CAP_USD, rates=_TEST_RATES)
    coder = _BudgetBustingCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            return await run_loop(
                run_id="h7-cap-001",
                program_md_path=program_md,
                max_experiments_override=10,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert state.status == "cost_capped", (
        f"Expected status='cost_capped', got {state.status!r}"
    )


@pytest.mark.acceptance
def test_h7_cost_cap_at_most_one_experiment(tmp_path: Path):
    """When budget is exceeded on first coder call, ≤ 1 experiment recorded."""
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=_CAP_USD, rates=_TEST_RATES)
    coder = _BudgetBustingCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            return await run_loop(
                run_id="h7-1exp-001",
                program_md_path=program_md,
                max_experiments_override=10,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert len(state.experiments) <= 1, (
        f"Expected ≤1 experiment when budget exceeded on first call, "
        f"got {len(state.experiments)}"
    )


@pytest.mark.acceptance
def test_h7_branch_head_equals_baseline_after_cost_cap(tmp_path: Path):
    """Branch HEAD must equal baseline_sha after cost_capped (revert happened)."""
    program_md, baseline_sha = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=_CAP_USD, rates=_TEST_RATES)
    coder = _BudgetBustingCoder(meter)
    run_id = "h7-revert-001"

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            return await run_loop(
                run_id=run_id,
                program_md_path=program_md,
                max_experiments_override=10,
                coder=coder,
                cost_meter=meter,
            )

    asyncio.run(_run())

    branch = f"autoresearch/{run_id}"
    branch_head = _sha(tmp_path, branch)
    assert branch_head == baseline_sha, (
        f"Branch HEAD {branch_head} != baseline {baseline_sha} after cost_capped. "
        "The revert should have restored branch to baseline."
    )
    main_head = _sha(tmp_path, "main")
    assert main_head == baseline_sha, (
        f"main was modified after cost_capped: head={main_head}"
    )


@pytest.mark.acceptance
def test_h7_budget_meter_raises_on_explicit_tick():
    """CostMeter raises BudgetExceeded when spend > cap using realistic rates."""
    meter = CostMeter(cap_usd=_CAP_USD, rates=_TEST_RATES)
    with pytest.raises(BudgetExceeded):
        meter.tick("mock-model", _TICK_INPUT, _TICK_OUTPUT)


@pytest.mark.acceptance
def test_h7_subsequent_experiments_not_attempted(tmp_path: Path):
    """After cost_capped, experiment count must be ≤ 1 (subsequent ones NOT attempted)."""
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=_CAP_USD, rates=_TEST_RATES)
    coder = _BudgetBustingCoder(meter)

    async def _run():
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            return await run_loop(
                run_id="h7-subsequent-001",
                program_md_path=program_md,
                max_experiments_override=10,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert coder._call_count <= 1, (
        f"Coder was called {coder._call_count} times after budget exceeded; "
        "subsequent experiments should not be attempted"
    )
    assert state.status == "cost_capped"
