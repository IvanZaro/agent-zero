from __future__ import annotations

import os
import shutil


class ClaudeCodeNotInstalled(Exception):
    """Raised when the claude binary is not on PATH in a local (non-VPS) environment."""


def _claude_cmd() -> list[str]:
    """Resolve the Claude Code command prefix based on A0_ENV.

    On VPS (A0_ENV=vps) the worker shells into the dedicated claude-mcp
    container rather than the local filesystem, so no PATH check is needed.
    On local, we verify the binary exists and raise a clear error if it doesn't.
    """
    if os.environ.get("A0_ENV") == "vps":
        return ["docker", "exec", "claude-mcp", "claude"]

    if shutil.which("claude") is None:
        raise ClaudeCodeNotInstalled(
            "claude binary not found on PATH. "
            "Install Claude Code or use backend: litellm."
        )
    return ["claude"]
