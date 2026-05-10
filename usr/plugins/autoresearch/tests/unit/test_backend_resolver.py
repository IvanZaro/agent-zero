from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder._backend_resolver import (
    ClaudeCodeNotInstalled,
    _claude_cmd,
)


class TestLocalResolution:
    def test_local_returns_claude_when_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A0_ENV", raising=False)
        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
            return_value="/usr/local/bin/claude",
        ):
            result = _claude_cmd()
        assert result == ["claude"]

    def test_local_raises_when_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A0_ENV", raising=False)
        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
            return_value=None,
        ):
            with pytest.raises(ClaudeCodeNotInstalled) as exc_info:
                _claude_cmd()
        assert "claude" in str(exc_info.value).lower()
        assert "litellm" in str(exc_info.value).lower()


class TestVpsResolution:
    def test_vps_returns_docker_exec_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A0_ENV", "vps")
        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
            return_value=None,
        ) as mock_which:
            result = _claude_cmd()
        assert result == ["docker", "exec", "claude-mcp", "claude"]
        # VPS path must never call shutil.which
        mock_which.assert_not_called()

    def test_vps_does_not_check_local_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A0_ENV=vps + claude not on PATH must not raise ClaudeCodeNotInstalled."""
        monkeypatch.setenv("A0_ENV", "vps")
        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
            return_value=None,
        ):
            # Must not raise
            result = _claude_cmd()
        assert result[0] == "docker"
