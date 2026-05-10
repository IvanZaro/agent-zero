from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import litellm as litellm  # noqa: PLC0414 — module-level so tests can monkeypatch
except ImportError:  # pragma: no cover
    litellm = None  # type: ignore[assignment]

_JUDGE_SYSTEM_PROMPT = """\
You are an impartial LLM-based evaluator. Compare two eval runs (new vs baseline) \
for an AI agent prompt.

Score the new run on three axes:
- correctness (0-5): how correct and accurate are the task outputs?
- conciseness (0-3): are responses concise and free of padding?
- tool_use_efficiency (0-2): does the agent use the minimum necessary tool calls?

Respond with ONLY valid JSON in exactly this format (no markdown, no extra text):
{"correctness": <int>, "conciseness": <int>, "tool_use_efficiency": <int>}
"""

_JUDGE_USER_TEMPLATE = """\
BASELINE EVAL:
- passed: {baseline_passed}/{baseline_total}
- total_tokens: {baseline_tokens}

NEW EVAL:
- passed: {new_passed}/{new_total}
- total_tokens: {new_tokens}

Rate the NEW eval relative to the baseline.
"""


@dataclass(frozen=True)
class JudgeScore:
    score: float | None
    rubric: dict[str, int]
    judge_tokens_used: int


def _parse_rubric(content: str) -> tuple[float, dict[str, int]] | None:
    """Return (score, rubric_int) or None on any parse failure."""
    try:
        rubric = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(rubric, dict):
        return None
    score = float(
        rubric.get("correctness", 0)
        + rubric.get("conciseness", 0)
        + rubric.get("tool_use_efficiency", 0)
    )
    rubric_int: dict[str, int] = {
        "correctness": int(rubric.get("correctness", 0)),
        "conciseness": int(rubric.get("conciseness", 0)),
        "tool_use_efficiency": int(rubric.get("tool_use_efficiency", 0)),
    }
    return score, rubric_int


async def judge_score(
    eval_result: object,
    baseline_eval: object,
    judge_model: str,
) -> JudgeScore:
    user_msg = _JUDGE_USER_TEMPLATE.format(
        baseline_passed=getattr(baseline_eval, "passed", 0),
        baseline_total=getattr(baseline_eval, "total", 0),
        baseline_tokens=getattr(baseline_eval, "total_tokens", 0),
        new_passed=getattr(eval_result, "passed", 0),
        new_total=getattr(eval_result, "total", 0),
        new_tokens=getattr(eval_result, "total_tokens", 0),
    )

    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        response = await litellm.acompletion(
            model=judge_model,
            messages=messages,
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        tokens_used = (getattr(usage, "prompt_tokens", 0) or 0) + (
            getattr(usage, "completion_tokens", 0) or 0
        )
    except Exception as exc:
        log.warning("judge LLM call failed: %s", exc)
        return JudgeScore(score=None, rubric={}, judge_tokens_used=0)

    parsed = _parse_rubric(content)
    if parsed is None:
        log.warning("judge response parse failed: %r", content)
        return JudgeScore(score=None, rubric={}, judge_tokens_used=tokens_used)

    score, rubric_int = parsed
    return JudgeScore(score=score, rubric=rubric_int, judge_tokens_used=tokens_used)
