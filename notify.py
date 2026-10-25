"""Send one logged decision to an n8n webhook.

Each POST is one flat row for Google Sheets. When the cycle evaluated both
pairs, BTC/USD and ETH/USD are sent as two rows that share the same decision.
A submitted entry or close also fills event_type, side, quantity, and prices
on the row for that symbol. An optional SMTP mail is sent only when both
TRADE_NOTIFY_EMAIL and SMTP_HOST are set.

A missing URL, a slow webhook, or an HTTP error is logged and swallowed.
Nothing here can approve a trade or stop the cycle.
"""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage
from typing import Mapping


logger = logging.getLogger("notify")

WEBHOOK_TIMEOUT_SECONDS = 10

# Column order for the sheet. The JSON object is built in this order.
SHEET_FIELDS = (
    "timestamp_utc",
    "mode",
    "equity",
    "day_start_equity",
    "day_pnl",
    "symbol",
    "trend",
    "momentum_status",
    "momentum_reasons",
    "extended",
    "pullback",
    "distance_from_high",
    "pullback_low",
    "suggested_stop",
    "target_low",
    "target_high",
    "macd_divergence_status",
    "macd_divergence_description",
    "ai_action",
    "ai_reason",
    "ai_symbol",
    "ai_direction",
    "ai_stop_price",
    "ai_target_price",
    "risk_gate_verdict",
    "risk_gate_approved",
    "risk_gate_reason",
    "final_action",
    "order_id",
    "chart_filename",
    "raw_strategy_json",
    "raw_ai_suggestion",
    "raw_risk_gate",
    "action_type",
    "position_decision",
    "position_price",
    "unrealized_pl",
    "position_reason",
    "event_type",
    "trade_side",
    "trade_qty",
    "trade_price",
    "trade_stop",
    "trade_target",
    "trade_description",
    "notify_email",
)


def post_decision(payload: Mapping[str, object], webhook_url: str | None) -> None:
    """POST one row as JSON. Skip when the URL is blank."""

    url = (webhook_url or "").strip()
    if not url:
        logger.warning("N8N_WEBHOOK skip N8N_WEBHOOK_URL is not set")
        return
    _post(url, dict(payload))


def post_rows(rows: list[dict[str, object]], webhook_url: str | None) -> None:
    """POST each sheet row. One failure does not skip the remaining rows."""

    url = (webhook_url or "").strip()
    if not url:
        logger.warning("N8N_WEBHOOK skip N8N_WEBHOOK_URL is not set")
        return
    for row in rows:
        _post(url, row)


def build_rows(
    *,
    timestamp: datetime,
    allow_send: bool,
    context: Mapping[str, object],
    suggestion: Mapping[str, object] | None,
    verdict: str,
    reason: str,
    approved: bool,
    final_action: str,
    order_id: str | None,
    chart_filenames: list[str],
    symbol: str | None,
    risk_decision: Mapping[str, object] | None,
    action_type: str | None = "entry_evaluation",
    position_symbol: str | None = None,
    position_decision: str | None = None,
    position_price: str | None = None,
    unrealized_pl: str | None = None,
    position_reason: str | None = None,
    trade_symbol: str | None = None,
    event_type: str | None = None,
    trade_side: str | None = None,
    trade_qty: str | None = None,
    trade_price: str | None = None,
    trade_stop: str | None = None,
    trade_target: str | None = None,
    trade_description: str | None = None,
    notify_email: str | None = None,
) -> list[dict[str, object]]:
    """Flat rows whose keys are the sheet columns, in column order.

    ``allow_send`` true is ``live`` in the sheet. Orders still go only to the
    Alpaca paper account; this flag means the process was started with
    ``--send-orders``.
    """

    when = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    shared = {
        "timestamp_utc": when.astimezone(timezone.utc).isoformat(),
        "mode": "live" if allow_send else "dry-run",
        "equity": context.get("equity"),
        "day_start_equity": context.get("day_start_equity"),
        "day_pnl": _day_pnl(context),
        "ai_action": _suggestion_field(suggestion, "action"),
        "ai_reason": _suggestion_field(suggestion, "reason"),
        "ai_symbol": _suggestion_field(suggestion, "symbol"),
        "ai_direction": _suggestion_field(suggestion, "direction"),
        "ai_stop_price": _suggestion_field(suggestion, "stop_price"),
        "ai_target_price": _suggestion_field(suggestion, "target_price"),
        "risk_gate_verdict": verdict,
        "risk_gate_approved": approved,
        "risk_gate_reason": reason,
        "final_action": final_action,
        "order_id": order_id,
        "raw_ai_suggestion": _json_text(None if suggestion is None else dict(suggestion)),
        "raw_risk_gate": _json_text(None if risk_decision is None else dict(risk_decision)),
    }
    if trade_description is None:
        trade_description = describe_trade(
            event_type=event_type,
            symbol=trade_symbol or symbol,
            side=trade_side or (None if risk_decision is None else str(risk_decision.get("side") or "") or None),
            qty=trade_qty or (None if risk_decision is None else risk_decision.get("qty")),
            price=trade_price,
            stop=trade_stop or (None if risk_decision is None else risk_decision.get("stop_price")),
            target=trade_target or (None if risk_decision is None else risk_decision.get("target_price")),
            reason=reason,
            mode=shared["mode"],
            final_action=final_action,
            ai_reason=_suggestion_field(suggestion, "reason"),
        )
    described_symbol = trade_symbol or (symbol if trade_description else None)
    subjects = _subjects(context, symbol)
    return [
        _row(
            shared,
            item,
            chart_filenames,
            action_type=action_type,
            position_symbol=position_symbol,
            position_decision=position_decision,
            position_price=position_price,
            unrealized_pl=unrealized_pl,
            position_reason=position_reason,
            trade_symbol=trade_symbol,
            event_type=event_type,
            trade_side=trade_side,
            trade_qty=trade_qty,
            trade_price=trade_price,
            trade_stop=trade_stop,
            trade_target=trade_target,
            trade_description=trade_description,
            described_symbol=described_symbol,
            notify_email=notify_email,
        )
        for item in subjects
    ]


def send_trade_email(
    event: Mapping[str, object],
    *,
    email: str | None,
    host: str | None,
    port: int,
    username: str | None,
    password: str | None,
    sender: str | None,
) -> None:
    """Mail one trade event. Missing SMTP settings are skipped, never raised."""

    if not email:
        return
    if not host:
        logger.info("TRADE_EMAIL skip SMTP_HOST is not set")
        return
    message = EmailMessage()
    message["To"] = email
    message["From"] = sender or username or email
    message["Subject"] = f"{event.get('event_type') or 'trade'} {event.get('symbol') or ''}".strip()
    lines = [
        f"event_type: {event.get('event_type')}",
        f"timestamp_utc: {event.get('timestamp_utc')}",
        f"symbol: {event.get('symbol')}",
        f"side: {event.get('side')}",
        f"qty: {event.get('qty')}",
        f"price: {event.get('price')}",
        f"stop: {event.get('stop')}",
        f"target: {event.get('target')}",
        f"reason: {event.get('reason')}",
        f"description: {event.get('description')}",
        f"order_id: {event.get('order_id')}",
    ]
    message.set_content("\n".join(lines))
    try:
        with smtplib.SMTP(host, port, timeout=WEBHOOK_TIMEOUT_SECONDS) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                logger.info("TRADE_EMAIL starttls not supported")
            if username:
                smtp.login(username, password or "")
            smtp.send_message(message)
    except Exception:
        logger.exception("TRADE_EMAIL failed symbol=%s", event.get("symbol"))
        return
    logger.info("TRADE_EMAIL sent symbol=%s event=%s", event.get("symbol"), event.get("event_type"))


def _post(url: str, payload: dict[str, object]) -> None:
    try:
        import requests
    except ImportError:
        logger.exception("N8N_WEBHOOK skip requests is not installed")
        return
    try:
        response = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("N8N_WEBHOOK failed symbol=%s", payload.get("symbol"))
        return
    if response.ok:
        logger.info("N8N_WEBHOOK ok status=%s symbol=%s", response.status_code, payload.get("symbol"))
        return
    logger.error("N8N_WEBHOOK failed status=%s symbol=%s", response.status_code, payload.get("symbol"))


def _subjects(context: Mapping[str, object], symbol: str | None) -> list[dict[str, object]]:
    """One subject per evaluated symbol. A repair with no scan is one subject."""

    raw = context.get("symbols")
    subjects: list[dict[str, object]] = []
    if isinstance(raw, dict) and raw:
        for name, item in raw.items():
            block = item.get("strategy") if isinstance(item, dict) else None
            trend = None
            if isinstance(block, dict):
                momentum = block.get("momentum_pullback")
                if isinstance(momentum, dict):
                    trend = momentum.get("trend")
            if trend is None and isinstance(item, dict):
                summary = item.get("candle_summary")
                if isinstance(summary, dict):
                    trend = summary.get("trend")
            subjects.append({"symbol": str(name), "trend": trend, "strategy": block if isinstance(block, dict) else None})
        return subjects
    return [{"symbol": symbol, "trend": None, "strategy": None}]


def _row(
    shared: dict[str, object],
    subject: dict[str, object],
    chart_filenames: list[str],
    *,
    action_type: str | None,
    position_symbol: str | None,
    position_decision: str | None,
    position_price: str | None,
    unrealized_pl: str | None,
    position_reason: str | None,
    trade_symbol: str | None,
    event_type: str | None,
    trade_side: str | None,
    trade_qty: str | None,
    trade_price: str | None,
    trade_stop: str | None,
    trade_target: str | None,
    trade_description: str | None,
    described_symbol: str | None,
    notify_email: str | None,
) -> dict[str, object]:
    strategy = subject.get("strategy")
    momentum = strategy.get("momentum_pullback") if isinstance(strategy, dict) else None
    macd = strategy.get("macd_divergence") if isinstance(strategy, dict) else None
    if not isinstance(momentum, dict):
        momentum = {}
    if not isinstance(macd, dict):
        macd = {}
    reasons = momentum.get("reasons")
    symbol = subject.get("symbol")
    values = {
        **shared,
        "symbol": symbol,
        "trend": subject.get("trend"),
        "momentum_status": momentum.get("status"),
        "momentum_reasons": " | ".join(str(item) for item in reasons) if isinstance(reasons, list) else "",
        "extended": momentum.get("extended"),
        "pullback": momentum.get("pullback"),
        "distance_from_high": momentum.get("distance_from_high"),
        "pullback_low": momentum.get("pullback_low"),
        "suggested_stop": momentum.get("suggested_stop"),
        "target_low": momentum.get("target_low"),
        "target_high": momentum.get("target_high"),
        "macd_divergence_status": macd.get("status"),
        "macd_divergence_description": macd.get("description"),
        "chart_filename": _chart_for(symbol if isinstance(symbol, str) else None, chart_filenames),
        "raw_strategy_json": _json_text(strategy if isinstance(strategy, dict) else None),
        "action_type": action_type,
    }
    if position_symbol is not None and symbol == position_symbol:
        values["position_decision"] = position_decision
        values["position_price"] = position_price
        values["unrealized_pl"] = unrealized_pl
        values["position_reason"] = position_reason
    if trade_symbol is not None and symbol == trade_symbol:
        values["event_type"] = event_type
        values["trade_side"] = trade_side
        values["trade_qty"] = trade_qty
        values["trade_price"] = trade_price
        values["trade_stop"] = trade_stop
        values["trade_target"] = trade_target
        values["notify_email"] = notify_email
    if described_symbol is not None and symbol == described_symbol:
        values["trade_description"] = trade_description
    return {name: values.get(name) for name in SHEET_FIELDS}


def _chart_for(symbol: str | None, filenames: list[str]) -> str | None:
    if not filenames:
        return None
    if symbol:
        token = symbol.replace("/", "-")
        for name in filenames:
            if token in name:
                return name
    if len(filenames) == 1 and symbol is None:
        return filenames[0]
    return None


def _suggestion_field(suggestion: Mapping[str, object] | None, name: str) -> object:
    if suggestion is None:
        return None
    return suggestion.get(name)


def _day_pnl(context: Mapping[str, object]) -> object:
    if "day_pnl_usd" in context:
        return context.get("day_pnl_usd")
    equity = _decimal(context.get("equity"))
    start = _decimal(context.get("day_start_equity"))
    if equity is None or start is None:
        return None
    return format(equity - start, "f")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def describe_trade(
    *,
    event_type: str | None,
    symbol: str | None,
    side: str | None,
    qty: object,
    price: object,
    stop: object,
    target: object,
    reason: str | None,
    mode: str | None = None,
    final_action: str | None = None,
    ai_reason: object = None,
) -> str | None:
    """One sentence that explains the trade for a webhook or an email.

    A hold, a rejection, or a missing symbol has no description. A dry-run
    row that would have sent an order still gets the sentence.
    """

    if not symbol:
        return None
    kind = (event_type or "").strip()
    if not kind:
        if final_action not in {"dry_run", "approved_awaiting_send", "submitted"}:
            return None
        side_text = (side or "").lower()
        if side_text == "buy":
            kind = "trade_opened"
        elif side_text == "sell":
            kind = "trade_closed"
        else:
            return None
    if kind == "trade_opened":
        opener = "Buy"
        if (side or "").lower() and (side or "").lower() != "buy":
            opener = (side or "").capitalize()
        sentence = f"{opener} {qty or 'an unknown quantity'} of {symbol}"
        if price:
            sentence += f" near {price}"
        if stop:
            sentence += f", stop at {stop}"
        if target:
            sentence += f", target {target}"
        sentence += "."
    elif kind == "trade_closed":
        sentence = f"Close the {symbol} long"
        if qty:
            sentence += f" of {qty}"
        if price:
            sentence += f" near {price}"
        sentence += "."
    else:
        return None
    notes: list[str] = []
    for candidate in (reason, ai_reason):
        text = str(candidate).strip() if candidate else ""
        if not text:
            continue
        text = text.rstrip(".")
        if text and text not in notes:
            notes.append(text)
    if notes:
        sentence += " " + ". ".join(notes) + "."
    if mode == "dry-run" or final_action == "dry_run":
        sentence += " This is a dry run; the paper order was not sent."
    return sentence


def _json_text(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)
