"""Helpers for inspecting `usr/autoresearch/runs/`.

Pure file-system reads — no Flask, no HTTP. Used by the API handlers to
implement single-concurrency guards, status look-ups, and run listings.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from usr.plugins.autoresearch.state.runs import RunState

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "stopped", "crashed", "cost_capped", "aborted"}


def runs_root(repo_root: Path | None = None) -> Path:
    """Default runs root: <repo>/usr/autoresearch/runs."""
    if repo_root is not None:
        return repo_root / "usr" / "autoresearch" / "runs"
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "agent.py").exists():
            return parent / "usr" / "autoresearch" / "runs"
    return Path("usr/autoresearch/runs").resolve()


def list_runs(root: Path | None = None) -> list[str]:
    """Return all run_ids that have a state.json under root."""
    base = root if root is not None else runs_root()
    if not base.exists():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if (child / "state.json").exists():
            out.append(child.name)
    return out


def load_state_safe(run_id: str, root: Path | None = None) -> RunState | None:
    """Load RunState; return None if state.json missing or corrupt."""
    base = root if root is not None else runs_root()
    state_path = base / run_id / "state.json"
    if not state_path.exists():
        return None
    try:
        return RunState.load(run_id, runs_root=base)
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        log.warning("corrupt state.json for run %s: %s", run_id, exc)
        return None


def find_active_run(root: Path | None = None) -> str | None:
    """Return run_id of the single active (status='running') run, else None.

    v1 invariant: at most one concurrent run. Used as the 409 guard in /start.
    """
    base = root if root is not None else runs_root()
    for run_id in list_runs(base):
        state = load_state_safe(run_id, base)
        if state is not None and state.status == "running":
            return run_id
    return None


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
