from __future__ import annotations

import hashlib
import json
import logging
import warnings
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, field_validator, model_validator

log = logging.getLogger(__name__)

_REQUIRED_TASK_FIELDS = {"id", "input", "assert_substring"}


class SuiteValidationError(ValueError):
    """Raised when a suite JSON fails schema or semantic validation."""


@dataclass(frozen=True)
class Task:
    id: str
    input: str
    assert_substring: str


@dataclass(frozen=True)
class TaskSuite:
    version: int
    tasks: tuple[Task, ...]
    suite_hash: str


class _TaskModel(BaseModel):
    id: str
    input: str
    assert_substring: str

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def warn_on_extra(cls, values: dict) -> dict:
        extra = set(values.keys()) - _REQUIRED_TASK_FIELDS
        if extra:
            warnings.warn(
                f"Task has unexpected fields (ignored): {sorted(extra)}",
                UserWarning,
                stacklevel=4,
            )
        return values


class _SuiteModel(BaseModel):
    version: int
    tasks: list[_TaskModel]

    @field_validator("version")
    @classmethod
    def version_must_be_one(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"Unsupported suite version {v!r}; expected 1")
        return v

    @field_validator("tasks")
    @classmethod
    def tasks_must_be_non_empty(cls, v: list[_TaskModel]) -> list[_TaskModel]:
        if not v:
            raise ValueError("tasks list must not be empty")
        return v


def _compute_suite_hash(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_suite(path: Path) -> TaskSuite:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuiteValidationError(f"Cannot read suite file {path}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SuiteValidationError(f"Suite file {path} is not valid JSON: {exc}") from exc

    try:
        model = _SuiteModel.model_validate(raw)
    except Exception as exc:
        raise SuiteValidationError(f"Suite validation failed: {exc}") from exc

    ids = [t.id for t in model.tasks]
    if len(ids) != len(set(ids)):
        seen: set[str] = set()
        dupes = [i for i in ids if i in seen or seen.add(i)]  # type: ignore[func-returns-value]
        raise SuiteValidationError(f"Duplicate task IDs found: {sorted(set(dupes))}")

    suite_hash = _compute_suite_hash(raw)

    tasks = tuple(
        Task(id=t.id, input=t.input, assert_substring=t.assert_substring)
        for t in model.tasks
    )
    return TaskSuite(version=model.version, tasks=tasks, suite_hash=suite_hash)
