from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.harness._suite_loader import (
    SuiteValidationError,
    TaskSuite,
    load_suite,
)


def _write_suite(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "suite.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_loads_valid_suite(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [
            {"id": "t-001", "input": "question?", "assert_substring": "answer"},
            {"id": "t-002", "input": "another?", "assert_substring": "yes"},
        ],
    })
    suite = load_suite(p)
    assert isinstance(suite, TaskSuite)
    assert suite.version == 1
    assert len(suite.tasks) == 2
    assert suite.tasks[0].id == "t-001"
    assert suite.tasks[1].id == "t-002"
    assert isinstance(suite.suite_hash, str) and len(suite.suite_hash) == 64


def test_suite_hash_is_stable_for_same_content(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "tasks": [{"id": "t-001", "input": "q", "assert_substring": "a"}],
    }
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    p1.write_text(json.dumps(data), encoding="utf-8")
    p2.write_text(json.dumps(data), encoding="utf-8")
    assert load_suite(p1).suite_hash == load_suite(p2).suite_hash


def test_loads_fixture_suite() -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "suite.json"
    suite = load_suite(fixture)
    assert suite.version == 1
    assert len(suite.tasks) >= 1
    assert suite.tasks[0].id == "fx-001"


# ---------------------------------------------------------------------------
# Version validation
# ---------------------------------------------------------------------------

def test_rejects_unknown_version(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 2,
        "tasks": [{"id": "t-001", "input": "q", "assert_substring": "a"}],
    })
    with pytest.raises(SuiteValidationError, match="version"):
        load_suite(p)


def test_rejects_version_zero(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 0,
        "tasks": [{"id": "t-001", "input": "q", "assert_substring": "a"}],
    })
    with pytest.raises(SuiteValidationError):
        load_suite(p)


# ---------------------------------------------------------------------------
# Duplicate task IDs
# ---------------------------------------------------------------------------

def test_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [
            {"id": "t-001", "input": "q1", "assert_substring": "a"},
            {"id": "t-001", "input": "q2", "assert_substring": "b"},
        ],
    })
    with pytest.raises(SuiteValidationError, match="Duplicate"):
        load_suite(p)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------

def test_rejects_missing_input_field(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [{"id": "t-001", "assert_substring": "a"}],
    })
    with pytest.raises(SuiteValidationError):
        load_suite(p)


def test_rejects_missing_assert_substring_field(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [{"id": "t-001", "input": "q"}],
    })
    with pytest.raises(SuiteValidationError):
        load_suite(p)


def test_rejects_missing_id_field(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [{"input": "q", "assert_substring": "a"}],
    })
    with pytest.raises(SuiteValidationError):
        load_suite(p)


# ---------------------------------------------------------------------------
# Empty tasks
# ---------------------------------------------------------------------------

def test_rejects_empty_task_list(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {"version": 1, "tasks": []})
    with pytest.raises(SuiteValidationError, match="empty"):
        load_suite(p)


# ---------------------------------------------------------------------------
# Extra fields warning
# ---------------------------------------------------------------------------

def test_warns_on_extra_fields(tmp_path: Path) -> None:
    p = _write_suite(tmp_path, {
        "version": 1,
        "tasks": [
            {
                "id": "t-001",
                "input": "q",
                "assert_substring": "a",
                "unexpected_key": "oops",
            }
        ],
    })
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        suite = load_suite(p)
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert any("unexpected_key" in str(w.message) for w in user_warnings)
    assert suite.tasks[0].id == "t-001"  # still loads successfully


# ---------------------------------------------------------------------------
# IO errors
# ---------------------------------------------------------------------------

def test_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SuiteValidationError, match="Cannot read"):
        load_suite(tmp_path / "nonexistent.json")


def test_raises_on_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(SuiteValidationError, match="not valid JSON"):
        load_suite(p)
