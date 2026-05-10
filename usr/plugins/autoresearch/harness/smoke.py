from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

SMOKE_TIMEOUT_S = 5


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    error: str | None
    took_ms: int


def _check_profile(profile_path: Path) -> SmokeResult:
    start = time.monotonic()

    context_yaml = profile_path / "_context.yaml"
    if not context_yaml.exists():
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(
            ok=False,
            error=f"_context.yaml not found at {context_yaml}",
            took_ms=took_ms,
        )

    try:
        with open(context_yaml) as fh:
            yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(
            ok=False,
            error=f"_context.yaml YAML parse error: {exc}",
            took_ms=took_ms,
        )

    prompts_dir = profile_path / "prompts"
    if not prompts_dir.is_dir():
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(
            ok=False,
            error=f"prompts/ directory not found at {prompts_dir}",
            took_ms=took_ms,
        )

    prompt_files = [p for p in prompts_dir.iterdir() if p.is_file()]
    if not prompt_files:
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(
            ok=False,
            error="no prompt files found in prompts/",
            took_ms=took_ms,
        )

    # Verify the editable prompt file exists and is non-empty.
    non_empty = [p for p in prompt_files if p.stat().st_size > 0]
    if not non_empty:
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(
            ok=False,
            error="no non-empty prompt files found in prompts/",
            took_ms=took_ms,
        )

    # Verify YAML frontmatter in any prompt that starts with "---".
    for prompt_file in non_empty:
        try:
            text = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            took_ms = int((time.monotonic() - start) * 1000)
            return SmokeResult(
                ok=False,
                error=f"Cannot read prompt file {prompt_file.name}: {exc}",
                took_ms=took_ms,
            )

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    yaml.safe_load(parts[1])
                except yaml.YAMLError as exc:
                    took_ms = int((time.monotonic() - start) * 1000)
                    return SmokeResult(
                        ok=False,
                        error=f"YAML frontmatter parse error in {prompt_file.name}: {exc}",
                        took_ms=took_ms,
                    )

    took_ms = int((time.monotonic() - start) * 1000)
    return SmokeResult(ok=True, error=None, took_ms=took_ms)


def smoke_check(profile_path: Path) -> SmokeResult:
    """Synchronous entry-point; enforces a 5-second hard timeout via asyncio."""
    start = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Called from within an async context — run blocking in thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_check_profile, profile_path)
                try:
                    return future.result(timeout=SMOKE_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    took_ms = int((time.monotonic() - start) * 1000)
                    return SmokeResult(ok=False, error="smoke check timed out", took_ms=took_ms)
    except RuntimeError:
        pass

    # No running loop — use asyncio.run with a timeout wrapper.
    async def _with_timeout() -> SmokeResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _check_profile, profile_path)

    try:
        result = asyncio.run(
            asyncio.wait_for(_with_timeout(), timeout=SMOKE_TIMEOUT_S)
        )
        return result
    except asyncio.TimeoutError:
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(ok=False, error="smoke check timed out", took_ms=took_ms)
    except Exception as exc:
        took_ms = int((time.monotonic() - start) * 1000)
        return SmokeResult(ok=False, error=str(exc), took_ms=took_ms)
