"""Shared USD-per-MTok price table and the cost formula (must match alter: contract C2/C3).

Prices come from the packaged ``ego/pricing.json``. A model that does not resolve
to a table entry has no known price, so its cost is None rather than a guess.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources

_PER_MTOK = 1_000_000
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@lru_cache(maxsize=1)
def load_table() -> dict:
    text = resources.files("ego").joinpath("pricing.json").read_text(encoding="utf-8")
    return json.loads(text)


def resolve_model(model: str) -> str | None:
    table = load_table()
    name = model.rsplit("/", 1)[-1].strip().lower().replace(".", "-")
    name = table["aliases"].get(name, name)
    name = _DATE_SUFFIX.sub("", name)
    return name if name in table["models"] else None


def _count(usage: dict, key: str) -> int:
    return int(usage.get(key) or 0)


def cache_write_tiers(usage: dict) -> tuple[int, int]:
    """Return (5m, 1h) cache-write tokens. Hosts that only report cache_creation_tokens are priced as 5m."""
    if "cache_write_5m_tokens" not in usage and "cache_write_1h_tokens" not in usage:
        return _count(usage, "cache_creation_tokens"), 0
    return _count(usage, "cache_write_5m_tokens"), _count(usage, "cache_write_1h_tokens")


def cost_usd(usage: dict, model: str) -> float | None:
    resolved = resolve_model(model)
    if resolved is None:
        return None
    price = load_table()["models"][resolved]
    write_5m, write_1h = cache_write_tiers(usage)
    total = (
        _count(usage, "input_tokens") * price["input"]
        + _count(usage, "output_tokens") * price["output"]
        + _count(usage, "cache_read_tokens") * price["cache_read"]
        + write_5m * price["cache_write_5m"]
        + write_1h * price["cache_write_1h"]
    )
    return round(total / _PER_MTOK, 6)
