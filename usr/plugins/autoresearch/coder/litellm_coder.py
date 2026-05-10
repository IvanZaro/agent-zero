from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.coder._cost_rates import load_rates
from usr.plugins.autoresearch.worker.cost_meter import CostMeter

if TYPE_CHECKING:
    from usr.plugins.autoresearch.state.runs import ExperimentRecord

log = logging.getLogger(__name__)

_ENV_MODEL_KEY = "AUTORESEARCH_CODER_MODEL"
_DEFAULT_MODEL = "openrouter/openai/gpt-4o-mini"

# E1: max retries for malformed JSON from LLM
_MAX_JSON_RETRIES = 3

# E6: max retries for transient network/rate errors
_MAX_TRANSIENT_RETRIES = 3
# Base backoff in seconds; actual = base * 2^attempt * jitter(0.75–1.25)
_BACKOFF_BASE = 1.0

_SYSTEM_PROMPT = """\
You are a prompt engineer. Given a research agenda (program.md) and the current
content of a prompt file, propose a single targeted edit to improve the prompt.

Return ONLY a JSON object with these exact keys:
  old_text   - the exact substring to replace (must appear exactly once in the file)
  new_text   - the replacement text
  rationale  - one sentence explaining your hypothesis

No markdown fences, no extra keys, no explanation outside the JSON object.
"""


class _CoderResponse(BaseModel):
    old_text: str
    new_text: str
    rationale: str


class LiteLLMCoder:
    name: str = "litellm"

    def __init__(
        self,
        model: str | None = None,
        cost_meter: CostMeter | None = None,
    ) -> None:
        self._model = model or os.environ.get(_ENV_MODEL_KEY, _DEFAULT_MODEL)
        if cost_meter is None:
            rates = load_rates()
            cost_meter = CostMeter(
                cap_usd=Decimal(os.environ.get("AUTORESEARCH_CAP_USD", "5")),
                rates=rates,
            )
        self._cost_meter = cost_meter

    async def propose_edit(
        self,
        program_md: str,
        file_text: str,
        recent_history: list[ExperimentRecord],
    ) -> EditPatch:
        # Lazy import — keeps module importable when litellm is missing/broken.
        import litellm  # noqa: PLC0415

        history_block = _format_history(recent_history)
        user_content = (
            f"## Research agenda (program.md)\n\n{program_md}\n\n"
            f"## Prompt file content\n\n{file_text}\n\n"
            f"{history_block}"
            "Return the JSON edit object now."
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        last_json_exc: Exception | None = None
        last_raw: str = ""

        # E1 loop: retry on malformed JSON from the LLM
        for json_attempt in range(1, _MAX_JSON_RETRIES + 1):
            # E6 inner loop: retry on transient network/rate-limit errors
            response = await _call_with_transient_retry(litellm, self._model, messages)

            last_raw = response.choices[0].message.content or ""
            usage = response.usage

            # BudgetExceeded propagates — do NOT catch it here.
            self._cost_meter.tick(
                self._model,
                usage.prompt_tokens or 0,
                usage.completion_tokens or 0,
            )

            try:
                parsed = _parse_response(last_raw)
                return EditPatch(
                    target_path=Path("__pending__"),
                    old_text=parsed.old_text,
                    new_text=parsed.new_text,
                    rationale=parsed.rationale,
                )
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_json_exc = exc
                log.warning(
                    "coder JSON attempt %d/%d malformed response: %s",
                    json_attempt,
                    _MAX_JSON_RETRIES,
                    exc,
                )
                messages.append({"role": "assistant", "content": last_raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your last response was not valid JSON matching the required schema. "
                        "Return ONLY the JSON object with keys old_text, new_text, rationale."
                    ),
                })

        _reraise(last_json_exc)

    def report_cost(self) -> Decimal:
        return self._cost_meter.spent


async def _call_with_transient_retry(litellm_mod, model: str, messages: list) -> object:
    """Call litellm.acompletion with exponential backoff on transient errors (E6).

    Retries up to _MAX_TRANSIENT_RETRIES times on:
      - RateLimitError
      - APIConnectionError
      - Timeout

    Does NOT retry on:
      - AuthenticationError
      - BadRequestError

    Raises the final exception after all retries are exhausted.
    """
    _no_retry_types = (
        litellm_mod.exceptions.AuthenticationError,
        litellm_mod.exceptions.BadRequestError,
    )
    _transient_types = (
        litellm_mod.exceptions.RateLimitError,
        litellm_mod.exceptions.APIConnectionError,
        litellm_mod.exceptions.Timeout,
    )

    last_exc: Exception | None = None
    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        try:
            return await litellm_mod.acompletion(
                model=model,
                messages=messages,
                temperature=0,
            )
        except _no_retry_types:
            raise
        except _transient_types as exc:
            last_exc = exc
            if attempt >= _MAX_TRANSIENT_RETRIES:
                raise
            delay = _BACKOFF_BASE * (2 ** attempt) * (0.75 + random.random() * 0.5)
            log.warning(
                "LiteLLM transient error (attempt %d/%d), retrying in %.2fs: %s",
                attempt + 1,
                _MAX_TRANSIENT_RETRIES,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    _reraise(last_exc)


def _reraise(exc: Exception | None) -> None:
    if exc is not None:
        raise exc
    raise RuntimeError("All coder retries exhausted with no exception captured")


def _parse_response(raw: str) -> _CoderResponse:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner.append(line)
        text = "\n".join(inner)
    data = json.loads(text)
    return _CoderResponse.model_validate(data)


def _format_history(records: list[ExperimentRecord]) -> str:
    if not records:
        return ""
    lines = ["## Recent experiment history\n"]
    for r in records[-5:]:
        eval_str = ""
        if r.eval_result is not None:
            eval_str = f" eval={r.eval_result.passed}/{r.eval_result.total}"
        lines.append(
            f"- exp {r.n}: outcome={r.outcome}{eval_str} rationale={r.rationale!r}"
        )
    lines.append("")
    return "\n".join(lines)
