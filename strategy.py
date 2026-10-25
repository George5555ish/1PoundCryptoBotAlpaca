"""Momentum-plus-pullback check for a long, plus the MACD note.

A long is eligible only when all three momentum conditions hold on the recent
5-minute window:

- the candle summary trend is "higher highs"
- the last close is more than 0.5% below the highest high (not extended)
- price has left that high and reached the range midpoint or the 5-bar average
- a prominent swing-high rejection needs a clearer pullback before a long is allowed

MACD bullish divergence is reported beside that result. It can support a long
that already passed. It does not make a failed check pass.

The risk gate does not read this module. These fields tell the model what it
may suggest; they do not size an order.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import config
import indicators


# "Within 0.5% of the high" uses the highest high as the base. A close that
# has fallen by this fraction or less is extended, and the long is not eligible.
NOT_EXTENDED_FRACTION = Decimal("0.005")
SHORT_AVERAGE_BARS = 5
TARGET_LOW_MULTIPLE = Decimal("1.5")
TARGET_HIGH_MULTIPLE = Decimal("2")
# A swing high within this fraction of the 30-bar high counts as prominent.
PROMINENT_SWING_FRACTION = Decimal("0.015")
# Upper wick longer than this share of the candle range is a rejection wick.
REJECTION_WICK_FRACTION = Decimal("0.5")
# After a rejection, the last close must be this far below that swing, or
# this far below the 5-bar average, before a long is allowed.
REJECTION_PULLBACK_FRACTION = Decimal("0.004")
REJECTION_AVERAGE_FRACTION = Decimal("0.002")
REJECTION_FAIL_REASON = "prominent swing high rejection — waiting for clearer pullback"


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
        "swing_high_rejection": _empty_rejection(reason),
    }


def build_context(summary: dict[str, object], history: list[dict[str, object]]) -> dict[str, object]:
    """Build the object stored in the decision log and sent to the model.

    ``history`` is oldest first and includes warmup bars before the chart
    window. The momentum rules use ``summary``, which was built from the last
    chart window only.
    """

    chart = history[-config.CANDLE_BARS :]
    momentum = evaluate_momentum(summary, chart)
    return {
        "momentum_pullback": momentum,
        "macd_divergence": evaluate_divergence(history, chart),
        "swing_high_rejection": momentum.get("swing_high_rejection") or _empty_rejection("rejection was not checked"),
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
    rejection = evaluate_swing_rejection(chart, last_close, short_average, highest)

    reasons: list[str] = []
    if trend != "higher highs":
        reasons.append(f"trend is {trend}; a long needs higher highs")
    if extended:
        reasons.append(
            f"last close is within 0.5% of the 30-bar high ({_percent(distance)}% below)"
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
    if (
        passed
        and rejection.get("status") == "present"
        and rejection.get("clearer_pullback") is not True
    ):
        reasons.append(REJECTION_FAIL_REASON)
        passed = False
    if passed:
        pullback_via = []
        if at_average:
            pullback_via.append("the 5-bar average")
        if at_midpoint:
            pullback_via.append("the range midpoint")
        reasons = [
            "trend is higher highs",
            f"last close is {_percent(distance)}% below the 30-bar high, outside the 0.5% extension band",
            "pullback: last close is at or below " + " and ".join(pullback_via),
        ]
        if rejection.get("status") == "present":
            reasons.append("swing-high rejection is present and the clearer pullback is in place")

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
        "swing_high_rejection": rejection,
    }


def evaluate_swing_rejection(
    chart: list[dict[str, object]],
    last_close: Decimal | None = None,
    short_average: Decimal | None = None,
    window_high: Decimal | None = None,
) -> dict[str, object]:
    """Describe the latest prominent swing-high rejection, if there is one.

    Missing or unconfirmed swings are ``absent``. That leaves the existing
    long rules unchanged. A present rejection never suggests a short.
    """

    candles = _ohlc_bars(chart)
    if len(candles) < indicators.SWING_WING * 2 + 1:
        return _empty_rejection("not enough bars to confirm a swing high")

    highs = [bar["high"] for bar in candles]
    peak = window_high if window_high is not None else max(highs)
    indexes = indicators.swing_indexes(highs, kind="high")
    if not indexes:
        return _empty_rejection("no confirmed swing high in the recent bars")

    chosen: int | None = None
    for index in reversed(indexes):
        if peak > 0 and highs[index] >= peak * (Decimal("1") - PROMINENT_SWING_FRACTION):
            chosen = index
            break
    if chosen is None:
        return _empty_rejection("latest swing highs are not prominent versus the 30-bar high")

    bar = candles[chosen]
    swing_high = bar["high"]
    following = candles[chosen + 1 : chosen + 3]
    range_size = swing_high - bar["low"]
    upper_wick = swing_high - max(bar["open"], bar["close"])
    wick_rejection = range_size > 0 and upper_wick > range_size * REJECTION_WICK_FRACTION
    close_back = any(item["close"] < swing_high for item in following)
    if not wick_rejection and not close_back:
        return {
            **_empty_rejection(
                f"swing high {format(swing_high, 'f')} did not show a rejection wick or a close back below it"
            ),
            "bar_index": chosen,
            "price": format(swing_high, "f"),
            "time": bar["time"],
        }

    kinds: list[str] = []
    if wick_rejection:
        kinds.append("upper wick")
    if close_back:
        kinds.append("close back below")
    kind = " and ".join(kinds)
    clearer = False
    if last_close is not None and swing_high > 0:
        if (swing_high - last_close) / swing_high >= REJECTION_PULLBACK_FRACTION:
            clearer = True
    if (
        not clearer
        and last_close is not None
        and short_average is not None
        and short_average > 0
        and last_close <= short_average * (Decimal("1") - REJECTION_AVERAGE_FRACTION)
    ):
        clearer = True

    if clearer:
        reason = f"prominent swing high rejection ({kind}) with a clearer pullback"
    else:
        reason = (
            f"prominent swing high rejection ({kind}); "
            "waiting for a closer pullback of at least 0.4% below that high "
            "or 0.2% below the 5-bar average"
        )
    return {
        "status": "present",
        "reason": reason,
        "bar_index": chosen,
        "price": format(swing_high, "f"),
        "time": bar["time"],
        "kind": kind,
        "clearer_pullback": clearer,
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
        "swing_high_rejection": _empty_rejection(reasons[0] if reasons else "insufficient bars"),
    }


def _empty_rejection(reason: str) -> dict[str, object]:
    return {
        "status": "absent",
        "reason": reason,
        "bar_index": None,
        "price": None,
        "time": None,
        "kind": None,
        "clearer_pullback": None,
    }


def _parsed_bars(chart: list[dict[str, object]]) -> list[tuple[Decimal, Decimal, Decimal]]:
    parsed: list[tuple[Decimal, Decimal, Decimal]] = []
    for bar in _ohlc_bars(chart):
        parsed.append((bar["high"], bar["low"], bar["close"]))
    return parsed


def _ohlc_bars(chart: list[dict[str, object]]) -> list[dict[str, Decimal | str]]:
    """Usable candles with an open. A missing open falls back to the close."""

    candles: list[dict[str, Decimal | str]] = []
    for bar in chart:
        high = indicators.decimal_price(bar.get("high"))
        low = indicators.decimal_price(bar.get("low"))
        close = indicators.decimal_price(bar.get("close"))
        open_price = indicators.decimal_price(bar.get("open")) or close
        if high is None or low is None or close is None or open_price is None or high < low:
            continue
        candles.append(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "time": indicators.bar_time(bar.get("time") or bar.get("timestamp")),
            }
        )
    return candles


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
