"""Shared candle helpers for the rule-based paper strategies."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import indicators


def closes(candles: list[Mapping[str, object]]) -> list[Decimal]:
    values: list[Decimal] = []
    for bar in candles:
        price = indicators.decimal_price(bar.get("close"))
        if price is not None:
            values.append(price)
    return values


def ema(values: list[Decimal], period: int) -> Decimal | None:
    series = indicators.ema(values, period)
    if not series:
        return None
    return series[-1]


def vwap(candles: list[Mapping[str, object]]) -> Decimal | None:
    total = Decimal("0")
    volume = Decimal("0")
    for bar in candles:
        high = indicators.decimal_price(bar.get("high"))
        low = indicators.decimal_price(bar.get("low"))
        close = indicators.decimal_price(bar.get("close"))
        raw_vol = bar.get("volume")
        try:
            shares = Decimal(str(raw_vol if raw_vol not in {None, ""} else "1"))
        except Exception:
            shares = Decimal("1")
        if high is None or low is None or close is None or shares <= 0:
            continue
        typical = (high + low + close) / Decimal(3)
        total += typical * shares
        volume += shares
    if volume <= 0:
        return None
    return total / volume


def last_close(candles: list[Mapping[str, object]]) -> Decimal | None:
    prices = closes(candles)
    return prices[-1] if prices else None


def atr(candles: list[Mapping[str, object]], period: int = 14) -> Decimal | None:
    if len(candles) < 2:
        return None
    ranges: list[Decimal] = []
    previous = None
    for bar in candles:
        high = indicators.decimal_price(bar.get("high"))
        low = indicators.decimal_price(bar.get("low"))
        close = indicators.decimal_price(bar.get("close"))
        if high is None or low is None or close is None:
            continue
        span = high - low
        if previous is not None:
            span = max(span, abs(high - previous), abs(low - previous))
        ranges.append(span)
        previous = close
    if len(ranges) < period:
        window = ranges
    else:
        window = ranges[-period:]
    if not window:
        return None
    return sum(window, Decimal("0")) / Decimal(len(window))


def hold(strategy_id: str, reason: str) -> dict[str, object]:
    return {
        "action": "hold",
        "direction": None,
        "stop_price": None,
        "target_price": None,
        "reason": reason,
        "strategy_id": strategy_id,
    }


def enter(
    strategy_id: str,
    *,
    direction: str,
    close: Decimal,
    stop: Decimal,
    multiple: Decimal = Decimal("2"),
    reason: str,
) -> dict[str, object]:
    if direction == "long":
        risk = close - stop
        target = close + risk * multiple if risk > 0 else None
    else:
        risk = stop - close
        target = close - risk * multiple if risk > 0 else None
    if target is None or target <= 0 or stop <= 0:
        return hold(strategy_id, "stop or target could not be computed")
    return {
        "action": "enter",
        "direction": direction,
        "stop_price": float(stop),
        "target_price": float(target),
        "reason": reason,
        "strategy_id": strategy_id,
    }


def exit_position(strategy_id: str, reason: str) -> dict[str, object]:
    return {
        "action": "exit",
        "direction": None,
        "stop_price": None,
        "target_price": None,
        "reason": reason,
        "strategy_id": strategy_id,
    }
