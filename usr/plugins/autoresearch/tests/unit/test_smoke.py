from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.harness.smoke import SmokeResult, smoke_check

FIXTURE_PROFILE = Path(__file__).parent.parent / "fixtures" / "profile"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_smoke_passes_on_valid_profile() -> None:
    result = smoke_check(FIXTURE_PROFILE)
    assert result.ok is True
    assert result.error is None
    assert result.took_ms >= 0


def test_smoke_result_is_frozen_dataclass() -> None:
    result = smoke_check(FIXTURE_PROFILE)
    with pytest.raises((AttributeError, TypeError)):
        result.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Missing _context.yaml
# ---------------------------------------------------------------------------

def test_smoke_fails_on_missing_context_yaml(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text("hello", encoding="utf-8")
    # No _context.yaml
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "_context.yaml" in (result.error or "")


# ---------------------------------------------------------------------------
# Broken YAML in _context.yaml
# ---------------------------------------------------------------------------

def test_smoke_fails_on_broken_yaml(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text(
        "key: [unclosed bracket\n", encoding="utf-8"
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text("hello", encoding="utf-8")
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "YAML" in (result.error or "") or "parse" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Missing prompts/ directory
# ---------------------------------------------------------------------------

def test_smoke_fails_when_prompts_dir_missing(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text("title: test\n", encoding="utf-8")
    # No prompts/ directory
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "prompts" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Missing prompt file
# ---------------------------------------------------------------------------

def test_smoke_fails_on_missing_prompt_file(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text("title: test\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    # Empty directory — no files
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "prompt" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Empty prompt file
# ---------------------------------------------------------------------------

def test_smoke_fails_on_empty_prompt_file(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text("title: test\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text("", encoding="utf-8")
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "non-empty" in (result.error or "").lower() or "empty" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Bad YAML frontmatter in prompt file
# ---------------------------------------------------------------------------

def test_smoke_fails_on_invalid_prompt_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text("title: test\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text(
        "---\nbad: [unclosed\n---\nBody text.\n", encoding="utf-8"
    )
    result = smoke_check(tmp_path)
    assert result.ok is False
    assert "frontmatter" in (result.error or "").lower() or "YAML" in (result.error or "")


def test_smoke_passes_on_valid_frontmatter_in_prompt(tmp_path: Path) -> None:
    (tmp_path / "_context.yaml").write_text("title: test\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text(
        "---\ntitle: My Prompt\nversion: 1\n---\nActual prompt body.\n",
        encoding="utf-8",
    )
    result = smoke_check(tmp_path)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_smoke_timeout_returns_failure(tmp_path: Path) -> None:
    """Simulate an extremely slow _check_profile by patching time.monotonic."""

    original_check = None

    def _slow_check(profile_path: Path) -> SmokeResult:
        time.sleep(0.05)  # Just ensure it runs briefly
        return SmokeResult(ok=True, error=None, took_ms=50)

    with patch(
        "usr.plugins.autoresearch.harness.smoke._check_profile",
        side_effect=_slow_check,
    ):
        with patch("usr.plugins.autoresearch.harness.smoke.SMOKE_TIMEOUT_S", 0.001):
            result = smoke_check(tmp_path)

    # With a 1ms timeout and 50ms sleep, it must time out.
    assert result.ok is False
    assert "timed out" in (result.error or "").lower()
