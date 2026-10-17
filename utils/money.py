"""utils/money.py — exact decimals at the database boundary (DATA-09).

Money, quantities and alert thresholds are NUMERIC(18,4) in Postgres. Python
floats enter only as display or as market quotes, and are turned into Decimal
through their shortest repr (`Decimal(str(x))`), never `Decimal(x)`, which
would carry the binary noise of the float.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

_QUANT = Decimal("0.0001")


def to_decimal(value: Any) -> Decimal | None:
    """Decimal for a number or numeric string; None for None / blank / unparseable / NaN / inf."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value).strip().replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
    return d if d.is_finite() else None


def to_db(value: Any) -> Decimal | None:
    """What goes into a NUMERIC(18,4) column: rounded to 4 places."""
    d = to_decimal(value)
    return None if d is None else d.quantize(_QUANT)


def tidy(value: Decimal | None) -> Decimal | None:
    """12.5000 -> 12.5, 70.0000 -> 70 (for display and JSON-ish payloads)."""
    if value is None:
        return None
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()
