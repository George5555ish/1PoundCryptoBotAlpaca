"""Small local webpage for paper-account and decision-log totals.

The trading loop stays the authority. This module only reads the SQLite log
and an optional last-trades webhook. A bind or fetch failure is logged and
does not stop a cycle.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

import config
import decision_log


logger = logging.getLogger("dashboard")

WEBHOOK_TIMEOUT_SECONDS = 10
WEBHOOK_CACHE_SECONDS = 30


class Snapshot:
    """Last cycle numbers for the page. Updated in place by the main loop."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.as_of: str | None = None
        self.mode: str = "dry-run"
        self.equity: str | None = None
        self.day_start_equity: str | None = None
        self.day_pnl: str | None = None
        self.positions: list[dict[str, object]] = []
        self.blocked: bool = False

    def update(
        self,
        *,
        as_of: str,
        mode: str,
        equity: str | None,
        day_start_equity: str | None,
        day_pnl: str | None,
        positions: list[dict[str, object]],
        blocked: bool,
    ) -> None:
        with self.lock:
            self.as_of = as_of
            self.mode = mode
            self.equity = equity
            self.day_start_equity = day_start_equity
            self.day_pnl = day_pnl
            self.positions = list(positions)
            self.blocked = blocked

    def as_dict(self) -> dict[str, object]:
        with self.lock:
            return {
                "as_of": self.as_of,
                "mode": self.mode,
                "equity": self.equity,
                "day_start_equity": self.day_start_equity,
                "day_pnl": self.day_pnl,
                "positions": list(self.positions),
                "blocked": self.blocked,
            }


def normalize_trades(payload: object) -> list[dict[str, object]]:
    """Accept a list, a single object, or a ``{trades|data|rows}`` wrapper."""

    items: list[object]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        nested = None
        for key in ("trades", "data", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                nested = value
                break
        items = nested if nested is not None else [payload]
    else:
        items = []
    trades: list[dict[str, object]] = []
    for item in items[:5]:
        if isinstance(item, dict):
            trades.append(item)
    return trades


def fetch_recent_trades(webhook_url: str | None) -> tuple[list[dict[str, object]], str | None]:
    """GET the last-trades webhook. Returns (rows, error)."""

    url = (webhook_url or "").strip()
    if not url:
        return [], "DASHBOARD_TRADES_WEBHOOK is not set"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return [], "dashboard trades webhook is not an http URL"
    try:
        import requests
    except ImportError:
        return [], "requests is not installed"
    try:
        response = requests.get(url, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.exception("DASHBOARD trades webhook failed")
        return [], f"trades webhook failed: {type(exc).__name__}"
    if not response.ok:
        return [], f"trades webhook status {response.status_code}"
    try:
        payload = response.json()
    except Exception:
        return [], "trades webhook did not return JSON"
    return normalize_trades(payload), None


class _WebhookCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fetched_at: float | None = None
        self.trades: list[dict[str, object]] = []
        self.error: str | None = None

    def get(self, webhook_url: str | None) -> tuple[list[dict[str, object]], str | None]:
        now = datetime.now(timezone.utc).timestamp()
        with self.lock:
            if self.fetched_at is not None and now - self.fetched_at < WEBHOOK_CACHE_SECONDS:
                return list(self.trades), self.error
        trades, error = fetch_recent_trades(webhook_url)
        with self.lock:
            self.fetched_at = now
            self.trades = trades
            self.error = error
        return trades, error


def _text(value: object) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def _money(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
    except Exception:
        return _text(value)
    if not number.is_finite():
        return _text(value)
    return html.escape(format(number.quantize(config.CENT), "f"))


def render_page(
    *,
    snapshot: Mapping[str, object],
    stats: Mapping[str, object],
    trades: list[dict[str, object]],
    trades_error: str | None,
) -> str:
    """HTML for GET /. Numbers are escaped."""

    equity = snapshot.get("equity") or stats.get("equity")
    day_start = snapshot.get("day_start_equity") or stats.get("day_start_equity")
    day_pnl = snapshot.get("day_pnl")
    if day_pnl is None:
        try:
            day_pnl = format(Decimal(str(equity)) - Decimal(str(day_start)), "f")
        except Exception:
            day_pnl = None
    positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
    recent = stats.get("recent") if isinstance(stats.get("recent"), list) else []
    latest = stats.get("latest") if isinstance(stats.get("latest"), dict) else {}

    position_rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('symbol') if isinstance(item, dict) else None)}</td>"
        f"<td>{_text(item.get('qty') if isinstance(item, dict) else None)}</td>"
        f"<td>{_money(item.get('unrealized_pl') if isinstance(item, dict) else None)}</td>"
        "</tr>"
        for item in positions
    ) or "<tr><td colspan='3'>No open crypto longs.</td></tr>"

    trade_rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('time') or item.get('timestamp_utc') or item.get('timestamp'))}</td>"
        f"<td>{_text(item.get('symbol') or item.get('ai_symbol'))}</td>"
        f"<td>{_text(item.get('ai_action') or item.get('event_type') or item.get('final_action'))}</td>"
        f"<td>{_money(item.get('equity'))}</td>"
        f"<td>{_text(item.get('ai_reason') or item.get('trade_description') or item.get('momentum_reasons'))}</td>"
        "</tr>"
        for item in trades
    ) or (
        f"<tr><td colspan='5'>{_text(trades_error or 'No recent trades from the webhook.')}</td></tr>"
    )

    log_rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('timestamp') if isinstance(item, dict) else None)}</td>"
        f"<td>{_text(item.get('symbol') if isinstance(item, dict) else None)}</td>"
        f"<td>{_text(item.get('final_action') if isinstance(item, dict) else None)}</td>"
        f"<td>{_text(item.get('action_type') if isinstance(item, dict) else None)}</td>"
        f"<td>{_text(item.get('risk_reason') if isinstance(item, dict) else None)}</td>"
        "</tr>"
        for item in recent
        if isinstance(item, dict)
    ) or "<tr><td colspan='5'>No decision rows yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Crypto paper bot</title>
  <style>
    body {{ font-family: Georgia, serif; background: #111; color: #eee; margin: 0; padding: 24px; }}
    h1, h2 {{ font-weight: 500; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 12px; }}
    .card {{ background: #1c1c1c; border: 1px solid #333; padding: 16px 20px; min-width: 140px; }}
    .card strong {{ display: block; font-size: 1.4rem; margin-top: 6px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0 28px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; vertical-align: top; }}
    th {{ color: #aaa; font-weight: 500; }}
    .muted {{ color: #888; }}
  </style>
</head>
<body>
  <h1>Crypto paper bot</h1>
  <p class="muted">Mode {_text(snapshot.get("mode"))}. Last cycle {_text(snapshot.get("as_of") or latest.get("timestamp"))}. Page refreshes every 30 seconds.</p>
  <div class="cards">
    <div class="card">Equity<strong>${_money(equity)}</strong></div>
    <div class="card">Day start<strong>${_money(day_start)}</strong></div>
    <div class="card">Day P/L<strong>${_money(day_pnl)}</strong></div>
    <div class="card">Decision logs<strong>{_text(stats.get("total"))}</strong></div>
    <div class="card">Submitted<strong>{_text(stats.get("submitted"))}</strong></div>
    <div class="card">Dry runs<strong>{_text(stats.get("dry_run"))}</strong></div>
    <div class="card">Holds / no trade<strong>{_text(stats.get("no_trade"))}</strong></div>
    <div class="card">Aborted / failed<strong>{_text((stats.get("send_aborted") or 0) + (stats.get("send_failed") or 0))}</strong></div>
  </div>
  <h2>Open positions</h2>
  <table><thead><tr><th>Symbol</th><th>Qty</th><th>Unrealized P/L</th></tr></thead><tbody>{position_rows}</tbody></table>
  <h2>Last trades from the webhook</h2>
  <table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Equity</th><th>Note</th></tr></thead><tbody>{trade_rows}</tbody></table>
  <h2>Recent decision log</h2>
  <table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Type</th><th>Reason</th></tr></thead><tbody>{log_rows}</tbody></table>
</body>
</html>
"""


def build_payload(
    *,
    snapshot: Mapping[str, object],
    stats: Mapping[str, object],
    trades: list[dict[str, object]],
    trades_error: str | None,
) -> dict[str, object]:
    return {
        "snapshot": dict(snapshot),
        "stats": dict(stats),
        "trades": trades,
        "trades_error": trades_error,
    }


def start(
    decisions: decision_log.DecisionLog,
    snapshot: Snapshot,
    webhook_url: str | None,
    port: int,
) -> ThreadingHTTPServer | None:
    """Serve GET / and GET /api on a background thread. Returns None on bind failure."""

    cache = _WebhookCache()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.info("DASHBOARD %s", format % args)

        def _payload(self) -> dict[str, object]:
            try:
                stats = decisions.stats()
            except Exception:
                logger.exception("DASHBOARD stats failed")
                stats = {"total": 0, "by_action": {}, "recent": [], "latest": None}
            trades, error = cache.get(webhook_url)
            return build_payload(
                snapshot=snapshot.as_dict(),
                stats=stats,
                trades=trades,
                trades_error=error,
            )

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/", "/api", "/api/"}:
                self.send_error(404)
                return
            payload = self._payload()
            if path.startswith("/api"):
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            page = render_page(
                snapshot=payload["snapshot"],
                stats=payload["stats"],
                trades=list(payload["trades"]),
                trades_error=payload["trades_error"] if isinstance(payload["trades_error"], str) else None,
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except Exception:
        logger.exception("DASHBOARD bind failed port=%s", port)
        return None
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    logger.info("DASHBOARD listening on 0.0.0.0:%s", port)
    return server
