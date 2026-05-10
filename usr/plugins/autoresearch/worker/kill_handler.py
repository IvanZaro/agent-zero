from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_baseline_sha: str | None = None
_repo_root: Path | None = None


def _sigterm_handler(signum: int, frame: object) -> None:
    log.warning("SIGTERM received — running kill_recovery")
    if _baseline_sha is not None and _repo_root is not None:
        from usr.plugins.autoresearch.state.git_ops import kill_recovery
        kill_recovery(_baseline_sha, _repo_root)
    sys.exit(0)


def register(baseline_sha: str, repo_root: Path) -> None:
    global _baseline_sha, _repo_root
    _baseline_sha = baseline_sha
    _repo_root = repo_root
    signal.signal(signal.SIGTERM, _sigterm_handler)
    log.debug("SIGTERM handler registered (baseline=%s)", baseline_sha)


def update_baseline(new_sha: str) -> None:
    global _baseline_sha
    _baseline_sha = new_sha
