"""LLM suggestion layer for the crypto bot.

The model returns one JSON object. It does not choose a quantity and it does
not place an order. Invalid output becomes an invalid suggestion, which the
risk gate rejects. A hold drops any stop or target so those prices cannot be
sized later.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

import config


logger = logging.getLogger("ai_decision")

SCHEMA_FIELDS = frozenset({"action", "symbol", "direction", "stop_price", "target_price", "reason"})
SIZE_FIELDS = frozenset({"qty", "quantity", "shares", "size", "notional", "risk", "risk_pct"})

SYSTEM_PROMPT = f"""You are the suggestion layer for a paper-trading crypto bot. You do not size positions and you do not place orders. A separate risk gate will reject anything that breaks its rules.

Return one JSON object and nothing else. Fields:
- action: "enter", "exit", or "hold"
- symbol: "BTC/USD" or "ETH/USD", or null on a hold
- direction: "long" or "short" on an enter, or null on a hold
- stop_price: number, required on enter. For a long it must be below the current ask.
- target_price: number, required on enter. For a long it must be above the current ask.
- reason: one short sentence

Rules:
- Prefer hold. Do not trade just to be active.
- The soft daily profit target of £1 is awareness only. Never enter a trade in order to reach it.
- Do not include a quantity, notional, or risk percentage. Those fields are ignored.
- This market is open 24/7. Overnight and weekend holds are allowed. Do not exit only because the equity market is closed.
- At most one position may be open. If a position is already open, action must be "hold" or "exit" for that symbol.
- Short entries are not executable in this version. Prefer hold over a short.
- Allowed symbols only: {", ".join(config.ALLOWED_SYMBOLS)}.

The user message is one JSON object. It contains the account, the open position, and a "symbols" map. Each symbol has the current bid, ask, and last trade, plus "candle_summary" for up to the last {config.CANDLE_BARS} completed {config.CANDLE_MINUTES}-minute bars:
- bar_count: how many bars were actually returned. This can be less than {config.CANDLE_BARS}.
- last_close: close of the most recent bar
- highest_high and lowest_low: extremes across those bars
- trend: "higher highs", "lower lows", "sideways", or "mixed"
- average_true_range: average true range of those bars, in price units
If candle_summary.error is set, or bar_count is 0, the candle read failed. Use the quote and the account only. Do not invent a trend, and do not expect raw candles; they are not included.

Each symbol also has "strategy":
- momentum_pullback.status is "pass" or "fail". reasons explains which checks passed or failed.
- A long entry is allowed only when momentum_pullback.status is "pass" for that symbol. The checks are: trend is "higher highs", the last close is more than 0.3% below the 30-bar high, and price has pulled back to the range midpoint or the 5-bar average.
- If status is "fail", or strategy is missing, do not enter. Prefer hold. Exiting an already open position is still allowed.
- When status is "pass" and you enter, direction must be "long". stop_price must be below pullback_low. You may instead use atr_stop (the last close minus one average true range) when that is the level you are using. The stop must still be below the current ask.
- target_price must be 1.5 to 2 times the risk distance above the entry. Risk distance is the ask minus stop_price. target_low and target_high are that band measured from the last close with suggested_stop. Recompute the band if you choose a different stop.
- macd_divergence.status is "present" or "absent". description and price_swing_lows are the swing comparison. Divergence is informational. It can support a long that already passes momentum and pullback. It does not override a failed momentum check, and it is not by itself a reason to enter. Do not invent a divergence when status is "absent".
"""


@dataclass(frozen=True)
class AiSuggestion:
    action: str
    symbol: str | None
    direction: str | None
    stop_price: str | None
    target_price: str | None
    reason: str
    valid: bool
    parse_error: str | None
    ignored_fields: tuple[str, ...] = ()

    def log_line(self) -> str:
        return json.dumps(
            {
                "action": self.action,
                "symbol": self.symbol,
                "direction": self.direction,
                "stop_price": self.stop_price,
                "target_price": self.target_price,
                "reason": self.reason,
                "valid": self.valid,
                "parse_error": self.parse_error,
                "ignored_fields": list(self.ignored_fields),
            },
            sort_keys=True,
        )


def _invalid(message: str, **kwargs: object) -> AiSuggestion:
    ignored = kwargs.get("ignored_fields", ())
    return AiSuggestion(
        action=str(kwargs.get("action") or "hold"),
        symbol=kwargs.get("symbol") if isinstance(kwargs.get("symbol"), str) else None,
        direction=kwargs.get("direction") if isinstance(kwargs.get("direction"), str) else None,
        stop_price=kwargs.get("stop_price") if isinstance(kwargs.get("stop_price"), str) else None,
        target_price=kwargs.get("target_price") if isinstance(kwargs.get("target_price"), str) else None,
        reason=str(kwargs.get("reason") or ""),
        valid=False,
        parse_error=message,
        ignored_fields=tuple(ignored) if isinstance(ignored, tuple) else (),
    )


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _price_string(value: object, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a positive number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a positive number") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} must be a positive number")
    return format(number.quantize(config.CENT, rounding=ROUND_HALF_UP), "f")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected text or null")
    text = value.strip()
    return text or None


def parse_suggestion(content: str) -> AiSuggestion:
    """Turn model text into a suggestion. Never raises for bad model output."""

    try:
        payload = json.loads(_strip_fence(content))
    except json.JSONDecodeError:
        return _invalid("model output was not JSON")
    if not isinstance(payload, dict):
        return _invalid("model output was not a JSON object")

    ignored = tuple(sorted(str(key) for key in payload if str(key) not in SCHEMA_FIELDS))
    raw_action = payload.get("action")
    action = raw_action.strip().lower() if isinstance(raw_action, str) else ""
    if action not in {"enter", "exit", "hold"}:
        return _invalid("action must be enter, exit, or hold", ignored_fields=ignored)

    try:
        symbol_text = _optional_text(payload.get("symbol"))
    except ValueError:
        return _invalid("symbol must be text or null", action=action, ignored_fields=ignored)
    symbol = config.normalize_symbol(symbol_text) if symbol_text else None

    try:
        direction_text = _optional_text(payload.get("direction"))
    except ValueError:
        return _invalid("direction must be text or null", action=action, symbol=symbol, ignored_fields=ignored)
    if direction_text is None:
        direction = None
    else:
        direction = direction_text.lower()
        if direction not in {"long", "short"}:
            return _invalid(
                "direction must be long, short, or null",
                action=action,
                symbol=symbol,
                ignored_fields=ignored,
            )

    reason_value = payload.get("reason", "")
    if not isinstance(reason_value, str):
        return _invalid(
            "reason must be text",
            action=action,
            symbol=symbol,
            direction=direction,
            ignored_fields=ignored,
        )
    reason = " ".join(reason_value.split())[:500]

    if action == "hold":
        return AiSuggestion(
            action="hold",
            symbol=None,
            direction=None,
            stop_price=None,
            target_price=None,
            reason=reason,
            valid=True,
            parse_error=None,
            ignored_fields=ignored,
        )

    if action == "enter":
        if symbol is None:
            return _invalid("enter requires a symbol", action="enter", ignored_fields=ignored, reason=reason)
        if direction not in {"long", "short"}:
            return _invalid(
                "enter requires direction long or short",
                action="enter",
                symbol=symbol,
                ignored_fields=ignored,
                reason=reason,
            )
        try:
            stop_price = _price_string(payload.get("stop_price"), "stop_price")
            target_price = _price_string(payload.get("target_price"), "target_price")
        except ValueError as exc:
            return _invalid(
                str(exc),
                action="enter",
                symbol=symbol,
                direction=direction,
                ignored_fields=ignored,
                reason=reason,
            )
        if not reason:
            return _invalid(
                "enter requires a reason",
                action="enter",
                symbol=symbol,
                direction=direction,
                stop_price=stop_price,
                target_price=target_price,
                ignored_fields=ignored,
            )
    else:
        stop_price = None
        target_price = None
        if not reason:
            return _invalid(
                "exit requires a reason",
                action="exit",
                symbol=symbol,
                direction=direction,
                ignored_fields=ignored,
            )

    sized = tuple(name for name in ignored if name in SIZE_FIELDS)
    if sized:
        logger.info("AI_IGNORED_SIZE_FIELDS %s", ",".join(sized))

    return AiSuggestion(
        action=action,
        symbol=symbol,
        direction=direction,
        stop_price=stop_price if action == "enter" else None,
        target_price=target_price if action == "enter" else None,
        reason=reason,
        valid=True,
        parse_error=None,
        ignored_fields=ignored,
    )


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:500]


def prompt_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the cycle context for the model and drop any raw candle arrays.

    The scanner is supposed to send ``candle_summary`` and ``strategy`` only.
    This is a second check so a raw candle array cannot reach the model.
    """

    payload = json.loads(json.dumps(context, default=str))
    hidden = {"bars", "chart_bars", "candles", "history"}

    def strip(value: object) -> None:
        if isinstance(value, dict):
            for key in hidden:
                value.pop(key, None)
            for child in value.values():
                strip(child)
        elif isinstance(value, list):
            for child in value:
                strip(child)

    strip(payload)
    return payload


def suggest(context: Mapping[str, Any], settings: config.Settings) -> AiSuggestion:
    """Ask the model for one suggestion. Network failures become invalid suggestions."""

    logger.info("AI_REQUEST model=%s", settings.openai_model)
    try:
        from openai import OpenAI
    except ImportError:
        suggestion = _invalid("openai package is not installed")
        logger.error("AI_SUGGESTION %s", suggestion.log_line())
        return suggestion

    try:
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=30.0,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt_payload(context), sort_keys=True)},
            ],
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:
        message = _redact(
            f"{type(exc).__name__}: {exc}",
            (settings.openai_api_key, settings.alpaca_api_key, settings.alpaca_secret_key),
        )
        suggestion = _invalid(f"AI request failed: {message}")
        logger.error("AI_SUGGESTION %s", suggestion.log_line())
        return suggestion

    suggestion = parse_suggestion(content)
    logger.info("AI_SUGGESTION %s", suggestion.log_line())
    return suggestion
