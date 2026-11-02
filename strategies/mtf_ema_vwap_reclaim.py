"""Long when the 21 EMA trend is up and price reclaims VWAP from below."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from strategies import common

STRATEGY_ID = "mtf_ema_vwap_reclaim"


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    prices = common.closes(candles)
    close = common.last_close(candles)
    mid = common.ema(prices, 21)
    session_vwap = common.vwap(candles)
    if close is None or mid is None or session_vwap is None:
        return common.hold(STRATEGY_ID, "EMA or VWAP is missing")
    if close < mid:
        return common.hold(STRATEGY_ID, "close is below the 21 EMA")
    prior = prices[-2] if len(prices) >= 2 else None
    if prior is None:
        return common.hold(STRATEGY_ID, "need a prior close to confirm a reclaim")
    if prior >= session_vwap:
        return common.hold(STRATEGY_ID, "prior close was already above VWAP")
    if close <= session_vwap:
        return common.hold(STRATEGY_ID, "close has not reclaimed VWAP")
    stop = session_vwap * Decimal("0.994")
    return common.enter(
        STRATEGY_ID,
        direction="long",
        close=close,
        stop=stop,
        reason=f"{symbol} 21 EMA trend is up and price reclaimed VWAP",
    )
