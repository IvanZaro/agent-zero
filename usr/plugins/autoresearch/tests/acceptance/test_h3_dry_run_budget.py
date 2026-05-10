"""
H3 — Dry-run wall-time + spend budget.

Design-doc invariant (§8):
  A 10-experiment dry run completes within ≤ 50 minutes wall-clock and
  spends ≤ $0.50 USD.

Test reality: fully-mocked LiteLLM means the run completes in <60 seconds.
The assertion is `wall_seconds < 60` (practical) while documenting that the
production budget is 50 minutes.

Assertions:
  - wall_seconds < 60                        (mocked run; prod budget is 50 min)
  - spend_usd ≤ Decimal("0.50")
  - 10 experiments completed
  - state.status == "completed"
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.worker.cost_meter import CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop


def _init_repo(path: Path) -> str:
    import subprocess
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _setup_repo(tmp_path: Path, max_experiments: int = 10) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    prompt = agent_dir / "system.md"
    prompt.write_text("You are a trading assistant. When momentum is positive, go long.\n")
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: h3 test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [
            {"id": "h3-001", "input": "momentum positive?", "assert_substring": "long"},
        ]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: {max_experiments}\n"
        f"cost_cap_usd: '0.50'\n---\n"
        "Improve the assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


# Cost per experiment: ~$0.04 (1000 input + 500 output tokens at default rates)
# 10 experiments × $0.04 = $0.40 < $0.50 cap
_TOKENS_INPUT = 1000
_TOKENS_OUTPUT = 500
_COST_PER_CALL = Decimal("1000") * Decimal("0.000003") / Decimal("1000") + \
                 Decimal("500") * Decimal("0.000015") / Decimal("1000")
# = 0.000003 + 0.0000075 = 0.0000105 per call — very cheap, well under $0.50 for 10


class _BudgetedCoder:
    """Coder that ticks the real CostMeter with controlled token counts."""

    name = "mock_litellm"

    def __init__(self, meter: CostMeter, old_text: str, new_text: str) -> None:
        self._meter = meter
        self._old = old_text
        self._new = new_text
        self._n = 0

    async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
        self._meter.tick("mock-model", _TOKENS_INPUT, _TOKENS_OUTPUT)
        self._n += 1
        # Alternate the patch to avoid 'old_text not found' after first apply
        if self._n % 2 == 1:
            return EditPatch(
                target_path=Path("__pending__"),
                old_text=self._old,
                new_text=self._new,
                rationale=f"h3 patch #{self._n}",
            )
        else:
            return EditPatch(
                target_path=Path("__pending__"),
                old_text=self._new,
                new_text=self._old,
                rationale=f"h3 revert #{self._n}",
            )

    def report_cost(self) -> Decimal:
        return self._meter.spent


def _make_passing_eval() -> EvalResult:
    return EvalResult(
        passed=1,
        total=1,
        total_tokens=50,
        per_task=[TaskOutcome(task_id="h3-001", passed=True, tokens=50, output="long", error=None)],
    )


def _make_litellm_mock():
    choice = MagicMock()
    choice.message.content = "long"
    usage = MagicMock()
    usage.prompt_tokens = 50
    usage.completion_tokens = 20
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return AsyncMock(return_value=resp)


@pytest.mark.acceptance
def test_h3_dry_run_budget_and_timing(tmp_path: Path):
    """
    10-experiment mocked run completes in <60 seconds and spends ≤ $0.50.

    Production budget: ≤ 50 minutes wall-clock per design §8 H3.
    Test budget: <60 seconds (mocked network = instant).
    """
    program_md, _ = _setup_repo(tmp_path, max_experiments=10)
    meter = CostMeter(cap_usd=Decimal("0.50"), rates={})
    coder = _BudgetedCoder(meter, old_text="go long", new_text="go long immediately")

    mock_acompletion = _make_litellm_mock()

    start = time.monotonic()

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = mock_acompletion
            return await run_loop(
                run_id="h3-dry-run-001",
                program_md_path=program_md,
                max_experiments_override=10,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())
    wall_seconds = time.monotonic() - start

    # Wall-time: mocked run must be fast
    assert wall_seconds < 60, (
        f"Dry run took {wall_seconds:.1f}s — expected <60s for mocked 10-experiment run. "
        f"Production budget: ≤ 50 minutes."
    )

    # All 10 experiments ran
    assert len(state.experiments) == 10, (
        f"Expected 10 experiments, got {len(state.experiments)}"
    )

    # Status completed (no budget hit since tokens are cheap at default rates)
    assert state.status == "completed", (
        f"Expected status='completed', got {state.status!r}"
    )

    # Spend is within $0.50 budget
    assert state.spend_usd <= Decimal("0.50"), (
        f"Spend ${state.spend_usd} exceeded $0.50 cap"
    )


@pytest.mark.acceptance
def test_h3_spend_tracked_per_experiment(tmp_path: Path):
    """Spend accumulates monotonically across experiments."""
    program_md, _ = _setup_repo(tmp_path, max_experiments=3)
    meter = CostMeter(cap_usd=Decimal("0.50"), rates={})
    coder = _BudgetedCoder(meter, old_text="go long", new_text="go long immediately")

    mock_acompletion = _make_litellm_mock()

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = mock_acompletion
            return await run_loop(
                run_id="h3-spend-001",
                program_md_path=program_md,
                max_experiments_override=3,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert state.spend_usd > Decimal("0"), "spend_usd should be > 0 after 3 experiments"
    assert state.spend_usd <= Decimal("0.50"), "spend_usd must stay within cap"
