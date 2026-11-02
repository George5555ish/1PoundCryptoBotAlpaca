"""Long when the slower 21/50 EMA trend is up and price holds above both."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from strategies import common

STRATEGY_ID = "long_horizon_momentum"


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    prices = common.closes(candles)
    close = common.last_close(candles)
    mid = common.ema(prices, 21)
    slow = common.ema(prices, 50)
    if close is None or mid is None or slow is None:
        return common.hold(STRATEGY_ID, "not enough closes for a longer EMA trend")
    if mid <= slow:
        return common.hold(STRATEGY_ID, "21 EMA is not above the 50 EMA")
    if close < mid:
        return common.hold(STRATEGY_ID, "close is below the 21 EMA")
    if close > mid * Decimal("1.012"):
        return common.hold(STRATEGY_ID, "close is extended versus the 21 EMA")
    stop = slow * Decimal("0.995")
    return common.enter(
        STRATEGY_ID,
        direction="long",
        close=close,
        stop=stop,
        reason=f"{symbol} slower EMA trend is up and price is holding the 21",
    )
