"""Momentum-plus-pullback check for a long, plus the MACD note.

A long is eligible only when all three momentum conditions hold on the recent
5-minute window:

- the candle summary trend is "higher highs"
- the last close is more than 0.3% below the highest high (not extended)
- price has left that high and reached the range midpoint or the 5-bar average

MACD bullish divergence is reported beside that result. It can support a long
that already passed. It does not make a failed check pass.

The risk gate does not read this module. These fields tell the model what it
may suggest; they do not size an order.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import config
import indicators


# "Within 0.3% of the high" uses the highest high as the base. A close that
# has fallen by this fraction or less is extended, and the long is not eligible.
NOT_EXTENDED_FRACTION = Decimal("0.003")
SHORT_AVERAGE_BARS = 5
TARGET_LOW_MULTIPLE = Decimal("1.5")
TARGET_HIGH_MULTIPLE = Decimal("2")


def empty_context(reason: str) -> dict[str, object]:
    """Strategy block used when the candle read produced nothing usable."""

    return {
        "momentum_pullback": _momentum_shell(status="fail", reasons=[reason]),
        "macd_divergence": {
            "status": "absent",
            "description": reason,
            "source": None,
            "insufficient_bars": True,
            "price_swing_lows": [],
        },
    }


def build_context(summary: dict[str, object], history: list[dict[str, object]]) -> dict[str, object]:
    """Build the object stored in the decision log and sent to the model.

    ``history`` is oldest first and includes warmup bars before the chart
    window. The momentum rules use ``summary``, which was built from the last
    chart window only.
    """

    chart = history[-config.CANDLE_BARS :]
    return {
        "momentum_pullback": evaluate_momentum(summary, chart),
        "macd_divergence": evaluate_divergence(history, chart),
    }


def evaluate_momentum(summary: dict[str, object], chart: list[dict[str, object]]) -> dict[str, object]:
    """Pass or fail the long setup, with the stop and target band when known."""

    if summary.get("error") or not summary.get("bar_count"):
        reason = str(summary.get("error") or "insufficient bars")
        return _momentum_shell(status="fail", reasons=[reason])

    parsed = _parsed_bars(chart)
    last_close = indicators.decimal_price(summary.get("last_close"))
    highest = indicators.decimal_price(summary.get("highest_high"))
    lowest = indicators.decimal_price(summary.get("lowest_low"))
    average_range = indicators.decimal_price(summary.get("average_true_range"))
    trend = summary.get("trend")
    if last_close is None or highest is None or lowest is None or not parsed:
        return _momentum_shell(status="fail", reasons=["candle summary has no usable prices"])

    distance = (highest - last_close) / highest
    extended = distance <= NOT_EXTENDED_FRACTION
    short_average = _short_average(parsed)
    midpoint = (highest + lowest) / Decimal(2)
    peak_index = max(index for index, bar in enumerate(parsed) if bar[0] == max(bar[0] for bar in parsed))
    high_is_latest = peak_index == len(parsed) - 1
    pullback_low = None if high_is_latest else min(bar[1] for bar in parsed[peak_index + 1 :])
    at_average = short_average is not None and last_close <= short_average
    at_midpoint = last_close <= midpoint
    pullback = (not high_is_latest) and (at_average or at_midpoint)

    reasons: list[str] = []
    if trend != "higher highs":
        reasons.append(f"trend is {trend}; a long needs higher highs")
    if extended:
        reasons.append(
            f"last close is within 0.3% of the 30-bar high ({_percent(distance)}% below)"
        )
    if high_is_latest:
        reasons.append("the 30-bar high is the latest bar, so price has not pulled back")
    elif not pullback:
        if short_average is None:
            reasons.append(
                "last close is not at or below the range midpoint, "
                f"and there are fewer than {SHORT_AVERAGE_BARS} closes for the short average"
            )
        else:
            reasons.append("last close is still above the 5-bar average and the range midpoint")

    passed = not reasons
    if passed:
        pullback_via = []
        if at_average:
            pullback_via.append("the 5-bar average")
        if at_midpoint:
            pullback_via.append("the range midpoint")
        reasons = [
            "trend is higher highs",
            f"last close is {_percent(distance)}% below the 30-bar high, outside the 0.3% extension band",
            "pullback: last close is at or below " + " and ".join(pullback_via),
        ]

    suggested_stop = _cent_below(pullback_low) if pullback_low is not None else None
    atr_stop = None
    if average_range is not None and average_range > 0:
        atr_level = last_close - average_range
        if atr_level > 0:
            atr_stop = _money(atr_level, ROUND_DOWN)
    target_low = None
    target_high = None
    if suggested_stop is not None and suggested_stop < last_close:
        risk = last_close - suggested_stop
        target_low = _money(last_close + risk * TARGET_LOW_MULTIPLE, ROUND_HALF_UP)
        target_high = _money(last_close + risk * TARGET_HIGH_MULTIPLE, ROUND_HALF_UP)

    return {
        "status": "pass" if passed else "fail",
        "reasons": reasons,
        "trend": trend,
        "extended": extended,
        "distance_from_high": format(distance, "f"),
        "pullback": pullback,
        "pullback_low": None if pullback_low is None else format(pullback_low, "f"),
        "range_midpoint": format(midpoint, "f"),
        "short_average": None if short_average is None else format(short_average, "f"),
        "suggested_stop": None if suggested_stop is None else format(suggested_stop, "f"),
        "atr_stop": None if atr_stop is None else format(atr_stop, "f"),
        "target_low": None if target_low is None else format(target_low, "f"),
        "target_high": None if target_high is None else format(target_high, "f"),
    }


def evaluate_divergence(history: list[dict[str, object]], chart: list[dict[str, object]]) -> dict[str, object]:
    """MACD divergence on the chart window, using older bars only as warmup."""

    closes: list[Decimal] = []
    for bar in history:
        price = indicators.decimal_price(bar.get("close"))
        if price is None:
            return {
                "status": "absent",
                "description": "MACD divergence was not checked because a bar has no close.",
                "source": None,
                "insufficient_bars": True,
                "price_swing_lows": [],
            }
        closes.append(price)
    line, _signal, histogram = indicators.macd(closes)
    offset = len(history) - len(chart)
    return indicators.bullish_divergence(chart, line[offset:], histogram[offset:])


def _momentum_shell(*, status: str, reasons: list[str]) -> dict[str, object]:
    return {
        "status": status,
        "reasons": reasons,
        "trend": None,
        "extended": None,
        "distance_from_high": None,
        "pullback": False,
        "pullback_low": None,
        "range_midpoint": None,
        "short_average": None,
        "suggested_stop": None,
        "atr_stop": None,
        "target_low": None,
        "target_high": None,
    }


def _parsed_bars(chart: list[dict[str, object]]) -> list[tuple[Decimal, Decimal, Decimal]]:
    parsed: list[tuple[Decimal, Decimal, Decimal]] = []
    for bar in chart:
        high = indicators.decimal_price(bar.get("high"))
        low = indicators.decimal_price(bar.get("low"))
        close = indicators.decimal_price(bar.get("close"))
        if high is None or low is None or close is None or high < low:
            continue
        parsed.append((high, low, close))
    return parsed


def _short_average(parsed: list[tuple[Decimal, Decimal, Decimal]]) -> Decimal | None:
    if len(parsed) < SHORT_AVERAGE_BARS:
        return None
    closes = [bar[2] for bar in parsed[-SHORT_AVERAGE_BARS:]]
    return sum(closes, Decimal("0")) / Decimal(SHORT_AVERAGE_BARS)


def _cent_below(level: Decimal) -> Decimal:
    """One cent under a swing, rounded down so the result stays below it."""

    stop = (level - config.CENT).quantize(config.CENT, rounding=ROUND_DOWN)
    if stop >= level:
        stop = level - config.CENT
    return stop


def _money(value: Decimal, rounding: str) -> Decimal:
    return value.quantize(config.CENT, rounding=rounding)


def _percent(fraction: Decimal) -> str:
    return format((fraction * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
