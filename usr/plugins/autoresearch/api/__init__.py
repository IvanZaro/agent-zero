"""Autoresearch HTTP API.

A0 routes `/api/plugins/autoresearch/<handler>` to handler classes in this
package. Each `<handler>.py` defines exactly one ApiHandler subclass which
A0's dispatcher (`helpers.api.register_api_route`) discovers via reflection.

Helpers (prefixed with _) are not routed.
"""
from __future__ import annotations

from usr.plugins.autoresearch.api._pid_registry import (
    forget_pid,
    get_pid,
    register_pid,
)
from usr.plugins.autoresearch.api._runs_index import (
    find_active_run,
    list_runs,
    load_state_safe,
    runs_root,
)

__all__ = [
    "forget_pid",
    "get_pid",
    "register_pid",
    "find_active_run",
    "list_runs",
    "load_state_safe",
    "runs_root",
]
