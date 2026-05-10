from __future__ import annotations

import json
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.api import _pid_registry, routes
from usr.plugins.autoresearch.state.runs import ExperimentRecord, RunState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A clean git repo so /start passes E12."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def app(runs_root: Path, repo_root: Path) -> Flask:
    flask_app = Flask(__name__)
    flask_app.config["AUTORESEARCH_RUNS_ROOT"] = str(runs_root)
    flask_app.config["AUTORESEARCH_REPO_ROOT"] = str(repo_root)
    routes.register_routes(flask_app)
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


@pytest.fixture(autouse=True)
def clear_pid_registry():
    # Reset the in-memory registry between tests for isolation.
    snap = _pid_registry.snapshot()
    for run_id in list(snap):
        _pid_registry.forget_pid(run_id)
    yield
    snap = _pid_registry.snapshot()
    for run_id in list(snap):
        _pid_registry.forget_pid(run_id)


def _write_state(
    runs_root: Path,
    run_id: str,
    status: str = "running",
    experiments: list[ExperimentRecord] | None = None,
) -> RunState:
    state = RunState(
        run_id=run_id,
        status=status,  # type: ignore[arg-type]
        experiments=experiments or [],
        spend_usd=Decimal("0.123"),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        baseline_sha="deadbeef" * 5,
        _run_dir=runs_root / run_id,
    )
    (runs_root / run_id).mkdir(parents=True, exist_ok=True)
    state.save()
    return state


def _write_program_md(repo_root: Path, *, profile: str = "trader") -> Path:
    suite = repo_root / "suite.json"
    suite.write_text(json.dumps({"version": 1, "tasks": [{"id": "t1", "input": "?", "assert_substring": "ok"}]}))
    program = repo_root / "program.md"
    program.write_text(
        f"---\nprofile: {profile}\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite}\nmax_experiments: 2\ncost_cap_usd: '5.00'\n---\n"
        "Body.\n"
    )
    return program


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_returns_run_id_and_status_url(self, client, repo_root: Path):
        program = _write_program_md(repo_root)
        # Mark all repo state as committed so worktree is clean.
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add program"], cwd=repo_root, check=True, capture_output=True
        )

        fake_proc = MagicMock(pid=12345)
        with patch("usr.plugins.autoresearch.api.routes.spawn_worker", return_value=fake_proc) as spawn:
            resp = client.post(
                "/autoresearch/start",
                json={"program_md_path": str(program)},
            )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert "run_id" in body
        assert body["status_url"].startswith("/autoresearch/status?run_id=")
        assert body["trace_url"].startswith("/autoresearch/trace?run_id=")
        assert body["pid"] == 12345
        spawn.assert_called_once()

    def test_start_rejects_missing_program_md(self, client, repo_root: Path):
        resp = client.post(
            "/autoresearch/start",
            json={"program_md_path": str(repo_root / "does-not-exist.md")},
        )
        assert resp.status_code == 400
        assert "not found" in resp.get_json()["error"]

    def test_start_rejects_missing_field(self, client):
        resp = client.post("/autoresearch/start", json={})
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"]

    def test_start_rejects_when_run_active(self, client, runs_root: Path, repo_root: Path):
        _write_state(runs_root, "active-run", status="running")
        program = _write_program_md(repo_root)
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add"], cwd=repo_root, check=True, capture_output=True
        )

        with patch("usr.plugins.autoresearch.api.routes.spawn_worker") as spawn:
            resp = client.post(
                "/autoresearch/start",
                json={"program_md_path": str(program)},
            )
        assert resp.status_code == 409
        assert "active-run" in resp.get_json()["error"]
        spawn.assert_not_called()

    def test_start_rejects_dirty_worktree(self, client, repo_root: Path):
        program = _write_program_md(repo_root)
        # Leave program.md uncommitted -> dirty worktree

        with patch("usr.plugins.autoresearch.api.routes.spawn_worker") as spawn:
            resp = client.post(
                "/autoresearch/start",
                json={"program_md_path": str(program)},
            )
        assert resp.status_code == 400
        assert "dirty" in resp.get_json()["error"].lower()
        spawn.assert_not_called()

    def test_start_rejects_invalid_program_md(self, client, repo_root: Path):
        bad = repo_root / "bad.md"
        bad.write_text("no frontmatter here")
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bad"], cwd=repo_root, check=True, capture_output=True)

        resp = client.post(
            "/autoresearch/start",
            json={"program_md_path": str(bad)},
        )
        assert resp.status_code == 400
        assert "program.md invalid" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# /stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_sends_sigterm(self, client, runs_root: Path):
        _write_state(runs_root, "run-x", status="running")
        _pid_registry.register_pid("run-x", 99999)

        with patch("usr.plugins.autoresearch.api.routes.os.kill") as mock_kill:
            resp = client.post("/autoresearch/stop", json={"run_id": "run-x"})

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "stopping"
        mock_kill.assert_called_once_with(99999, signal.SIGTERM)

    def test_stop_404_for_unknown_run(self, client):
        resp = client.post("/autoresearch/stop", json={"run_id": "nope"})
        assert resp.status_code == 404
        assert "unknown" in resp.get_json()["error"]

    def test_stop_409_for_terminal_run(self, client, runs_root: Path):
        _write_state(runs_root, "run-done", status="completed")

        resp = client.post("/autoresearch/stop", json={"run_id": "run-done"})
        assert resp.status_code == 409
        assert "terminal" in resp.get_json()["error"]

    def test_stop_missing_run_id(self, client):
        resp = client.post("/autoresearch/stop", json={})
        assert resp.status_code == 400

    def test_stop_handles_process_already_gone(self, client, runs_root: Path):
        _write_state(runs_root, "run-gone", status="running")
        _pid_registry.register_pid("run-gone", 99999)

        with patch(
            "usr.plugins.autoresearch.api.routes.os.kill",
            side_effect=ProcessLookupError(),
        ):
            resp = client.post("/autoresearch/stop", json={"run_id": "run-gone"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "stopping"
        assert body.get("warning") == "process_gone"

    def test_stop_no_pid_in_registry_returns_warning(self, client, runs_root: Path):
        _write_state(runs_root, "orphan", status="running")
        # No pid registered, no psutil match expected
        with patch(
            "usr.plugins.autoresearch.api.routes._find_pid_by_run_id",
            return_value=None,
        ):
            resp = client.post("/autoresearch/stop", json={"run_id": "orphan"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("warning") == "pid_unknown"


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_runstate_json(self, client, runs_root: Path):
        _write_state(runs_root, "run-y", status="running")

        resp = client.get("/autoresearch/status?run_id=run-y")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["run_id"] == "run-y"
        assert data["status"] == "running"
        assert "experiments" in data
        assert "spend_usd" in data
        assert "baseline_sha" in data
        assert "started_at" in data

    def test_status_404_for_unknown_run(self, client):
        resp = client.get("/autoresearch/status?run_id=nope")
        assert resp.status_code == 404

    def test_status_400_when_run_id_missing(self, client):
        resp = client.get("/autoresearch/status")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /trace
# ---------------------------------------------------------------------------


class TestTrace:
    def test_trace_streams_existing_log(self, client, runs_root: Path, monkeypatch):
        run_dir = runs_root / "run-t"
        run_dir.mkdir()
        exp_dir = run_dir / "exp-001"
        exp_dir.mkdir()
        (exp_dir / "trace.log").write_text("line one\nline two\nline three\n")
        # Mark experiment as complete via score.json so the SSE generator returns
        (exp_dir / "score.json").write_text("{}")
        # Skip sleeps in the tail loop
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.time.sleep", lambda s: None)

        resp = client.get("/autoresearch/trace?run_id=run-t&exp=1")
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")

        body = resp.get_data(as_text=True)
        assert "line one" in body
        assert "line two" in body
        assert "line three" in body
        assert "event: end" in body

    def test_trace_404_for_unknown_run(self, client):
        resp = client.get("/autoresearch/trace?run_id=nope&exp=1")
        assert resp.status_code == 404

    def test_trace_404_for_unknown_exp(self, client, runs_root: Path):
        run_dir = runs_root / "run-e"
        run_dir.mkdir()
        # No exp-* subdir
        resp = client.get("/autoresearch/trace?run_id=run-e&exp=99")
        assert resp.status_code == 404

    def test_trace_400_for_missing_params(self, client):
        resp = client.get("/autoresearch/trace")
        assert resp.status_code == 400

    def test_trace_400_for_non_integer_exp(self, client, runs_root: Path):
        run_dir = runs_root / "run-bad"
        run_dir.mkdir()
        resp = client.get("/autoresearch/trace?run_id=run-bad&exp=abc")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /runs
# ---------------------------------------------------------------------------


class TestRunsList:
    def test_lists_all_runs(self, client, runs_root: Path):
        _write_state(runs_root, "run-a", status="completed")
        _write_state(runs_root, "run-b", status="running")

        resp = client.get("/autoresearch/runs")
        assert resp.status_code == 200
        runs = resp.get_json()["runs"]
        run_ids = sorted(r["run_id"] for r in runs)
        assert run_ids == ["run-a", "run-b"]


# ---------------------------------------------------------------------------
# Internals: helpers and edge paths for coverage
# ---------------------------------------------------------------------------


class TestInternals:
    def test_runs_root_default_when_no_override(self, app):
        # Drop the override and verify the default lookup runs.
        app.config.pop("AUTORESEARCH_RUNS_ROOT", None)
        with app.test_request_context("/autoresearch/status?run_id=x"):
            result = routes._runs_root_for_request()
        assert isinstance(result, Path)

    def test_repo_root_default_when_no_override(self, app):
        app.config.pop("AUTORESEARCH_REPO_ROOT", None)
        with app.test_request_context("/autoresearch/status?run_id=x"):
            result = routes._repo_root_for_request()
        assert isinstance(result, Path)

    def test_generate_run_id_falls_back_when_uuid_utils_missing(self, monkeypatch):
        original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "uuid_utils":
                raise ImportError("simulated missing")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        result = routes._generate_run_id()
        assert isinstance(result, str)
        assert "-" in result

    def test_find_pid_by_run_id_returns_pid_when_match(self):
        fake_proc = MagicMock()
        fake_proc.info = {
            "pid": 7777,
            "cmdline": ["python", "-m", "usr.plugins.autoresearch.worker", "--run-id", "abc"],
        }
        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [fake_proc]
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})

        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            result = routes._find_pid_by_run_id("abc")
        assert result == 7777

    def test_find_pid_by_run_id_returns_none_when_no_match(self):
        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = []
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})

        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            result = routes._find_pid_by_run_id("nope")
        assert result is None

    def test_find_pid_by_run_id_psutil_missing_returns_none(self, monkeypatch):
        # Simulate the ImportError branch.
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("simulated")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert routes._find_pid_by_run_id("any") is None

    def test_stop_oskill_oserror_returns_500(self, client, runs_root: Path):
        _write_state(runs_root, "run-fail", status="running")
        _pid_registry.register_pid("run-fail", 1234)

        with patch(
            "usr.plugins.autoresearch.api.routes.os.kill",
            side_effect=OSError("eperm"),
        ):
            resp = client.post("/autoresearch/stop", json={"run_id": "run-fail"})
        assert resp.status_code == 500
        assert "SIGTERM" in resp.get_json()["error"]

    def test_start_handles_spawn_oserror(self, client, repo_root: Path):
        program = _write_program_md(repo_root)
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "p"], cwd=repo_root, check=True, capture_output=True)

        with patch(
            "usr.plugins.autoresearch.api.routes.spawn_worker",
            side_effect=OSError("ENOEXEC"),
        ):
            resp = client.post(
                "/autoresearch/start",
                json={"program_md_path": str(program)},
            )
        assert resp.status_code == 500

    def test_experiment_complete_via_state_json_terminal(self, runs_root: Path):
        run_dir = runs_root / "rx"
        run_dir.mkdir()
        (run_dir / "exp-001").mkdir()
        (run_dir / "state.json").write_text(json.dumps({"status": "completed", "experiments": []}))
        assert routes._experiment_complete(run_dir, 1) is True

    def test_experiment_complete_via_state_json_finished_record(self, runs_root: Path):
        run_dir = runs_root / "ry"
        run_dir.mkdir()
        (run_dir / "exp-002").mkdir()
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "status": "running",
                    "experiments": [{"n": 2, "finished_at": "2026-01-01T00:00:00"}],
                }
            )
        )
        assert routes._experiment_complete(run_dir, 2) is True

    def test_experiment_complete_returns_false_when_running(self, runs_root: Path):
        run_dir = runs_root / "rz"
        run_dir.mkdir()
        (run_dir / "exp-001").mkdir()
        (run_dir / "state.json").write_text(json.dumps({"status": "running", "experiments": []}))
        assert routes._experiment_complete(run_dir, 1) is False

    def test_experiment_complete_handles_corrupt_state(self, runs_root: Path):
        run_dir = runs_root / "rcorrupt"
        run_dir.mkdir()
        (run_dir / "exp-001").mkdir()
        (run_dir / "state.json").write_text("not json")
        assert routes._experiment_complete(run_dir, 1) is False

    def test_experiment_complete_no_state_no_score(self, runs_root: Path):
        run_dir = runs_root / "rempty"
        run_dir.mkdir()
        (run_dir / "exp-001").mkdir()
        assert routes._experiment_complete(run_dir, 1) is False

    def test_trace_timeout_path(self, client, runs_root: Path, monkeypatch):
        run_dir = runs_root / "run-timeout"
        run_dir.mkdir()
        exp_dir = run_dir / "exp-001"
        exp_dir.mkdir()
        # No trace.log, no score.json — never completes naturally.
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.time.sleep", lambda s: None)
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.TRACE_MAX_IDLE_S", 0.1)
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.TRACE_POLL_INTERVAL_S", 1.0)

        resp = client.get("/autoresearch/trace?run_id=run-timeout&exp=1")
        body = resp.get_data(as_text=True)
        assert "event: timeout" in body

    def test_trace_handles_oserror_reading_log(self, client, runs_root: Path, monkeypatch):
        run_dir = runs_root / "run-err"
        run_dir.mkdir()
        exp_dir = run_dir / "exp-001"
        exp_dir.mkdir()
        trace_log = exp_dir / "trace.log"
        trace_log.write_text("hi\n")
        (exp_dir / "score.json").write_text("{}")

        original_open = Path.open

        call_count = {"n": 0}

        def flaky_open(self, *args, **kwargs):
            if str(self).endswith("trace.log"):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise OSError("boom")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flaky_open)
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.time.sleep", lambda s: None)

        resp = client.get("/autoresearch/trace?run_id=run-err&exp=1")
        # Should still complete (score.json triggers end) without crashing
        assert resp.status_code == 200
        assert "event: end" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# A0 ApiHandler shims
# ---------------------------------------------------------------------------


@pytest.fixture
def shim_app(runs_root: Path, repo_root: Path) -> Flask:
    """A Flask app where the shim handlers can resolve their inner test_request_context."""
    flask_app = Flask(__name__)
    flask_app.config["AUTORESEARCH_RUNS_ROOT"] = str(runs_root)
    flask_app.config["AUTORESEARCH_REPO_ROOT"] = str(repo_root)
    routes.register_routes(flask_app)
    return flask_app


class TestShims:
    def test_status_shim_routes_through(self, shim_app, runs_root: Path):
        from usr.plugins.autoresearch.api.status import Status

        _write_state(runs_root, "run-shim", status="running")
        import threading
        import asyncio

        handler = Status(shim_app, threading.Lock())

        with shim_app.test_request_context(
            "/api/plugins/autoresearch/status?run_id=run-shim"
        ):
            resp = asyncio.run(handler.process({}, request=__import__("flask").request))

        assert resp.status_code == 200
        body = json.loads(resp.get_data(as_text=True))
        assert body["run_id"] == "run-shim"

    def test_stop_shim_routes_through(self, shim_app, runs_root: Path):
        from usr.plugins.autoresearch.api.stop import Stop

        _write_state(runs_root, "run-stop-shim", status="running")
        _pid_registry.register_pid("run-stop-shim", 8888)
        import threading
        import asyncio

        handler = Stop(shim_app, threading.Lock())

        with patch("usr.plugins.autoresearch.api.routes.os.kill"):
            with shim_app.test_request_context(
                "/api/plugins/autoresearch/stop", method="POST", json={"run_id": "run-stop-shim"}
            ):
                resp = asyncio.run(
                    handler.process(
                        {"run_id": "run-stop-shim"},
                        request=__import__("flask").request,
                    )
                )
        assert resp.status_code == 200

    def test_start_shim_routes_through(self, shim_app, repo_root: Path):
        from usr.plugins.autoresearch.api.start import Start

        program = _write_program_md(repo_root)
        import subprocess as sp

        sp.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
        sp.run(["git", "commit", "-m", "p"], cwd=repo_root, check=True, capture_output=True)

        import threading
        import asyncio

        handler = Start(shim_app, threading.Lock())

        with patch(
            "usr.plugins.autoresearch.api.routes.spawn_worker",
            return_value=MagicMock(pid=1234),
        ):
            with shim_app.test_request_context(
                "/api/plugins/autoresearch/start",
                method="POST",
                json={"program_md_path": str(program)},
            ):
                resp = asyncio.run(
                    handler.process(
                        {"program_md_path": str(program)},
                        request=__import__("flask").request,
                    )
                )
        assert resp.status_code == 200

    def test_trace_shim_routes_through(self, shim_app, runs_root: Path, monkeypatch):
        from usr.plugins.autoresearch.api.trace import Trace

        run_dir = runs_root / "run-trace-shim"
        run_dir.mkdir()
        (run_dir / "exp-001").mkdir()
        (run_dir / "exp-001" / "trace.log").write_text("hello\n")
        (run_dir / "exp-001" / "score.json").write_text("{}")
        monkeypatch.setattr("usr.plugins.autoresearch.api.routes.time.sleep", lambda s: None)

        import threading
        import asyncio

        handler = Trace(shim_app, threading.Lock())

        with shim_app.test_request_context(
            "/api/plugins/autoresearch/trace?run_id=run-trace-shim&exp=1"
        ):
            resp = asyncio.run(handler.process({}, request=__import__("flask").request))
        assert resp.status_code == 200
