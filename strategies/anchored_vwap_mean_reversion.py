"""Fade a stretch away from session VWAP back toward the mean.

This rule prefers an aligned trend, but it does not require candle-summary
"higher highs" when ``ALLOW_AGAINST_TREND`` is true. A long is allowed on a
mixed or lower-lows label if price is stretched below VWAP and the latest
bar shows a bounce or stabilization. Stops come from recent structure and
ATR. The shared position review must not flatten this strategy on the
trend label alone; stop, target, and the -2% dump still apply.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import indicators
from strategies import common

STRATEGY_ID = "anchored_vwap_mean_reversion"
ALLOW_AGAINST_TREND = True
STRETCH = Decimal("0.008")
RECLAIM = Decimal("0.004")
ATR_STOP_MULT = Decimal("1.5")
STRUCTURE_BARS = 8
BOUNCE_CLOSE_FRACTION = Decimal("0.66")
FALLBACK_STOP_FRACTION = Decimal("0.008")


def evaluate(symbol: str, candles: list[Mapping[str, object]], context: Mapping[str, object]) -> dict[str, object]:
    close = common.last_close(candles)
    session_vwap = common.vwap(candles)
    if close is None or session_vwap is None or session_vwap <= 0:
        return common.hold(STRATEGY_ID, "VWAP or last close is missing")
    gap = (close - session_vwap) / session_vwap
    trend = _trend(context)
    if abs(gap) < RECLAIM:
        return common.hold(STRATEGY_ID, "price is already near VWAP")
    if gap <= -STRETCH:
        bounce, bounce_why = _bounce(candles, side="long")
        if not bounce:
            return common.hold(
                STRATEGY_ID,
                f"{symbol} stretched below VWAP but has no bounce or stabilization ({bounce_why})",
            )
        blocked = _trend_block(direction="long", trend=trend)
        if blocked:
            return common.hold(STRATEGY_ID, blocked)
        stop = _stop(candles, close, side="long")
        return common.enter(
            STRATEGY_ID,
            direction="long",
            close=close,
            stop=stop,
            reason=_reason(symbol, "long", gap, trend, bounce_why),
        )
    if gap >= STRETCH:
        bounce, bounce_why = _bounce(candles, side="short")
        if not bounce:
            return common.hold(
                STRATEGY_ID,
                f"{symbol} stretched above VWAP but has no bounce or stabilization ({bounce_why})",
            )
        blocked = _trend_block(direction="short", trend=trend)
        if blocked:
            return common.hold(STRATEGY_ID, blocked)
        stop = _stop(candles, close, side="short")
        return common.enter(
            STRATEGY_ID,
            direction="short",
            close=close,
            stop=stop,
            reason=_reason(symbol, "short", gap, trend, bounce_why),
        )
    return common.hold(STRATEGY_ID, "stretch from VWAP is not large enough")


def skip_trend_flip_exit(strategy_id: str | None) -> bool:
    """True when a still-messy candle trend must not reverse this strategy."""

    return strategy_id == STRATEGY_ID


def _trend(context: Mapping[str, object]) -> str:
    raw = context.get("candle_summary") if isinstance(context, Mapping) else None
    if not isinstance(raw, dict):
        return ""
    trend = raw.get("trend")
    return trend.strip().lower() if isinstance(trend, str) and trend.strip() else ""


def _trend_block(*, direction: str, trend: str) -> str | None:
    if ALLOW_AGAINST_TREND:
        return None
    if direction == "long" and trend and trend != "higher highs":
        return f"trend is {trend}; aligned-only mode needs higher highs"
    if direction == "short" and trend and trend != "lower lows":
        return f"trend is {trend}; aligned-only mode needs lower lows"
    return None


def _bounce(candles: list[Mapping[str, object]], *, side: str) -> tuple[bool, str]:
    if len(candles) < 2:
        return False, "need two bars"
    last_close = indicators.decimal_price(candles[-1].get("close"))
    prev_close = indicators.decimal_price(candles[-2].get("close"))
    last_high = indicators.decimal_price(candles[-1].get("high"))
    last_low = indicators.decimal_price(candles[-1].get("low"))
    if last_close is None or prev_close is None:
        return False, "missing closes"
    if side == "long" and last_close > prev_close:
        return True, "higher close"
    if side == "short" and last_close < prev_close:
        return True, "lower close"
    if last_high is None or last_low is None or last_high <= last_low:
        return False, "no directional close"
    span = last_high - last_low
    if side == "long" and (last_close - last_low) / span >= BOUNCE_CLOSE_FRACTION:
        return True, "closed in the upper third of the bar"
    if side == "short" and (last_high - last_close) / span >= BOUNCE_CLOSE_FRACTION:
        return True, "closed in the lower third of the bar"
    return False, "latest bar is still extending"


def _stop(candles: list[Mapping[str, object]], close: Decimal, *, side: str) -> Decimal:
    atr = common.atr(candles)
    structure = _structure_level(candles, side=side)
    fallback = close * (Decimal("1") - FALLBACK_STOP_FRACTION) if side == "long" else close * (
        Decimal("1") + FALLBACK_STOP_FRACTION
    )
    atr_stop = None
    if atr is not None and atr > 0:
        atr_stop = close - ATR_STOP_MULT * atr if side == "long" else close + ATR_STOP_MULT * atr
    candidates = [fallback]
    if structure is not None:
        candidates.append(structure)
    if atr_stop is not None:
        candidates.append(atr_stop)
    if side == "long":
        stop = min(candidates)
        if stop >= close:
            stop = fallback if fallback < close else close * (Decimal("1") - FALLBACK_STOP_FRACTION)
        return stop
    stop = max(candidates)
    if stop <= close:
        stop = fallback if fallback > close else close * (Decimal("1") + FALLBACK_STOP_FRACTION)
    return stop


def _structure_level(candles: list[Mapping[str, object]], *, side: str) -> Decimal | None:
    window = candles[-STRUCTURE_BARS:] if candles else []
    levels: list[Decimal] = []
    for bar in window:
        raw = bar.get("low") if side == "long" else bar.get("high")
        price = indicators.decimal_price(raw)
        if price is None:
            price = indicators.decimal_price(bar.get("close"))
        if price is not None:
            levels.append(price)
    if not levels:
        return None
    return min(levels) if side == "long" else max(levels)


def _reason(symbol: str, direction: str, gap: Decimal, trend: str, bounce_why: str) -> str:
    side = "below" if direction == "long" else "above"
    trend_text = trend or "unlabeled"
    mode = "against-trend allowed" if ALLOW_AGAINST_TREND else "aligned-only"
    return (
        f"{symbol} stretched {format(abs(gap) * 100, '.2f')}% {side} VWAP with {bounce_why}; "
        f"trend={trend_text}; {mode}"
    )
