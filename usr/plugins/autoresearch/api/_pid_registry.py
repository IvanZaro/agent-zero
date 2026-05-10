"""In-memory PID registry for autoresearch worker subprocesses.

Lives only for the lifetime of the Flask process. On reload, it resets.
That's acceptable: the worker keeps writing state.json regardless, so /stop
falls back to scanning state.json for running runs and killing by run_id
(via psutil, falling back to a process-list grep on the command line).
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_pids: dict[str, int] = {}


def register_pid(run_id: str, pid: int) -> None:
    with _lock:
        _pids[run_id] = pid


def get_pid(run_id: str) -> int | None:
    with _lock:
        return _pids.get(run_id)


def forget_pid(run_id: str) -> None:
    with _lock:
        _pids.pop(run_id, None)


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_pids)
