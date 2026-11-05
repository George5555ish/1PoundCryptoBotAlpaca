"""One cycle of the multi-strategy paper tester.

Each enabled strategy is a pure-Python rule. Enter and exit suggestions go
through ``risk_gate.evaluate``. Nothing here sizes an order or calls the
broker. ``main._commit`` is the only path that can send after a log row.

US cash hours are 09:30–16:00 America/New_York, weekdays. Stock brackets are
DAY orders, so they die at the close. That drives three modes:

- ``open``: scan, enter, and review as usual.
- ``flatten``: last 20 minutes of the session, an early broker close, or
  leftover equity after the bell. No new entries. Close every stock/ETF
  we still hold, then retry until the book is flat.
- ``closed``: nights, weekends, and holidays with no leftover equity. No
  scan and no orders. One heartbeat webhook, then sleep until the next open.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import ai_decision
import config
import decision_log
import execution
import main as bot
import notify
import positions
import risk_gate
import scanner
import strategies

import equity_scan


logger = logging.getLogger("multi_runner")

MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTES = 9 * 60 + 30
SESSION_CLOSE_MINUTES = 16 * 60
FLATTEN_MINUTES_BEFORE_CLOSE = 20
CLOSED_HEARTBEAT_SECONDS = 1800
WAKE_BEFORE_OPEN_SECONDS = 30


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _eastern(now: datetime) -> datetime:
    return _aware(now).astimezone(MARKET_TZ)


def cash_session_open(now: datetime) -> bool:
    """Regular US cash hours, 09:30–16:00 America/New_York, weekdays only."""

    eastern = _eastern(now)
    if eastern.weekday() >= 5:
        return False
    minutes = eastern.hour * 60 + eastern.minute
    return SESSION_OPEN_MINUTES <= minutes < SESSION_CLOSE_MINUTES


def flatten_window(now: datetime) -> bool:
    """Last 20 minutes of a weekday cash session, before the bell."""

    eastern = _eastern(now)
    if eastern.weekday() >= 5:
        return False
    minutes = eastern.hour * 60 + eastern.minute
    start = SESSION_CLOSE_MINUTES - FLATTEN_MINUTES_BEFORE_CLOSE
    return start <= minutes < SESSION_CLOSE_MINUTES


def next_cash_open(now: datetime) -> datetime:
    """Next 09:30 America/New_York on a weekday, in UTC."""

    eastern = _eastern(now)
    cursor = eastern.replace(hour=9, minute=30, second=0, microsecond=0)
    if eastern.weekday() >= 5 or eastern >= cursor:
        cursor = (eastern + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
        while cursor.weekday() >= 5:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    return cursor.astimezone(timezone.utc)


def session_status(
    now: datetime,
    broker_open: bool | None = None,
    *,
    holding_equity: bool = False,
) -> str:
    """``open``, ``flatten``, or ``closed``.

    Flatten wins inside the pre-close window even if the broker clock has
    already flipped. Leftover stock/ETF positions after the bell or on an
    early close also flatten instead of idling overnight.
    """

    if flatten_window(now):
        return "flatten"
    calendar_open = cash_session_open(now)
    if holding_equity and (not calendar_open or broker_open is False):
        return "flatten"
    if not calendar_open or broker_open is False:
        return "closed"
    return "open"


def cycle_sleep_seconds(now: datetime, interval: int, status: str) -> int:
    """How long the loop should wait after this cycle."""

    wait = max(config.MIN_LOOP_INTERVAL_SECONDS, int(interval))
    if status == "flatten":
        if flatten_window(now) or cash_session_open(now):
            return config.MIN_LOOP_INTERVAL_SECONDS
        return CLOSED_HEARTBEAT_SECONDS
    if status != "closed":
        return wait
    remaining = int((next_cash_open(now) - _aware(now)).total_seconds()) - WAKE_BEFORE_OPEN_SECONDS
    if remaining <= 0:
        return config.MIN_LOOP_INTERVAL_SECONDS
    return max(config.MIN_LOOP_INTERVAL_SECONDS, min(CLOSED_HEARTBEAT_SECONDS, remaining))


def _price_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number.quantize(config.CENT, rounding=ROUND_HALF_UP), "f")


def suggestion_from(symbol: str, payload: dict[str, object]) -> ai_decision.AiSuggestion:
    action = str(payload.get("action") or "hold").strip().lower()
    direction = payload.get("direction")
    direction_text = direction.strip().lower() if isinstance(direction, str) and direction.strip() else None
    return ai_decision.AiSuggestion(
        action=action if action in {"enter", "exit", "hold"} else "hold",
        symbol=symbol,
        direction=direction_text if direction_text in {"long", "short"} else None,
        stop_price=_price_text(payload.get("stop_price")),
        target_price=_price_text(payload.get("target_price")),
        reason=str(payload.get("reason") or ""),
        valid=True,
        parse_error=None,
    )


def _candles(view: scanner.SymbolView) -> list[dict[str, object]]:
    source = view.eval_bars or view.chart_bars
    return [dict(bar) for bar in source]


def first_signal(
    symbol: str,
    view: scanner.SymbolView,
    strategy_ids: tuple[str, ...],
    held: bool,
) -> dict[str, object] | None:
    """Return the first non-hold from the enabled list. Exit is ignored when flat."""

    context = {
        "symbol": symbol,
        "candle_summary": view.candle_summary,
        "strategy": view.strategy,
        "held": held,
    }
    candles = _candles(view)
    for strategy_id in strategy_ids:
        evaluate = strategies.load_evaluate(strategy_id)
        payload = evaluate(symbol, candles, context)
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload["strategy_id"] = payload.get("strategy_id") or strategy_id
        action = str(payload.get("action") or "hold").strip().lower()
        if action == "hold":
            continue
        if action == "exit" and not held:
            continue
        if action == "enter" and held:
            continue
        return payload
    return None


def _chart_scan(view: scanner.SymbolView | None) -> scanner.ScanResult | None:
    if view is None:
        return None
    return scanner.ScanResult(views=(view,))


def _flush_cycle(
    *,
    now: datetime,
    account,
    day_start: str,
    pnl: Decimal,
    allow_send: bool,
    session: str,
    summary: dict[str, int | str | None],
    rows: list[dict[str, object]],
    webhook_url: str | None,
    next_open: datetime | None = None,
) -> None:
    """One n8n POST for the whole cycle. SQLite rows are already written."""

    try:
        notify.post_cycle(
            {
                "event_type": "multi_cycle",
                "timestamp_utc": now.isoformat(),
                "mode": "live" if allow_send else "dry-run",
                "equity": account.equity,
                "day_start_equity": day_start,
                "day_pnl": format(pnl, "f"),
                "session": session,
                "next_open": None if next_open is None else next_open.isoformat(),
                "scanned": summary.get("scanned", 0),
                "signals": summary.get("signals", 0),
                "approved": summary.get("approved", 0),
                "rejected": summary.get("rejected", 0),
                "strategy_count": len(rows),
                "strategy_trades": rows,
            },
            webhook_url,
        )
    except Exception:
        logger.exception("N8N_WEBHOOK failed")


def _broker_open(trading) -> bool | None:
    try:
        return execution.fetch_clock(trading).is_open
    except Exception:
        logger.exception("CLOCK failed")
        return None


def run_cycle(
    trading,
    data_client,
    decisions: decision_log.DecisionLog,
    settings: config.Settings,
    allow_send: bool,
    strategy_ids: tuple[str, ...] | None = None,
    loop_interval: int | None = None,
) -> dict[str, object]:
    """Scan the equity universe and feed the first signal per symbol through the gate."""

    enabled = strategies.enabled_ids(strategy_ids)
    now = datetime.now(timezone.utc)
    interval = loop_interval if loop_interval is not None else settings.loop_interval_seconds
    account = execution.fetch_account(trading)
    day_start = decisions.day_start_equity(now, account.equity)
    book = execution.fetch_book(trading, include_equities=True)
    broker_open = _broker_open(trading)
    holding_equity = any(config.is_multi_strategy_symbol(item.symbol) for item in book.positions)
    status = session_status(now, broker_open, holding_equity=holding_equity)
    next_open = next_cash_open(now)
    try:
        pnl = Decimal(account.equity) - Decimal(day_start)
    except Exception:
        pnl = Decimal("0")
    logger.info("MODE %s", "paper-send" if allow_send else "dry-run")
    logger.info(
        "MULTI_CYCLE equity=%s day_start=%s positions=%s session=%s next_open=%s strategies=%s",
        account.equity,
        day_start,
        ",".join(f"{item.symbol}:{item.side}" for item in book.positions) or "flat",
        status,
        next_open.isoformat(),
        ",".join(enabled),
    )
    logger.info("SOFT_TARGET %s", config.format_soft_target_note(pnl, settings.pounds_per_usd))
    bot.SNAPSHOT.update(
        as_of=now.isoformat(),
        mode="paper-send" if allow_send else "dry-run",
        equity=account.equity,
        day_start_equity=day_start,
        day_pnl=format(pnl, "f"),
        positions=[
            {
                "symbol": position.symbol,
                "qty": format(position.qty, "f"),
                "unrealized_pl": position.unrealized_pl,
            }
            for position in book.positions
        ],
        blocked=account.trading_blocked,
    )

    cycle_rows: list[dict[str, object]] = []

    def finish(summary: dict[str, int]) -> dict[str, object]:
        payload = {
            **summary,
            "session": status,
            "sleep_seconds": cycle_sleep_seconds(now, interval, status),
            "next_open": next_open.isoformat() if status == "closed" else None,
        }
        logger.info(
            "MULTI_SUMMARY scanned=%s signals=%s approved=%s rejected=%s session=%s sleep=%s",
            payload.get("scanned", 0),
            payload.get("signals", 0),
            payload.get("approved", 0),
            payload.get("rejected", 0),
            status,
            payload["sleep_seconds"],
        )
        _flush_cycle(
            now=now,
            account=account,
            day_start=day_start,
            pnl=pnl,
            allow_send=allow_send,
            session=status,
            summary=payload,
            rows=cycle_rows,
            webhook_url=settings.n8n_webhook_url,
            next_open=next_open if status == "closed" else None,
        )
        return payload

    if status == "closed":
        logger.info(
            "SESSION_IDLE next_open=%s positions=%s",
            next_open.isoformat(),
            len(book.positions),
        )
        return finish({"scanned": 0, "signals": 0, "approved": 0, "rejected": 0})

    if status == "flatten":
        held = ",".join(f"{item.symbol}:{item.side}" for item in book.positions) or "flat"
        logger.warning("FLATTEN_FAILSAFE closing stock/ETF positions before the bell symbols=%s", held)

    repairs = risk_gate.plan_repairs(book.positions, book.orders)
    if repairs:
        if account.trading_blocked:
            logger.critical("REPAIR_BLOCKED trading is blocked; book left unchanged")
        for action in repairs:
            decision = bot._repair_decision(action)
            context = {
                "as_of": now.isoformat(),
                "equity": account.equity,
                "day_start_equity": day_start,
                "strategy_id": "repair",
                "symbol": action.symbol,
                "repair": action.reason,
            }
            cycle_rows.extend(
                bot._commit(
                    decisions,
                    trading,
                    now,
                    context,
                    None,
                    decision,
                    allow_send and not account.trading_blocked,
                    decision,
                    book,
                    webhook_url=None,
                    action_type="position_review",
                    position_decision="close" if decision.order_kind != risk_gate.ORDER_KIND_CANCEL else None,
                    settings=settings,
                    post_webhook=False,
                )
            )
        if status != "flatten":
            return finish({"scanned": 0, "signals": 0, "approved": 0, "rejected": len(repairs)})
        book = execution.fetch_book(trading, include_equities=True)

    try:
        scan = equity_scan.scan(data_client)
    except Exception:
        logger.exception("EQUITY_SCAN failed")
        scan = scanner.ScanResult(views=())

    closed_any = False
    for position in book.positions:
        if status == "flatten" and not config.is_multi_strategy_symbol(position.symbol):
            continue
        stop, target = decisions.latest_entry_levels(position.symbol)
        view = scan.for_symbol(position.symbol)
        if status == "flatten":
            review = positions.PositionReview(
                symbol=position.symbol,
                qty=format(position.qty, "f"),
                current_price=position.current_price,
                unrealized_pl=position.unrealized_pl,
                decision="close",
                reason="flatten before the US cash close; DAY brackets expire",
                trend=bot._trend_for(scan, position.symbol),
            )
            strategy_exit = None
        else:
            review = positions.review_position(
                symbol=position.symbol,
                qty=format(position.qty, "f"),
                current_price=position.current_price,
                unrealized_pl=position.unrealized_pl,
                trend=bot._trend_for(scan, position.symbol),
                stop_price=stop,
                target_price=target,
                side=position.side,
            )
            strategy_exit = None
            if review.decision != "close" and view is not None:
                strategy_exit = first_signal(position.symbol, view, enabled, held=True)
                if strategy_exit is not None and strategy_exit.get("action") == "exit":
                    review = positions.PositionReview(
                        symbol=position.symbol,
                        qty=format(position.qty, "f"),
                        current_price=position.current_price,
                        unrealized_pl=position.unrealized_pl,
                        decision="close",
                        reason=str(strategy_exit.get("reason") or "strategy exit"),
                        trend=review.trend,
                    )
        context = bot._position_context(now, account, day_start, book, review, stop, target)
        context["strategy_id"] = None if strategy_exit is None else strategy_exit.get("strategy_id")
        if review.decision != "close":
            cycle_rows.extend(
                bot._log_position_hold(
                    decisions,
                    now,
                    context,
                    review,
                    settings,
                    allow_send,
                    post_webhook=False,
                )
            )
            continue
        logger.info(
            "POSITION_REVIEW symbol=%s decision=close price=%s reason=%s",
            review.symbol,
            review.current_price,
            review.reason,
        )
        close_side = "buy" if position.side == "short" else "sell"
        decision = risk_gate.approve_full_close(position.symbol, position.qty, review.reason, side=close_side)
        refreshed: risk_gate.RiskDecision | None = decision
        fresh_book = book
        if allow_send:
            fresh_book = execution.fetch_book(trading, include_equities=True)
            fresh = next((item for item in fresh_book.positions if item.symbol == position.symbol), None)
            if fresh is None:
                refreshed = risk_gate.RiskDecision(
                    verdict=risk_gate.VERDICT_NO_TRADE,
                    approved=False,
                    reason="position was already closed",
                    model_reason="",
                    symbol=position.symbol,
                )
            else:
                refreshed = risk_gate.approve_full_close(
                    fresh.symbol, fresh.qty, review.reason, side=close_side
                )
        cycle_rows.extend(
            bot._commit(
                decisions,
                trading,
                now,
                context,
                None if strategy_exit is None else suggestion_from(position.symbol, strategy_exit),
                decision,
                allow_send,
                refreshed,
                fresh_book,
                _chart_scan(view),
                webhook_url=None,
                action_type="position_review",
                position_decision="close",
                position_price=review.current_price,
                unrealized_pl=review.unrealized_pl,
                settings=settings,
                post_webhook=False,
            )
        )
        closed_any = True
    if status == "flatten":
        leftover: list[str] = []
        try:
            leftover = [
                f"{item.symbol}:{item.side}"
                for item in execution.fetch_book(trading, include_equities=True).positions
                if config.is_multi_strategy_symbol(item.symbol)
            ]
        except Exception:
            logger.exception("FLATTEN_INCOMPLETE could not re-read the book")
        if leftover:
            logger.critical("FLATTEN_INCOMPLETE still open %s; retrying", ",".join(leftover))
        else:
            logger.info("FLATTEN_CLEAR book is flat before the bell")
    if closed_any:
        return finish({"scanned": len(scan.views), "signals": 0, "approved": 1, "rejected": 0})
    if status == "flatten":
        return finish({"scanned": len(scan.views), "signals": 0, "approved": 0, "rejected": 0})

    held = {position.symbol for position in book.positions}
    pending = set(book.pending_symbols)
    signals = 0
    approved = 0
    rejected = 0
    for symbol in config.MULTI_STRATEGY_SYMBOLS:
        if symbol in held or symbol in pending:
            continue
        view = scan.for_symbol(symbol)
        if view is None:
            continue
        payload = first_signal(symbol, view, enabled, held=False)
        if payload is None:
            continue
        signals += 1
        suggestion = suggestion_from(symbol, payload)
        context = bot._context(now, account, day_start, book, scanner.ScanResult(views=(view,)), symbol)
        context["strategy_id"] = payload.get("strategy_id")
        context["strategy_action"] = payload.get("action")
        context["strategy_reason"] = payload.get("reason")
        decision = risk_gate.evaluate(bot._request_from(suggestion, account, day_start, book, view, now))
        if decision.approved:
            approved += 1
        else:
            rejected += 1
        logger.info(
            "MULTI_SIGNAL symbol=%s strategy=%s action=%s approved=%s reason=%s",
            symbol,
            payload.get("strategy_id"),
            payload.get("action"),
            decision.approved,
            decision.reason,
        )
        refreshed = decision
        fresh_book = book
        if allow_send and decision.approved:
            fresh_book = execution.fetch_book(trading, include_equities=True)
            fresh_scan = equity_scan.scan(data_client)
            fresh_view = fresh_scan.for_symbol(symbol)
            refreshed = risk_gate.evaluate(
                bot._request_from(suggestion, account, day_start, fresh_book, fresh_view, datetime.now(timezone.utc))
            )
        before = len(cycle_rows)
        cycle_rows.extend(
            bot._commit(
                decisions,
                trading,
                now,
                context,
                suggestion,
                decision,
                allow_send,
                refreshed,
                fresh_book,
                _chart_scan(view),
                webhook_url=None,
                action_type="entry_evaluation",
                settings=settings,
                post_webhook=False,
            )
        )
        filled = {str(row.get("final_action") or "") for row in cycle_rows[before:]}
        occupy = decision.approved and ("submitted" in filled or "dry_run" in filled)
        if occupy:
            held.add(symbol)
            book = execution.PositionBook(
                positions=book.positions
                + (
                    risk_gate.OpenPosition(
                        symbol=symbol,
                        qty=Decimal(decision.qty or "0") or Decimal("1"),
                        side="short" if decision.side == "sell" else "long",
                    ),
                )
                if decision.qty
                else book.positions,
                orders=book.orders,
                other_position_open=book.other_position_open,
                pending_entry_while_flat=book.pending_entry_while_flat,
                pending_symbols=book.pending_symbols,
            )

    return finish(
        {
            "scanned": len(scan.views),
            "signals": signals,
            "approved": approved,
            "rejected": rejected,
        }
    )
