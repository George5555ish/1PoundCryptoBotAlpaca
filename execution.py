"""Order placement for the crypto bot.

Alpaca crypto orders are simple orders only: no bracket, no OCO, and no
short. An approved entry is a market buy with time-in-force IOC, then a
good-till-cancelled stop-limit sell at the gate's stop. The stop-limit's
limit price sits one slippage allowance below the stop, which is the slack
the gate already reserved. If that protective order cannot be placed, this
module closes the position before returning.

``submit`` is the only function that sends an order. The caller must already
have stored the audit row. ``allow_send`` false raises, so a dry run cannot
reach the network by accident.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import quote

import config
import risk_gate


logger = logging.getLogger("execution")


@dataclass(frozen=True)
class AccountView:
    equity: str
    buying_power: str
    trading_blocked: bool
    crypto_status: str | None


@dataclass(frozen=True)
class PositionBook:
    positions: tuple[risk_gate.OpenPosition, ...]
    orders: tuple[risk_gate.WorkingOrder, ...]
    other_position_open: bool
    pending_entry_while_flat: bool


@dataclass(frozen=True)
class OrderResult:
    order_id: str | None
    status: str
    message: str

    def log_line(self) -> str:
        return f"order_id={self.order_id} status={self.status} message={self.message}"


def _url_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def build_trading_client(settings: config.Settings):
    """Paper trading client pinned to the Alpaca paper host."""

    from alpaca.trading.client import TradingClient

    client = TradingClient(
        settings.alpaca_api_key,
        settings.alpaca_secret_key,
        paper=True,
        url_override=config.PAPER_TRADING_BASE_URL,
    )
    config.assert_paper_trading_url(_url_text(getattr(client, "_base_url", "")))
    return client


def _dec(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def fetch_account(client) -> AccountView:
    account = client.get_account()
    status = getattr(account, "crypto_status", None)
    status_text = str(getattr(status, "value", status)) if status is not None else None
    blocked = bool(
        getattr(account, "trading_blocked", False)
        or getattr(account, "account_blocked", False)
        or getattr(account, "trade_suspended_by_user", False)
    )
    if status_text and status_text.lower() not in {"active", "accountstatus.active"}:
        blocked = True
    equity = str(getattr(account, "equity", "") or "")
    buying_power = str(getattr(account, "buying_power", "") or "")
    return AccountView(
        equity=equity,
        buying_power=buying_power,
        trading_blocked=blocked,
        crypto_status=status_text,
    )


def fetch_book(client) -> PositionBook:
    """Read positions and working orders. Equity positions are not closed."""

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    positions: list[risk_gate.OpenPosition] = []
    other = False
    for position in client.get_all_positions():
        raw_symbol = str(getattr(position, "symbol", "") or "")
        qty = _dec(getattr(position, "qty", None))
        if qty is None:
            continue
        side = str(getattr(getattr(position, "side", ""), "value", getattr(position, "side", ""))).lower()
        asset_class = str(
            getattr(getattr(position, "asset_class", ""), "value", getattr(position, "asset_class", ""))
        ).lower()
        canonical = config.normalize_symbol(raw_symbol)
        if asset_class and asset_class not in {"crypto", "crypto_perp"}:
            other = True
            continue
        if not config.is_allowed_symbol(canonical) or side != "long":
            other = True
            continue
        positions.append(risk_gate.OpenPosition(symbol=canonical, qty=qty, side="long"))

    orders: list[risk_gate.WorkingOrder] = []
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
    for order in client.get_orders(filter=request):
        raw_symbol = str(getattr(order, "symbol", "") or "")
        canonical = config.normalize_symbol(raw_symbol)
        if not config.is_allowed_symbol(canonical):
            continue
        side = str(getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))).lower()
        if side not in {"buy", "sell"}:
            continue
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            continue
        orders.append(risk_gate.WorkingOrder(order_id=order_id, symbol=canonical, side=side))

    held = {position.symbol for position in positions}
    pending_entry = (not held) and any(order.side == "buy" for order in orders)
    return PositionBook(
        positions=tuple(positions),
        orders=tuple(orders),
        other_position_open=other,
        pending_entry_while_flat=pending_entry,
    )


def _client_order_id(symbol: str, purpose: str) -> str:
    compact = config.normalize_symbol(symbol).replace("/", "")
    stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    return f"c1-{compact}-{purpose}-{stamp}-{uuid.uuid4().hex[:8]}"[:48]


def _protective_limit(stop_price: str) -> float:
    stop = Decimal(stop_price)
    raw = stop * (Decimal("1") - config.SLIPPAGE_ALLOWANCE_FRACTION)
    limit = raw.quantize(config.CENT, rounding=ROUND_DOWN)
    if limit >= stop:
        limit = (stop - config.CENT).quantize(config.CENT, rounding=ROUND_DOWN)
    if limit <= 0:
        raise RuntimeError("protective limit price is not positive")
    return float(limit)


def _assert_paper(client) -> None:
    config.assert_paper_trading_url(_url_text(getattr(client, "_base_url", "")))


def _close_symbol(client, symbol: str):
    """Close one crypto pair. The slash is encoded so the path stays one segment."""

    encoded = quote(config.normalize_symbol(symbol), safe="")
    from alpaca.trading.models import Order

    response = client.delete(f"/positions/{encoded}", {})
    return Order(**response)


def _cancel_symbol_orders(client, symbol: str, orders: tuple[risk_gate.WorkingOrder, ...]) -> None:
    canonical = config.normalize_symbol(symbol)
    for order in orders:
        if order.symbol == canonical:
            client.cancel_order_by_id(order.order_id)


def submit(client, decision: risk_gate.RiskDecision, allow_send: bool, book: PositionBook | None = None) -> OrderResult:
    """Send one already-approved decision. Raises when sending is not allowed."""

    if not allow_send:
        raise RuntimeError("refusing to send: allow_send is false")
    if not decision.approved:
        raise RuntimeError("refusing to send: decision is not approved")
    _assert_paper(client)

    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest

    if decision.order_kind == risk_gate.ORDER_KIND_CANCEL:
        if not decision.cancel_order_id:
            raise RuntimeError("cancel is missing an order id")
        client.cancel_order_by_id(decision.cancel_order_id)
        return OrderResult(decision.cancel_order_id, "canceled", "cancelled working order")

    if decision.order_kind in {risk_gate.ORDER_KIND_EXIT, risk_gate.ORDER_KIND_FULL_CLOSE}:
        if book is not None and decision.symbol:
            _cancel_symbol_orders(client, decision.symbol, book.orders)
        if not decision.symbol:
            raise RuntimeError("close is missing a symbol")
        order = _close_symbol(client, decision.symbol)
        return OrderResult(str(order.id), str(order.status), "close submitted")

    if decision.order_kind != risk_gate.ORDER_KIND_ENTRY:
        raise RuntimeError(f"unsupported order kind {decision.order_kind}")
    if not decision.symbol or not decision.qty or not decision.stop_price:
        raise RuntimeError("entry is missing symbol, quantity, or stop")

    entry = client.submit_order(
        MarketOrderRequest(
            symbol=decision.symbol,
            qty=float(decision.qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.IOC,
            order_class=OrderClass.SIMPLE,
            client_order_id=_client_order_id(decision.symbol, "buy"),
        )
    )
    filled = _dec(getattr(entry, "filled_qty", None)) or Decimal("0")
    entry_id = str(entry.id)
    status = str(entry.status)
    if filled <= 0:
        return OrderResult(entry_id, status, "entry submitted with no fill; no protective stop placed")

    try:
        protection = client.submit_order(
            StopLimitOrderRequest(
                symbol=decision.symbol,
                qty=float(filled),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=float(decision.stop_price),
                limit_price=_protective_limit(decision.stop_price),
                order_class=OrderClass.SIMPLE,
                client_order_id=_client_order_id(decision.symbol, "stop"),
            )
        )
    except Exception as exc:
        logger.exception("PROTECTION_FAILED symbol=%s entry=%s", decision.symbol, entry_id)
        try:
            close = _close_symbol(client, decision.symbol)
            return OrderResult(
                entry_id,
                "protection_failed_closed",
                f"stop submit failed ({type(exc).__name__}); close order {close.id}",
            )
        except Exception as close_exc:
            return OrderResult(
                entry_id,
                "protection_failed_open",
                f"stop submit failed ({type(exc).__name__}); close also failed ({type(close_exc).__name__})",
            )
    return OrderResult(
        entry_id,
        status,
        f"entry filled {format(filled, 'f')}; protective stop {protection.id}",
    )
