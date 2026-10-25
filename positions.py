"""Hold or close decisions for positions that are already open.

Entry rules are not applied here. A review only looks at the trend summary
and the stop and target stored when the position was opened. The risk gate
still has to approve a close before any order is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PositionReview:
    """One symbol's hold or close, with the reason that will be logged."""

    symbol: str
    qty: str
    current_price: str | None
    unrealized_pl: str | None
    decision: str
    reason: str
    trend: str | None


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def review_position(
    *,
    symbol: str,
    qty: str,
    current_price: str | None,
    unrealized_pl: str | None,
    trend: str | None,
    stop_price: str | None,
    target_price: str | None,
) -> PositionReview:
    """Decide hold or close for one open long.

    Close when the last price is at or below the tracked stop, when it has
    reached the tracked target, or when the candle trend is "lower lows".
    Anything else is a hold. Missing prices do not invent an exit.
    """

    price = _number(current_price)
    stop = _number(stop_price)
    target = _number(target_price)
    trend_text = trend.strip() if isinstance(trend, str) and trend.strip() else None

    if price is not None and stop is not None and price <= stop:
        decision = "close"
        reason = "price is at or below the protective stop"
    elif price is not None and target is not None and price >= target:
        decision = "close"
        reason = "price reached the original target"
    elif trend_text == "lower lows":
        decision = "close"
        reason = "trend flipped to lower lows"
    elif trend_text:
        decision = "hold"
        reason = f"hold; trend is {trend_text} and neither the stop nor the target has been hit"
    else:
        decision = "hold"
        reason = "hold; no lower-lows trend and no tracked stop or target was hit"

    return PositionReview(
        symbol=symbol,
        qty=qty,
        current_price=current_price,
        unrealized_pl=unrealized_pl,
        decision=decision,
        reason=reason,
        trend=trend_text,
    )
