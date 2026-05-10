from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.state.runs import (
    RunState,
    compute_suite_hash,
    read_config_suite_hash,
    write_config,
)


def _make_run_state(run_dir: Path, run_id: str = "run-001") -> RunState:
    state = RunState(
        run_id=run_id,
        status="running",
        experiments=[],
        spend_usd=Decimal("0"),
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        baseline_sha="abc123",
        _run_dir=run_dir,
    )
    return state


class TestAtomicStateWrite:
    def test_atomic_state_write_no_torn_reads_under_concurrent_load(
        self, tmp_path: Path
    ) -> None:
        state = _make_run_state(tmp_path)
        state.save()

        errors: list[str] = []
        stop_event = threading.Event()

        def writer() -> None:
            for i in range(100):
                s = _make_run_state(tmp_path)
                s.save()
            stop_event.set()

        def reader() -> None:
            path = tmp_path / "state.json"
            for _ in range(250):
                try:
                    raw = path.read_text(encoding="utf-8")
                    json.loads(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"torn read: {exc}")
                except FileNotFoundError:
                    pass  # transient during first write is acceptable

        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader) for _ in range(4)]

        for t in reader_threads:
            t.start()
        writer_thread.start()

        writer_thread.join(timeout=10)
        stop_event.wait(timeout=10)
        for t in reader_threads:
            t.join(timeout=5)

        assert errors == [], f"Torn reads observed: {errors}"

    def test_save_writes_to_tmp_then_replace(self, tmp_path: Path) -> None:
        # Verify no partial state.json.tmp survives after a successful save.
        state = _make_run_state(tmp_path)
        state.save()
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Leftover .tmp files after save: {tmp_files}"

    def test_save_produces_valid_json(self, tmp_path: Path) -> None:
        state = _make_run_state(tmp_path)
        state.save()
        raw = (tmp_path / "state.json").read_text()
        parsed = json.loads(raw)
        assert parsed["run_id"] == "run-001"
        assert parsed["status"] == "running"


class TestComputeSuiteHash:
    def test_compute_suite_hash_deterministic(self, tmp_path: Path) -> None:
        suite = tmp_path / "suite.json"
        suite.write_text('{"tasks": [{"id": "t1"}, {"id": "t2"}]}')

        h1 = compute_suite_hash(suite)
        h2 = compute_suite_hash(suite)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_compute_suite_hash_same_content_different_formatting(
        self, tmp_path: Path
    ) -> None:
        # Canonical JSON: key-sorted, no whitespace — same logical content
        # must produce the same hash regardless of source formatting.
        compact = tmp_path / "compact.json"
        pretty = tmp_path / "pretty.json"

        data = {"b": 2, "a": 1}
        compact.write_text(json.dumps(data))
        pretty.write_text(json.dumps(data, indent=4))

        assert compute_suite_hash(compact) == compute_suite_hash(pretty)

    def test_compute_suite_hash_different_content_differs(
        self, tmp_path: Path
    ) -> None:
        suite_a = tmp_path / "a.json"
        suite_b = tmp_path / "b.json"
        suite_a.write_text('{"tasks": [{"id": "t1"}]}')
        suite_b.write_text('{"tasks": [{"id": "t2"}]}')

        assert compute_suite_hash(suite_a) != compute_suite_hash(suite_b)

    def test_compute_suite_hash_key_order_invariant(self, tmp_path: Path) -> None:
        order_a = tmp_path / "order_a.json"
        order_b = tmp_path / "order_b.json"
        # Same keys, different insertion order.
        order_a.write_text('{"z": 3, "a": 1, "m": 2}')
        order_b.write_text('{"a": 1, "m": 2, "z": 3}')

        assert compute_suite_hash(order_a) == compute_suite_hash(order_b)


class TestSuiteDriftDetection:
    def test_suite_drift_raises_when_hash_changes(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.state.runs import SuiteDriftError

        suite = tmp_path / "suite.json"
        suite.write_text('{"tasks": [{"id": "t1"}]}')
        original_hash = compute_suite_hash(suite)

        write_config(
            run_id="run-drift",
            runs_root=tmp_path,
            suite_hash=original_hash,
            profile="trader",
            suite_path=str(suite),
        )

        state = _make_run_state(tmp_path / "run-drift", "run-drift")

        # Mutate suite after config was written.
        suite.write_text('{"tasks": [{"id": "t1"}, {"id": "INJECTED"}]}')

        with pytest.raises(SuiteDriftError):
            state.verify_suite_hash(suite, runs_root=tmp_path)

    def test_suite_no_drift_passes(self, tmp_path: Path) -> None:
        suite = tmp_path / "suite.json"
        suite.write_text('{"tasks": [{"id": "t1"}]}')
        original_hash = compute_suite_hash(suite)

        write_config(
            run_id="run-nodrift",
            runs_root=tmp_path,
            suite_hash=original_hash,
            profile="trader",
            suite_path=str(suite),
        )

        state = _make_run_state(tmp_path / "run-nodrift", "run-nodrift")
        assert state.verify_suite_hash(suite, runs_root=tmp_path) is True


class TestConfigImmutability:
    def test_config_json_is_immutable_after_first_write(
        self, tmp_path: Path
    ) -> None:
        from usr.plugins.autoresearch.state.runs import ConfigAlreadyExistsError

        write_config(
            run_id="run-imm",
            runs_root=tmp_path,
            suite_hash="abc123",
            profile="trader",
            suite_path="/some/path",
        )

        with pytest.raises(ConfigAlreadyExistsError):
            write_config(
                run_id="run-imm",
                runs_root=tmp_path,
                suite_hash="different-hash",
                profile="trader",
                suite_path="/some/path",
            )

    def test_read_config_suite_hash_round_trips(self, tmp_path: Path) -> None:
        write_config(
            run_id="run-rtrip",
            runs_root=tmp_path,
            suite_hash="deadbeef",
            profile="trader",
            suite_path="/suite.json",
        )
        h = read_config_suite_hash("run-rtrip", runs_root=tmp_path)
        assert h == "deadbeef"
