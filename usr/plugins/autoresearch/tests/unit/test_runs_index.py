from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.api._runs_index import (
    find_active_run,
    is_terminal,
    list_runs,
    load_state_safe,
)
from usr.plugins.autoresearch.state.runs import RunState


def _write_state(
    runs_root: Path,
    run_id: str,
    status: str = "running",
    spend: str = "0",
) -> None:
    state = RunState(
        run_id=run_id,
        status=status,  # type: ignore[arg-type]
        experiments=[],
        spend_usd=Decimal(spend),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        baseline_sha="abc123",
        _run_dir=runs_root / run_id,
    )
    (runs_root / run_id).mkdir(parents=True, exist_ok=True)
    state.save()


class TestListRuns:
    def test_lists_all_runs_under_runs_root(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "run-001")
        _write_state(tmp_path, "run-002", status="completed")
        _write_state(tmp_path, "run-003", status="stopped")

        result = list_runs(tmp_path)
        assert sorted(result) == ["run-001", "run-002", "run-003"]

    def test_skips_dirs_without_state_json(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "run-001")
        # bare dir with no state.json should be ignored
        (tmp_path / "garbage-dir").mkdir()

        assert list_runs(tmp_path) == ["run-001"]

    def test_returns_empty_when_root_missing(self, tmp_path: Path) -> None:
        assert list_runs(tmp_path / "missing") == []


class TestFindActiveRun:
    def test_finds_active_run_returns_run_id_or_none(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "run-done", status="completed")
        _write_state(tmp_path, "run-active", status="running")

        assert find_active_run(tmp_path) == "run-active"

    def test_returns_none_when_no_running(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "run-001", status="completed")
        _write_state(tmp_path, "run-002", status="stopped")

        assert find_active_run(tmp_path) is None

    def test_returns_none_when_root_empty(self, tmp_path: Path) -> None:
        assert find_active_run(tmp_path) is None


class TestLoadStateSafe:
    def test_load_state_handles_corrupt_json_gracefully(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-bad"
        run_dir.mkdir()
        (run_dir / "state.json").write_text("not json {{{")

        assert load_state_safe("run-bad", tmp_path) is None

    def test_load_state_returns_runstate_when_valid(self, tmp_path: Path) -> None:
        _write_state(tmp_path, "run-ok")
        result = load_state_safe("run-ok", tmp_path)
        assert result is not None
        assert result.run_id == "run-ok"
        assert result.status == "running"

    def test_load_state_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_state_safe("nonexistent", tmp_path) is None

    def test_load_state_handles_missing_required_field(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-partial"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps({"run_id": "run-partial"}))

        # Missing status, experiments, etc → KeyError caught -> None
        assert load_state_safe("run-partial", tmp_path) is None


class TestIsTerminal:
    def test_terminal_statuses(self) -> None:
        for s in ("completed", "stopped", "crashed", "cost_capped", "aborted"):
            assert is_terminal(s) is True

    def test_running_is_not_terminal(self) -> None:
        assert is_terminal("running") is False
