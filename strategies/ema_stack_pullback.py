"""Long when EMA 8 > 21 > 50 and price pulls back toward the 21."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from strategies import common

STRATEGY_ID = "ema_stack_pullback"


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    prices = common.closes(candles)
    close = common.last_close(candles)
    fast = common.ema(prices, 8)
    mid = common.ema(prices, 21)
    slow = common.ema(prices, 50)
    if close is None or fast is None or mid is None or slow is None:
        return common.hold(STRATEGY_ID, "not enough closes for the EMA stack")
    if not (fast > mid > slow):
        return common.hold(STRATEGY_ID, "EMA stack is not aligned 8 > 21 > 50")
    if close > mid * Decimal("1.004"):
        return common.hold(STRATEGY_ID, "price has not pulled back to the 21 EMA")
    if close < mid * Decimal("0.992"):
        return common.hold(STRATEGY_ID, "pullback has broken the 21 EMA")
    stop = min(mid, close) * Decimal("0.992")
    return common.enter(
        STRATEGY_ID,
        direction="long",
        close=close,
        stop=stop,
        reason=f"{symbol} EMA stack aligned; close pulled back to the 21 EMA",
    )
