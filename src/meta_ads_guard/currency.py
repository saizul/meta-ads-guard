"""Currency offsets for budget values.

Insights report ``spend`` in major units (``"1520.75"`` taka), but ad set and
campaign budgets are stored in the currency's minimum denomination
(``daily_budget=150000`` is 1,500.00 BDT). Meta's currency table lists an
offset of 100 for most currencies and 1 for the ones below.
"""

from __future__ import annotations

from typing import Optional

OFFSET_ONE = frozenset(
    {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}
)


def offset(currency: Optional[str]) -> int:
    return 1 if (currency or "").upper() in OFFSET_ONE else 100


def to_major(minor: object, currency: Optional[str]) -> Optional[float]:
    if minor in (None, "", "0", 0):
        return None
    try:
        return float(minor) / offset(currency)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def to_minor(major: float, currency: Optional[str]) -> int:
    return round(major * offset(currency))


def format_money(amount: Optional[float], currency: Optional[str]) -> str:
    if amount is None:
        return "n/a"
    digits = 0 if offset(currency) == 1 else 2
    return f"{amount:,.{digits}f} {currency or ''}".strip()
