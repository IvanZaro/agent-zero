from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TASK_TIMEOUT_S = 30

try:
    import litellm as litellm  # noqa: PLC0414  — re-export for monkeypatching in tests
except ImportError:  # pragma: no cover
    litellm = None  # type: ignore[assignment]


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    passed: bool
    tokens: int          # input + output combined for that task
    output: str | None
    error: str | None


@dataclass(frozen=True)
class EvalResult:
    passed: int
    total: int
    total_tokens: int
    per_task: list[TaskOutcome]


async def _run_task(
    task: object,
    prompt_text: str,
    model: str,
) -> TaskOutcome:
    task_id: str = task.id  # type: ignore[union-attr]
    user_input: str = task.input  # type: ignore[union-attr]
    assert_substring: str = task.assert_substring  # type: ignore[union-attr]

    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": user_input},
    ]

    try:
        response = await asyncio.wait_for(
            litellm.acompletion(model=model, messages=messages, temperature=0),
            timeout=TASK_TIMEOUT_S,
        )
        output = response.choices[0].message.content or ""
        usage = response.usage
        tokens = (getattr(usage, "prompt_tokens", 0) or 0) + (
            getattr(usage, "completion_tokens", 0) or 0
        )
        passed = assert_substring.lower() in output.lower()
        return TaskOutcome(task_id=task_id, passed=passed, tokens=tokens, output=output, error=None)
    except asyncio.TimeoutError:
        return TaskOutcome(task_id=task_id, passed=False, tokens=0, output=None, error="timeout")
    except Exception as exc:
        return TaskOutcome(task_id=task_id, passed=False, tokens=0, output=None, error=str(exc))


async def run_eval(
    prompt_text: str,
    suite_path: Path,
    model: str,
    parallelism: int = 4,
) -> EvalResult:
    from usr.plugins.autoresearch.harness._suite_loader import load_suite

    suite = load_suite(suite_path)
    tasks = suite.tasks

    if not tasks:
        return EvalResult(passed=0, total=0, total_tokens=0, per_task=[])

    sem = asyncio.Semaphore(parallelism)

    async def _bounded(task: object) -> TaskOutcome:
        async with sem:
            return await _run_task(task, prompt_text, model)

    # asyncio.gather preserves submission order regardless of completion order.
    outcomes: list[TaskOutcome] = list(
        await asyncio.gather(*(_bounded(t) for t in tasks))
    )

    for outcome in outcomes:
        log.info(
            "task %s: passed=%s tokens=%d", outcome.task_id, outcome.passed, outcome.tokens
        )

    passed = sum(1 for o in outcomes if o.passed)
    total_tokens = sum(o.tokens for o in outcomes)
    return EvalResult(passed=passed, total=len(outcomes), total_tokens=total_tokens, per_task=outcomes)
