from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.harness.judge import JudgeScore, judge_score


def _make_eval(passed: int = 3, total: int = 5, tokens: int = 100) -> EvalResult:
    return EvalResult(
        passed=passed,
        total=total,
        total_tokens=tokens,
        per_task=[],
    )


def _make_litellm_response(content: str, prompt_tokens: int = 10, completion_tokens: int = 20) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_returns_valid_score_in_range() -> None:
    mock_resp = _make_litellm_response(
        '{"correctness": 4, "conciseness": 2, "tool_use_efficiency": 1}'
    )
    with patch(
        "usr.plugins.autoresearch.harness.judge.litellm",
        create=True,
    ) as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        result = await judge_score(
            eval_result=_make_eval(passed=4),
            baseline_eval=_make_eval(passed=3),
            judge_model="openrouter/openai/gpt-4o-mini",
        )

    assert isinstance(result, JudgeScore)
    assert result.score == 7.0  # 4 + 2 + 1
    assert result.rubric["correctness"] == 4
    assert result.rubric["conciseness"] == 2
    assert result.rubric["tool_use_efficiency"] == 1
    assert result.judge_tokens_used == 30  # 10 + 20
    # Score must be within 0-10 range
    assert 0.0 <= result.score <= 10.0


@pytest.mark.asyncio
async def test_judge_score_max_is_ten() -> None:
    mock_resp = _make_litellm_response(
        '{"correctness": 5, "conciseness": 3, "tool_use_efficiency": 2}'
    )
    with patch(
        "usr.plugins.autoresearch.harness.judge.litellm",
        create=True,
    ) as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        result = await judge_score(
            eval_result=_make_eval(),
            baseline_eval=_make_eval(),
            judge_model="test-model",
        )
    assert result.score == 10.0


# ---------------------------------------------------------------------------
# Malformed response — must NOT raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_handles_malformed_json_response_gracefully() -> None:
    mock_resp = _make_litellm_response("This is not JSON at all!")
    with patch(
        "usr.plugins.autoresearch.harness.judge.litellm",
        create=True,
    ) as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        result = await judge_score(
            eval_result=_make_eval(),
            baseline_eval=_make_eval(),
            judge_model="test-model",
        )
    assert result.score is None
    assert result.rubric == {}
    # tokens_used may or may not be 0 — just must not raise


@pytest.mark.asyncio
async def test_judge_handles_wrong_json_shape_gracefully() -> None:
    mock_resp = _make_litellm_response('["a", "b", "c"]')  # array, not object
    with patch(
        "usr.plugins.autoresearch.harness.judge.litellm",
        create=True,
    ) as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        result = await judge_score(
            eval_result=_make_eval(),
            baseline_eval=_make_eval(),
            judge_model="test-model",
        )
    assert result.score is None


@pytest.mark.asyncio
async def test_judge_handles_litellm_exception_gracefully() -> None:
    with patch(
        "usr.plugins.autoresearch.harness.judge.litellm",
        create=True,
    ) as mock_litellm:
        mock_litellm.acompletion = AsyncMock(side_effect=RuntimeError("LLM down"))
        result = await judge_score(
            eval_result=_make_eval(),
            baseline_eval=_make_eval(),
            judge_model="test-model",
        )
    assert result.score is None
    assert result.rubric == {}
    assert result.judge_tokens_used == 0


# ---------------------------------------------------------------------------
# Meta-test: judge module NEVER gates commits (no raise paths)
# ---------------------------------------------------------------------------

def test_judge_does_not_gate_commit() -> None:
    """
    Parse the judge module source. Verify judge_score() contains no bare
    'raise' statements outside of except clauses (it only logs + returns
    gracefully on any error path).
    """
    import usr.plugins.autoresearch.harness.judge as _judge_module
    judge_module_path = Path(_judge_module.__file__)
    source = judge_module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the judge_score function node.
    judge_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "judge_score":
            judge_fn = node
            break

    assert judge_fn is not None, "judge_score not found in judge.py"

    # Walk the function body looking for Raise nodes that are NOT inside
    # an ExceptHandler (i.e. re-raises or new raises that propagate to callers).
    class _RaiseFinder(ast.NodeVisitor):
        def __init__(self) -> None:
            self.bare_raises: list[ast.Raise] = []
            self._in_except: int = 0

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            self._in_except += 1
            self.generic_visit(node)
            self._in_except -= 1

        def visit_Raise(self, node: ast.Raise) -> None:
            if self._in_except == 0:
                self.bare_raises.append(node)

    finder = _RaiseFinder()
    finder.visit(judge_fn)
    assert finder.bare_raises == [], (
        f"judge_score() contains {len(finder.bare_raises)} non-except raise(s) — "
        "judge must never gate commits"
    )
