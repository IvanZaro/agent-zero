from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder._cost_rates import DEFAULT_RATES, load_rates


class TestDefaultRates:
    def test_required_models_present(self) -> None:
        required = [
            "openrouter/openai/gpt-4o-mini",
            "openrouter/anthropic/claude-haiku-4-5",
            "openrouter/openai/gpt-4o",
            "openrouter/anthropic/claude-sonnet-4-6",
        ]
        for model in required:
            assert model in DEFAULT_RATES, f"Missing model: {model}"

    def test_each_entry_has_input_and_output_keys(self) -> None:
        for model, rates in DEFAULT_RATES.items():
            assert "input_per_1k" in rates, f"{model} missing input_per_1k"
            assert "output_per_1k" in rates, f"{model} missing output_per_1k"

    def test_rates_are_decimals(self) -> None:
        for model, rates in DEFAULT_RATES.items():
            assert isinstance(rates["input_per_1k"], Decimal), f"{model} input not Decimal"
            assert isinstance(rates["output_per_1k"], Decimal), f"{model} output not Decimal"

    def test_rates_are_positive(self) -> None:
        for model, rates in DEFAULT_RATES.items():
            assert rates["input_per_1k"] > Decimal("0"), f"{model} input not positive"
            assert rates["output_per_1k"] > Decimal("0"), f"{model} output not positive"

    def test_gpt4o_mini_cheaper_than_gpt4o(self) -> None:
        mini = DEFAULT_RATES["openrouter/openai/gpt-4o-mini"]
        full = DEFAULT_RATES["openrouter/openai/gpt-4o"]
        assert mini["input_per_1k"] < full["input_per_1k"]

    def test_haiku_cheaper_than_sonnet(self) -> None:
        haiku = DEFAULT_RATES["openrouter/anthropic/claude-haiku-4-5"]
        sonnet = DEFAULT_RATES["openrouter/anthropic/claude-sonnet-4-6"]
        assert haiku["input_per_1k"] < sonnet["input_per_1k"]


class TestLoadRates:
    def test_load_rates_default_returns_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTORESEARCH_COST_RATES", raising=False)
        rates = load_rates()
        assert rates == DEFAULT_RATES

    def test_load_rates_env_override_merges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        override = {
            "openrouter/openai/gpt-4o-mini": {
                "input_per_1k": "0.001",
                "output_per_1k": "0.002",
            }
        }
        monkeypatch.setenv("AUTORESEARCH_COST_RATES", json.dumps(override))
        rates = load_rates()
        # Overridden model uses override values
        assert rates["openrouter/openai/gpt-4o-mini"]["input_per_1k"] == Decimal("0.001")
        assert rates["openrouter/openai/gpt-4o-mini"]["output_per_1k"] == Decimal("0.002")
        # Other models still present from defaults
        assert "openrouter/openai/gpt-4o" in rates

    def test_load_rates_env_override_adds_new_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        new_model = {
            "openrouter/custom/model-x": {
                "input_per_1k": "0.005",
                "output_per_1k": "0.010",
            }
        }
        monkeypatch.setenv("AUTORESEARCH_COST_RATES", json.dumps(new_model))
        rates = load_rates()
        assert "openrouter/custom/model-x" in rates
        assert rates["openrouter/custom/model-x"]["input_per_1k"] == Decimal("0.005")

    def test_load_rates_custom_env_var_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_CUSTOM_RATES", raising=False)
        rates = load_rates(env_var="MY_CUSTOM_RATES")
        assert rates == DEFAULT_RATES

    def test_load_rates_invalid_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTORESEARCH_COST_RATES", "not valid json {{{")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            load_rates()
