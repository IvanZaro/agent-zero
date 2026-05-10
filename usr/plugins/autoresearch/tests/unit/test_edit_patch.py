from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder, _parse_response


class TestEditPatchDataclass:
    def test_frozen(self) -> None:
        patch = EditPatch(
            target_path=Path("agents/trader/prompts/system.md"),
            old_text="old",
            new_text="new",
            rationale="reason",
        )
        with pytest.raises((TypeError, AttributeError)):
            patch.old_text = "mutated"  # type: ignore[misc]

    def test_fields_accessible(self) -> None:
        p = EditPatch(
            target_path=Path("agents/x/prompts/y.md"),
            old_text="foo",
            new_text="bar",
            rationale="test",
        )
        assert p.old_text == "foo"
        assert p.new_text == "bar"
        assert p.rationale == "test"


class TestParseResponse:
    def test_valid_json_parses(self) -> None:
        raw = json.dumps({"old_text": "a", "new_text": "b", "rationale": "r"})
        result = _parse_response(raw)
        assert result.old_text == "a"
        assert result.new_text == "b"
        assert result.rationale == "r"

    def test_strips_markdown_fences(self) -> None:
        raw = '```json\n{"old_text": "x", "new_text": "y", "rationale": "z"}\n```'
        result = _parse_response(raw)
        assert result.old_text == "x"

    def test_missing_field_raises_validation_error(self) -> None:
        raw = json.dumps({"old_text": "a", "new_text": "b"})  # missing rationale
        with pytest.raises(ValidationError):
            _parse_response(raw)

    def test_invalid_json_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_response("not json at all")

    def test_extra_fields_ignored(self) -> None:
        raw = json.dumps({"old_text": "a", "new_text": "b", "rationale": "r", "extra": "ignored"})
        result = _parse_response(raw)
        assert result.old_text == "a"


def _make_mock_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices[0].message.content = content
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 20
    return response


class TestLiteLLMCoderRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self) -> None:
        valid = json.dumps({"old_text": "old", "new_text": "new", "rationale": "test"})
        mock_resp = _make_mock_response(valid)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            coder = LiteLLMCoder(model="test-model")
            result = await coder.propose_edit("agenda", "file text", [])

        assert result.old_text == "old"
        assert result.new_text == "new"

    @pytest.mark.asyncio
    async def test_retries_on_malformed_then_succeeds(self) -> None:
        valid = json.dumps({"old_text": "old", "new_text": "new", "rationale": "ok"})
        responses = [
            _make_mock_response("not json"),
            _make_mock_response("still not json"),
            _make_mock_response(valid),
        ]
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        with patch("litellm.acompletion", new=mock_completion):
            coder = LiteLLMCoder(model="test-model")
            result = await coder.propose_edit("agenda", "file text", [])

        assert result.old_text == "old"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_3_malformed_responses(self) -> None:
        responses = [_make_mock_response("bad json")] * 4
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        with patch("litellm.acompletion", new=mock_completion):
            coder = LiteLLMCoder(model="test-model")
            with pytest.raises(Exception):
                await coder.propose_edit("agenda", "file text", [])

        assert call_count == 3  # exactly 3 attempts

    @pytest.mark.asyncio
    async def test_4_malformed_responses_does_not_succeed(self) -> None:
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_mock_response("{malformed}")

        with patch("litellm.acompletion", new=mock_completion):
            coder = LiteLLMCoder(model="test-model")
            with pytest.raises(Exception):
                await coder.propose_edit("agenda", "file text", [])

    def test_report_cost_initially_zero(self) -> None:
        coder = LiteLLMCoder(model="test-model")
        assert coder.report_cost() == Decimal("0")
