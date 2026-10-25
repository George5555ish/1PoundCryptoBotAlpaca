"""Hard risk gate for the crypto bot.

The model is not trusted. ``evaluate`` is the only function that can mark a
model suggestion as approved. ``plan_repairs`` is the only function that can
mark a book repair as required. Neither function places an order.

Rules enforced here:

- New entries are long only. Alpaca crypto orders cannot open a short, and
  V1 does not try.
- Symbol on a new entry must be BTC/USD or ETH/USD.
- The market is 24/7. A weekend or overnight timestamp is not a reason to
  reject. A naive timestamp is.
- At most five open positions in this universe. A position outside this
  universe still blocks a new entry. A working buy occupies a slot only for
  its own symbol. The same symbol cannot be entered again while it is open.
- Quantity comes from the stop distance. A full stop-out at the stop price,
  after a small slippage allowance, loses no more than 1% of current equity.
  Quantity is floored to 1e-8. Notional is capped at equity and buying power.
- The stop has to sit below the ask, and the target above it. The ask is the
  entry. A caller-supplied entry price is ignored.
- Daily loss at or beyond 3% of UTC-day-start equity halts new entries.
  Closing an open long is still allowed.
- An entry quote must be present, uncrossed, inside the spread guard, and fresh.
- An order needs a non-empty reason.

The £1 awareness figure is intentionally not imported here.

A gap through the stop-limit can lose more than 1%. Crypto has no bracket
order, so the execution layer places the protective stop-limit after the
entry fill. ``plan_repairs`` closes a long that has no working sell.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Sequence

import config


logger = logging.getLogger("risk_gate")

ORDER_KIND_NONE = "none"
ORDER_KIND_ENTRY = "entry_market"
ORDER_KIND_EXIT = "market_exit"
ORDER_KIND_FULL_CLOSE = "full_close"
ORDER_KIND_CANCEL = "cancel_order"

VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"
VERDICT_NO_TRADE = "no_trade"


@dataclass(frozen=True)
class OpenPosition:
    """A long the gate can reason about, in coin units rather than shares."""

    symbol: str
    qty: Decimal
    side: str = "long"
    current_price: str | None = None
    unrealized_pl: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("position symbol is required")
        object.__setattr__(self, "symbol", config.normalize_symbol(self.symbol))
        if not isinstance(self.qty, Decimal) or not self.qty.is_finite() or self.qty <= 0:
            raise ValueError("position qty must be a positive Decimal")
        if self.side != "long":
            raise ValueError("crypto V1 positions are long only")


@dataclass(frozen=True)
class WorkingOrder:
    """An open order in this bot's universe."""

    order_id: str
    symbol: str
    side: str  # "buy" or "sell"

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("order id is required")
        object.__setattr__(self, "order_id", self.order_id.strip())
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("order symbol is required")
        object.__setattr__(self, "symbol", config.normalize_symbol(self.symbol))
        if self.side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")


@dataclass(frozen=True)
class GateRequest:
    """Everything ``evaluate`` may see about one suggestion."""

    action: object
    symbol: object
    direction: object
    stop_price: object
    target_price: object
    reason: object
    suggestion_valid: bool
    parse_error: str | None
    bid: object
    ask: object
    quote_time: datetime | None
    equity: object
    day_start_equity: object
    buying_power: object
    open_positions: tuple[OpenPosition, ...]
    pending_entry_while_flat: bool
    other_position_open: bool
    trading_blocked: bool
    now: datetime
    pending_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    """The gate's answer. Execution must look at ``approved`` and ``order_kind``."""

    verdict: str
    approved: bool
    reason: str
    model_reason: str
    symbol: str | None = None
    side: str | None = None
    qty: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    order_kind: str = ORDER_KIND_NONE
    risk_budget: str | None = None
    planned_loss: str | None = None
    planned_loss_with_slippage: str | None = None
    size_capped: bool = False
    failed_rules: tuple[str, ...] = ()
    cancel_order_id: str | None = None

    def log_line(self) -> str:
        payload = {
            "verdict": self.verdict,
            "approved": self.approved,
            "reason": self.reason,
            "model_reason": self.model_reason,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "order_kind": self.order_kind,
            "risk_budget": self.risk_budget,
            "planned_loss": self.planned_loss,
            "planned_loss_with_slippage": self.planned_loss_with_slippage,
            "size_capped": self.size_capped,
            "failed_rules": list(self.failed_rules),
            "cancel_order_id": self.cancel_order_id,
        }
        return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True)
class RepairAction:
    """A close or cancel required to put the book back inside the rules."""

    symbol: str
    kind: str
    reason: str
    order_id: str | None = None
    qty: str | None = None
    side: str | None = None


class _BadNumber(ValueError):
    """A money input the gate will not guess about."""


def _aware_utc(now: datetime) -> datetime | None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return None
    return now.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _BadNumber(f"{label} is missing")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _BadNumber(f"{label} is not a number") from exc
    if not number.is_finite():
        raise _BadNumber(f"{label} is not finite")
    return number


def _money(value: object, label: str) -> Decimal:
    number = _decimal(value, label)
    if number <= 0:
        raise _BadNumber(f"{label} must be positive")
    return number.quantize(config.CENT, rounding=ROUND_HALF_UP)


def _qty_text(qty: Decimal) -> str:
    return format(qty.quantize(config.QTY_QUANTUM, rounding=ROUND_DOWN), "f")


def _price_text(price: Decimal) -> str:
    return format(price.quantize(config.CENT, rounding=ROUND_HALF_UP), "f")


def _reject(
    reason: str,
    *,
    model_reason: str = "",
    symbol: str | None = None,
    failed: tuple[str, ...] | None = None,
) -> RiskDecision:
    rules = failed if failed is not None else (reason,)
    decision = RiskDecision(
        verdict=VERDICT_REJECT,
        approved=False,
        reason=reason,
        model_reason=model_reason,
        symbol=symbol,
        failed_rules=rules,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def _no_trade(reason: str, model_reason: str, symbol: str | None) -> RiskDecision:
    decision = RiskDecision(
        verdict=VERDICT_NO_TRADE,
        approved=False,
        reason=reason,
        model_reason=model_reason,
        symbol=symbol,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def daily_loss_halted(equity: object, day_start_equity: object) -> bool:
    """True when the UTC day's loss has reached 3% of day-start equity.

    Missing or unreadable equity fails closed and reports a halt.
    """

    try:
        current = _money(equity, "equity")
        start = _money(day_start_equity, "day-start equity")
    except _BadNumber:
        return True
    limit = (start * config.DAILY_LOSS_FRACTION).quantize(config.CENT, rounding=ROUND_DOWN)
    pnl = current - start
    return pnl <= -limit


def _quote_ok(request: GateRequest, ask: Decimal, failures: list[str]) -> Decimal | None:
    try:
        bid = _money(request.bid, "bid")
    except _BadNumber as exc:
        failures.append(str(exc))
        return None
    if bid >= ask:
        failures.append("quote is crossed or locked")
        return None
    mid = (bid + ask) / Decimal("2")
    spread = (ask - bid) / mid
    if spread > config.MAX_SPREAD_FRACTION:
        failures.append("spread is wider than the guard")
    if request.quote_time is None:
        failures.append("quote time is missing")
        return bid
    quote_utc = _aware_utc(request.quote_time)
    now_utc = _aware_utc(request.now)
    if quote_utc is None or now_utc is None:
        failures.append("quote time must be timezone-aware")
        return bid
    age = (now_utc - quote_utc).total_seconds()
    if age < -config.MAX_QUOTE_FUTURE_SKEW_SECONDS:
        failures.append("quote timestamp is in the future")
    elif age > config.MAX_QUOTE_AGE_SECONDS:
        failures.append("quote is stale")
    return bid


def _position_for(symbol: str, positions: tuple[OpenPosition, ...]) -> OpenPosition | None:
    matches = [item for item in positions if item.symbol == symbol]
    if len(matches) != 1:
        return None
    return matches[0]


def _evaluate_exit(request: GateRequest, symbol: str, model_reason: str) -> RiskDecision:
    if request.direction not in {None, "long", "sell"}:
        return _reject(
            "exit direction conflicts with the long-only book",
            model_reason=model_reason,
            symbol=symbol,
        )
    if request.trading_blocked:
        return _reject("trading is blocked at the broker", model_reason=model_reason, symbol=symbol)
    if _aware_utc(request.now) is None:
        return _reject("timestamp must be timezone-aware", model_reason=model_reason, symbol=symbol)
    matches = [item for item in request.open_positions if item.symbol == symbol]
    if len(matches) == 0:
        return _reject("no open long to close", model_reason=model_reason, symbol=symbol)
    if len(matches) > 1:
        return _reject(
            "duplicate position records; refusing to size a close",
            model_reason=model_reason,
            symbol=symbol,
        )
    position = matches[0]
    decision = RiskDecision(
        verdict=VERDICT_APPROVE,
        approved=True,
        reason="close the open long",
        model_reason=model_reason,
        symbol=symbol,
        side="sell",
        qty=_qty_text(position.qty),
        order_kind=ORDER_KIND_EXIT,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def _evaluate_entry(request: GateRequest, symbol: str, model_reason: str) -> RiskDecision:
    failures: list[str] = []
    if request.direction != "long":
        failures.append("crypto V1 is long only")
    if not config.is_allowed_symbol(symbol):
        failures.append("symbol is not in the crypto universe")
    if request.trading_blocked:
        failures.append("trading is blocked at the broker")
    if _aware_utc(request.now) is None:
        failures.append("timestamp must be timezone-aware")
    if not model_reason:
        failures.append("reason is empty")
    if request.other_position_open:
        failures.append("another position is already open")
    held_symbols = {item.symbol for item in request.open_positions}
    pending = []
    for raw in request.pending_symbols:
        pending_symbol = config.normalize_symbol(raw)
        if pending_symbol not in held_symbols and pending_symbol not in pending:
            pending.append(pending_symbol)
    slots = len(held_symbols) + len(pending)
    if request.pending_entry_while_flat and not pending and not held_symbols:
        slots += 1
    if symbol in held_symbols:
        failures.append("already long this symbol")
    if symbol in pending:
        failures.append("a working entry already exists for this symbol")
    if slots >= config.MAX_OPEN_POSITIONS and symbol not in held_symbols:
        failures.append(f"max open positions is {config.MAX_OPEN_POSITIONS}")
    if daily_loss_halted(request.equity, request.day_start_equity):
        failures.append("daily loss halt is in effect for this UTC day")

    try:
        ask = _money(request.ask, "ask")
    except _BadNumber as exc:
        failures.append(str(exc))
        ask = None
    if ask is not None:
        _quote_ok(request, ask, failures)

    stop: Decimal | None = None
    target: Decimal | None = None
    try:
        stop = _money(request.stop_price, "stop")
    except _BadNumber as exc:
        failures.append(str(exc))
    try:
        target = _money(request.target_price, "target")
    except _BadNumber as exc:
        failures.append(str(exc))

    if ask is not None and stop is not None:
        min_distance = max(config.CENT, (ask * config.MIN_STOP_FRACTION).quantize(config.CENT, rounding=ROUND_UP))
        if stop >= ask:
            failures.append("stop must be below the ask")
        elif ask - stop < min_distance:
            failures.append("stop is closer than the minimum distance")
    if ask is not None and target is not None and target <= ask:
        failures.append("target must be above the ask")

    if failures or ask is None or stop is None or target is None:
        reason = "; ".join(failures) if failures else "entry is incomplete"
        return _reject(reason, model_reason=model_reason, symbol=symbol, failed=tuple(failures or [reason]))

    try:
        equity = _money(request.equity, "equity")
        buying_power = _money(request.buying_power, "buying power")
    except _BadNumber as exc:
        return _reject(str(exc), model_reason=model_reason, symbol=symbol)

    budget = (equity * config.MAX_RISK_FRACTION).quantize(config.CENT, rounding=ROUND_DOWN)
    stop_distance = ask - stop
    slippage = max(
        config.CENT,
        (ask * config.SLIPPAGE_ALLOWANCE_FRACTION).quantize(config.CENT, rounding=ROUND_UP),
    )
    sizing_distance = stop_distance + slippage
    raw_qty = (budget / sizing_distance).quantize(config.QTY_QUANTUM, rounding=ROUND_DOWN)
    max_notional = min(equity, buying_power) * config.MAX_POSITION_FRACTION
    max_qty = (max_notional / ask).quantize(config.QTY_QUANTUM, rounding=ROUND_DOWN)
    size_capped = False
    qty = raw_qty
    if qty > max_qty:
        qty = max_qty
        size_capped = True
    if qty <= 0:
        return _reject(
            "stop distance sizes the position to zero",
            model_reason=model_reason,
            symbol=symbol,
        )
    notional = qty * ask
    if notional < config.MIN_NOTIONAL_USD:
        return _reject(
            "sized notional is below the one-dollar minimum",
            model_reason=model_reason,
            symbol=symbol,
        )
    planned = qty * stop_distance
    planned_slip = qty * sizing_distance
    if planned > budget or planned_slip > budget:
        return _reject(
            "planned loss exceeds the 1% budget",
            model_reason=model_reason,
            symbol=symbol,
        )

    decision = RiskDecision(
        verdict=VERDICT_APPROVE,
        approved=True,
        reason="entry sized from the stop" + ("; size capped by equity" if size_capped else ""),
        model_reason=model_reason,
        symbol=symbol,
        side="buy",
        qty=_qty_text(qty),
        stop_price=_price_text(stop),
        target_price=_price_text(target),
        order_kind=ORDER_KIND_ENTRY,
        risk_budget=format(budget, "f"),
        planned_loss=format(planned.quantize(config.CENT, rounding=ROUND_HALF_UP), "f"),
        planned_loss_with_slippage=format(planned_slip.quantize(config.CENT, rounding=ROUND_HALF_UP), "f"),
        size_capped=size_capped,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def evaluate(request: GateRequest) -> RiskDecision:
    """Approve, reject, or ignore one suggestion. Never raises for bad input."""

    model_reason = request.reason.strip() if isinstance(request.reason, str) else ""
    if not request.suggestion_valid:
        detail = request.parse_error or "suggestion is invalid"
        return _reject(f"suggestion rejected: {detail}", model_reason=model_reason)

    action = request.action.strip().lower() if isinstance(request.action, str) else ""
    symbol_text = request.symbol.strip() if isinstance(request.symbol, str) else ""
    symbol = config.normalize_symbol(symbol_text) if symbol_text else None

    if action == "hold":
        return _no_trade("hold", model_reason, symbol)

    if action == "exit":
        if symbol is None or not config.is_allowed_symbol(symbol):
            return _reject("exit symbol is not in the crypto universe", model_reason=model_reason, symbol=symbol)
        if not model_reason:
            return _reject("reason is empty", model_reason=model_reason, symbol=symbol)
        return _evaluate_exit(request, symbol, model_reason)

    if action != "enter":
        return _reject("action must be enter, exit, or hold", model_reason=model_reason, symbol=symbol)
    if symbol is None:
        return _reject("enter requires a symbol", model_reason=model_reason)
    return _evaluate_entry(request, symbol, model_reason)


def approve_full_close(symbol: str, qty: Decimal, reason: str) -> RiskDecision:
    """Approve closing one long without asking the model."""

    canonical = config.normalize_symbol(symbol)
    decision = RiskDecision(
        verdict=VERDICT_APPROVE,
        approved=True,
        reason=reason,
        model_reason="",
        symbol=canonical,
        side="sell",
        qty=_qty_text(qty),
        order_kind=ORDER_KIND_FULL_CLOSE,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def approve_cancel(order: WorkingOrder, reason: str) -> RiskDecision:
    """Approve cancelling one working order without asking the model."""

    decision = RiskDecision(
        verdict=VERDICT_APPROVE,
        approved=True,
        reason=reason,
        model_reason="",
        symbol=order.symbol,
        side=order.side,
        order_kind=ORDER_KIND_CANCEL,
        cancel_order_id=order.order_id,
    )
    logger.info("RISK_GATE %s", decision.log_line())
    return decision


def plan_repairs(
    positions: Sequence[OpenPosition],
    orders: Sequence[WorkingOrder],
) -> tuple[RepairAction, ...]:
    """Return closes and cancels that bring this universe back inside the rules.

    Holds are allowed overnight and on weekends. A repair is only for a book
    that already breaks a rule: more longs than the max, a long with no
    working sell, a stray sell while flat, or a buy that would add size to
    an open symbol or push the book past the max.

    When too many allowed longs are open, the earlier symbols in
    ``ALLOWED_SYMBOLS`` are the ones we keep.
    """

    held = {position.symbol: position for position in positions}
    actions: list[RepairAction] = []
    closing: set[str] = set()

    for symbol, position in held.items():
        if symbol not in config.ALLOWED_SYMBOLS:
            closing.add(symbol)
            actions.append(
                RepairAction(
                    symbol=symbol,
                    kind=ORDER_KIND_FULL_CLOSE,
                    reason="symbol is not in the crypto universe",
                    qty=_qty_text(position.qty),
                )
            )

    keepers = [symbol for symbol in config.ALLOWED_SYMBOLS if symbol in held and symbol not in closing]
    for symbol in keepers[config.MAX_OPEN_POSITIONS :]:
        position = held[symbol]
        closing.add(symbol)
        actions.append(
            RepairAction(
                symbol=symbol,
                kind=ORDER_KIND_FULL_CLOSE,
                reason=f"max open positions is {config.MAX_OPEN_POSITIONS}",
                qty=_qty_text(position.qty),
            )
        )

    sell_symbols = {order.symbol for order in orders if order.side == "sell"}
    for symbol, position in held.items():
        if symbol in closing:
            continue
        if symbol not in sell_symbols:
            closing.add(symbol)
            actions.append(
                RepairAction(
                    symbol=symbol,
                    kind=ORDER_KIND_FULL_CLOSE,
                    reason="open long has no protective sell order",
                    qty=_qty_text(position.qty),
                )
            )

    live = {symbol for symbol in held if symbol not in closing}
    kept_buys: set[str] = set()
    for order in orders:
        if order.symbol in closing:
            actions.append(
                RepairAction(
                    symbol=order.symbol,
                    kind=ORDER_KIND_CANCEL,
                    reason="working order belongs to a position being closed",
                    order_id=order.order_id,
                    side=order.side,
                )
            )
            continue
        if order.side == "sell" and order.symbol not in live:
            actions.append(
                RepairAction(
                    symbol=order.symbol,
                    kind=ORDER_KIND_CANCEL,
                    reason="working sell has no open long",
                    order_id=order.order_id,
                    side=order.side,
                )
            )
            continue
        if order.side != "buy":
            continue
        if order.symbol in live:
            reason = "working buy would add size to an open long"
        elif order.symbol in kept_buys:
            reason = "duplicate working entry for this symbol"
        elif len(live) + len(kept_buys) >= config.MAX_OPEN_POSITIONS:
            reason = f"working buy would exceed max open positions of {config.MAX_OPEN_POSITIONS}"
        else:
            kept_buys.add(order.symbol)
            continue
        actions.append(
            RepairAction(
                symbol=order.symbol,
                kind=ORDER_KIND_CANCEL,
                reason=reason,
                order_id=order.order_id,
                side=order.side,
            )
        )
    if actions:
        logger.info(
            "REPAIR_PLAN %s",
            json.dumps(
                [
                    {"symbol": item.symbol, "kind": item.kind, "reason": item.reason}
                    for item in actions
                ],
                sort_keys=True,
            ),
        )
    return tuple(actions)


def same_order(left: RiskDecision, right: RiskDecision) -> bool:
    """True when a second pass still approves the same order."""

    if not left.approved or not right.approved:
        return False
    return (
        left.symbol == right.symbol
        and left.side == right.side
        and left.order_kind == right.order_kind
        and left.qty == right.qty
        and left.stop_price == right.stop_price
        and left.target_price == right.target_price
        and left.cancel_order_id == right.cancel_order_id
    )


def _demo() -> None:
    """Print a few approvals and rejections. Does not call a broker or a model."""

    from datetime import timezone as tz

    now = datetime(2026, 10, 24, 12, 0, tzinfo=tz.utc)  # Saturday
    base = dict(
        suggestion_valid=True,
        parse_error=None,
        bid="100000.00",
        ask="100050.00",
        quote_time=now,
        equity="100000",
        day_start_equity="100000",
        buying_power="100000",
        open_positions=(),
        pending_entry_while_flat=False,
        other_position_open=False,
        trading_blocked=False,
        now=now,
    )
    samples = [
        GateRequest(
            action="enter",
            symbol="BTC/USD",
            direction="long",
            stop_price="99000",
            target_price="102000",
            reason="demo long",
            **base,
        ),
        GateRequest(
            action="enter",
            symbol="BTC/USD",
            direction="short",
            stop_price="101000",
            target_price="98000",
            reason="demo short",
            **base,
        ),
        GateRequest(
            action="enter",
            symbol="SOL/USD",
            direction="long",
            stop_price="140",
            target_price="170",
            reason="demo universe",
            bid="149.90",
            ask="150.00",
            **{key: value for key, value in base.items() if key not in {"bid", "ask"}},
        ),
    ]
    for request in samples:
        print(evaluate(request).log_line())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _demo()
