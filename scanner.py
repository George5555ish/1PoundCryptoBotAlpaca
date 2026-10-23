"""Alpaca crypto quotes, a compact 5-minute summary, and strategy context.

Prices leave this module as decimal strings. The gate converts the quote. The
model receives the candle summary and the strategy result. It does not receive
the bars. The bars stay on the scan result so a chart can be drawn after the
decision is logged. A failed candle fetch is logged and the quote is still
returned.

The data client cannot place orders.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas

import config
import indicators
import strategy


logger = logging.getLogger("scanner")


@dataclass(frozen=True)
class SymbolView:
    """One pair as the model and the gate will see it."""

    symbol: str
    bid: str | None
    ask: str | None
    last: str | None
    quote_time: datetime | None
    candle_summary: dict[str, object]
    strategy: dict[str, object]
    chart_bars: tuple[dict[str, str], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ScanResult:
    views: tuple[SymbolView, ...]

    def for_symbol(self, symbol: str) -> SymbolView | None:
        canonical = config.normalize_symbol(symbol)
        for view in self.views:
            if view.symbol == canonical:
                return view
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    """Label a naive Alpaca data timestamp as UTC. Aware values are converted."""

    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _price(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number, "f")


def empty_candle_summary(error: str | None = None) -> dict[str, object]:
    """A summary that says the candle read did not produce usable bars."""

    return {
        "timeframe_minutes": config.CANDLE_MINUTES,
        "bar_count": 0,
        "last_close": None,
        "highest_high": None,
        "lowest_low": None,
        "trend": None,
        "average_true_range": None,
        "error": error,
    }


def _decimal_price(value: object) -> Decimal | None:
    text = _price(value)
    if text is None:
        return None
    return Decimal(text)


def summarize_candles(bars: list[dict[str, object]]) -> dict[str, object]:
    """Build the compact description the model is allowed to see.

    ``bars`` is oldest first. Fewer than 30 bars is fine; the count is part
    of the summary. With fewer than four usable bars the trend is ``mixed``,
    because a half-versus-half comparison would be noise.

    Trend labels, comparing the earlier half with the later half:
    - higher highs: the later high is above the earlier high, and the later
      low did not break the earlier low
    - lower lows: the later low is below the earlier low, and the later high
      did not break the earlier high
    - sideways: neither a new high nor a new low
    - mixed: both a new high and a new low, or too few bars to split
    """

    usable: list[tuple[Decimal, Decimal, Decimal]] = []
    for bar in bars:
        high = _decimal_price(bar.get("high"))
        low = _decimal_price(bar.get("low"))
        close = _decimal_price(bar.get("close"))
        if high is None or low is None or close is None or high < low:
            continue
        usable.append((high, low, close))
    if not usable:
        return empty_candle_summary("no usable bars")

    window = usable[-config.CANDLE_BARS :]
    true_ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    highest = window[0][0]
    lowest = window[0][1]
    for high, low, close in window:
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close
        if high > highest:
            highest = high
        if low < lowest:
            lowest = low
    average_range = sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges))

    if len(window) < 4:
        trend = "mixed"
    else:
        midpoint = len(window) // 2
        earlier = window[:midpoint]
        later = window[midpoint:]
        earlier_high = max(bar[0] for bar in earlier)
        earlier_low = min(bar[1] for bar in earlier)
        later_high = max(bar[0] for bar in later)
        later_low = min(bar[1] for bar in later)
        made_higher_high = later_high > earlier_high
        made_lower_low = later_low < earlier_low
        if made_higher_high and not made_lower_low:
            trend = "higher highs"
        elif made_lower_low and not made_higher_high:
            trend = "lower lows"
        elif not made_higher_high and not made_lower_low:
            trend = "sideways"
        else:
            trend = "mixed"

    return {
        "timeframe_minutes": config.CANDLE_MINUTES,
        "bar_count": len(window),
        "last_close": format(window[-1][2], "f"),
        "highest_high": format(highest, "f"),
        "lowest_low": format(lowest, "f"),
        "trend": trend,
        "average_true_range": format(average_range.quantize(config.CENT), "f"),
        "error": None,
    }


def _rows_from_frame(frame: pandas.DataFrame, symbol: str, limit: int) -> list[dict[str, object]]:
    """Take the last ``limit`` bars for one symbol out of an Alpaca bar frame.

    ``limit`` is the chart window plus the MACD warmup, not the 30 bars the
    model is told about.
    """

    if frame is None or frame.empty:
        return []
    names = list(frame.index.names or [])
    if "symbol" not in names:
        return []
    try:
        sliced = frame.xs(symbol, level="symbol")
    except KeyError:
        return []
    tail = sliced.tail(limit)
    rows: list[dict[str, object]] = []
    for timestamp, row in tail.iterrows():
        rows.append(
            {
                "timestamp": timestamp,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
        )
    return rows


def _clean_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop bars that cannot be priced. The open falls back to the close."""

    cleaned: list[dict[str, object]] = []
    for row in rows:
        high = _price(row.get("high"))
        low = _price(row.get("low"))
        close = _price(row.get("close"))
        if high is None or low is None or close is None or Decimal(high) < Decimal(low):
            continue
        cleaned.append(
            {
                "timestamp": row.get("timestamp"),
                "open": _price(row.get("open")) or close,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return cleaned


def _public_bar(row: dict[str, object]) -> dict[str, str]:
    return {
        "time": _bar_time(row.get("timestamp")),
        "open": str(row.get("open") or ""),
        "high": str(row.get("high") or ""),
        "low": str(row.get("low") or ""),
        "close": str(row.get("close") or ""),
    }


def _bar_time(value: object) -> str:
    """Format a bar timestamp for the log. Naive values are labeled UTC."""

    if isinstance(value, datetime):
        stamped = _as_utc(value)
        return stamped.isoformat() if stamped is not None else ""
    if value is None:
        return ""
    return str(value)


def _log_retrieved_candles(symbol: str, rows: list[dict[str, object]], summary: dict[str, object]) -> None:
    """Log the bars that were kept and the compact summary built from them.

    These lines stay in the bot log. The chart bars stay on the scan result for
    the snapshot, and the prompt builder drops them before the model sees them.
    """

    logger.info(
        "SCAN candles %s retrieved=%s timeframe=%sm",
        symbol,
        len(rows),
        config.CANDLE_MINUTES,
    )
    for row in rows:
        logger.info(
            "SCAN candle %s time=%s open=%s high=%s low=%s close=%s",
            symbol,
            _bar_time(row.get("timestamp") or row.get("time")),
            _price(row.get("open")) or "",
            _price(row.get("high")) or "",
            _price(row.get("low")) or "",
            _price(row.get("close")) or "",
        )
    logger.info(
        "SCAN candle_summary %s bar_count=%s last_close=%s highest_high=%s lowest_low=%s trend=%s average_true_range=%s error=%s",
        symbol,
        summary.get("bar_count"),
        summary.get("last_close"),
        summary.get("highest_high"),
        summary.get("lowest_low"),
        summary.get("trend"),
        summary.get("average_true_range"),
        summary.get("error"),
    )


@dataclass(frozen=True)
class CandleRead:
    """Summary and strategy for the model, plus the chart bars kept off the prompt."""

    summary: dict[str, object]
    strategy_context: dict[str, object]
    chart_bars: tuple[dict[str, str], ...]


def _failed_read(error: str) -> CandleRead:
    summary = empty_candle_summary(error)
    return CandleRead(summary=summary, strategy_context=strategy.empty_context(error), chart_bars=())


def _read_symbol(rows: list[dict[str, object]]) -> CandleRead:
    """Summarize the last 30 clean bars and score them. Older bars warm up MACD."""

    clean = _clean_rows(rows)
    chart_rows = clean[-config.CANDLE_BARS :]
    summary = summarize_candles(chart_rows)
    context = strategy.build_context(summary, clean)
    return CandleRead(
        summary=summary,
        strategy_context=context,
        chart_bars=tuple(_public_bar(row) for row in chart_rows),
    )


def fetch_candle_summaries(client, symbols: tuple[str, ...]) -> dict[str, CandleRead]:
    """Fetch 5-minute bars, summarize the last 30, and score the strategy.

    The request is longer than 30 bars so MACD 12/26/9 is warmed up. A request
    failure logs the error and returns an empty read per symbol so the caller
    can continue with the quote alone.
    """

    from alpaca.data.enums import CryptoFeed
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    canonical = tuple(config.normalize_symbol(symbol) for symbol in symbols)
    history_bars = indicators.history_bar_count(config.CANDLE_BARS)
    lookback = timedelta(minutes=config.CANDLE_MINUTES * (history_bars + 2))
    try:
        bar_set = client.get_crypto_bars(
            CryptoBarsRequest(
                symbol_or_symbols=list(canonical),
                timeframe=TimeFrame(config.CANDLE_MINUTES, TimeFrameUnit.Minute),
                start=datetime.now(timezone.utc) - lookback,
            ),
            feed=CryptoFeed.US,
        )
        frame = bar_set.df if getattr(bar_set, "df", None) is not None else pandas.DataFrame()
    except Exception:
        logger.exception("SCAN candles failed")
        failed = {symbol: _failed_read("candle fetch failed") for symbol in canonical}
        for symbol, read in failed.items():
            _log_retrieved_candles(symbol, [], read.summary)
            logger.info("STRATEGY %s %s", symbol, json.dumps(read.strategy_context, sort_keys=True))
        return failed

    reads: dict[str, CandleRead] = {}
    for symbol in canonical:
        fetched = 0
        try:
            rows = _rows_from_frame(frame, symbol, history_bars)
            fetched = len(rows)
            reads[symbol] = _read_symbol(rows)
        except Exception:
            logger.exception("SCAN candle summary failed for %s", symbol)
            reads[symbol] = _failed_read("candle summary failed")
        read = reads[symbol]
        logger.info(
            "SCAN candle_history %s fetched=%s chart=%s",
            symbol,
            fetched,
            len(read.chart_bars),
        )
        _log_retrieved_candles(symbol, list(read.chart_bars), read.summary)
        logger.info("STRATEGY %s %s", symbol, json.dumps(read.strategy_context, sort_keys=True))
    return reads


def build_data_client(settings: config.Settings):
    """Market-data client. It has no order methods."""

    from alpaca.data.historical.crypto import CryptoHistoricalDataClient

    return CryptoHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)


def scan(client, symbols: tuple[str, ...] = config.ALLOWED_SYMBOLS) -> ScanResult:
    """Fetch latest quotes, latest trades, the candle summary, and the strategy."""

    from alpaca.data.enums import CryptoFeed
    from alpaca.data.requests import CryptoLatestQuoteRequest, CryptoLatestTradeRequest

    canonical = tuple(config.normalize_symbol(symbol) for symbol in symbols)
    quotes = {}
    trades = {}
    errors: list[str] = []
    try:
        quotes = client.get_crypto_latest_quote(
            CryptoLatestQuoteRequest(symbol_or_symbols=list(canonical)),
            feed=CryptoFeed.US,
        )
    except Exception as exc:
        errors.append(f"quotes: {type(exc).__name__}")
        logger.exception("SCAN quotes failed")
    try:
        trades = client.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=list(canonical)),
            feed=CryptoFeed.US,
        )
    except Exception as exc:
        errors.append(f"trades: {type(exc).__name__}")
        logger.exception("SCAN trades failed")
    reads = fetch_candle_summaries(client, canonical)

    views: list[SymbolView] = []
    for symbol in canonical:
        quote = quotes.get(symbol) if isinstance(quotes, dict) else None
        trade = trades.get(symbol) if isinstance(trades, dict) else None
        bid = _price(getattr(quote, "bid_price", None))
        ask = _price(getattr(quote, "ask_price", None))
        last = _price(getattr(trade, "price", None))
        quote_time = _as_utc(getattr(quote, "timestamp", None))
        symbol_errors = []
        if bid is None or ask is None:
            symbol_errors.append("missing bid or ask")
        read = reads.get(symbol) or _failed_read("candle summary missing")
        if read.summary.get("error"):
            symbol_errors.append(str(read.summary["error"]))
        if errors:
            symbol_errors.extend(errors)
        views.append(
            SymbolView(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last,
                quote_time=quote_time,
                candle_summary=read.summary,
                strategy=read.strategy_context,
                chart_bars=read.chart_bars,
                error="; ".join(symbol_errors) or None,
            )
        )
    logger.info("SCAN symbols=%s errors=%s", ",".join(canonical), len(errors))
    return ScanResult(views=tuple(views))
