"""Token pricing table (per 1M tokens, USD) with file override via PI_PRICES_FILE."""

from __future__ import annotations

import json
import os
from pathlib import Path

# input/output USD per 1M tokens; estimates, override with a prices file
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "qwen3.8-max": (1.6, 6.4),
    "qwen3.7-max": (1.6, 6.4),
    "qwen3.7-plus": (0.4, 1.6),
    "qwen3.8-flash": (0.06, 0.6),
    "qwen3.6-flash": (0.06, 0.6),
    "deepseek-v4-pro": (0.7, 2.8),
    "deepseek-v4-flash-0731": (0.1, 0.4),
    "glm-5.2": (0.7, 2.8),
    "demo": (0.0, 0.0),
}


def load_prices() -> dict[str, tuple[float, float]]:
    prices = dict(DEFAULT_PRICES)
    path = os.environ.get("PI_PRICES_FILE", "")
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for model, entry in data.items():
            if isinstance(entry, dict) and "input" in entry and "output" in entry:
                prices[model] = (float(entry["input"]), float(entry["output"]))
    return prices


def estimate_cost(model: str, input_tokens: int, output_tokens: int, prices: dict[str, tuple[float, float]] | None = None) -> float:
    prices = prices or load_prices()
    price_in, price_out = prices.get(_base(model), (0.0, 0.0))
    return (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out


def _base(model: str) -> str:
    """Strip provider prefix: 'openai/qwen3.8-max' -> 'qwen3.8-max'."""
    return model.partition("/")[-1] if "/" in model else model
