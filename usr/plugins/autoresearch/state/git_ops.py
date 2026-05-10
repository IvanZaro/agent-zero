from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from usr.plugins.autoresearch.coder.base import EditPatch

log = logging.getLogger(__name__)

_BRANCH_PREFIX = "autoresearch"


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    error: str | None
    occurrences_found: int


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _current_sha(repo_root: Path) -> str:
    result = _git(["rev-parse", "HEAD"], cwd=repo_root)
    return result.stdout.strip()


def _current_branch(repo_root: Path) -> str:
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    return result.stdout.strip()


def start_run_branch(run_id: str, repo_root: Path) -> str:
    branch = f"{_BRANCH_PREFIX}/{run_id}"
    _git(["checkout", "-b", branch], cwd=repo_root)
    sha = _current_sha(repo_root)
    log.info("created branch %s at %s", branch, sha)
    return sha


def commit_experiment(
    _run_id: str,
    n: int,
    message: str,
    repo_root: Path,
) -> str:
    _git(["add", "-A"], cwd=repo_root)
    _git(["commit", "-m", message], cwd=repo_root)
    sha = _current_sha(repo_root)
    log.info("experiment %d committed: %s", n, sha)
    return sha


def revert_to_baseline(baseline_sha: str, repo_root: Path) -> None:
    _git(["reset", "--hard", baseline_sha], cwd=repo_root)
    log.info("reverted to baseline %s", baseline_sha)


def kill_recovery(baseline_sha: str, repo_root: Path) -> None:
    # Safety: refuse to reset if not on an autoresearch branch.
    # Resetting main would destroy work that is not ours to touch.
    try:
        branch = _current_branch(repo_root)
    except subprocess.CalledProcessError as exc:
        log.critical(
            "kill_recovery: could not determine current branch — refusing reset. stderr=%s",
            exc.stderr,
        )
        return

    if not branch.startswith(f"{_BRANCH_PREFIX}/"):
        log.critical(
            "kill_recovery: current branch '%s' is not an autoresearch branch — "
            "refusing git reset --hard to protect non-autoresearch history",
            branch,
        )
        return

    try:
        _git(["reset", "--hard", baseline_sha], cwd=repo_root)
        # git reset --hard does NOT remove untracked files (exp-NNN/ artifacts).
        # This is intentional: partial artifacts are preserved for post-mortem.
        log.info("kill_recovery: reset to %s", baseline_sha)
    except subprocess.CalledProcessError as exc:
        log.error("kill_recovery git reset failed: %s", exc.stderr)


def apply_edit_patch(patch: EditPatch, _repo_root: Path) -> ApplyResult:
    """Apply an EditPatch to its target file with uniqueness validation.

    Returns ApplyResult(ok=False, ...) when old_text is absent or ambiguous —
    the caller (worker/loop.py) translates non-ok into outcome='patch_failed'.
    The file is never modified when ok=False.
    """
    target = patch.target_path

    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ApplyResult(ok=False, error=f"target file not found: {target}", occurrences_found=0)
    except OSError as exc:
        return ApplyResult(ok=False, error=f"cannot read target file: {exc}", occurrences_found=0)

    count = original.count(patch.old_text)

    if count == 0:
        return ApplyResult(ok=False, error="old_text not found", occurrences_found=0)

    if count > 1:
        return ApplyResult(
            ok=False,
            error=f"old_text not unique ({count} occurrences)",
            occurrences_found=count,
        )

    # Exactly 1 occurrence — safe to replace.
    new_content = original.replace(patch.old_text, patch.new_text, 1)
    target.write_text(new_content, encoding="utf-8")
    return ApplyResult(ok=True, error=None, occurrences_found=1)
