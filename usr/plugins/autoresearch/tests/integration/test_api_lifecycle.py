from __future__ import annotations

import json
import signal
import subprocess
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

from usr.plugins.autoresearch.api import _pid_registry, _subprocess, routes
from usr.plugins.autoresearch.state.runs import RunState


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
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
    for run_id in list(_pid_registry.snapshot()):
        _pid_registry.forget_pid(run_id)
    yield
    for run_id in list(_pid_registry.snapshot()):
        _pid_registry.forget_pid(run_id)


def _write_program_md(repo_root: Path) -> Path:
    suite = repo_root / "suite.json"
    suite.write_text(json.dumps({"version": 1, "tasks": [{"id": "t1", "input": "?", "assert_substring": "ok"}]}))
    program = repo_root / "program.md"
    program.write_text(
        f"---\nprofile: trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite}\nmax_experiments: 2\ncost_cap_usd: '5.00'\n---\n"
        "Body.\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "program"], cwd=repo_root, check=True, capture_output=True)
    return program


def _simulate_worker_state(runs_root: Path, run_id: str, status: str = "running") -> None:
    state = RunState(
        run_id=run_id,
        status=status,  # type: ignore[arg-type]
        experiments=[],
        spend_usd=Decimal("0"),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        baseline_sha="0" * 40,
        _run_dir=runs_root / run_id,
    )
    (runs_root / run_id).mkdir(parents=True, exist_ok=True)
    state.save()


class TestStartStatusStopRoundtrip:
    def test_start_status_stop_roundtrip(self, client, runs_root: Path, repo_root: Path):
        program = _write_program_md(repo_root)
        captured: dict = {}

        def fake_spawn(*, run_id, program_md_path, repo_root):  # type: ignore[no-redef]
            # Simulate the worker writing a `running` state.json synchronously
            _simulate_worker_state(runs_root, run_id, status="running")
            captured["run_id"] = run_id
            proc = MagicMock(pid=42424)
            captured["proc"] = proc
            return proc

        with patch("usr.plugins.autoresearch.api.routes.spawn_worker", side_effect=fake_spawn):
            resp = client.post(
                "/autoresearch/start",
                json={"program_md_path": str(program)},
            )
        assert resp.status_code == 200
        run_id = resp.get_json()["run_id"]
        assert captured["run_id"] == run_id

        # Status while running
        resp = client.get(f"/autoresearch/status?run_id={run_id}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

        # Stop — sends SIGTERM to recorded pid
        with patch("usr.plugins.autoresearch.api.routes.os.kill") as mock_kill:
            resp = client.post("/autoresearch/stop", json={"run_id": run_id})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "stopping"
        mock_kill.assert_called_once_with(42424, signal.SIGTERM)

        # Simulate worker handling SIGTERM
        _simulate_worker_state(runs_root, run_id, status="stopped")

        resp = client.get(f"/autoresearch/status?run_id={run_id}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "stopped"

        # Second stop after terminal -> 409
        resp = client.post("/autoresearch/stop", json={"run_id": run_id})
        assert resp.status_code == 409


class TestSubprocessEnv:
    def test_subprocess_env_filters_secrets(self, monkeypatch):
        monkeypatch.setenv("BIRDEYE_API_KEY", "leaked")
        monkeypatch.setenv("HL_PRIVATE_KEY", "leaked")
        monkeypatch.setenv("BINANCE_API_KEY", "leaked")
        monkeypatch.setenv("PATH_OK", "kept")

        captured: dict = {}

        class FakePopen:
            def __init__(self, cmd, *, cwd, env, **kwargs):
                captured["env"] = env
                captured["kwargs"] = kwargs
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                self.pid = 1234

        _subprocess.spawn_worker(
            run_id="r1",
            program_md_path="/tmp/program.md",
            repo_root=Path("/tmp"),
            popen=FakePopen,
        )
        env = captured["env"]
        assert "BIRDEYE_API_KEY" not in env
        assert "HL_PRIVATE_KEY" not in env
        assert "BINANCE_API_KEY" not in env
        assert env.get("PATH_OK") == "kept"
        assert captured["kwargs"].get("start_new_session") is True

    def test_filtered_env_excludes_full_deny_list(self, monkeypatch):
        for name in _subprocess.SECRET_DENY_LIST:
            monkeypatch.setenv(name, "secret")
        env = _subprocess.filtered_env()
        for name in _subprocess.SECRET_DENY_LIST:
            assert name not in env
