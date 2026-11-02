"""One cycle of the multi-strategy paper tester.

Each enabled strategy is a pure-Python rule. Enter and exit suggestions go
through ``risk_gate.evaluate``. Nothing here sizes an order or calls the
broker. ``main._commit`` is the only path that can send after a log row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import pandas

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


def cash_session_open(now: datetime) -> bool:
    """Regular US cash hours, 09:30–16:00 America/New_York, weekdays only."""

    stamp = pandas.Timestamp(now)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    eastern = stamp.tz_convert("America/New_York")
    if int(eastern.dayofweek) >= 5:
        return False
    minutes = int(eastern.hour) * 60 + int(eastern.minute)
    return (9 * 60 + 30) <= minutes < (16 * 60)


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
    session: bool,
    summary: dict[str, int],
    rows: list[dict[str, object]],
    webhook_url: str | None,
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
                "session": "open" if session else "closed",
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


def run_cycle(
    trading,
    data_client,
    decisions: decision_log.DecisionLog,
    settings: config.Settings,
    allow_send: bool,
    strategy_ids: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Scan the equity universe and feed the first signal per symbol through the gate."""

    enabled = strategies.enabled_ids(strategy_ids)
    now = datetime.now(timezone.utc)
    account = execution.fetch_account(trading)
    day_start = decisions.day_start_equity(now, account.equity)
    book = execution.fetch_book(trading, include_equities=True)
    session = cash_session_open(now)
    try:
        pnl = Decimal(account.equity) - Decimal(day_start)
    except Exception:
        pnl = Decimal("0")
    logger.info("MODE %s", "paper-send" if allow_send else "dry-run")
    logger.info(
        "MULTI_CYCLE equity=%s day_start=%s positions=%s session=%s strategies=%s",
        account.equity,
        day_start,
        ",".join(f"{item.symbol}:{item.side}" for item in book.positions) or "flat",
        "open" if session else "closed",
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
        summary = {"scanned": 0, "signals": 0, "approved": 0, "rejected": len(repairs)}
        logger.info("MULTI_SUMMARY %s", summary)
        _flush_cycle(
            now=now,
            account=account,
            day_start=day_start,
            pnl=pnl,
            allow_send=allow_send,
            session=session,
            summary=summary,
            rows=cycle_rows,
            webhook_url=settings.n8n_webhook_url,
        )
        return summary

    try:
        scan = equity_scan.scan(data_client)
    except Exception:
        logger.exception("EQUITY_SCAN failed")
        scan = scanner.ScanResult(views=())

    closed_any = False
    for position in book.positions:
        stop, target = decisions.latest_entry_levels(position.symbol)
        view = scan.for_symbol(position.symbol)
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
    if closed_any:
        summary = {"scanned": len(scan.views), "signals": 0, "approved": 1, "rejected": 0}
        logger.info("MULTI_SUMMARY %s", summary)
        _flush_cycle(
            now=now,
            account=account,
            day_start=day_start,
            pnl=pnl,
            allow_send=allow_send,
            session=session,
            summary=summary,
            rows=cycle_rows,
            webhook_url=settings.n8n_webhook_url,
        )
        return summary

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
        session_blocked = payload.get("action") == "enter" and not session
        if session_blocked:
            payload = {
                **payload,
                "action": "hold",
                "reason": f"{payload.get('reason')}; US cash session is closed",
            }
        suggestion = suggestion_from(symbol, payload)
        context = bot._context(now, account, day_start, book, scanner.ScanResult(views=(view,)), symbol)
        context["strategy_id"] = payload.get("strategy_id")
        context["strategy_action"] = payload.get("action")
        context["strategy_reason"] = payload.get("reason")
        if session_blocked:
            decision = risk_gate.RiskDecision(
                verdict=risk_gate.VERDICT_NO_TRADE,
                approved=False,
                reason="US cash session is closed",
                model_reason=str(payload.get("reason") or ""),
                symbol=symbol,
                stop_price=suggestion.stop_price,
                target_price=suggestion.target_price,
            )
        else:
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
        if decision.approved:
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

    summary = {
        "scanned": len(scan.views),
        "signals": signals,
        "approved": approved,
        "rejected": rejected,
    }
    logger.info(
        "MULTI_SUMMARY scanned=%s signals=%s approved=%s rejected=%s session=%s",
        summary["scanned"],
        summary["signals"],
        summary["approved"],
        summary["rejected"],
        "open" if session else "closed",
    )
    _flush_cycle(
        now=now,
        account=account,
        day_start=day_start,
        pnl=pnl,
        allow_send=allow_send,
        session=session,
        summary=summary,
        rows=cycle_rows,
        webhook_url=settings.n8n_webhook_url,
    )
    return summary
