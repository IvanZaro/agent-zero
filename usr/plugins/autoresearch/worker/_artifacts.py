"""Atomic artifact writes for exp-NNN/ directories."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def _serialise(obj: object) -> object:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Cannot serialise {type(obj)}")


def write_atomic(path: Path, content: str) -> None:
    """Write content to path atomically (tmp → os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def git_diff_patch(baseline_sha: str, prompt_file_rel: str, repo_root: Path) -> str:
    """Return unified diff between baseline_sha and HEAD for the given file."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{baseline_sha}..HEAD", "--", prompt_file_rel],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def write_experiment_artifacts(
    exp_dir: Path,
    *,
    n: int,
    outcome: str,
    sha: str | None,
    started_at: datetime,
    finished_at: datetime,
    spend_usd: Decimal,
    rationale: str | None,
    eval_result: object | None,
    diff_patch: str | None,
    program_md_text: str,
    trace_lines: list[str],
    judge_score: object | None = None,
) -> None:
    """Atomically write all per-experiment artifact files."""
    exp_dir.mkdir(parents=True, exist_ok=True)

    write_atomic(exp_dir / "program.md", program_md_text)

    outcome_data = {
        "n": n,
        "outcome": outcome,
        "sha": sha,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "spend_usd": str(spend_usd),
        "rationale": rationale,
    }
    write_atomic(exp_dir / "outcome.json", json.dumps(outcome_data, indent=2))

    if eval_result is not None:
        try:
            score_data = asdict(eval_result)
        except TypeError:
            score_data = None
    else:
        score_data = None
    write_atomic(
        exp_dir / "score.json",
        json.dumps(score_data, default=_serialise, indent=2),
    )

    if diff_patch is not None:
        write_atomic(exp_dir / "diff.patch", diff_patch)

    write_atomic(exp_dir / "trace.log", "\n".join(trace_lines) + "\n")

    if eval_result is not None:
        try:
            eval_data = asdict(eval_result)
        except TypeError:
            eval_data = None
        if eval_data is not None:
            write_atomic(
                exp_dir / "eval_result.json",
                json.dumps(eval_data, default=_serialise, indent=2),
            )

    if judge_score is not None:
        try:
            judge_data = asdict(judge_score)
        except TypeError:
            judge_data = {"score": getattr(judge_score, "score", None)}
        write_atomic(
            exp_dir / "judge.json",
            json.dumps(judge_data, default=_serialise, indent=2),
        )
