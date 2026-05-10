from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

log = logging.getLogger(__name__)

# Rates are USD per 1k tokens (input, output).
# Sources checked 2026-05-07 from OpenRouter pricing page:
# https://openrouter.ai/models (filtered by provider)
#
# OpenAI via OpenRouter — https://openrouter.ai/openai/gpt-4o-mini
# OpenAI via OpenRouter — https://openrouter.ai/openai/gpt-4o
# Anthropic via OpenRouter — https://openrouter.ai/anthropic/claude-haiku-4-5
# Anthropic via OpenRouter — https://openrouter.ai/anthropic/claude-sonnet-4-6

DEFAULT_RATES: dict[str, dict[str, Decimal]] = {
    # Source: https://openrouter.ai/openai/gpt-4o-mini  ($0.15/1M input, $0.60/1M output)
    "openrouter/openai/gpt-4o-mini": {
        "input_per_1k": Decimal("0.00015"),
        "output_per_1k": Decimal("0.00060"),
    },
    # Source: https://openrouter.ai/openai/gpt-4o  ($2.50/1M input, $10.00/1M output)
    "openrouter/openai/gpt-4o": {
        "input_per_1k": Decimal("0.00250"),
        "output_per_1k": Decimal("0.01000"),
    },
    # Source: https://openrouter.ai/anthropic/claude-haiku-4-5  ($0.80/1M input, $4.00/1M output)
    "openrouter/anthropic/claude-haiku-4-5": {
        "input_per_1k": Decimal("0.00080"),
        "output_per_1k": Decimal("0.00400"),
    },
    # Source: https://openrouter.ai/anthropic/claude-sonnet-4-6  ($3.00/1M input, $15.00/1M output)
    "openrouter/anthropic/claude-sonnet-4-6": {
        "input_per_1k": Decimal("0.00300"),
        "output_per_1k": Decimal("0.01500"),
    },
}


def load_rates(env_var: str = "AUTORESEARCH_COST_RATES") -> dict[str, dict[str, Decimal]]:
    """Return DEFAULT_RATES merged with optional JSON override from *env_var*.

    The env var value must be a JSON object mapping model names to dicts with
    ``input_per_1k`` and ``output_per_1k`` string-formatted Decimal values.
    Raises ValueError / json.JSONDecodeError if the env var is set but malformed.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return dict(DEFAULT_RATES)

    try:
        override = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in env var {env_var!r}: {exc}") from exc

    merged: dict[str, dict[str, Decimal]] = dict(DEFAULT_RATES)
    for model, rates in override.items():
        merged[model] = {
            "input_per_1k": Decimal(str(rates["input_per_1k"])),
            "output_per_1k": Decimal(str(rates["output_per_1k"])),
        }
    return merged
