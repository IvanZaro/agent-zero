from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.worker.cost_meter import BudgetExceeded, CostMeter

_RATES = {
    "test-model": {
        "input_per_1k": Decimal("0.001"),
        "output_per_1k": Decimal("0.002"),
    }
}


class TestCostMeter:
    def setup_method(self) -> None:
        self.meter = CostMeter(cap_usd=Decimal("0.01"), rates=_RATES)

    def test_initial_spent_is_zero(self) -> None:
        assert self.meter.spent == Decimal("0")

    def test_tick_accumulates_cost(self) -> None:
        self.meter.tick("test-model", input_tokens=1000, output_tokens=1000)
        expected = Decimal("0.001") + Decimal("0.002")
        assert self.meter.spent == expected

    def test_raises_budget_exceeded_when_over_cap(self) -> None:
        # Each tick costs 0.003 USD; cap is 0.01; after 4 ticks (0.012) it should fire
        with pytest.raises(BudgetExceeded):
            for _ in range(10):
                self.meter.tick("test-model", input_tokens=1000, output_tokens=1000)

    def test_does_not_raise_at_cap_minus_epsilon(self) -> None:
        # 3 ticks = 0.009, cap = 0.01 → should NOT raise
        for _ in range(3):
            self.meter.tick("test-model", input_tokens=1000, output_tokens=1000)
        assert self.meter.spent == Decimal("0.009")

    def test_raises_exactly_at_cap_plus_epsilon(self) -> None:
        # 3 ticks = 0.009 (OK); 4th = 0.012 → exceeds cap 0.01
        for _ in range(3):
            self.meter.tick("test-model", input_tokens=1000, output_tokens=1000)
        with pytest.raises(BudgetExceeded):
            self.meter.tick("test-model", input_tokens=1000, output_tokens=1000)

    def test_unknown_model_uses_default_rates(self) -> None:
        meter = CostMeter(cap_usd=Decimal("100"), rates={})
        meter.tick("unknown-model", input_tokens=1000, output_tokens=1000)
        assert meter.spent > Decimal("0")

    def test_zero_tokens_no_cost(self) -> None:
        self.meter.tick("test-model", input_tokens=0, output_tokens=0)
        assert self.meter.spent == Decimal("0")

    def test_spent_property_is_immutable_snapshot(self) -> None:
        self.meter.tick("test-model", input_tokens=500, output_tokens=500)
        first = self.meter.spent
        self.meter.tick("test-model", input_tokens=500, output_tokens=500)
        second = self.meter.spent
        assert second > first

    def test_multiple_models_accumulate(self) -> None:
        rates = {
            "model-a": {"input_per_1k": Decimal("0.001"), "output_per_1k": Decimal("0.001")},
            "model-b": {"input_per_1k": Decimal("0.002"), "output_per_1k": Decimal("0.002")},
        }
        meter = CostMeter(cap_usd=Decimal("10"), rates=rates)
        meter.tick("model-a", input_tokens=1000, output_tokens=1000)
        meter.tick("model-b", input_tokens=1000, output_tokens=1000)
        assert meter.spent == Decimal("0.006")
