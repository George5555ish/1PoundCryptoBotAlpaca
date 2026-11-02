"""Quotes and 5-minute bars for the multi-strategy equity universe.

Uses the IEX stock feed. The crypto scanner is left unchanged. Failed
fetches are logged and that symbol continues without a quote or candles.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas

import config
import indicators
import scanner
import strategy


logger = logging.getLogger("equity_scan")

EVAL_BARS = 80


def build_stock_client(settings: config.Settings):
    """Stock market-data client. It has no order methods."""

    from alpaca.data.historical.stock import StockHistoricalDataClient

    return StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)


def _rows_from_frame(frame: pandas.DataFrame, symbol: str, limit: int) -> list[dict[str, object]]:
    if frame is None or frame.empty:
        return []
    names = list(frame.index.names or [])
    if "symbol" not in names:
        return []
    try:
        sliced = frame.xs(symbol, level="symbol")
    except KeyError:
        return []
    rows: list[dict[str, object]] = []
    for timestamp, row in sliced.tail(limit).iterrows():
        rows.append(
            {
                "timestamp": timestamp,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        )
    return rows


def _clean_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    for row in rows:
        high = scanner._price(row.get("high"))
        low = scanner._price(row.get("low"))
        close = scanner._price(row.get("close"))
        if high is None or low is None or close is None:
            continue
        if float(high) < float(low):
            continue
        volume = row.get("volume")
        try:
            shares = str(volume) if volume not in {None, ""} else None
        except Exception:
            shares = None
        cleaned.append(
            {
                "timestamp": row.get("timestamp"),
                "open": scanner._price(row.get("open")) or close,
                "high": high,
                "low": low,
                "close": close,
                "volume": shares,
            }
        )
    return cleaned


def _public_bar(row: dict[str, object]) -> dict[str, object]:
    bar = scanner._public_bar(row)
    if row.get("volume") is not None:
        bar["volume"] = str(row.get("volume"))
    return bar


def scan(client, symbols: tuple[str, ...] = config.MULTI_STRATEGY_SYMBOLS) -> scanner.ScanResult:
    """Fetch IEX snapshots and 5-minute bars for the equity tester list."""

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    canonical = tuple(config.normalize_symbol(symbol) for symbol in symbols)
    now = datetime.now(timezone.utc)
    errors: list[str] = []
    snapshots: dict[str, object] = {}
    try:
        snapshots = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=list(canonical), feed=DataFeed.IEX)
        )
    except Exception as exc:
        errors.append(f"snapshot: {type(exc).__name__}")
        logger.exception("EQUITY_SCAN snapshot failed")

    history_bars = max(indicators.history_bar_count(config.CANDLE_BARS), EVAL_BARS)
    frame = pandas.DataFrame()
    try:
        bar_set = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=list(canonical),
                timeframe=TimeFrame(config.CANDLE_MINUTES, TimeFrameUnit.Minute),
                start=now - timedelta(days=5),
                feed=DataFeed.IEX,
            )
        )
        frame = bar_set.df if getattr(bar_set, "df", None) is not None else pandas.DataFrame()
    except Exception as exc:
        errors.append(f"bars: {type(exc).__name__}")
        logger.exception("EQUITY_SCAN bars failed")

    views: list[scanner.SymbolView] = []
    for symbol in canonical:
        snapshot = snapshots.get(symbol) if isinstance(snapshots, dict) else None
        quote = getattr(snapshot, "latest_quote", None)
        trade = getattr(snapshot, "latest_trade", None)
        bid = scanner._price(getattr(quote, "bid_price", None) or getattr(quote, "bp", None))
        ask = scanner._price(getattr(quote, "ask_price", None) or getattr(quote, "ap", None))
        last = scanner._price(getattr(trade, "price", None) or getattr(trade, "p", None))
        quote_time = scanner._as_utc(getattr(quote, "timestamp", None) or getattr(quote, "t", None))
        symbol_errors: list[str] = []
        if bid is None or ask is None:
            symbol_errors.append("missing bid or ask")
        fetched = 0
        try:
            rows = _clean_rows(_rows_from_frame(frame, symbol, history_bars))
            fetched = len(rows)
            chart_rows = rows[-config.CANDLE_BARS :]
            eval_rows = rows[-EVAL_BARS :]
            summary = scanner.summarize_candles(chart_rows)
            context = strategy.build_context(summary, rows)
            chart_bars = tuple(_public_bar(row) for row in chart_rows)
            eval_bars = tuple(_public_bar(row) for row in eval_rows)
        except Exception:
            logger.exception("EQUITY_SCAN candle summary failed for %s", symbol)
            failed = scanner._failed_read("candle summary failed")
            summary = failed.summary
            context = failed.strategy_context
            chart_bars = ()
            eval_bars = ()
            symbol_errors.append("candle summary failed")
        if summary.get("error"):
            symbol_errors.append(str(summary["error"]))
        if errors:
            symbol_errors.extend(errors)
        logger.info(
            "EQUITY_SCAN %s fetched=%s chart=%s eval=%s bid=%s ask=%s",
            symbol,
            fetched,
            len(chart_bars),
            len(eval_bars),
            bid,
            ask,
        )
        scanner._log_retrieved_candles(symbol, summary)
        logger.info("STRATEGY %s %s", symbol, json.dumps(context, sort_keys=True))
        views.append(
            scanner.SymbolView(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last,
                quote_time=quote_time,
                candle_summary=summary,
                strategy=context,
                chart_bars=chart_bars,
                eval_bars=eval_bars,
                error="; ".join(symbol_errors) or None,
            )
        )
    logger.info("EQUITY_SCAN symbols=%s errors=%s", ",".join(canonical), len(errors))
    return scanner.ScanResult(views=tuple(views))
