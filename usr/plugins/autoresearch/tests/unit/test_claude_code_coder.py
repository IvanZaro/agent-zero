from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder._backend_resolver import ClaudeCodeNotInstalled
from usr.plugins.autoresearch.coder.claude_code_coder import (
    ClaudeCodeBackendError,
    ClaudeCodeCoder,
)
from usr.plugins.autoresearch.worker.cost_meter import CostMeter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_meter(cap: Decimal = Decimal("100")) -> CostMeter:
    rates: dict[str, dict[str, Decimal]] = {
        "claude-test": {
            "input_per_1k": Decimal("0.001"),
            "output_per_1k": Decimal("0.002"),
        }
    }
    return CostMeter(cap_usd=cap, rates=rates)


def _make_coder(model: str = "claude-test", **kwargs) -> ClaudeCodeCoder:
    return ClaudeCodeCoder(model=model, cost_meter=_make_meter(), **kwargs)


def _envelope(content: str, input_tokens: int = 50, output_tokens: int = 20) -> str:
    """Canonical claude --output-format json envelope (single-turn --print mode)."""
    return json.dumps({
        "type": "result",
        "result": content,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    })


def _valid_json_content() -> str:
    return json.dumps({"old_text": "old", "new_text": "new", "rationale": "test reason"})


def _make_run_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def _patch_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _resolve_cmd to avoid PATH/docker checks in tests."""
    monkeypatch.setattr(
        ClaudeCodeCoder,
        "_resolve_cmd",
        lambda self: ["claude"],
    )


# ---------------------------------------------------------------------------
# subprocess argument verification
# ---------------------------------------------------------------------------

class TestSubprocessArgs:
    @pytest.mark.asyncio
    async def test_propose_edit_calls_subprocess_with_correct_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder(model="claude-sonnet-4-5")

        captured_args: dict = {}

        def fake_run(cmd, **kwargs):
            captured_args["cmd"] = cmd
            captured_args["kwargs"] = kwargs
            return _make_run_result(_envelope(_valid_json_content()))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            await coder.propose_edit("agenda", "file text", [])

        cmd = captured_args["cmd"]
        assert "claude" in cmd
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--model" in cmd
        assert "claude-sonnet-4-5" in cmd
        # Prompt must NOT be passed on argv (multi-line safety)
        joined = " ".join(cmd)
        assert "agenda" not in joined

    @pytest.mark.asyncio
    async def test_propose_edit_uses_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder(timeout_seconds=42)

        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _make_run_result(_envelope(_valid_json_content()))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            await coder.propose_edit("agenda", "file text", [])

        assert captured["timeout"] == 42


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

class TestParsesValidOutput:
    @pytest.mark.asyncio
    async def test_parses_valid_claude_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder()

        good_content = json.dumps({
            "old_text": "Buy when",
            "new_text": "Sell when",
            "rationale": "reversed signal",
        })
        stdout = _envelope(good_content)

        with patch(
            "usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run",
            return_value=_make_run_result(stdout),
        ):
            patch_obj = await coder.propose_edit("program", "file", [])

        assert patch_obj.old_text == "Buy when"
        assert patch_obj.new_text == "Sell when"
        assert patch_obj.rationale == "reversed signal"
        assert str(patch_obj.target_path) == "__pending__"


# ---------------------------------------------------------------------------
# Retry on malformed JSON (E1 path)
# ---------------------------------------------------------------------------

class TestRetryOnMalformedResponse:
    @pytest.mark.asyncio
    async def test_retries_on_malformed_response_3_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return _make_run_result(_envelope("not valid json at all"))
            return _make_run_result(_envelope(_valid_json_content()))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            result = await coder.propose_edit("program", "file", [])

        assert call_count == 3
        assert result.old_text == "old"

    @pytest.mark.asyncio
    async def test_raises_after_3_malformed_responses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder()

        def fake_run(cmd, **kwargs):
            return _make_run_result(_envelope("{ bad json"))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            with pytest.raises((json.JSONDecodeError, ValueError, RuntimeError)):
                await coder.propose_edit("program", "file", [])


# ---------------------------------------------------------------------------
# No retry on fatal errors
# ---------------------------------------------------------------------------

class TestNoRetryOnFatalErrors:
    @pytest.mark.asyncio
    async def test_does_not_retry_on_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_run_result("", returncode=1, stderr="Error: auth failed — unauthorized")

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            with pytest.raises(ClaudeCodeBackendError) as exc_info:
                await coder.propose_edit("program", "file", [])

        assert call_count == 1
        assert "auth" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_does_not_retry_on_model_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder(model="bad-model")

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_run_result("", returncode=1, stderr="Error: model not found: bad-model")

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            with pytest.raises(ClaudeCodeBackendError) as exc_info:
                await coder.propose_edit("program", "file", [])

        assert call_count == 1
        assert "model" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Timeout propagation
# ---------------------------------------------------------------------------

class TestTimeoutPropagates:
    @pytest.mark.asyncio
    async def test_timeout_propagates_as_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder(timeout_seconds=1)

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            with pytest.raises(subprocess.TimeoutExpired):
                await coder.propose_edit("program", "file", [])


# ---------------------------------------------------------------------------
# Cost meter
# ---------------------------------------------------------------------------

class TestCostMeterIntegration:
    @pytest.mark.asyncio
    async def test_cost_meter_ticked_from_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        rates = {
            "claude-test": {
                "input_per_1k": Decimal("1.0"),
                "output_per_1k": Decimal("2.0"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("100"), rates=rates)
        coder = ClaudeCodeCoder(model="claude-test", cost_meter=meter)
        _patch_cmd(monkeypatch)
        coder._cmd = ["claude"]  # bypass lazy resolve

        stdout = _envelope(_valid_json_content(), input_tokens=1000, output_tokens=500)

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run",
                   return_value=_make_run_result(stdout)):
            await coder.propose_edit("program", "file", [])

        # 1000 input @ $1/1k = $1.0 ; 500 output @ $2/1k = $1.0 → total $2.0
        assert coder.report_cost() == Decimal("2.0")

    @pytest.mark.asyncio
    async def test_cost_meter_unaffected_when_usage_missing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When usage is absent, meter ticks with (0, 0) — fail-open."""
        _patch_cmd(monkeypatch)
        meter = _make_meter()
        coder = ClaudeCodeCoder(model="claude-test", cost_meter=meter)
        coder._cmd = ["claude"]

        # Envelope without usage key
        stdout = json.dumps({"type": "result", "result": _valid_json_content()})

        import logging

        with caplog.at_level(logging.WARNING):
            with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run",
                       return_value=_make_run_result(stdout)):
                await coder.propose_edit("program", "file", [])

        # Cost should be zero (0 tokens ticked)
        assert coder.report_cost() == Decimal("0")
        # Warning must appear
        warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("usage" in t.lower() or "0 token" in t.lower() for t in warning_texts)

    def test_report_cost_returns_meter_spent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cmd(monkeypatch)
        rates = {
            "claude-test": {
                "input_per_1k": Decimal("1.0"),
                "output_per_1k": Decimal("2.0"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("100"), rates=rates)
        coder = ClaudeCodeCoder(model="claude-test", cost_meter=meter)

        meter.tick("claude-test", 100, 50)
        assert coder.report_cost() == meter.spent


# ---------------------------------------------------------------------------
# Secret env filtering
# ---------------------------------------------------------------------------

class TestSecretEnvFiltering:
    @pytest.mark.asyncio
    async def test_secret_env_vars_filtered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_cmd(monkeypatch)
        monkeypatch.setenv("BIRDEYE_API_KEY", "secret-key-123")
        monkeypatch.setenv("HL_SECRET_TOKEN", "hl-secret-456")
        monkeypatch.setenv("BINANCE_API_KEY", "binance-secret")
        monkeypatch.setenv("SAFE_VAR", "safe-value")

        coder = _make_coder()
        coder._cmd = ["claude"]

        captured_env: dict = {}

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _make_run_result(_envelope(_valid_json_content()))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            await coder.propose_edit("program", "file", [])

        assert "BIRDEYE_API_KEY" not in captured_env
        assert "HL_SECRET_TOKEN" not in captured_env
        assert "BINANCE_API_KEY" not in captured_env
        assert "SAFE_VAR" in captured_env


# ---------------------------------------------------------------------------
# Lazy binary resolution
# ---------------------------------------------------------------------------

class TestLazyBinaryResolution:
    def test_module_import_succeeds_when_claude_not_on_path(self) -> None:
        """Importing claude_code_coder must NOT raise even when claude is absent."""
        real_mod = sys.modules.pop(
            "usr.plugins.autoresearch.coder.claude_code_coder", None
        )
        try:
            with patch(
                "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
                return_value=None,
            ):
                mod = importlib.import_module(
                    "usr.plugins.autoresearch.coder.claude_code_coder"
                )
            assert mod is not None
        finally:
            if real_mod is not None:
                sys.modules["usr.plugins.autoresearch.coder.claude_code_coder"] = real_mod

    def test_lazy_resolves_claude_binary_raises_on_first_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Module import OK; first propose_edit() raises ClaudeCodeNotInstalled."""
        monkeypatch.delenv("A0_ENV", raising=False)
        coder = _make_coder()
        # Ensure no cached cmd
        coder._cmd = None

        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver.shutil.which",
            return_value=None,
        ):
            with pytest.raises(ClaudeCodeNotInstalled):
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    coder.propose_edit("program", "file", [])
                )


# ---------------------------------------------------------------------------
# _extract_content_and_usage: fallback paths
# ---------------------------------------------------------------------------

class TestExtractContentAndUsage:
    """Unit tests for the envelope parsing helper."""

    def test_non_json_stdout_returned_as_raw(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _extract_content_and_usage
        text, usage = _extract_content_and_usage("not json at all")
        assert text == "not json at all"
        assert usage == {}

    def test_result_key_path(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _extract_content_and_usage
        stdout = json.dumps({"result": "hello world", "usage": {"input_tokens": 5, "output_tokens": 3}})
        text, usage = _extract_content_and_usage(stdout)
        assert text == "hello world"
        assert usage["input_tokens"] == 5

    def test_messages_array_assistant_string_content(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _extract_content_and_usage
        stdout = json.dumps({
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello from assistant"},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 8},
        })
        text, usage = _extract_content_and_usage(stdout)
        assert text == "hello from assistant"
        assert usage["output_tokens"] == 8

    def test_messages_array_assistant_content_blocks(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _extract_content_and_usage
        stdout = json.dumps({
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "block one "},
                        {"type": "text", "text": "block two"},
                    ],
                }
            ],
        })
        text, usage = _extract_content_and_usage(stdout)
        assert text == "block one block two"
        assert usage == {}

    def test_envelope_with_no_result_and_no_messages_returns_raw(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _extract_content_and_usage
        raw = json.dumps({"something": "else"})
        text, usage = _extract_content_and_usage(raw)
        assert text == raw


# ---------------------------------------------------------------------------
# _tick_meter: malformed usage path
# ---------------------------------------------------------------------------

class TestTickMeterMalformedUsage:
    def test_malformed_usage_data_logs_warning_and_ticks_zero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _tick_meter
        import logging

        meter = _make_meter()
        before = meter.spent

        with caplog.at_level(logging.WARNING):
            _tick_meter(meter, "claude-test", {"input_tokens": "not-a-number", "output_tokens": None})

        # meter still advanced (with zeros — unknown model fallback warning may fire)
        # Key thing: no exception raised
        assert meter.spent >= before

        warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("malformed" in t.lower() or "0 token" in t.lower() or "usage" in t.lower()
                   for t in warning_texts)


# ---------------------------------------------------------------------------
# _run_claude: generic non-zero exit (not auth, not model-not-found)
# ---------------------------------------------------------------------------

class TestGenericNonZeroExit:
    @pytest.mark.asyncio
    async def test_generic_nonzero_exit_raises_backend_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_cmd(monkeypatch)
        coder = _make_coder()

        def fake_run(_cmd, **kwargs):
            return _make_run_result("", returncode=2, stderr="Some unknown internal error")

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            with pytest.raises(ClaudeCodeBackendError) as exc_info:
                await coder.propose_edit("program", "file", [])

        assert "2" in str(exc_info.value) or "internal" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# _format_history with eval_result present
# ---------------------------------------------------------------------------

class TestFormatHistoryWithEvalResult:
    @pytest.mark.asyncio
    async def test_history_with_eval_result_included_in_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover the _format_history branch where eval_result is not None."""
        _patch_cmd(monkeypatch)
        coder = _make_coder()
        captured: dict = {}

        def fake_run(_cmd, stdin=None, **kwargs):
            # Read the prompt from the temp file via stdin
            if stdin is not None:
                captured["prompt"] = stdin.read()
            return _make_run_result(_envelope(_valid_json_content()))

        # Build a fake ExperimentRecord with an eval_result
        from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
        from usr.plugins.autoresearch.state.runs import ExperimentRecord
        from datetime import datetime, timezone
        from decimal import Decimal

        eval_r = EvalResult(
            passed=1,
            total=2,
            total_tokens=100,
            per_task=[TaskOutcome(task_id="t-1", passed=True, tokens=50, output="ok", error=None)],
        )
        record = ExperimentRecord(
            n=1,
            started_at=datetime.now(tz=timezone.utc),
            finished_at=datetime.now(tz=timezone.utc),
            outcome="kept",
            eval_result=eval_r,
            judge_score=None,
            spend_usd=Decimal("0.01"),
            sha="abc123",
            rationale="test rationale",
        )

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            await coder.propose_edit("program", "file", [record])

        # The prompt must mention the eval result
        assert "eval=1/2" in captured.get("prompt", "")


# ---------------------------------------------------------------------------
# _parse_response: markdown fence stripping
# ---------------------------------------------------------------------------

class TestParseResponseMarkdownFence:
    def test_strips_markdown_fence_before_parsing(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _parse_response

        fenced = "```json\n" + json.dumps({"old_text": "a", "new_text": "b", "rationale": "r"}) + "\n```"
        result = _parse_response(fenced)
        assert result.old_text == "a"
        assert result.new_text == "b"


# ---------------------------------------------------------------------------
# _resolve_cmd: cached path (branch where _cmd already set)
# ---------------------------------------------------------------------------

class TestResolveCmdCached:
    @pytest.mark.asyncio
    async def test_cached_cmd_not_recalculated(self) -> None:
        """Cover the `return self._cmd` branch (line 74) when already resolved."""
        coder = _make_coder()
        coder._cmd = ["claude"]  # pre-populate cache

        resolve_calls = 0

        def counting_claude_cmd():
            nonlocal resolve_calls
            resolve_calls += 1
            return ["claude"]

        with patch(
            "usr.plugins.autoresearch.coder._backend_resolver._claude_cmd",
            side_effect=counting_claude_cmd,
        ):
            with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run",
                       return_value=_make_run_result(_envelope(_valid_json_content()))):
                await coder.propose_edit("program", "file", [])
                await coder.propose_edit("program", "file", [])

        # _claude_cmd never called because _cmd was already cached
        assert resolve_calls == 0


# ---------------------------------------------------------------------------
# _reraise: exc=None fallback (line 312)
# ---------------------------------------------------------------------------

class TestReraiseNoneExc:
    def test_reraise_with_none_raises_runtime_error(self) -> None:
        from usr.plugins.autoresearch.coder.claude_code_coder import _reraise

        with pytest.raises(RuntimeError, match="retries exhausted"):
            _reraise(None)


# ---------------------------------------------------------------------------
# _build_env: custom env overlay
# ---------------------------------------------------------------------------

class TestBuildEnvOverlay:
    @pytest.mark.asyncio
    async def test_custom_env_overlay_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env dict passed to __init__ must appear in the subprocess env."""
        _patch_cmd(monkeypatch)
        meter = _make_meter()
        coder = ClaudeCodeCoder(
            model="claude-test",
            cost_meter=meter,
            env={"MY_CUSTOM_VAR": "custom_value"},
        )
        coder._cmd = ["claude"]

        captured_env: dict = {}

        def fake_run(_cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _make_run_result(_envelope(_valid_json_content()))

        with patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run", side_effect=fake_run):
            await coder.propose_edit("program", "file", [])

        assert captured_env.get("MY_CUSTOM_VAR") == "custom_value"
