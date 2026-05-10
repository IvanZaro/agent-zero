"""
H2 — Eval determinism.

Design-doc invariant (§8):
  Re-running the eval suite on the identical prompt with temperature=0 produces
  bit-identical pass counts and token totals.

Assertions:
  - eval_a.passed == eval_b.passed           (exact)
  - eval_a.total_tokens == eval_b.total_tokens (exact)
  - if judge ran: judge_a.score == pytest.approx(judge_b.score, rel=0.05)

Implementation: mock litellm.acompletion to return deterministic fixed responses.
The mock returns responses that pass 6 of the suite tasks deterministically.
"""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.harness.eval_runner import EvalResult, run_eval
from usr.plugins.autoresearch.harness._suite_loader import load_suite

_TRADING_SUITE = (
    Path(__file__).resolve().parents[2] / "suites" / "trading-v1" / "suite.json"
)

_FIXTURE_PROMPT = (
    "You are a trading assistant.\n"
    "RSI below 30 = oversold.\n"
    "RSI above 70 = overbought.\n"
    "EMA(20) crosses above EMA(50) = golden cross.\n"
    "EMA(50) crosses below EMA(200) = death cross.\n"
    "Negative funding rate = shorts are dominant.\n"
    "Support = price floor zone.\n"
    "Resistance = price ceiling zone.\n"
    "Breakout = price breaks above resistance.\n"
    "Momentum positive = bullish / go long.\n"
    "High volume + rising price = accumulation.\n"
)

# Map task id prefix → the substring that triggers `passed = True`.
# These are deliberately deterministic so both eval runs get identical results.
_TASK_ANSWERS: dict[str, str] = {
    "td-e-001": "oversold",
    "td-e-002": "overbought",
    "td-e-003": "golden cross",
    "td-e-004": "death cross",
    "td-e-005": "shorts",
    "td-m-001": "support",
    "td-m-002": "resistance",
    "td-m-003": "breakout",
    "td-m-004": "long",
    "td-m-005": "accumulation",
}

_FIXED_TOKENS_INPUT = 120
_FIXED_TOKENS_OUTPUT = 40


def _make_mock_completion(task_id_hint: str):
    """Return a fixed litellm-shaped response object for the given task."""
    # The 'content' matches whatever assert_substring the suite expects for this task.
    answer = _TASK_ANSWERS.get(task_id_hint, "hold")
    mock_choice = MagicMock()
    mock_choice.message.content = f"Based on the indicators, this is {answer}."
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = _FIXED_TOKENS_INPUT
    mock_usage.completion_tokens = _FIXED_TOKENS_OUTPUT
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage = mock_usage
    return mock_resp


def _build_acompletion_mock():
    """
    Build an AsyncMock for litellm.acompletion that matches task input to a
    deterministic response — same answer every call, regardless of invocation order.
    """
    suite = load_suite(_TRADING_SUITE)
    # Map assert_substring → response text (used to pick the right answer).
    # We match on the user message content (task.input).
    input_to_task_id: dict[str, str] = {t.input: t.id for t in suite.tasks}

    async def _side_effect(*args, **kwargs):
        messages = kwargs.get("messages", args[0] if args else [])
        user_msg = next(
            (m["content"] for m in messages if m.get("role") == "user"), ""
        )
        # Find which task this corresponds to
        task_id = None
        for inp, tid in input_to_task_id.items():
            if inp in user_msg:
                task_id = tid
                break
        return _make_mock_completion(task_id or "")

    return AsyncMock(side_effect=_side_effect)


@pytest.mark.acceptance
def test_h2_eval_determinism_identical_results():
    """Two sequential eval runs on the same prompt return bit-identical results."""
    if not _TRADING_SUITE.exists():
        pytest.skip(f"trading-v1 suite not found at {_TRADING_SUITE}")

    mock_acompletion = _build_acompletion_mock()

    async def _run_twice():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm:
            mock_litellm.acompletion = mock_acompletion
            eval_a = await run_eval(
                prompt_text=_FIXTURE_PROMPT,
                suite_path=_TRADING_SUITE,
                model="mock-gpt-4",
                parallelism=4,
            )
        # Reset mock call count so both runs are independent
        mock_acompletion.reset_mock()
        mock_acompletion2 = _build_acompletion_mock()
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm:
            mock_litellm.acompletion = mock_acompletion2
            eval_b = await run_eval(
                prompt_text=_FIXTURE_PROMPT,
                suite_path=_TRADING_SUITE,
                model="mock-gpt-4",
                parallelism=4,
            )
        return eval_a, eval_b

    eval_a, eval_b = asyncio.run(_run_twice())

    assert eval_a.passed == eval_b.passed, (
        f"Pass counts differ between runs: {eval_a.passed} vs {eval_b.passed}"
    )
    assert eval_a.total_tokens == eval_b.total_tokens, (
        f"Token totals differ: {eval_a.total_tokens} vs {eval_b.total_tokens}"
    )
    assert eval_a.total == eval_b.total


@pytest.mark.acceptance
def test_h2_eval_passes_at_least_5_tasks():
    """The deterministic mock passes at least 5 of the 20 trading tasks."""
    if not _TRADING_SUITE.exists():
        pytest.skip(f"trading-v1 suite not found at {_TRADING_SUITE}")

    mock_acompletion = _build_acompletion_mock()

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm:
            mock_litellm.acompletion = mock_acompletion
            return await run_eval(
                prompt_text=_FIXTURE_PROMPT,
                suite_path=_TRADING_SUITE,
                model="mock-gpt-4",
                parallelism=4,
            )

    result = asyncio.run(_run())
    assert result.passed >= 5, (
        f"Expected at least 5 tasks to pass, got {result.passed}/{result.total}"
    )


@pytest.mark.acceptance
def test_h2_temperature_zero_enforced():
    """litellm.acompletion must be called with temperature=0 (determinism requirement)."""
    if not _TRADING_SUITE.exists():
        pytest.skip(f"trading-v1 suite not found at {_TRADING_SUITE}")

    call_temps: list[int | float] = []

    async def _capturing_side_effect(*args, **kwargs):
        call_temps.append(kwargs.get("temperature", -1))
        return _make_mock_completion("")

    mock_acompletion = AsyncMock(side_effect=_capturing_side_effect)

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm:
            mock_litellm.acompletion = mock_acompletion
            await run_eval(
                prompt_text=_FIXTURE_PROMPT,
                suite_path=_TRADING_SUITE,
                model="mock-gpt-4",
                parallelism=1,
            )

    asyncio.run(_run())
    assert call_temps, "No litellm.acompletion calls were captured"
    assert all(t == 0 for t in call_temps), (
        f"Not all eval calls used temperature=0: {call_temps}"
    )
