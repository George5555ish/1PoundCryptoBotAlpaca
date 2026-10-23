"""PNG snapshots of the 30 five-minute candles used for a decision.

Drawn after the decision row is stored. The picture marks swing highs and
lows, labels a higher high or a lower low when the next swing confirms it,
and connects the two price lows when MACD bullish divergence was found.
A short or empty series is skipped. A drawing error must not affect the
order path; the caller logs it and continues.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import config
import indicators


logger = logging.getLogger("charts")

UP_COLOR = "#1b7f4e"
DOWN_COLOR = "#c0392b"
HIGH_COLOR = "#e67e22"
LOW_COLOR = "#2471a3"
DIVERGENCE_COLOR = "#6c3483"


def save_snapshot(
    symbol: str,
    bars: list[dict[str, object]] | tuple[dict[str, object], ...],
    strategy: dict[str, object],
    when: datetime,
    directory: Path | None = None,
) -> Path | None:
    """Write one chart and return its path. Returns None when there is nothing to draw."""

    candles = _candles(list(bars))
    if len(candles) < 2:
        logger.info("CHART skip %s insufficient bars", symbol)
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        logger.exception("CHART skip %s matplotlib is not installed", symbol)
        return None

    folder = directory if directory is not None else config.data_directory() / "charts"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}_{symbol.replace('/', '-')}.png"

    figure, axis = plt.subplots(figsize=(11, 6))
    width = 0.62
    for index, candle in enumerate(candles):
        color = UP_COLOR if candle["close"] >= candle["open"] else DOWN_COLOR
        axis.vlines(index, float(candle["low"]), float(candle["high"]), color=color, linewidth=1)
        body_low = min(candle["open"], candle["close"])
        body_high = max(candle["open"], candle["close"])
        body = body_high - body_low
        if body == 0:
            axis.hlines(float(candle["close"]), index - width / 2, index + width / 2, color=color, linewidth=1.5)
        else:
            axis.add_patch(
                Rectangle(
                    (index - width / 2, float(body_low)),
                    width,
                    float(body),
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.6,
                )
            )

    _mark_swings(axis, candles)
    _mark_divergence(axis, candles, strategy)
    _style(axis, symbol, candles, strategy)
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)
    logger.info("CHART saved %s", path)
    return path


def _candles(bars: list[dict[str, object]]) -> list[dict[str, object]]:
    candles: list[dict[str, object]] = []
    for bar in bars:
        open_price = indicators.decimal_price(bar.get("open")) or indicators.decimal_price(bar.get("close"))
        high = indicators.decimal_price(bar.get("high"))
        low = indicators.decimal_price(bar.get("low"))
        close = indicators.decimal_price(bar.get("close"))
        if open_price is None or high is None or low is None or close is None or high < low:
            continue
        candles.append(
            {
                "time": indicators.bar_time(bar.get("time") or bar.get("timestamp")),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return candles


def _mark_swings(axis, candles: list[dict[str, object]]) -> None:
    highs = [candle["high"] for candle in candles]
    lows = [candle["low"] for candle in candles]
    high_indexes = indicators.swing_indexes(highs, kind="high")
    low_indexes = indicators.swing_indexes(lows, kind="low")
    if high_indexes:
        axis.scatter(
            high_indexes,
            [float(highs[index]) for index in high_indexes],
            marker="^",
            color=HIGH_COLOR,
            s=28,
            zorder=3,
            label="swing high",
        )
    if low_indexes:
        axis.scatter(
            low_indexes,
            [float(lows[index]) for index in low_indexes],
            marker="v",
            color=LOW_COLOR,
            s=28,
            zorder=3,
            label="swing low",
        )
    for previous, index in zip(high_indexes, high_indexes[1:]):
        if highs[index] > highs[previous]:
            axis.annotate(
                "HH",
                (index, float(highs[index])),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                color=HIGH_COLOR,
                fontsize=8,
            )
    for previous, index in zip(low_indexes, low_indexes[1:]):
        if lows[index] < lows[previous]:
            axis.annotate(
                "LL",
                (index, float(lows[index])),
                textcoords="offset points",
                xytext=(0, -12),
                ha="center",
                color=LOW_COLOR,
                fontsize=8,
            )


def _mark_divergence(axis, candles: list[dict[str, object]], strategy: dict[str, object]) -> None:
    divergence = strategy.get("macd_divergence") if isinstance(strategy, dict) else None
    if not isinstance(divergence, dict) or divergence.get("status") != "present":
        return
    points = divergence.get("price_swing_lows")
    if not isinstance(points, list) or len(points) < 2:
        return
    indexes: list[int] = []
    prices: list[float] = []
    for point in points[-2:]:
        if not isinstance(point, dict):
            return
        index = point.get("bar_index")
        price = indicators.decimal_price(point.get("price"))
        if not isinstance(index, int) or price is None or not 0 <= index < len(candles):
            return
        indexes.append(index)
        prices.append(float(price))
    axis.plot(indexes, prices, color=DIVERGENCE_COLOR, linewidth=1.4, zorder=4, label="bullish MACD divergence")
    axis.annotate(
        "bullish MACD divergence",
        (indexes[-1], prices[-1]),
        textcoords="offset points",
        xytext=(8, -16),
        color=DIVERGENCE_COLOR,
        fontsize=8,
    )


def _style(axis, symbol: str, candles: list[dict[str, object]], strategy: dict[str, object]) -> None:
    momentum = strategy.get("momentum_pullback") if isinstance(strategy, dict) else None
    divergence = strategy.get("macd_divergence") if isinstance(strategy, dict) else None
    momentum_status = momentum.get("status") if isinstance(momentum, dict) else "unknown"
    divergence_status = divergence.get("status") if isinstance(divergence, dict) else "absent"
    trend = momentum.get("trend") if isinstance(momentum, dict) and momentum.get("trend") else "no trend"
    axis.set_title(
        f"{symbol}  {config.CANDLE_MINUTES}-minute candles\n"
        f"momentum+pullback: {momentum_status} ({trend})   MACD divergence: {divergence_status}"
    )
    axis.set_ylabel("price")
    axis.set_xlim(-1, len(candles))
    step = max(1, len(candles) // 6)
    ticks = list(range(0, len(candles), step))
    axis.set_xticks(ticks)
    axis.set_xticklabels([_clock(str(candles[index]["time"])) for index in ticks])
    axis.grid(True, axis="y", alpha=0.3)
    handles, _labels = axis.get_legend_handles_labels()
    if handles:
        axis.legend(loc="upper left", frameon=False, fontsize=8)
    low = min(candle["low"] for candle in candles)
    high = max(candle["high"] for candle in candles)
    pad = (high - low) * Decimal("0.08") or Decimal("1")
    axis.set_ylim(float(low - pad), float(high + pad))


def _clock(value: str) -> str:
    """HH:MM from an ISO timestamp. Anything shorter is left as it is."""

    if "T" in value and len(value) >= 16:
        return value[11:16]
    return value
