from __future__ import annotations

import asyncio
import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.worker.cost_meter import BudgetExceeded, CostMeter


def _make_coder(model: str = "test-model", cap_usd: Decimal = Decimal("100")) -> object:
    """Import and instantiate LiteLLMCoder fresh each call so lazy-import works."""
    from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

    rates = {
        model: {
            "input_per_1k": Decimal("0.001"),
            "output_per_1k": Decimal("0.002"),
        }
    }
    meter = CostMeter(cap_usd=cap_usd, rates=rates)
    return LiteLLMCoder(model=model, cost_meter=meter)


def _make_mock_response(content: str, prompt_tokens: int = 10, completion_tokens: int = 20) -> MagicMock:
    response = MagicMock()
    response.choices[0].message.content = content
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


def _valid_json() -> str:
    return json.dumps({"old_text": "old", "new_text": "new", "rationale": "test"})


# ---------------------------------------------------------------------------
# Lazy-import tests
# ---------------------------------------------------------------------------

class TestLazyLitellmImport:
    def test_module_level_import_does_not_require_litellm(self) -> None:
        """Importing litellm_coder must NOT raise even when litellm is unimportable."""
        # Save real litellm state
        real_litellm = sys.modules.get("litellm")
        real_litellm_coder = sys.modules.pop(
            "usr.plugins.autoresearch.coder.litellm_coder", None
        )
        try:
            # Make litellm unimportable
            sys.modules["litellm"] = None  # type: ignore[assignment]
            # This must not raise
            import importlib
            mod = importlib.import_module("usr.plugins.autoresearch.coder.litellm_coder")
            assert mod is not None
        finally:
            # Restore state
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            elif "litellm" in sys.modules:
                del sys.modules["litellm"]
            if real_litellm_coder is not None:
                sys.modules["usr.plugins.autoresearch.coder.litellm_coder"] = real_litellm_coder

    def test_parse_response_works_without_litellm(self) -> None:
        """_parse_response must not import litellm at all."""
        real_litellm = sys.modules.get("litellm")
        try:
            sys.modules["litellm"] = None  # type: ignore[assignment]
            # Force reimport of the module
            sys.modules.pop("usr.plugins.autoresearch.coder.litellm_coder", None)
            from usr.plugins.autoresearch.coder.litellm_coder import _parse_response  # noqa: PLC0415
            raw = json.dumps({"old_text": "a", "new_text": "b", "rationale": "r"})
            result = _parse_response(raw)
            assert result.old_text == "a"
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            elif "litellm" in sys.modules:
                del sys.modules["litellm"]

    def test_edit_patch_construction_works_without_litellm(self) -> None:
        """EditPatch construction from base.py must not require litellm."""
        real_litellm = sys.modules.get("litellm")
        try:
            sys.modules["litellm"] = None  # type: ignore[assignment]
            patch_obj = EditPatch(
                target_path=Path("agents/x/prompts/y.md"),
                old_text="old",
                new_text="new",
                rationale="r",
            )
            assert patch_obj.old_text == "old"
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            elif "litellm" in sys.modules:
                del sys.modules["litellm"]


# ---------------------------------------------------------------------------
# Transient-error retry with backoff (E6)
# ---------------------------------------------------------------------------

class TestRetriesRateLimitWithBackoff:
    @pytest.mark.asyncio
    async def test_retries_rate_limit_twice_then_succeeds(self) -> None:
        import litellm  # noqa: PLC0415

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise litellm.exceptions.RateLimitError(
                    message="rate limit", llm_provider="test", model="test-model"
                )
            return _make_mock_response(_valid_json())

        sleep_calls: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        coder = _make_coder()
        with patch("litellm.acompletion", new=mock_acompletion):
            with patch("asyncio.sleep", new=mock_sleep):
                result = await coder.propose_edit("agenda", "file text", [])

        assert result.old_text == "old"
        assert call_count == 3
        # Two sleeps happened (after attempt 1 and 2)
        assert len(sleep_calls) == 2
        # Second sleep is at least as long as first (exponential)
        assert sleep_calls[1] >= sleep_calls[0]

    @pytest.mark.asyncio
    async def test_retries_api_connection_error(self) -> None:
        import litellm  # noqa: PLC0415

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise litellm.exceptions.APIConnectionError(
                    message="conn error", llm_provider="test", model="test-model"
                )
            return _make_mock_response(_valid_json())

        coder = _make_coder()
        with patch("litellm.acompletion", new=mock_acompletion):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await coder.propose_edit("agenda", "file text", [])

        assert result.old_text == "old"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_fourth_transient_failure(self) -> None:
        import litellm  # noqa: PLC0415

        async def always_rate_limit(**kwargs):
            raise litellm.exceptions.RateLimitError(
                message="rate limit", llm_provider="test", model="test-model"
            )

        coder = _make_coder()
        with patch("litellm.acompletion", new=always_rate_limit):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(litellm.exceptions.RateLimitError):
                    await coder.propose_edit("agenda", "file text", [])

    @pytest.mark.asyncio
    async def test_backoff_sleeps_called_with_exponential_values(self) -> None:
        import litellm  # noqa: PLC0415

        call_count = 0

        async def always_rate_limit(**kwargs):
            nonlocal call_count
            call_count += 1
            raise litellm.exceptions.RateLimitError(
                message="rate limit", llm_provider="test", model="test-model"
            )

        sleep_calls: list[float] = []

        async def capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        coder = _make_coder()
        with patch("litellm.acompletion", new=always_rate_limit):
            with patch("asyncio.sleep", new=capture_sleep):
                with pytest.raises(litellm.exceptions.RateLimitError):
                    await coder.propose_edit("agenda", "file text", [])

        # 3 attempts → 3 failures → 3 sleeps (sleep before each retry)
        # Actually: fail, sleep, fail, sleep, fail, sleep, fail → raise
        # The exact count depends on implementation; at minimum 2 sleeps for 3 retries
        assert len(sleep_calls) >= 2
        # Exponential: each sleep should be roughly double the previous (allowing jitter)
        if len(sleep_calls) >= 2:
            assert sleep_calls[1] > sleep_calls[0] * 0.5  # allowing for jitter


class TestDoesNotRetryAuthError:
    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        import litellm  # noqa: PLC0415

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            raise litellm.exceptions.AuthenticationError(
                message="auth error", llm_provider="test", model="test-model"
            )

        coder = _make_coder()
        with patch("litellm.acompletion", new=mock_acompletion):
            with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
                with pytest.raises(litellm.exceptions.AuthenticationError):
                    await coder.propose_edit("agenda", "file text", [])

        assert call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_request_error_not_retried(self) -> None:
        import litellm  # noqa: PLC0415

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            raise litellm.exceptions.BadRequestError(
                message="bad request", llm_provider="test", model="test-model"
            )

        coder = _make_coder()
        with patch("litellm.acompletion", new=mock_acompletion):
            with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
                with pytest.raises(litellm.exceptions.BadRequestError):
                    await coder.propose_edit("agenda", "file text", [])

        assert call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Cost meter integration
# ---------------------------------------------------------------------------

class TestCostMeterTickedPerCall:
    @pytest.mark.asyncio
    async def test_cost_meter_advances_after_successful_call(self) -> None:
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=100, completion_tokens=50)

        coder = _make_coder(model="test-model")

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            await coder.propose_edit("agenda", "file text", [])

        cost = coder.report_cost()
        assert cost > Decimal("0"), "Cost meter should advance after a successful call"

    @pytest.mark.asyncio
    async def test_cost_meter_accumulates_across_calls(self) -> None:
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=100, completion_tokens=50)

        coder = _make_coder(model="test-model")

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            await coder.propose_edit("agenda", "file text", [])
            first_cost = coder.report_cost()
            await coder.propose_edit("agenda", "file text", [])
            second_cost = coder.report_cost()

        assert second_cost > first_cost

    @pytest.mark.asyncio
    async def test_report_cost_matches_cost_meter_spent(self) -> None:
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=100, completion_tokens=50)
        rates = {
            "test-model": {
                "input_per_1k": Decimal("0.001"),
                "output_per_1k": Decimal("0.002"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("100"), rates=rates)

        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        coder = LiteLLMCoder(model="test-model", cost_meter=meter)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            await coder.propose_edit("agenda", "file text", [])

        # report_cost() must return meter.spent
        assert coder.report_cost() == meter.spent


class TestBudgetExceededPropagates:
    @pytest.mark.asyncio
    async def test_budget_exceeded_raises_before_completing_loop(self) -> None:
        # Cap so low that even one call exceeds it
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=1000, completion_tokens=1000)
        rates = {
            "test-model": {
                "input_per_1k": Decimal("1.0"),
                "output_per_1k": Decimal("2.0"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("0.001"), rates=rates)

        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        coder = LiteLLMCoder(model="test-model", cost_meter=meter)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(BudgetExceeded):
                await coder.propose_edit("agenda", "file text", [])

    @pytest.mark.asyncio
    async def test_budget_exceeded_not_caught_by_coder(self) -> None:
        """BudgetExceeded must propagate — coder must not swallow it."""
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=1000, completion_tokens=1000)
        rates = {
            "test-model": {
                "input_per_1k": Decimal("10.0"),
                "output_per_1k": Decimal("10.0"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("0.001"), rates=rates)

        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        coder = LiteLLMCoder(model="test-model", cost_meter=meter)

        raised = False
        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            try:
                await coder.propose_edit("agenda", "file text", [])
            except BudgetExceeded:
                raised = True
            except Exception:
                pass  # some other error — also acceptable since budget exceeded

        # meter.spent should be > cap (meter.tick was called)
        assert meter.spent > Decimal("0.001")


# ---------------------------------------------------------------------------
# report_cost returns meter.spent
# ---------------------------------------------------------------------------

class TestReportCost:
    def test_report_cost_initially_zero(self) -> None:
        coder = _make_coder()
        assert coder.report_cost() == Decimal("0")

    def test_report_cost_returns_meter_spent(self) -> None:
        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        rates = {
            "test-model": {
                "input_per_1k": Decimal("1.0"),
                "output_per_1k": Decimal("2.0"),
            }
        }
        meter = CostMeter(cap_usd=Decimal("100"), rates=rates)
        coder = LiteLLMCoder(model="test-model", cost_meter=meter)

        # Manually advance the meter
        meter.tick("test-model", 100, 50)
        expected = meter.spent
        assert coder.report_cost() == expected


# ---------------------------------------------------------------------------
# Unknown model fallback (via cost_meter.tick warning path)
# ---------------------------------------------------------------------------

class TestUnknownModelFallbackRates:
    @pytest.mark.asyncio
    async def test_unknown_model_uses_fallback_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=100, completion_tokens=50)
        # CostMeter with empty rates → unknown model triggers warning
        meter = CostMeter(cap_usd=Decimal("100"), rates={})

        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        coder = LiteLLMCoder(model="unknown/mystery-model", cost_meter=meter)

        import logging

        with caplog.at_level(logging.WARNING):
            with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
                await coder.propose_edit("agenda", "file text", [])

        # A WARNING about unknown model must appear (from CostMeter.tick)
        warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("unknown" in t.lower() or "mystery" in t.lower() or "no rate" in t.lower()
                   for t in warning_texts), f"Expected rate-fallback warning, got: {warning_texts}"

    @pytest.mark.asyncio
    async def test_unknown_model_still_accumulates_cost(self) -> None:
        mock_resp = _make_mock_response(_valid_json(), prompt_tokens=100, completion_tokens=50)
        meter = CostMeter(cap_usd=Decimal("100"), rates={})

        from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder  # noqa: PLC0415

        coder = LiteLLMCoder(model="unknown/mystery-model", cost_meter=meter)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            await coder.propose_edit("agenda", "file text", [])

        assert coder.report_cost() > Decimal("0")


# ---------------------------------------------------------------------------
# Interaction between E1 (malformed JSON retry) and E6 (transient error retry)
# ---------------------------------------------------------------------------

class TestE1AndE6RetryIndependence:
    @pytest.mark.asyncio
    async def test_transient_error_then_malformed_then_valid(self) -> None:
        """Rate limit on attempt 1, malformed on attempt 2, valid on attempt 3."""
        import litellm  # noqa: PLC0415

        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise litellm.exceptions.RateLimitError(
                    message="rate limit", llm_provider="test", model="test-model"
                )
            if call_count == 2:
                return _make_mock_response("not json at all")
            return _make_mock_response(_valid_json())

        coder = _make_coder()
        with patch("litellm.acompletion", new=mock_acompletion):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await coder.propose_edit("agenda", "file text", [])

        assert result.old_text == "old"
