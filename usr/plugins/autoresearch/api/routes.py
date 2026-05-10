"""Flask Blueprint exposing the autoresearch HTTP API.

The same handler logic is also exposed via A0's per-file ApiHandler convention
in `api/start.py`, `api/stop.py`, `api/status.py`, `api/trace.py`. The Blueprint
exists primarily so unit tests can drive the full request/response cycle with
Flask's `test_client`.

Routes (mounted at `/autoresearch` by `register_routes`):

  POST /start    {program_md_path}                  -> {run_id, status_url, trace_url}
  POST /stop     {run_id}                           -> {status: "stopping"}
  GET  /status?run_id=<id>                          -> RunState JSON
  GET  /trace?run_id=<id>&exp=<n>                   -> SSE text/event-stream
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from flask import Blueprint, Flask, Response, jsonify, request, stream_with_context

from usr.plugins.autoresearch.api._pid_registry import (
    forget_pid,
    get_pid,
    register_pid,
)
from usr.plugins.autoresearch.api._runs_index import (
    find_active_run,
    is_terminal,
    list_runs,
    load_state_safe,
    runs_root,
)
from usr.plugins.autoresearch.api._subprocess import find_repo_root, spawn_worker
from usr.plugins.autoresearch.worker._program_md import ProgramMdError, parse_program_md

log = logging.getLogger(__name__)

bp = Blueprint("autoresearch", __name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _runs_root_for_request() -> Path:
    """Allow tests to override via app config."""
    from flask import current_app

    override = current_app.config.get("AUTORESEARCH_RUNS_ROOT")
    if override is not None:
        return Path(override)
    return runs_root()


def _repo_root_for_request() -> Path:
    from flask import current_app

    override = current_app.config.get("AUTORESEARCH_REPO_ROOT")
    if override is not None:
        return Path(override)
    return find_repo_root()


def _generate_run_id() -> str:
    try:
        import uuid_utils

        return str(uuid_utils.uuid7())
    except ImportError:
        import random
        import string

        suffix = "".join(random.choices(string.hexdigits[:16], k=4))
        return f"{time.time_ns()}-{suffix}"


def _state_to_jsonable(state) -> dict:
    raw = asdict(state)
    raw.pop("_run_dir", None)
    return json.loads(json.dumps(raw, default=str))


def _json_error(message: str, status: int) -> Response:
    return Response(
        response=json.dumps({"error": message}),
        status=status,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


@bp.route("/start", methods=["POST"])
def start():  # type: ignore[no-untyped-def]
    body = request.get_json(silent=True) or {}
    program_md_path_str = body.get("program_md_path")
    if not program_md_path_str:
        return _json_error("program_md_path is required", 400)

    repo_root = _repo_root_for_request()
    runs = _runs_root_for_request()

    program_md_path = Path(program_md_path_str)
    if not program_md_path.is_absolute():
        program_md_path = (repo_root / program_md_path).resolve()

    if not program_md_path.exists():
        return _json_error(f"program_md not found: {program_md_path}", 400)

    try:
        parse_program_md(program_md_path)
    except ProgramMdError as exc:
        return _json_error(f"program.md invalid: {exc}", 400)

    # Single concurrency
    active = find_active_run(runs)
    if active is not None:
        return _json_error(
            f"another run is already active: {active}",
            409,
        )

    # E12: dirty worktree
    try:
        _check_clean_worktree(repo_root)
    except RuntimeError as exc:
        return _json_error(str(exc), 400)

    run_id = _generate_run_id()

    try:
        proc = spawn_worker(
            run_id=run_id,
            program_md_path=str(program_md_path),
            repo_root=repo_root,
        )
    except OSError as exc:
        return _json_error(f"failed to spawn worker: {exc}", 500)

    register_pid(run_id, proc.pid)

    return jsonify(
        {
            "run_id": run_id,
            "status_url": f"/autoresearch/status?run_id={run_id}",
            "trace_url": f"/autoresearch/trace?run_id={run_id}&exp=1",
            "pid": proc.pid,
        }
    )


def _check_clean_worktree(repo_root: Path) -> None:
    """Reject /start if the worktree has uncommitted changes (E12)."""
    import subprocess

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()}")
    if result.stdout.strip():
        raise RuntimeError(
            "dirty worktree — commit or stash changes before starting a run"
        )


# ---------------------------------------------------------------------------
# /stop
# ---------------------------------------------------------------------------


@bp.route("/stop", methods=["POST"])
def stop():  # type: ignore[no-untyped-def]
    body = request.get_json(silent=True) or {}
    run_id = body.get("run_id")
    if not run_id:
        return _json_error("run_id is required", 400)

    runs = _runs_root_for_request()
    state = load_state_safe(run_id, runs)
    if state is None:
        return _json_error(f"unknown run_id: {run_id}", 404)

    if is_terminal(state.status):
        return _json_error(
            f"run is already terminal (status={state.status})", 409
        )

    pid = get_pid(run_id) or _find_pid_by_run_id(run_id)
    if pid is None:
        log.warning("/stop: no PID found for run %s — relying on state.json", run_id)
        return jsonify({"status": "stopping", "warning": "pid_unknown"})

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        forget_pid(run_id)
        return jsonify({"status": "stopping", "warning": "process_gone"})
    except OSError as exc:
        return _json_error(f"failed to send SIGTERM: {exc}", 500)

    return jsonify({"status": "stopping", "pid": pid})


def _find_pid_by_run_id(run_id: str) -> int | None:
    """Fallback PID lookup by scanning process command lines for --run-id."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if "--run-id" in cmdline:
                idx = cmdline.index("--run-id")
                if idx + 1 < len(cmdline) and cmdline[idx + 1] == run_id:
                    return int(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[attr-defined]
            continue
    return None


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


@bp.route("/status", methods=["GET"])
def status():  # type: ignore[no-untyped-def]
    run_id = request.args.get("run_id")
    if not run_id:
        return _json_error("run_id query param required", 400)

    runs = _runs_root_for_request()
    state = load_state_safe(run_id, runs)
    if state is None:
        return _json_error(f"unknown run_id: {run_id}", 404)

    return jsonify(_state_to_jsonable(state))


# ---------------------------------------------------------------------------
# /trace — Server-Sent Events tail
# ---------------------------------------------------------------------------


TRACE_POLL_INTERVAL_S = 0.5
TRACE_MAX_IDLE_S = 300  # 5 minutes of no new content -> close


@bp.route("/trace", methods=["GET"])
def trace():  # type: ignore[no-untyped-def]
    run_id = request.args.get("run_id")
    exp_raw = request.args.get("exp")
    if not run_id or not exp_raw:
        return _json_error("run_id and exp query params required", 400)

    try:
        exp_n = int(exp_raw)
    except ValueError:
        return _json_error("exp must be an integer", 400)

    runs = _runs_root_for_request()
    run_dir = runs / run_id
    if not run_dir.exists():
        return _json_error(f"unknown run_id: {run_id}", 404)

    exp_dir = run_dir / f"exp-{exp_n:03d}"
    if not exp_dir.exists():
        return _json_error(f"unknown exp_n {exp_n} for run {run_id}", 404)

    trace_path = exp_dir / "trace.log"

    def _events() -> Iterator[str]:
        yield from _tail_trace(trace_path, run_dir, run_id, exp_n)

    return Response(
        stream_with_context(_events()),
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _tail_trace(
    trace_path: Path,
    run_dir: Path,
    run_id: str,
    exp_n: int,
) -> Iterator[str]:
    """Yield SSE-framed lines from trace.log; tail-follow until exp completes."""
    last_size = 0
    idle_for = 0.0

    while True:
        if trace_path.exists():
            try:
                with trace_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(last_size)
                    chunk = fh.read()
                    last_size = fh.tell()
            except OSError:
                chunk = ""
            if chunk:
                idle_for = 0.0
                for line in chunk.splitlines():
                    if line:
                        yield f"data: {line}\n\n"

        if _experiment_complete(run_dir, exp_n):
            yield "event: end\ndata: {}\n\n"
            return

        idle_for += TRACE_POLL_INTERVAL_S
        if idle_for > TRACE_MAX_IDLE_S:
            yield "event: timeout\ndata: {}\n\n"
            return

        time.sleep(TRACE_POLL_INTERVAL_S)


def _experiment_complete(run_dir: Path, exp_n: int) -> bool:
    """Heuristic: an experiment is done when score.json appears, OR when
    the run's state.json has a record for that experiment with a finished_at."""
    exp_dir = run_dir / f"exp-{exp_n:03d}"
    if (exp_dir / "score.json").exists():
        return True
    state_path = run_dir / "state.json"
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if is_terminal(raw.get("status", "")):
            return True
        for exp in raw.get("experiments", []):
            if exp.get("n") == exp_n and exp.get("finished_at"):
                return True
    return False


# ---------------------------------------------------------------------------
# /runs — list past runs (panel "past runs" expansion)
# ---------------------------------------------------------------------------


@bp.route("/runs", methods=["GET"])
def runs_list():  # type: ignore[no-untyped-def]
    runs = _runs_root_for_request()
    out = []
    for run_id in list_runs(runs):
        state = load_state_safe(run_id, runs)
        if state is None:
            continue
        out.append(
            {
                "run_id": run_id,
                "status": state.status,
                "started_at": state.started_at.isoformat()
                if hasattr(state.started_at, "isoformat")
                else str(state.started_at),
                "experiments": len(state.experiments),
                "spend_usd": str(state.spend_usd),
            }
        )
    return jsonify({"runs": out})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(app: Flask, prefix: str = "/autoresearch") -> None:
    """Mount the blueprint on `app` at `prefix`."""
    app.register_blueprint(bp, url_prefix=prefix)
