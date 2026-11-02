"""Long when a tight range around VWAP expands above the range high."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import indicators
from strategies import common

STRATEGY_ID = "vwap_squeeze_breakout"
SQUEEZE_BARS = 12
SQUEEZE_FRACTION = Decimal("0.004")


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    if len(candles) < SQUEEZE_BARS + 2:
        return common.hold(STRATEGY_ID, "not enough bars for a VWAP squeeze")
    window = candles[-(SQUEEZE_BARS + 1) : -1]
    highs = [indicators.decimal_price(bar.get("high")) for bar in window]
    lows = [indicators.decimal_price(bar.get("low")) for bar in window]
    highs = [price for price in highs if price is not None]
    lows = [price for price in lows if price is not None]
    close = common.last_close(candles)
    session_vwap = common.vwap(candles)
    if close is None or session_vwap is None or not highs or not lows:
        return common.hold(STRATEGY_ID, "squeeze range or VWAP is missing")
    high = max(highs)
    low = min(lows)
    mid = (high + low) / Decimal(2)
    if high <= 0 or (high - low) / high > SQUEEZE_FRACTION:
        return common.hold(STRATEGY_ID, "range is too wide for a squeeze")
    if abs(mid - session_vwap) / session_vwap > Decimal("0.003"):
        return common.hold(STRATEGY_ID, "squeeze is not centered on VWAP")
    if close <= high:
        return common.hold(STRATEGY_ID, "close has not broken the squeeze high")
    stop = session_vwap
    return common.enter(
        STRATEGY_ID,
        direction="long",
        close=close,
        stop=stop,
        reason=f"{symbol} broke above a VWAP-centered squeeze",
    )
