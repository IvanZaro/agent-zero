from __future__ import annotations

import logging
from decimal import Decimal

log = logging.getLogger(__name__)

_DEFAULT_INPUT_PER_1K = Decimal("0.000003")
_DEFAULT_OUTPUT_PER_1K = Decimal("0.000015")


class BudgetExceeded(Exception):
    pass


class CostMeter:
    def __init__(self, cap_usd: Decimal, rates: dict[str, dict[str, Decimal]]) -> None:
        self._cap_usd = cap_usd
        self._rates = rates
        self._spent = Decimal("0")

    def tick(self, model: str, input_tokens: int, output_tokens: int) -> None:
        if model in self._rates:
            rate = self._rates[model]
            input_rate = rate["input_per_1k"]
            output_rate = rate["output_per_1k"]
        else:
            log.warning(
                "CostMeter: no rate configured for model %r; using placeholder rates. "
                "Production callers must supply real rates.",
                model,
            )
            input_rate = _DEFAULT_INPUT_PER_1K
            output_rate = _DEFAULT_OUTPUT_PER_1K

        cost = (
            Decimal(input_tokens) * input_rate / Decimal("1000")
            + Decimal(output_tokens) * output_rate / Decimal("1000")
        )
        self._spent += cost

        if self._spent > self._cap_usd:
            raise BudgetExceeded(
                f"Budget exceeded: spent ${self._spent} > cap ${self._cap_usd}"
            )

    @property
    def spent(self) -> Decimal:
        return self._spent
