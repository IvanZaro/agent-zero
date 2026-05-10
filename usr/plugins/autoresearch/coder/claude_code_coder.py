from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.coder._backend_resolver import ClaudeCodeNotInstalled, _claude_cmd
from usr.plugins.autoresearch.worker.cost_meter import CostMeter

if TYPE_CHECKING:
    from usr.plugins.autoresearch.state.runs import ExperimentRecord

log = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 3

# Env vars to strip from the subprocess environment (secrets posture §10).
_SECRET_DENY_PREFIXES = ("HL_", "BINANCE_")
_SECRET_DENY_EXACT = frozenset({"BIRDEYE_API_KEY"})

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


class ClaudeCodeBackendError(Exception):
    """Non-retryable error from the claude subprocess (auth, model-not-found, etc.)."""


class ClaudeCodeCoder:
    name = "claude_code"

    def __init__(
        self,
        model: str,
        cost_meter: CostMeter,
        *,
        timeout_seconds: int = 180,
        env: dict | None = None,
    ) -> None:
        self._model = model
        self._cost_meter = cost_meter
        self._timeout = timeout_seconds
        self._env = env or {}
        # Resolved lazily on first propose_edit() call.
        self._cmd: list[str] | None = None

    def _resolve_cmd(self) -> list[str]:
        """Lazy resolution — module import never fails even when claude is absent."""
        if self._cmd is None:
            self._cmd = _claude_cmd()
        return self._cmd

    def _build_env(self) -> dict[str, str]:
        """Build subprocess env: inherit os.environ, apply deny-list, then overlay self._env."""
        env = {k: v for k, v in os.environ.items() if not _is_secret(k)}
        env.update(self._env)
        return env

    async def propose_edit(
        self,
        program_md: str,
        file_text: str,
        recent_history: list[ExperimentRecord],
    ) -> EditPatch:
        cmd_prefix = self._resolve_cmd()
        prompt_text = _build_prompt(program_md, file_text, recent_history)

        last_exc: Exception | None = None

        for attempt in range(1, _MAX_JSON_RETRIES + 1):
            raw, usage = _run_claude(
                cmd_prefix=cmd_prefix,
                model=self._model,
                prompt_text=prompt_text,
                timeout=self._timeout,
                env=self._build_env(),
            )

            # Tick cost meter regardless of JSON parse outcome — fail-open.
            _tick_meter(self._cost_meter, self._model, usage)

            try:
                parsed = _parse_response(raw)
                return EditPatch(
                    target_path=Path("__pending__"),
                    old_text=parsed.old_text,
                    new_text=parsed.new_text,
                    rationale=parsed.rationale,
                )
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "claude_code JSON attempt %d/%d malformed: %s",
                    attempt,
                    _MAX_JSON_RETRIES,
                    exc,
                )

        _reraise(last_exc)

    def report_cost(self) -> Decimal:
        return self._cost_meter.spent


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_secret(key: str) -> bool:
    if key in _SECRET_DENY_EXACT:
        return True
    return any(key.startswith(p) for p in _SECRET_DENY_PREFIXES)


def _build_prompt(program_md: str, file_text: str, history: list[ExperimentRecord]) -> str:
    history_block = _format_history(history)
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"## Research agenda (program.md)\n\n{program_md}\n\n"
        f"## Prompt file content\n\n{file_text}\n\n"
        f"{history_block}"
        "Return the JSON edit object now."
    )


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


def _run_claude(
    *,
    cmd_prefix: list[str],
    model: str,
    prompt_text: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[str, dict]:
    """Invoke claude CLI, return (stdout_text, usage_dict).

    Uses a temp file for the prompt to avoid argv quoting issues with
    multi-line text. The claude CLI supports --print (-p) + stdin via
    --input-format text; we fall back to a temp file if stdin piping
    isn't available (older CLI versions).

    Raises ClaudeCodeBackendError on non-retryable failures.
    Raises subprocess.TimeoutExpired on timeout (propagates to loop).
    """
    cmd = cmd_prefix + [
        "--print",
        "--output-format", "json",
        "--model", model,
    ]

    # Pass prompt via stdin using a temp file to dodge argv quoting issues.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(prompt_text)
        tmp_path = tf.name

    try:
        with open(tmp_path, "r", encoding="utf-8") as stdin_fh:
            result = subprocess.run(
                cmd,
                stdin=stdin_fh,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "auth" in stderr or "unauthorized" in stderr or "api_key" in stderr:
            raise ClaudeCodeBackendError(
                f"Claude authentication error: {result.stderr.strip()}"
            )
        if "model" in stderr and ("not found" in stderr or "invalid" in stderr):
            raise ClaudeCodeBackendError(
                f"Claude model not found: {result.stderr.strip()}"
            )
        raise ClaudeCodeBackendError(
            f"claude exited {result.returncode}: {result.stderr.strip()}"
        )

    return _extract_content_and_usage(result.stdout)


def _extract_content_and_usage(stdout: str) -> tuple[str, dict]:
    """Parse claude's --output-format json envelope.

    Returns (assistant_text, usage_dict).
    usage_dict may be empty if the CLI version doesn't emit it — callers
    handle that gracefully (fail-open cost tracking).
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        # Older CLI or non-JSON output — treat entire stdout as raw text.
        return stdout.strip(), {}

    # claude --output-format json wraps the response in an envelope.
    # Structure (as of Claude Code CLI): {"type": "result", "result": "...", "usage": {...}}
    # or may nest messages under "messages".
    usage: dict = {}
    if isinstance(envelope, dict):
        usage = envelope.get("usage") or {}
        # Try "result" key first (single-turn --print mode)
        if "result" in envelope:
            return str(envelope["result"]), usage
        # Fall back to extracting from messages array
        messages = envelope.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # content blocks
                    text_blocks = [
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    return "".join(text_blocks), usage
                return str(content), usage

    # Fallback: return raw stdout.
    return stdout.strip(), usage


def _tick_meter(meter: CostMeter, model: str, usage: dict) -> None:
    input_tokens = 0
    output_tokens = 0
    try:
        if usage:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        else:
            log.warning(
                "claude_code: no usage data in response for model %r; "
                "cost meter ticked with 0 tokens (fail-open).",
                model,
            )
    except (TypeError, ValueError):
        log.warning(
            "claude_code: malformed usage data %r; ticking meter with 0 tokens.",
            usage,
        )
    meter.tick(model, input_tokens, output_tokens)


def _parse_response(raw: str) -> _CoderResponse:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
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


def _reraise(exc: Exception | None) -> None:
    if exc is not None:
        raise exc
    raise RuntimeError("All claude_code retries exhausted with no exception captured")
