from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.state.git_ops import (
    ApplyResult,
    apply_edit_patch,
    commit_experiment,
    kill_recovery,
    revert_to_baseline,
    start_run_branch,
)
from usr.plugins.autoresearch.coder.base import EditPatch


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _current_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "--list", branch], cwd=repo, capture_output=True, text=True
    )
    return branch in result.stdout


def _untracked_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


class TestStartRunBranch:
    def test_creates_branch_with_correct_name(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        sha = start_run_branch("test-run-1", tmp_path)
        assert _branch_exists(tmp_path, "autoresearch/test-run-1")
        assert _current_branch(tmp_path) == "autoresearch/test-run-1"

    def test_returns_baseline_sha(self, tmp_path: Path) -> None:
        init_sha = _init_repo(tmp_path)
        returned_sha = start_run_branch("test-run-2", tmp_path)
        assert returned_sha == init_sha

    def test_main_unchanged_after_branch_creation(self, tmp_path: Path) -> None:
        init_sha = _init_repo(tmp_path)
        start_run_branch("test-run-3", tmp_path)
        # switch back to main and verify
        subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
        assert _current_sha(tmp_path) == init_sha


class TestCommitExperiment:
    def test_commit_advances_head(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        start_run_branch("test-commit-1", tmp_path)
        (tmp_path / "prompt.md").write_text("new content")
        sha = commit_experiment("test-commit-1", 1, "exp 1", tmp_path)
        assert _current_sha(tmp_path) == sha

    def test_main_head_unchanged_after_commit(self, tmp_path: Path) -> None:
        init_sha = _init_repo(tmp_path)
        start_run_branch("test-commit-2", tmp_path)
        (tmp_path / "prompt.md").write_text("new content")
        commit_experiment("test-commit-2", 1, "exp 1", tmp_path)
        subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
        assert _current_sha(tmp_path) == init_sha


class TestRevertToBaseline:
    def test_reverts_file_change(self, tmp_path: Path) -> None:
        baseline_sha = _init_repo(tmp_path)
        start_run_branch("test-revert-1", tmp_path)
        (tmp_path / "file.md").write_text("modified")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        revert_to_baseline(baseline_sha, tmp_path)
        assert _current_sha(tmp_path) == baseline_sha
        assert not (tmp_path / "file.md").exists() or (tmp_path / "file.md").read_text() != "modified"

    def test_uncommitted_changes_discarded(self, tmp_path: Path) -> None:
        baseline_sha = _init_repo(tmp_path)
        start_run_branch("test-revert-2", tmp_path)
        (tmp_path / "README.md").write_text("dirty")
        revert_to_baseline(baseline_sha, tmp_path)
        assert (tmp_path / "README.md").read_text() == "init"


class TestKillRecovery:
    def test_resets_to_baseline(self, tmp_path: Path) -> None:
        baseline_sha = _init_repo(tmp_path)
        start_run_branch("test-kill-1", tmp_path)
        (tmp_path / "dirty.md").write_text("mid-experiment")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        kill_recovery(baseline_sha, tmp_path)
        assert _current_sha(tmp_path) == baseline_sha

    def test_no_remote_calls(self, tmp_path: Path) -> None:
        baseline_sha = _init_repo(tmp_path)
        start_run_branch("test-kill-2", tmp_path)
        # confirm no 'origin' remote configured
        result = subprocess.run(
            ["git", "remote"], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.stdout.strip() == ""
        kill_recovery(baseline_sha, tmp_path)
        # if this reaches here without CalledProcessError on push, we're safe

    def test_kill_recovery_refuses_to_reset_main(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Set up a repo that is on main (not an autoresearch branch).
        baseline_sha = _init_repo(tmp_path)
        assert _current_branch(tmp_path) == "main"

        with caplog.at_level(logging.CRITICAL, logger="usr.plugins.autoresearch.state.git_ops"):
            kill_recovery(baseline_sha, tmp_path)

        # HEAD must not have moved — reset was refused.
        assert _current_sha(tmp_path) == baseline_sha
        assert any("CRITICAL" in r.levelname or r.levelno >= logging.CRITICAL for r in caplog.records)
        # Verify the log message describes the safety refusal.
        assert any("autoresearch" in r.message for r in caplog.records)

    def test_kill_recovery_preserves_untracked_files(self, tmp_path: Path) -> None:
        # git reset --hard does NOT delete untracked files; assert this explicitly.
        baseline_sha = _init_repo(tmp_path)
        start_run_branch("test-kill-untracked", tmp_path)

        # Simulate partial exp artifact — untracked, never staged.
        artifact = tmp_path / "exp-001" / "trace.log"
        artifact.parent.mkdir()
        artifact.write_text("partial artifact")

        kill_recovery(baseline_sha, tmp_path)

        # Verified: untracked artifact survives reset.
        assert artifact.exists(), "kill_recovery must NOT delete untracked files"
        assert artifact.read_text() == "partial artifact"


class TestApplyEditPatch:
    def test_apply_edit_patch_unique_match_succeeds(self, tmp_path: Path) -> None:
        target = tmp_path / "prompt.md"
        target.write_text("Hello world\nThis is a test\nGoodbye world\n")

        patch = EditPatch(
            target_path=target,
            old_text="This is a test",
            new_text="This is the replacement",
            rationale="test patch",
        )
        result = apply_edit_patch(patch, tmp_path)

        assert result.ok is True
        assert result.error is None
        assert result.occurrences_found == 1
        assert target.read_text() == "Hello world\nThis is the replacement\nGoodbye world\n"

    def test_apply_edit_patch_zero_matches_returns_error(self, tmp_path: Path) -> None:
        target = tmp_path / "prompt.md"
        target.write_text("Hello world\n")

        patch = EditPatch(
            target_path=target,
            old_text="text that does not exist",
            new_text="replacement",
            rationale="test patch",
        )
        result = apply_edit_patch(patch, tmp_path)

        assert result.ok is False
        assert result.occurrences_found == 0
        assert result.error is not None
        assert "not found" in result.error.lower()
        # File unchanged.
        assert target.read_text() == "Hello world\n"

    def test_apply_edit_patch_multiple_matches_returns_error_with_count(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "prompt.md"
        target.write_text("repeat\nrepeat\nrepeat\n")

        patch = EditPatch(
            target_path=target,
            old_text="repeat",
            new_text="unique",
            rationale="test patch",
        )
        result = apply_edit_patch(patch, tmp_path)

        assert result.ok is False
        assert result.occurrences_found == 3
        assert result.error is not None
        assert "3" in result.error
        # File unchanged.
        assert target.read_text() == "repeat\nrepeat\nrepeat\n"

    def test_apply_edit_patch_missing_file_returns_error(self, tmp_path: Path) -> None:
        patch = EditPatch(
            target_path=tmp_path / "nonexistent.md",
            old_text="anything",
            new_text="replacement",
            rationale="test patch",
        )
        result = apply_edit_patch(patch, tmp_path)

        assert result.ok is False
        assert result.occurrences_found == 0
        assert result.error is not None


class TestApplyResultContract:
    def test_apply_result_is_frozen(self) -> None:
        r = ApplyResult(ok=True, error=None, occurrences_found=1)
        with pytest.raises((AttributeError, TypeError)):
            r.ok = False  # type: ignore[misc]


class TestNoPushEver:
    def test_git_ops_never_push(self) -> None:
        import usr.plugins.autoresearch.state.git_ops as module
        import inspect
        source = inspect.getsource(module)
        assert "git push" not in source
        assert '"push"' not in source
        assert "'push'" not in source
