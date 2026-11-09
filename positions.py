"""Hold or close decisions for positions that are already open.

Entry rules are not applied here. A review only looks at the trend summary
and the stop and target stored when the position was opened. The risk gate
still has to approve a close before any order is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import config


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


def signed_pnl_fraction(
    *,
    side: str,
    current_price: str | None,
    avg_entry_price: str | None,
    unrealized_plpc: str | None = None,
) -> Decimal | None:
    """Return P/L as a fraction of entry. Negative is a loss for long or short."""

    price = _number(current_price)
    entry = _number(avg_entry_price)
    if price is not None and entry is not None and entry > 0:
        move = (price - entry) / entry
        return -move if side == "short" else move
    plpc = _number(unrealized_plpc)
    if plpc is None:
        return None
    if abs(plpc) > 1:
        return plpc / Decimal("100")
    return plpc


def review_position(
    *,
    symbol: str,
    qty: str,
    current_price: str | None,
    unrealized_pl: str | None,
    trend: str | None,
    stop_price: str | None,
    target_price: str | None,
    side: str = "long",
    avg_entry_price: str | None = None,
    unrealized_plpc: str | None = None,
) -> PositionReview:
    """Decide hold or close for one open position.

    Stock/ETF names also close immediately when unrealized P/L from average
    entry reaches ``POSITION_LOSS_CLOSE_FRACTION`` (default -2%). Longs then
    close on stop, target, or "lower lows". Shorts invert those prices and
    close on "higher highs". Missing prices do not invent an exit.
    """

    price = _number(current_price)
    stop = _number(stop_price)
    target = _number(target_price)
    trend_text = trend.strip() if isinstance(trend, str) and trend.strip() else None
    short = side == "short"
    pnl_frac = signed_pnl_fraction(
        side=side,
        current_price=current_price,
        avg_entry_price=avg_entry_price,
        unrealized_plpc=unrealized_plpc,
    )

    if config.is_equity_hold(symbol) and pnl_frac is not None and pnl_frac <= -config.POSITION_LOSS_CLOSE_FRACTION:
        decision = "close"
        reason = f"{config.POSITION_LOSS_CLOSE_REASON} pnl={format(pnl_frac, 'f')}"
    elif short and price is not None and stop is not None and price >= stop:
        decision = "close"
        reason = "price is at or above the protective stop"
    elif short and price is not None and target is not None and price <= target:
        decision = "close"
        reason = "price reached the original target"
    elif short and trend_text == "higher highs":
        decision = "close"
        reason = "trend flipped against the short"
    elif not short and price is not None and stop is not None and price <= stop:
        decision = "close"
        reason = "price is at or below the protective stop"
    elif not short and price is not None and target is not None and price >= target:
        decision = "close"
        reason = "price reached the original target"
    elif not short and trend_text == "lower lows":
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
