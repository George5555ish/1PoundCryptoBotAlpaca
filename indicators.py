"""MACD and swing points for the 5-minute crypto bars.

MACD is the standard 12, 26, 9. The slow average needs 26 closes and the
signal average needs 9 MACD values after that, so the first histogram value
lands on bar 34 (index 33). Callers fetch older bars for that warmup and then
judge divergence only on the recent chart window.

Bullish divergence here means the later of the last two swing lows is a lower
price low, while the MACD line or the histogram makes a higher low. It is a
description of those two points, not a trade signal by itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SWING_WING = 2
MACD_DISPLAY = Decimal("0.0001")


def history_bar_count(chart_bars: int) -> int:
    """How many bars to fetch so the chart window has a warmed-up MACD.

    The extra bars are the slow length plus the signal length. That puts the
    first chart bar at or after the first histogram value when the feed
    returns a full window.
    """

    return chart_bars + MACD_SLOW + MACD_SIGNAL


def bar_time(value: object) -> str:
    """ISO-8601 timestamp for a log line or a chart label. Naive values are UTC."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if value is None:
        return ""
    return str(value)


def decimal_price(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    """EMA seeded with the simple average of the first ``period`` values.

    Earlier positions are None. Seeding with the SMA is the usual definition;
    an EMA that starts on bar 1 looks precise and is not warmed up.
    """

    series: list[Decimal | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return series
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    series[period - 1] = seed
    alpha = Decimal(2) / Decimal(period + 1)
    remain = Decimal(1) - alpha
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * remain
        series[index] = previous
    return series


def macd(
    closes: list[Decimal],
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """MACD line, signal line, and histogram, aligned with ``closes``.

    A value is None until that component has enough history.
    """

    fast = ema(closes, MACD_FAST)
    slow = ema(closes, MACD_SLOW)
    line: list[Decimal | None] = [
        None if fast[index] is None or slow[index] is None else fast[index] - slow[index]
        for index in range(len(closes))
    ]
    signal: list[Decimal | None] = [None] * len(closes)
    first = next((index for index, value in enumerate(line) if value is not None), None)
    if first is None:
        return line, signal, signal
    run: list[Decimal] = []
    for value in line[first:]:
        if value is None:
            break
        run.append(value)
    for offset, value in enumerate(ema(run, MACD_SIGNAL)):
        signal[first + offset] = value
    histogram = [
        None if line[index] is None or signal[index] is None else line[index] - signal[index]
        for index in range(len(closes))
    ]
    return line, signal, histogram


def swing_indexes(values: list[Decimal], *, kind: str, wing: int = SWING_WING) -> list[int]:
    """Indexes of local highs or lows.

    A point counts when it is strictly beyond the ``wing`` bars on each side.
    The latest bars are not swings yet, because the right-hand side is missing.
    """

    if kind not in {"high", "low"}:
        raise ValueError(f"swing kind must be high or low, got {kind!r}")
    found: list[int] = []
    if wing <= 0 or len(values) < (wing * 2 + 1):
        return found
    for index in range(wing, len(values) - wing):
        left = values[index - wing : index]
        right = values[index + 1 : index + 1 + wing]
        if kind == "low" and values[index] < min(left) and values[index] < min(right):
            found.append(index)
        elif kind == "high" and values[index] > max(left) and values[index] > max(right):
            found.append(index)
    return found


def format_macd(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(MACD_DISPLAY, rounding=ROUND_HALF_UP), "f")


def bullish_divergence(
    chart_bars: list[dict[str, object]],
    macd_line: list[Decimal | None],
    histogram: list[Decimal | None],
) -> dict[str, object]:
    """Compare the last two price swing lows with MACD at those bars.

    ``macd_line`` and ``histogram`` line up with ``chart_bars``. None means
    that bar is still inside the warmup.
    """

    empty = {
        "status": "absent",
        "description": "No bullish MACD divergence.",
        "source": None,
        "insufficient_bars": False,
        "price_swing_lows": [],
    }
    if len(chart_bars) != len(macd_line) or len(chart_bars) != len(histogram):
        empty["description"] = "MACD divergence was not checked because the series lengths did not match."
        empty["insufficient_bars"] = True
        return empty

    lows: list[Decimal] = []
    for bar in chart_bars:
        price = decimal_price(bar.get("low"))
        if price is None:
            empty["description"] = "MACD divergence was not checked because a recent bar has no low."
            empty["insufficient_bars"] = True
            return empty
        lows.append(price)

    if not any(value is not None for value in histogram):
        empty["description"] = (
            f"Not enough bars for MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}. "
            "Bullish divergence is absent."
        )
        empty["insufficient_bars"] = True
        return empty

    indexes = [
        index
        for index in swing_indexes(lows, kind="low")
        if macd_line[index] is not None or histogram[index] is not None
    ]
    if len(indexes) < 2:
        empty["description"] = "Fewer than two swing lows in the recent bars, so bullish divergence is absent."
        return empty

    earlier_index, later_index = indexes[-2], indexes[-1]

    def point(index: int) -> dict[str, object]:
        return {
            "bar_index": index,
            "time": bar_time(chart_bars[index].get("time") or chart_bars[index].get("timestamp")),
            "price": format(lows[index], "f"),
            "macd": format_macd(macd_line[index]),
            "histogram": format_macd(histogram[index]),
        }

    earlier = point(earlier_index)
    later = point(later_index)
    price_lower = lows[later_index] < lows[earlier_index]
    macd_higher = (
        macd_line[earlier_index] is not None
        and macd_line[later_index] is not None
        and macd_line[later_index] > macd_line[earlier_index]
    )
    histogram_higher = (
        histogram[earlier_index] is not None
        and histogram[later_index] is not None
        and histogram[later_index] > histogram[earlier_index]
    )
    result = {
        "status": "absent",
        "description": "",
        "source": None,
        "insufficient_bars": False,
        "price_swing_lows": [earlier, later],
    }
    if price_lower and (macd_higher or histogram_higher):
        if macd_higher and histogram_higher:
            source = "macd and histogram"
        elif macd_higher:
            source = "macd"
        else:
            source = "histogram"
        result["status"] = "present"
        result["source"] = source
        result["description"] = (
            f"Bullish divergence on the {source}: price low {later['price']} at {later['time']} "
            f"is below {earlier['price']} at {earlier['time']}, while momentum made a higher low."
        )
        return result

    if price_lower:
        result["description"] = (
            f"No bullish divergence. Price low {later['price']} is below {earlier['price']}, "
            "but the MACD line and the histogram did not make a higher low."
        )
    else:
        result["description"] = (
            f"No bullish divergence. The latest swing low {later['price']} is not below "
            f"the previous swing low {earlier['price']}."
        )
    return result
