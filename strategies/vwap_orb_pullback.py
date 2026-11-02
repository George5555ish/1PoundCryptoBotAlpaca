"""Long after an opening-range hold above VWAP, on a pullback toward VWAP."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import indicators
from strategies import common

STRATEGY_ID = "vwap_orb_pullback"
ORB_BARS = 6


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    if len(candles) < ORB_BARS + 4:
        return common.hold(STRATEGY_ID, "not enough bars for an opening-range pullback")
    close = common.last_close(candles)
    session_vwap = common.vwap(candles)
    if close is None or session_vwap is None:
        return common.hold(STRATEGY_ID, "VWAP or last close is missing")
    highs = [indicators.decimal_price(bar.get("high")) for bar in candles[:ORB_BARS]]
    highs = [price for price in highs if price is not None]
    if not highs:
        return common.hold(STRATEGY_ID, "opening range has no highs")
    orb_high = max(highs)
    if close < session_vwap:
        return common.hold(STRATEGY_ID, "close is below VWAP")
    if close > orb_high:
        return common.hold(STRATEGY_ID, "close is extended above the opening-range high")
    if close > session_vwap * Decimal("1.006"):
        return common.hold(STRATEGY_ID, "close has not pulled back toward VWAP")
    stop = session_vwap * Decimal("0.994")
    return common.enter(
        STRATEGY_ID,
        direction="long",
        close=close,
        stop=stop,
        reason=f"{symbol} held above VWAP after the opening range and pulled back",
    )
