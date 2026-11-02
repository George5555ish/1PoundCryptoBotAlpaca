"""Existing momentum-plus-pullback rule, wrapped for the strategy engine."""

from __future__ import annotations

from typing import Mapping

import config
import scanner
import strategy
from strategies import common

STRATEGY_ID = "current_momentum_pullback"


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    window = list(candles)[-config.CANDLE_BARS :]
    if not window:
        return common.hold(STRATEGY_ID, "no candles for the momentum pullback")
    summary = scanner.summarize_candles(window)
    result = strategy.evaluate_momentum(summary, window)
    if result.get("status") != "pass":
        reasons = result.get("reasons")
        if isinstance(reasons, list) and reasons:
            reason = " | ".join(str(item) for item in reasons)
        else:
            reason = "momentum pullback failed"
        return common.hold(STRATEGY_ID, reason)
    stop = result.get("suggested_stop")
    target = result.get("target_high") or result.get("target_low")
    if stop is None or target is None:
        return common.hold(STRATEGY_ID, "momentum pullback passed without a stop or target")
    return {
        "action": "enter",
        "direction": "long",
        "stop_price": float(stop),
        "target_price": float(target),
        "reason": " | ".join(str(item) for item in result.get("reasons") or [f"{symbol} momentum pullback"]),
        "strategy_id": STRATEGY_ID,
    }
