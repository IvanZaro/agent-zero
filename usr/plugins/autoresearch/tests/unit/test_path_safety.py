from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.state._path_safety import EditablePathError, validate_editable_path


class TestValidateEditablePath:
    def test_validate_editable_path_accepts_valid_prompt(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts"
        prompts_dir.mkdir(parents=True)
        target = prompts_dir / "system.md"
        target.write_text("# System prompt")

        result = validate_editable_path(target, profile_root)
        assert result == target.resolve()

    def test_validate_editable_path_accepts_nested_under_prompts(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts" / "sub"
        prompts_dir.mkdir(parents=True)
        target = prompts_dir / "deep.md"
        target.write_text("content")

        result = validate_editable_path(target, profile_root)
        assert result.is_absolute()

    def test_validate_editable_path_rejects_traversal(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        profile_root.mkdir(parents=True)
        # Attempt to escape via .. components.
        traversal = profile_root / "prompts" / ".." / ".." / ".." / "etc" / "passwd"

        with pytest.raises(EditablePathError, match=r"(?i)prompts"):
            validate_editable_path(traversal, profile_root)

    def test_validate_editable_path_rejects_symlink_escape(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts"
        prompts_dir.mkdir(parents=True)

        # Create a symlink inside prompts/ that points outside profile_root.
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.md").write_text("secret")

        link = prompts_dir / "evil_link.md"
        link.symlink_to(outside_dir / "secret.md")

        with pytest.raises(EditablePathError, match=r"(?i)symlink|outside|prompts"):
            validate_editable_path(link, profile_root)

    def test_validate_editable_path_rejects_outside_prompts_dir(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        profile_root.mkdir(parents=True)
        # File is under profile_root but NOT under prompts/.
        outside_prompts = profile_root / "config" / "settings.yaml"
        outside_prompts.parent.mkdir()
        outside_prompts.write_text("key: value")

        with pytest.raises(EditablePathError, match=r"(?i)prompts"):
            validate_editable_path(outside_prompts, profile_root)

    def test_validate_editable_path_rejects_absolute_path_outside_profile(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        profile_root.mkdir(parents=True)

        # Absolute path that looks unrelated to profile_root.
        arbitrary = tmp_path / "unrelated" / "prompts" / "attack.md"
        arbitrary.parent.mkdir(parents=True)
        arbitrary.write_text("evil")

        with pytest.raises(EditablePathError):
            validate_editable_path(arbitrary, profile_root)

    def test_validate_editable_path_rejects_profile_root_itself(
        self, tmp_path: Path
    ) -> None:
        profile_root = tmp_path / "agents" / "trader"
        profile_root.mkdir(parents=True)

        with pytest.raises(EditablePathError):
            validate_editable_path(profile_root, profile_root)

    def test_error_message_is_descriptive(self, tmp_path: Path) -> None:
        profile_root = tmp_path / "agents" / "trader"
        profile_root.mkdir(parents=True)
        bad_path = tmp_path / "etc" / "passwd"

        with pytest.raises(EditablePathError) as exc_info:
            validate_editable_path(bad_path, profile_root)

        assert str(exc_info.value)  # non-empty message
