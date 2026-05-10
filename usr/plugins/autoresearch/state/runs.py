from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome

ExperimentOutcome = Literal[
    "kept", "reverted",
    "smoke_failed", "coder_failed", "patch_failed",
    "eval_unusable", "crashed",
]

RunStatus = Literal[
    "running", "completed", "stopped",
    "crashed", "cost_capped", "aborted",
]


class SuiteDriftError(Exception):
    """Raised when the suite file's hash no longer matches the run's config.json."""


class ConfigAlreadyExistsError(Exception):
    """Raised when write_config() is called for a run_id that already has a config.json."""


def _default_runs_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "agent.py").exists():
            return parent / "usr" / "autoresearch" / "runs"
    return Path("usr/autoresearch/runs")


def _decimal_or_none(v: object) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _parse_task_outcome(d: dict) -> TaskOutcome:
    return TaskOutcome(
        task_id=d["task_id"],
        passed=d["passed"],
        tokens=d["tokens"],
        output=d.get("output"),
        error=d.get("error"),
    )


def _parse_eval_result(d: dict | None) -> EvalResult | None:
    if d is None:
        return None
    return EvalResult(
        passed=d["passed"],
        total=d["total"],
        total_tokens=d["total_tokens"],
        per_task=[_parse_task_outcome(t) for t in d["per_task"]],
    )


def _serialise(obj: object) -> object:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Cannot serialise {type(obj)}")


def compute_suite_hash(suite_path: Path) -> str:
    """SHA-256 of canonical-JSON (keys sorted, no whitespace) of the suite file.

    Normalising to canonical JSON before hashing means formatting differences
    (indentation, key order) in the source file do not create spurious drift
    signals — only actual content changes matter (E11).
    """
    raw = json.loads(suite_path.read_text(encoding="utf-8"))
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_config(
    run_id: str,
    runs_root: Path | None = None,
    *,
    suite_hash: str,
    profile: str,
    suite_path: str,
) -> None:
    """Write config.json for a run. Raises ConfigAlreadyExistsError if it exists.

    config.json is immutable per design §6 — once written it must never be
    overwritten, which is the single source of truth for suite_hash.
    """
    root = runs_root if runs_root is not None else _default_runs_root()
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "config.json"

    if config_path.exists():
        raise ConfigAlreadyExistsError(
            f"config.json already exists for run '{run_id}' — it is immutable"
        )

    payload = {
        "run_id": run_id,
        "profile": profile,
        "suite_path": suite_path,
        "suite_hash": suite_hash,
    }
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_config_suite_hash(run_id: str, runs_root: Path | None = None) -> str:
    root = runs_root if runs_root is not None else _default_runs_root()
    config_path = root / run_id / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return raw["suite_hash"]


@dataclass
class ExperimentRecord:
    n: int
    started_at: datetime
    finished_at: datetime | None
    outcome: ExperimentOutcome
    eval_result: EvalResult | None     # None on coder/patch/smoke failures
    judge_score: float | None           # always None in skeleton (judge is S5)
    spend_usd: Decimal
    sha: str | None                     # only set when outcome="kept"
    rationale: str | None               # from EditPatch when available


@dataclass
class RunState:
    run_id: str
    status: RunStatus
    experiments: list[ExperimentRecord]
    spend_usd: Decimal
    started_at: datetime
    baseline_sha: str                   # rotates on each "kept" commit
    # Not serialised — set by loop.py so save() writes to the right tmp dir.
    _run_dir: Path | None = field(default=None, init=True, repr=False, compare=False)

    @classmethod
    def load(cls, run_id: str, runs_root: Path | None = None) -> RunState:
        root = runs_root if runs_root is not None else _default_runs_root()
        path = root / run_id / "state.json"
        with open(path) as fh:
            raw = json.load(fh)
        experiments = []
        for e in raw.get("experiments", []):
            experiments.append(ExperimentRecord(
                n=e["n"],
                started_at=datetime.fromisoformat(e["started_at"]),
                finished_at=datetime.fromisoformat(e["finished_at"]) if e.get("finished_at") else None,
                outcome=e["outcome"],
                eval_result=_parse_eval_result(e.get("eval_result")),
                judge_score=e.get("judge_score"),
                spend_usd=Decimal(str(e["spend_usd"])),
                sha=e.get("sha"),
                rationale=e.get("rationale"),
            ))
        run_dir = root / run_id
        return cls(
            run_id=raw["run_id"],
            status=raw["status"],
            experiments=experiments,
            spend_usd=Decimal(str(raw["spend_usd"])),
            started_at=datetime.fromisoformat(raw["started_at"]),
            baseline_sha=raw["baseline_sha"],
            _run_dir=run_dir,
        )

    def save(self) -> None:
        run_dir = self._run_dir if self._run_dir is not None else _default_runs_root() / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "state.json"

        # Exclude the private _run_dir from the serialised dict.
        raw_dict = asdict(self)
        raw_dict.pop("_run_dir", None)

        data = json.dumps(raw_dict, default=_serialise, indent=2)
        # Atomic write: write to a temp file in the same directory, then
        # os.replace() which is atomic on both POSIX and Windows.
        # Readers will always see either the old complete file or the new one —
        # never a partial write (concurrent-read safety, requirement 4).
        fd, tmp_path = tempfile.mkstemp(dir=run_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(data)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def verify_suite_hash(
        self, current_suite_path: Path, runs_root: Path | None = None
    ) -> bool:
        """Compare current suite hash against the one locked in config.json.

        Raises SuiteDriftError if they differ (E11: suite drift mid-run).
        Returns True if they match.
        """
        stored = read_config_suite_hash(self.run_id, runs_root=runs_root)
        current = compute_suite_hash(current_suite_path)
        if stored != current:
            raise SuiteDriftError(
                f"Suite drift detected for run '{self.run_id}': "
                f"stored={stored[:12]}… current={current[:12]}…"
            )
        return True
