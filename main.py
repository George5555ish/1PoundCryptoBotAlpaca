"""One cycle of the crypto paper bot.

Flow, every cycle:

1. Read the paper account and freeze UTC-day-start equity on the first look.
2. Repair a book that already breaks a rule (too many longs, a long with no
   protective sell, a stray order). Each repair is logged before it is sent.
3. Review every remaining open position. Close it when the trend is lower
   lows, the tracked target is hit, or the tracked stop is hit. Otherwise
   log a hold. A close is logged before it is sent.
4. If nothing was closed, ask the model for one suggestion, run the risk
   gate, and log the result before any broker call.
5. With ``--send-orders``, run the gate a second time on a fresh read. If
   the approved order changed, store ``send_aborted`` and do not send.
   The default is a dry run, which logs the decision and sends nothing.

Nothing in this file sizes a position. ``execution.submit`` is called from
``_commit`` only, and only after an ``approved_awaiting_send`` row exists.
The default is a dry run. ``--once`` is a single cycle and still does not
send unless ``--send-orders`` is also present.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import ai_decision
import charts
import config
import decision_log
import execution
import notify
import positions
import risk_gate
import scanner


logger = logging.getLogger("main")


def configure_logging(log_dir) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream)


def _suggestion_payload(suggestion: ai_decision.AiSuggestion) -> dict[str, object]:
    return json.loads(suggestion.log_line())


def _risk_payload(decision: risk_gate.RiskDecision) -> dict[str, object]:
    return json.loads(decision.log_line())


def _context(now, account, day_start, book, scan, focused: str | None) -> dict[str, object]:
    try:
        pnl = Decimal(account.equity) - Decimal(day_start)
    except Exception:
        pnl = None
    view = scan.for_symbol(focused) if focused else None
    return {
        "as_of": now.isoformat(),
        "symbol": focused,
        "price": None
        if view is None
        else {
            "bid": view.bid,
            "ask": view.ask,
            "last": view.last,
            "quote_time": None if view.quote_time is None else view.quote_time.isoformat(),
            "candle_summary": view.candle_summary,
            "strategy": view.strategy,
        },
        "symbols": {
            item.symbol: {
                "bid": item.bid,
                "ask": item.ask,
                "last": item.last,
                "quote_time": None if item.quote_time is None else item.quote_time.isoformat(),
                "candle_summary": item.candle_summary,
                "strategy": item.strategy,
                "error": item.error,
            }
            for item in scan.views
        },
        "equity": account.equity,
        "day_start_equity": day_start,
        "buying_power": account.buying_power,
        "day_pnl_usd": None if pnl is None else format(pnl, "f"),
        "daily_loss_halted": risk_gate.daily_loss_halted(account.equity, day_start),
        "positions": [
            {
                "symbol": position.symbol,
                "qty": format(position.qty, "f"),
                "side": position.side,
                "current_price": position.current_price,
                "unrealized_pl": position.unrealized_pl,
            }
            for position in book.positions
        ],
        "other_position_open": book.other_position_open,
        "pending_entry_while_flat": book.pending_entry_while_flat,
        "pending_symbols": list(book.pending_symbols),
        "trading_blocked": account.trading_blocked,
    }


def _request_from(suggestion, account, day_start, book, view, now) -> risk_gate.GateRequest:
    return risk_gate.GateRequest(
        action=suggestion.action,
        symbol=suggestion.symbol,
        direction=suggestion.direction,
        stop_price=suggestion.stop_price,
        target_price=suggestion.target_price,
        reason=suggestion.reason,
        suggestion_valid=suggestion.valid,
        parse_error=suggestion.parse_error,
        bid=None if view is None else view.bid,
        ask=None if view is None else view.ask,
        quote_time=None if view is None else view.quote_time,
        equity=account.equity,
        day_start_equity=day_start,
        buying_power=account.buying_power,
        open_positions=book.positions,
        pending_entry_while_flat=book.pending_entry_while_flat,
        pending_symbols=book.pending_symbols,
        other_position_open=book.other_position_open,
        trading_blocked=account.trading_blocked,
        now=now,
    )


def _save_charts(scan: scanner.ScanResult | None, when: datetime) -> list[str]:
    """Draw the evaluated symbols after the decision row exists.

    A missing series or a drawing error is logged and skipped. The chart is
    not an input to the risk gate and it is not sent to the model. The return
    value is the saved file names, for the n8n payload.
    """

    if scan is None:
        return []
    directory = config.data_directory() / "charts"
    saved: list[str] = []
    for view in scan.views:
        try:
            path = charts.save_snapshot(view.symbol, view.chart_bars, view.strategy, when, directory)
        except Exception:
            logger.exception("CHART failed symbol=%s", view.symbol)
            continue
        if path is not None:
            saved.append(path.name)
    return saved


def _trade_text(trade: dict[str, object] | None, key: str) -> str | None:
    if trade is None or trade.get(key) is None:
        return None
    text = str(trade.get(key))
    return text or None


def _trade_notice(
    decision: risk_gate.RiskDecision,
    result: execution.OrderResult,
    context: dict[str, object],
) -> dict[str, object] | None:
    """Details for a submitted entry or close. A failed send has no event."""

    if result.status == "protection_failed_open":
        return None
    if result.status == "protection_failed_closed":
        event_type = "trade_closed"
        reason = result.message
    elif decision.order_kind == risk_gate.ORDER_KIND_ENTRY:
        event_type = "trade_opened"
        reason = decision.reason
    elif decision.order_kind in {risk_gate.ORDER_KIND_EXIT, risk_gate.ORDER_KIND_FULL_CLOSE}:
        event_type = "trade_closed"
        reason = decision.reason
    else:
        return None
    price = result.fill_price or context.get("current_price")
    if not price and decision.order_kind == risk_gate.ORDER_KIND_ENTRY:
        block = context.get("price")
        if isinstance(block, dict):
            price = block.get("ask") or block.get("last")
    return {
        "event_type": event_type,
        "symbol": decision.symbol,
        "side": decision.side,
        "qty": decision.qty,
        "price": price,
        "stop": decision.stop_price,
        "target": decision.target_price,
        "reason": reason,
        "description": notify.describe_trade(
            event_type=event_type,
            symbol=decision.symbol,
            side=decision.side,
            qty=decision.qty,
            price=price,
            stop=decision.stop_price,
            target=decision.target_price,
            reason=reason,
            mode="live",
            final_action="submitted",
        ),
        "order_id": result.order_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def _commit(
    decisions: decision_log.DecisionLog,
    trading,
    now: datetime,
    context: dict[str, object],
    suggestion: ai_decision.AiSuggestion | None,
    decision: risk_gate.RiskDecision,
    allow_send: bool,
    refreshed: risk_gate.RiskDecision | None,
    book: execution.PositionBook | None,
    scan: scanner.ScanResult | None = None,
    webhook_url: str | None = None,
    action_type: str = "entry_evaluation",
    position_decision: str | None = None,
    position_price: str | None = None,
    unrealized_pl: str | None = None,
    settings: config.Settings | None = None,
) -> None:
    """Write the audit row, draw the charts, then send only after an awaiting-send row.

    The n8n POST runs after the charts and after the final action is known.
    A webhook or email failure is logged and does not change this function's
    control flow. ``--send-orders`` is what sets ``allow_send``. Without it
    the row is ``dry_run`` and nothing is sent.
    """

    payload = None if suggestion is None else _suggestion_payload(suggestion)
    risk = _risk_payload(decision)
    saved_charts: list[str] = []
    stored_context = dict(context)
    stored_context["action_type"] = action_type

    def _store(**kwargs) -> int:
        row_id = decisions.write(**kwargs, action_type=action_type)
        saved_charts.extend(_save_charts(scan, now))
        return row_id

    def _notify(final_action: str, order_id: str | None, reason: str, trade: dict[str, object] | None = None) -> None:
        try:
            rows = notify.build_rows(
                timestamp=now,
                allow_send=allow_send,
                context=stored_context,
                suggestion=payload,
                verdict=decision.verdict,
                reason=reason,
                approved=decision.approved,
                final_action=final_action,
                order_id=order_id,
                chart_filenames=saved_charts,
                symbol=decision.symbol,
                risk_decision=risk,
                action_type=action_type,
                position_symbol=decision.symbol if position_decision else None,
                position_decision=position_decision,
                position_price=position_price,
                unrealized_pl=unrealized_pl,
                position_reason=reason if position_decision else None,
                trade_symbol=None if trade is None else str(trade.get("symbol") or ""),
                event_type=None if trade is None else str(trade.get("event_type") or ""),
                trade_side=_trade_text(trade, "side"),
                trade_qty=_trade_text(trade, "qty"),
                trade_price=_trade_text(trade, "price"),
                trade_stop=_trade_text(trade, "stop"),
                trade_target=_trade_text(trade, "target"),
                trade_description=_trade_text(trade, "description"),
                notify_email=None if trade is None or settings is None else settings.trade_notify_email,
            )
            notify.post_rows(rows, webhook_url)
            if trade is not None and settings is not None:
                notify.send_trade_email(
                    trade,
                    email=settings.trade_notify_email,
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    username=settings.smtp_user,
                    password=settings.smtp_password,
                    sender=settings.smtp_from,
                )
        except Exception:
            logger.exception("N8N_WEBHOOK failed")

    if not decision.approved:
        action = "no_trade" if decision.verdict == risk_gate.VERDICT_NO_TRADE else "rejected"
        _store(
            timestamp=now,
            market_context=stored_context,
            ai_suggestion=payload,
            risk_verdict=decision.verdict,
            risk_reason=decision.reason,
            risk_decision=risk,
            position_size=decision.qty,
            final_action=action,
            symbol=decision.symbol,
            order_kind=decision.order_kind,
        )
        _notify(action, None, decision.reason)
        return
    if not allow_send:
        logger.info("DRY_RUN would send %s %s qty=%s", decision.order_kind, decision.symbol, decision.qty)
        _store(
            timestamp=now,
            market_context=stored_context,
            ai_suggestion=payload,
            risk_verdict=decision.verdict,
            risk_reason=decision.reason,
            risk_decision=risk,
            position_size=decision.qty,
            final_action="dry_run",
            symbol=decision.symbol,
            order_kind=decision.order_kind,
        )
        _notify("dry_run", None, decision.reason)
        return
    if refreshed is None or not risk_gate.same_order(decision, refreshed):
        detail = None if refreshed is None else refreshed.log_line()
        _store(
            timestamp=now,
            market_context=stored_context,
            ai_suggestion=payload,
            risk_verdict=decision.verdict,
            risk_reason="second pass did not match the approved order",
            risk_decision=risk,
            position_size=decision.qty,
            final_action="send_aborted",
            symbol=decision.symbol,
            order_kind=decision.order_kind,
            detail=detail,
        )
        _notify("send_aborted", None, "second pass did not match the approved order")
        return

    row_id = _store(
        timestamp=now,
        market_context=stored_context,
        ai_suggestion=payload,
        risk_verdict=decision.verdict,
        risk_reason=decision.reason,
        risk_decision=risk,
        position_size=decision.qty,
        final_action="approved_awaiting_send",
        symbol=decision.symbol,
        order_kind=decision.order_kind,
    )
    try:
        result = execution.submit(trading, refreshed, allow_send=True, book=book)
    except Exception as exc:
        logger.exception("SEND_FAILED id=%s", row_id)
        decisions.complete(row_id, final_action="send_failed", order_id=None, detail=type(exc).__name__)
        _notify("send_failed", None, decision.reason)
        return
    final = "send_failed" if result.status == "protection_failed_open" else "submitted"
    if result.status.startswith("protection_failed"):
        logger.critical("PROTECTION %s", result.log_line())
    decisions.complete(row_id, final_action=final, order_id=result.order_id, detail=result.message)
    trade = _trade_notice(decision, result, stored_context) if final == "submitted" else None
    _notify(final, result.order_id, decision.reason, trade)


def _repair_decision(action: risk_gate.RepairAction) -> risk_gate.RiskDecision:
    if action.kind == risk_gate.ORDER_KIND_CANCEL:
        order = risk_gate.WorkingOrder(
            order_id=action.order_id or "",
            symbol=action.symbol,
            side=action.side or "buy",
        )
        return risk_gate.approve_cancel(order, action.reason)
    qty = Decimal(action.qty or "0")
    return risk_gate.approve_full_close(action.symbol, qty, action.reason)


def _trend_for(scan: scanner.ScanResult | None, symbol: str) -> str | None:
    if scan is None:
        return None
    view = scan.for_symbol(symbol)
    if view is None:
        return None
    summary = view.candle_summary if isinstance(view.candle_summary, dict) else {}
    trend = summary.get("trend")
    if isinstance(trend, str) and trend.strip():
        return trend.strip()
    strategy = view.strategy if isinstance(view.strategy, dict) else {}
    momentum = strategy.get("momentum_pullback")
    if isinstance(momentum, dict):
        trend = momentum.get("trend")
        if isinstance(trend, str) and trend.strip():
            return trend.strip()
    return None


def _position_context(now, account, day_start, book, review: positions.PositionReview, stop, target) -> dict[str, object]:
    return {
        "as_of": now.isoformat(),
        "action_type": "position_review",
        "equity": account.equity,
        "day_start_equity": day_start,
        "symbol": review.symbol,
        "current_price": review.current_price,
        "unrealized_pl": review.unrealized_pl,
        "decision": review.decision,
        "reason": review.reason,
        "trend": review.trend,
        "qty": review.qty,
        "stop_price": stop,
        "target_price": target,
        "positions": [
            {
                "symbol": position.symbol,
                "qty": format(position.qty, "f"),
                "current_price": position.current_price,
                "unrealized_pl": position.unrealized_pl,
            }
            for position in book.positions
        ],
    }


def _log_position_hold(
    decisions: decision_log.DecisionLog,
    now: datetime,
    context: dict[str, object],
    review: positions.PositionReview,
    settings: config.Settings,
    allow_send: bool,
) -> None:
    """Store a hold. No order is considered, so this never reaches the broker."""

    logger.info(
        "POSITION_REVIEW symbol=%s decision=hold price=%s unrealized_pl=%s reason=%s",
        review.symbol,
        review.current_price,
        review.unrealized_pl,
        review.reason,
    )
    risk = {
        "verdict": risk_gate.VERDICT_NO_TRADE,
        "approved": False,
        "reason": review.reason,
        "symbol": review.symbol,
    }
    try:
        decisions.write(
            timestamp=now,
            market_context=context,
            ai_suggestion=None,
            risk_verdict=risk_gate.VERDICT_NO_TRADE,
            risk_reason=review.reason,
            risk_decision=risk,
            position_size=review.qty,
            final_action="no_trade",
            symbol=review.symbol,
            order_kind=risk_gate.ORDER_KIND_NONE,
            action_type="position_review",
        )
    except Exception:
        logger.exception("POSITION_REVIEW log failed symbol=%s", review.symbol)
        return
    try:
        notify.post_rows(
            notify.build_rows(
                timestamp=now,
                allow_send=allow_send,
                context=context,
                suggestion=None,
                verdict=risk_gate.VERDICT_NO_TRADE,
                reason=review.reason,
                approved=False,
                final_action="no_trade",
                order_id=None,
                chart_filenames=[],
                symbol=review.symbol,
                risk_decision=risk,
                action_type="position_review",
                position_symbol=review.symbol,
                position_decision="hold",
                position_price=review.current_price,
                unrealized_pl=review.unrealized_pl,
                position_reason=review.reason,
            ),
            settings.n8n_webhook_url,
        )
    except Exception:
        logger.exception("N8N_WEBHOOK failed")


def _refreshed_close(trading, data_client, decisions, position, decision: risk_gate.RiskDecision):
    """Re-read the book and review. A hold on the second pass aborts the close."""

    fresh_book = execution.fetch_book(trading)
    fresh = next((item for item in fresh_book.positions if item.symbol == position.symbol), None)
    if fresh is None:
        aborted = risk_gate.RiskDecision(
            verdict=risk_gate.VERDICT_NO_TRADE,
            approved=False,
            reason="position was already closed",
            model_reason="",
            symbol=position.symbol,
        )
        return aborted, fresh_book
    try:
        fresh_scan = scanner.scan(data_client)
    except Exception:
        logger.exception("SCAN failed during close recheck symbol=%s", position.symbol)
        fresh_scan = None
    stop, target = decisions.latest_entry_levels(fresh.symbol)
    review = positions.review_position(
        symbol=fresh.symbol,
        qty=format(fresh.qty, "f"),
        current_price=fresh.current_price,
        unrealized_pl=fresh.unrealized_pl,
        trend=_trend_for(fresh_scan, fresh.symbol),
        stop_price=stop,
        target_price=target,
    )
    if review.decision != "close":
        aborted = risk_gate.RiskDecision(
            verdict=risk_gate.VERDICT_NO_TRADE,
            approved=False,
            reason=review.reason,
            model_reason="",
            symbol=fresh.symbol,
        )
        return aborted, fresh_book
    refreshed = risk_gate.approve_full_close(fresh.symbol, fresh.qty, review.reason)
    if not risk_gate.same_order(decision, refreshed):
        return refreshed, fresh_book
    return refreshed, fresh_book


def run_cycle(trading, data_client, decisions: decision_log.DecisionLog, settings: config.Settings, allow_send: bool) -> None:
    now = datetime.now(timezone.utc)
    account = execution.fetch_account(trading)
    day_start = decisions.day_start_equity(now, account.equity)
    book = execution.fetch_book(trading)
    try:
        pnl = Decimal(account.equity) - Decimal(day_start)
    except Exception:
        pnl = Decimal("0")
    logger.info("MODE %s", "paper-send" if allow_send else "dry-run")
    logger.info(
        "CYCLE equity=%s day_start=%s positions=%s other=%s pending=%s blocked=%s",
        account.equity,
        day_start,
        ",".join(position.symbol for position in book.positions) or "flat",
        book.other_position_open,
        book.pending_entry_while_flat,
        account.trading_blocked,
    )
    logger.info("SOFT_TARGET %s", config.format_soft_target_note(pnl, settings.pounds_per_usd))

    repairs = risk_gate.plan_repairs(book.positions, book.orders)
    if repairs:
        if account.trading_blocked:
            logger.critical("REPAIR_BLOCKED trading is blocked; book left unchanged")
        for action in repairs:
            decision = _repair_decision(action)
            context = {
                "as_of": now.isoformat(),
                "equity": account.equity,
                "day_start_equity": day_start,
                "positions": [
                    {"symbol": position.symbol, "qty": format(position.qty, "f")}
                    for position in book.positions
                ],
                "repair": action.reason,
            }
            if account.trading_blocked:
                decisions.write(
                    timestamp=now,
                    market_context={**context, "action_type": "position_review"},
                    ai_suggestion=None,
                    risk_verdict=risk_gate.VERDICT_REJECT,
                    risk_reason="trading is blocked at the broker",
                    risk_decision=_risk_payload(decision),
                    position_size=decision.qty,
                    final_action="rejected",
                    symbol=decision.symbol,
                    order_kind=decision.order_kind,
                    action_type="position_review",
                )
                try:
                    notify.post_rows(
                        notify.build_rows(
                            timestamp=now,
                            allow_send=allow_send,
                            context=context,
                            suggestion=None,
                            verdict=risk_gate.VERDICT_REJECT,
                            reason="trading is blocked at the broker",
                            approved=False,
                            final_action="rejected",
                            order_id=None,
                            chart_filenames=[],
                            symbol=decision.symbol,
                            risk_decision=_risk_payload(decision),
                            action_type="position_review",
                            position_symbol=decision.symbol,
                            position_decision="close" if decision.order_kind != risk_gate.ORDER_KIND_CANCEL else None,
                            position_reason=action.reason,
                        ),
                        settings.n8n_webhook_url,
                    )
                except Exception:
                    logger.exception("N8N_WEBHOOK failed")
                continue
            refreshed = decision
            if allow_send:
                fresh_book = execution.fetch_book(trading)
                fresh_repairs = risk_gate.plan_repairs(fresh_book.positions, fresh_book.orders)
                refreshed = next(
                    (
                        _repair_decision(item)
                        for item in fresh_repairs
                        if item.symbol == action.symbol and item.kind == action.kind
                    ),
                    None,
                )
                book = fresh_book
            _commit(
                decisions,
                trading,
                now,
                context,
                None,
                decision,
                allow_send,
                refreshed if allow_send else decision,
                book,
                webhook_url=settings.n8n_webhook_url,
                action_type="position_review",
                position_decision="close" if decision.order_kind != risk_gate.ORDER_KIND_CANCEL else None,
                settings=settings,
            )
        return

    try:
        scan = scanner.scan(data_client)
    except Exception:
        logger.exception("SCAN failed")
        scan = None

    closed_any = False
    for position in book.positions:
        stop, target = decisions.latest_entry_levels(position.symbol)
        review = positions.review_position(
            symbol=position.symbol,
            qty=format(position.qty, "f"),
            current_price=position.current_price,
            unrealized_pl=position.unrealized_pl,
            trend=_trend_for(scan, position.symbol),
            stop_price=stop,
            target_price=target,
        )
        context = _position_context(now, account, day_start, book, review, stop, target)
        if review.decision != "close":
            _log_position_hold(decisions, now, context, review, settings, allow_send)
            continue
        logger.info(
            "POSITION_REVIEW symbol=%s decision=close price=%s unrealized_pl=%s reason=%s",
            review.symbol,
            review.current_price,
            review.unrealized_pl,
            review.reason,
        )
        decision = risk_gate.approve_full_close(position.symbol, position.qty, review.reason)
        refreshed: risk_gate.RiskDecision | None = decision
        fresh_book = book
        if allow_send:
            refreshed, fresh_book = _refreshed_close(trading, data_client, decisions, position, decision)
        _commit(
            decisions,
            trading,
            now,
            context,
            None,
            decision,
            allow_send,
            refreshed,
            fresh_book,
            scan,
            webhook_url=settings.n8n_webhook_url,
            action_type="position_review",
            position_decision="close",
            position_price=review.current_price,
            unrealized_pl=review.unrealized_pl,
            settings=settings,
        )
        closed_any = True
    if closed_any:
        return

    held = {position.symbol for position in book.positions}
    pending = set(book.pending_symbols)
    free = [symbol for symbol in config.ALLOWED_SYMBOLS if symbol not in held and symbol not in pending]
    if len(held) + len(pending) >= config.MAX_OPEN_POSITIONS or not free:
        reason = "no free symbol under the position limit"
        logger.info("ENTRY_EVALUATION %s", reason)
        risk = {"verdict": risk_gate.VERDICT_NO_TRADE, "approved": False, "reason": reason}
        entry_context = {
            "as_of": now.isoformat(),
            "action_type": "entry_evaluation",
            "equity": account.equity,
            "day_start_equity": day_start,
            "positions": [
                {"symbol": position.symbol, "qty": format(position.qty, "f")}
                for position in book.positions
            ],
            "reason": reason,
        }
        try:
            decisions.write(
                timestamp=now,
                market_context=entry_context,
                ai_suggestion=None,
                risk_verdict=risk_gate.VERDICT_NO_TRADE,
                risk_reason=reason,
                risk_decision=risk,
                position_size=None,
                final_action="no_trade",
                order_kind=risk_gate.ORDER_KIND_NONE,
                action_type="entry_evaluation",
            )
        except Exception:
            logger.exception("ENTRY_EVALUATION log failed")
        return
    if scan is None:
        logger.info("NO_QUOTES skipping the model")
        return
    focused = None
    usable = [view for view in scan.views if view.bid and view.ask and view.symbol in free]
    if not usable:
        logger.info("NO_QUOTES skipping the model")
        return
    suggestion = ai_decision.suggest(
        _context(now, account, day_start, book, scan, focused),
        settings,
    )
    view = scan.for_symbol(suggestion.symbol) if suggestion.symbol else None
    if suggestion.action == "exit" and suggestion.symbol in held:
        decision = risk_gate.RiskDecision(
            verdict=risk_gate.VERDICT_NO_TRADE,
            approved=False,
            reason="position review chose hold",
            model_reason=suggestion.reason,
            symbol=suggestion.symbol,
        )
    else:
        decision = risk_gate.evaluate(_request_from(suggestion, account, day_start, book, view, now))
    refreshed = decision
    fresh_book = book
    if allow_send and decision.approved:
        fresh_book = execution.fetch_book(trading)
        fresh_scan = scanner.scan(data_client)
        fresh_view = fresh_scan.for_symbol(suggestion.symbol) if suggestion.symbol else None
        refreshed = risk_gate.evaluate(
            _request_from(suggestion, account, day_start, fresh_book, fresh_view, datetime.now(timezone.utc))
        )
    _commit(
        decisions,
        trading,
        now,
        _context(now, account, day_start, book, scan, suggestion.symbol or focused),
        suggestion,
        decision,
        allow_send,
        refreshed,
        fresh_book,
        scan,
        webhook_url=settings.n8n_webhook_url,
        action_type="entry_evaluation",
        settings=settings,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca paper crypto bot. Dry-run unless --send-orders.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument(
        "--send-orders",
        action="store_true",
        help="Send approved paper orders. Without this flag every cycle is a dry run.",
    )
    parser.add_argument("--interval", type=int, default=None, help="Seconds between cycles.")
    args = parser.parse_args(argv)

    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    configure_logging(config.data_directory())
    try:
        settings = config.load_settings()
    except Exception as exc:
        logger.error("STARTUP %s", exc)
        return 1

    interval = args.interval if args.interval is not None else settings.loop_interval_seconds
    try:
        interval = config.require_interval(interval)
    except Exception as exc:
        logger.error("STARTUP %s", exc)
        return 1

    logger.warning("MODE %s", "paper-send" if args.send_orders else "dry-run")
    logger.info("STARTUP %s", settings)
    logger.info(
        "RISK_LIMITS max_risk=%s daily_loss=%s max_positions=%s symbols=%s window=24/7 UTC",
        config.MAX_RISK_FRACTION,
        config.DAILY_LOSS_FRACTION,
        config.MAX_OPEN_POSITIONS,
        ",".join(config.ALLOWED_SYMBOLS),
    )
    try:
        decisions = decision_log.DecisionLog(config.decisions_db_path())
    except Exception as exc:
        logger.error("DECISION_LOG %s", exc)
        return 1
    logger.info("DECISION_LOG %s", decisions.path)
    try:
        trading = execution.build_trading_client(settings)
        data_client = scanner.build_data_client(settings)
    except Exception as exc:
        logger.error("CLIENT %s", exc)
        decisions.close()
        return 1

    try:
        while True:
            try:
                run_cycle(trading, data_client, decisions, settings, allow_send=args.send_orders)
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("CYCLE_FAILED")
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("STOP keyboard interrupt")
        return 0
    finally:
        decisions.close()


if __name__ == "__main__":
    raise SystemExit(main())
