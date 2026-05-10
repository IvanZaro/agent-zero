from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.harness.eval_runner import (
    EvalResult,
    TaskOutcome,
    _run_task,
    run_eval,
)
from usr.plugins.autoresearch.harness._suite_loader import Task, TaskSuite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id: str, input_: str = "question?", substr: str = "yes") -> Task:
    return Task(id=task_id, input=input_, assert_substring=substr)


def _make_suite_file(tmp_path: Path, tasks: list[dict]) -> Path:
    data = {"version": 1, "tasks": tasks}
    p = tmp_path / "suite.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _make_litellm_response(content: str, prompt_tokens: int = 5, completion_tokens: int = 10) -> MagicMock:
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
# _run_task unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_task_passes_when_substring_present() -> None:
    task = _make_task("t-001", "should I go long?", "long")
    mock_resp = _make_litellm_response("Yes, go LONG on this trade.")
    with patch("usr.plugins.autoresearch.harness.eval_runner.litellm", create=True) as mock_ll:
        mock_ll.acompletion = AsyncMock(return_value=mock_resp)
        outcome = await _run_task(task, "system prompt", "test-model")
    assert outcome.passed is True
    assert outcome.task_id == "t-001"
    assert outcome.tokens == 15  # 5 + 10
    assert outcome.error is None


@pytest.mark.asyncio
async def test_assert_substring_case_insensitive() -> None:
    task = _make_task("t-001", "question?", "LONG")
    mock_resp = _make_litellm_response("go long")  # lowercase
    with patch("usr.plugins.autoresearch.harness.eval_runner.litellm", create=True) as mock_ll:
        mock_ll.acompletion = AsyncMock(return_value=mock_resp)
        outcome = await _run_task(task, "system", "test-model")
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_run_task_fails_when_substring_absent() -> None:
    task = _make_task("t-001", "question?", "long")
    mock_resp = _make_litellm_response("short is better here")
    with patch("usr.plugins.autoresearch.harness.eval_runner.litellm", create=True) as mock_ll:
        mock_ll.acompletion = AsyncMock(return_value=mock_resp)
        outcome = await _run_task(task, "system", "test-model")
    assert outcome.passed is False
    assert outcome.error is None


@pytest.mark.asyncio
async def test_run_task_timeout_returns_failed_outcome() -> None:
    task = _make_task("slow-001", "q?", "a")

    async def _sleep_forever(*_a, **_kw):
        await asyncio.sleep(999)

    with patch("usr.plugins.autoresearch.harness.eval_runner.litellm", create=True) as mock_ll:
        mock_ll.acompletion = AsyncMock(side_effect=_sleep_forever)
        with patch("usr.plugins.autoresearch.harness.eval_runner.TASK_TIMEOUT_S", 0.05):
            outcome = await _run_task(task, "system", "test-model")

    assert outcome.passed is False
    assert outcome.error == "timeout"
    assert outcome.tokens == 0
    assert outcome.output is None


@pytest.mark.asyncio
async def test_litellm_exception_marks_task_failed_not_eval_aborted() -> None:
    task = _make_task("t-001", "q?", "a")
    with patch("usr.plugins.autoresearch.harness.eval_runner.litellm", create=True) as mock_ll:
        mock_ll.acompletion = AsyncMock(side_effect=RuntimeError("5xx server error"))
        outcome = await _run_task(task, "system", "test-model")
    assert outcome.passed is False
    assert "5xx server error" in (outcome.error or "")
    assert outcome.tokens == 0


# ---------------------------------------------------------------------------
# run_eval integration tests (suite-level)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_per_task_timeout_does_not_abort_other_tasks(tmp_path: Path) -> None:
    """One task returns timeout error; the other task still completes and passes."""
    suite_path = _make_suite_file(tmp_path, [
        {"id": "slow", "input": "q", "assert_substring": "yes"},
        {"id": "fast", "input": "q", "assert_substring": "yes"},
    ])

    # _run_task already handles TimeoutError internally and returns a failed outcome.
    # Mock it to return pre-baked outcomes so we test run_eval aggregation behaviour.
    async def _mock_run_task(task: object, _prompt_text: str, _model: str) -> TaskOutcome:
        task_id = task.id  # type: ignore[union-attr]
        if task_id == "slow":
            return TaskOutcome(task_id="slow", passed=False, tokens=0, output=None, error="timeout")
        return TaskOutcome(task_id="fast", passed=True, tokens=10, output="yes", error=None)

    with patch("usr.plugins.autoresearch.harness.eval_runner._run_task", side_effect=_mock_run_task):
        result = await run_eval(prompt_text="system", suite_path=suite_path, model="test-model")

    assert result.total == 2
    slow_outcome = next(o for o in result.per_task if o.task_id == "slow")
    fast_outcome = next(o for o in result.per_task if o.task_id == "fast")
    assert slow_outcome.passed is False
    assert slow_outcome.error == "timeout"
    assert fast_outcome.passed is True
    # eval-level result is not aborted — we get outcomes for both tasks
    assert result.passed == 1


@pytest.mark.asyncio
async def test_runs_tasks_in_parallel_with_concurrency_limit(tmp_path: Path) -> None:
    """
    4 tasks with parallelism=2. Each task takes ~50ms.
    Wall time should be ≈ 2 batches × 50ms = ~100ms, not 4 × 50ms = 200ms.
    """
    n_tasks = 4
    task_sleep = 0.05
    parallelism = 2

    tasks_raw = [
        {"id": f"t-{i:03d}", "input": "q", "assert_substring": "yes"}
        for i in range(n_tasks)
    ]
    suite_path = _make_suite_file(tmp_path, tasks_raw)

    concurrent_now: list[int] = []
    max_concurrent = [0]

    async def _slow_run_task(task, prompt_text, model):
        concurrent_now.append(1)
        max_concurrent[0] = max(max_concurrent[0], len(concurrent_now))
        await asyncio.sleep(task_sleep)
        concurrent_now.pop()
        return TaskOutcome(task_id=task.id, passed=True, tokens=5, output="yes", error=None)

    with patch("usr.plugins.autoresearch.harness.eval_runner._run_task", side_effect=_slow_run_task):
        t0 = time.monotonic()
        result = await run_eval(
            prompt_text="system",
            suite_path=suite_path,
            model="test-model",
            parallelism=parallelism,
        )
        elapsed = time.monotonic() - t0

    # All tasks passed
    assert result.passed == n_tasks
    assert result.total == n_tasks

    # Max concurrency was bounded
    assert max_concurrent[0] <= parallelism

    # Wall time should be well under sequential time (n_tasks * task_sleep).
    # With parallelism=2, expect ≈ 2 batches = 2 * task_sleep.
    sequential_time = n_tasks * task_sleep
    assert elapsed < sequential_time * 0.85, (
        f"elapsed {elapsed:.3f}s is not meaningfully faster than sequential {sequential_time:.3f}s"
    )


@pytest.mark.asyncio
async def test_token_accounting_aggregates_correctly(tmp_path: Path) -> None:
    suite_path = _make_suite_file(tmp_path, [
        {"id": "t-001", "input": "q1", "assert_substring": "yes"},
        {"id": "t-002", "input": "q2", "assert_substring": "no"},
    ])

    outcomes = {
        "t-001": TaskOutcome(task_id="t-001", passed=True, tokens=30, output="yes", error=None),
        "t-002": TaskOutcome(task_id="t-002", passed=True, tokens=50, output="no indeed", error=None),
    }

    async def _mock_run_task(task, _prompt_text, _model):
        return outcomes[task.id]

    with patch("usr.plugins.autoresearch.harness.eval_runner._run_task", side_effect=_mock_run_task):
        result = await run_eval(prompt_text="system", suite_path=suite_path, model="test-model")

    assert result.total_tokens == 80  # 30 + 50
    assert result.passed == 2


@pytest.mark.asyncio
async def test_per_task_ordering_preserved(tmp_path: Path) -> None:
    """Tasks that complete out-of-order must appear in suite order in per_task."""
    tasks_raw = [
        {"id": "first", "input": "q", "assert_substring": "yes"},
        {"id": "second", "input": "q", "assert_substring": "yes"},
        {"id": "third", "input": "q", "assert_substring": "yes"},
    ]
    suite_path = _make_suite_file(tmp_path, tasks_raw)

    # Make them complete in reverse order via sleep
    async def _out_of_order(task, _prompt_text, _model):
        delays = {"first": 0.06, "second": 0.03, "third": 0.0}
        await asyncio.sleep(delays[task.id])
        return TaskOutcome(task_id=task.id, passed=True, tokens=5, output="yes", error=None)

    with patch("usr.plugins.autoresearch.harness.eval_runner._run_task", side_effect=_out_of_order):
        result = await run_eval(
            prompt_text="system",
            suite_path=suite_path,
            model="test-model",
            parallelism=3,
        )

    assert [o.task_id for o in result.per_task] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_zero_tasks_returns_empty_eval_result(tmp_path: Path) -> None:
    """Empty task list returns EvalResult with zeros, does not raise."""
    # load_suite will reject empty tasks, so we bypass via patching load_suite.
    empty_suite = TaskSuite(version=1, tasks=(), suite_hash="abc")

    with patch(
        "usr.plugins.autoresearch.harness._suite_loader.load_suite",
        return_value=empty_suite,
    ):
        result = await run_eval(
            prompt_text="system",
            suite_path=tmp_path / "fake.json",
            model="test-model",
        )

    assert result.passed == 0
    assert result.total == 0
    assert result.total_tokens == 0
    assert result.per_task == []
