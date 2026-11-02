"""Fade a stretch away from session VWAP back toward the mean."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from strategies import common

STRATEGY_ID = "anchored_vwap_mean_reversion"
STRETCH = Decimal("0.008")
RECLAIM = Decimal("0.004")


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    close = common.last_close(candles)
    session_vwap = common.vwap(candles)
    if close is None or session_vwap is None or session_vwap <= 0:
        return common.hold(STRATEGY_ID, "VWAP or last close is missing")
    gap = (close - session_vwap) / session_vwap
    if gap <= -STRETCH:
        stop = close * Decimal("0.992")
        return common.enter(
            STRATEGY_ID,
            direction="long",
            close=close,
            stop=stop,
            reason=f"{symbol} stretched below VWAP and is set up to revert",
        )
    if gap >= STRETCH:
        stop = close * Decimal("1.008")
        return common.enter(
            STRATEGY_ID,
            direction="short",
            close=close,
            stop=stop,
            reason=f"{symbol} stretched above VWAP and is set up to revert",
        )
    if abs(gap) < RECLAIM:
        return common.hold(STRATEGY_ID, "price is already near VWAP")
    return common.hold(STRATEGY_ID, "stretch from VWAP is not large enough")
