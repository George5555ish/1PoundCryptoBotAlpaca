"""SQLite audit trail for every crypto decision that could become an order.

The main loop writes a row here before it calls the broker. A failed write
raises, and the caller must not send. ``complete`` is the only way to attach
an order id, and it can only finish a row that is still waiting to be sent.

The same database stores the equity observed at the start of each UTC date.
That row is the baseline for the 3% daily loss halt. The first observation of
a date wins; later calls do not move it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

import config


logger = logging.getLogger("decision_log")

FINAL_ACTIONS = frozenset(
    {
        "rejected",
        "no_trade",
        "dry_run",
        "approved_awaiting_send",
        "submitted",
        "send_failed",
        "send_aborted",
    }
)
_FINISH_ACTIONS = frozenset({"submitted", "send_failed"})
ACTION_TYPES = frozenset({"entry_evaluation", "position_review"})

_CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    market_context_json TEXT NOT NULL,
    ai_suggestion_json TEXT,
    risk_verdict TEXT NOT NULL,
    risk_reason TEXT NOT NULL,
    risk_decision_json TEXT NOT NULL,
    position_size TEXT,
    final_action TEXT NOT NULL,
    order_id TEXT,
    symbol TEXT,
    order_kind TEXT,
    detail TEXT,
    action_type TEXT
)
"""

_CREATE_DAY = """
CREATE TABLE IF NOT EXISTS utc_day_equity (
    utc_date TEXT PRIMARY KEY,
    equity TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _size_text(position_size: str | None) -> str | None:
    """Accept a positive decimal quantity string. Whole shares are not required."""

    if position_size is None:
        return None
    if not isinstance(position_size, str) or isinstance(position_size, bool):
        raise ValueError("position size must be a decimal string or null")
    try:
        qty = Decimal(position_size)
    except Exception as exc:
        raise ValueError("position size must be a decimal string or null") from exc
    if not qty.is_finite() or qty <= 0:
        raise ValueError("position size must be a positive quantity or null")
    return format(qty, "f")


class DecisionLog:
    """One process-wide writer for ``decisions.db``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(_CREATE_DECISIONS)
        self._ensure_action_type()
        self._conn.execute(_CREATE_DAY)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol)"
        )
        self._conn.commit()

    def _ensure_action_type(self) -> None:
        """Add action_type on a database created before position reviews existed."""

        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(decisions)")}
        if "action_type" not in columns:
            self._conn.execute("ALTER TABLE decisions ADD COLUMN action_type TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def day_start_equity(self, now: datetime, equity: str) -> str:
        """Return the equity frozen for ``now``'s UTC date.

        The first call for a date stores ``equity``. Later calls return that
        stored value even if the account has moved. ``now`` must be aware.
        """

        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("day-start timestamp must be timezone-aware")
        try:
            amount = Decimal(equity)
        except Exception as exc:
            raise ValueError("day-start equity must be a decimal string") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("day-start equity must be a positive number")
        utc_date = now.astimezone(timezone.utc).date().isoformat()
        recorded_at = datetime.now(timezone.utc).isoformat()
        stored = format(amount, "f")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO utc_day_equity (utc_date, equity, recorded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(utc_date) DO NOTHING
                """,
                (utc_date, stored, recorded_at),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT equity FROM utc_day_equity WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"UTC day equity for {utc_date} was not stored")
        logger.info("UTC_DAY_EQUITY date=%s equity=%s", utc_date, row["equity"])
        return str(row["equity"])

    def write(
        self,
        *,
        timestamp: datetime,
        market_context: Mapping[str, object],
        ai_suggestion: Mapping[str, object] | None,
        risk_verdict: str,
        risk_reason: str,
        risk_decision: Mapping[str, object],
        position_size: str | None,
        final_action: str,
        order_id: str | None = None,
        symbol: str | None = None,
        order_kind: str | None = None,
        detail: str | None = None,
        action_type: str | None = None,
    ) -> int:
        """Insert one decision and return its row id.

        ``timestamp`` is the UTC clock for the decision. ``recorded_at`` is
        when this process stored the row, also in UTC.
        """

        if final_action not in FINAL_ACTIONS:
            raise ValueError(f"unknown final action {final_action!r}")
        if action_type is not None and action_type not in ACTION_TYPES:
            raise ValueError(f"unknown action type {action_type!r}")
        size_text = _size_text(position_size)
        if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
            raise ValueError("decision timestamp must be timezone-aware")
        if final_action == "approved_awaiting_send" and order_id:
            raise ValueError("an order id cannot exist before the order is sent")

        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO decisions (
                    timestamp, recorded_at, market_context_json, ai_suggestion_json,
                    risk_verdict, risk_reason, risk_decision_json, position_size,
                    final_action, order_id, symbol, order_kind, detail, action_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.astimezone(timezone.utc).isoformat(),
                    recorded_at,
                    _json(dict(market_context)),
                    None if ai_suggestion is None else _json(dict(ai_suggestion)),
                    risk_verdict,
                    risk_reason,
                    _json(dict(risk_decision)),
                    size_text,
                    final_action,
                    order_id,
                    symbol,
                    order_kind,
                    detail,
                    action_type,
                ),
            )
            self._conn.commit()
            row_id = int(cursor.lastrowid)
        logger.info(
            "DECISION_RECORD id=%s action=%s type=%s symbol=%s size=%s verdict=%s order_id=%s",
            row_id,
            final_action,
            action_type,
            symbol,
            size_text,
            risk_verdict,
            order_id,
        )
        return row_id

    def complete(
        self,
        row_id: int,
        *,
        final_action: str,
        order_id: str | None,
        detail: str | None = None,
    ) -> None:
        """Attach the broker result to a row that was stored before the send.

        Refuses to rewrite a row that already has a finished action. The
        risk fields and the position size stay as they were when the send
        was authorized.
        """

        if final_action not in _FINISH_ACTIONS:
            raise ValueError(f"cannot finish a decision as {final_action!r}")
        with self._lock:
            row = self._conn.execute(
                "SELECT final_action FROM decisions WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"decision {row_id} does not exist")
            if row["final_action"] != "approved_awaiting_send":
                raise RuntimeError(
                    f"decision {row_id} is {row['final_action']}; refusing to rewrite it"
                )
            self._conn.execute(
                """
                UPDATE decisions
                SET final_action = ?, order_id = ?, detail = ?
                WHERE id = ? AND final_action = 'approved_awaiting_send'
                """,
                (final_action, order_id, detail, row_id),
            )
            self._conn.commit()
        logger.info(
            "DECISION_RECORD id=%s action=%s order_id=%s",
            row_id,
            final_action,
            order_id,
        )

    def get(self, row_id: int) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM decisions WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["market_context"] = json.loads(payload.pop("market_context_json"))
        suggestion = payload.pop("ai_suggestion_json")
        payload["ai_suggestion"] = None if suggestion is None else json.loads(suggestion)
        payload["risk_decision"] = json.loads(payload.pop("risk_decision_json"))
        return payload

    def latest_entry_levels(self, symbol: str) -> tuple[str | None, str | None]:
        """Stop and target from the newest submitted or dry-run entry for a symbol.

        A position opened before this column existed, or with no stored entry,
        returns ``(None, None)``. The review then cannot use a target or stop
        and falls through to the trend check.
        """

        canonical = config.normalize_symbol(symbol)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT risk_decision_json FROM decisions
                WHERE symbol = ?
                  AND order_kind = ?
                  AND final_action IN ('submitted', 'dry_run')
                ORDER BY id DESC
                LIMIT 1
                """,
                (canonical, "entry_market"),
            ).fetchone()
        if row is None:
            return None, None
        try:
            payload = json.loads(row["risk_decision_json"])
        except Exception:
            logger.exception("ENTRY_LEVELS unreadable symbol=%s", canonical)
            return None, None
        if not isinstance(payload, dict):
            return None, None
        stop = payload.get("stop_price")
        target = payload.get("target_price")
        return (
            str(stop) if stop else None,
            str(target) if target else None,
        )

    def stats(self) -> dict[str, object]:
        """Counts and the newest row for the dashboard. Never touches the broker."""

        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])
            by_action = {
                str(row["final_action"]): int(row["n"])
                for row in self._conn.execute(
                    "SELECT final_action, COUNT(*) AS n FROM decisions GROUP BY final_action"
                )
            }
            latest = self._conn.execute(
                """
                SELECT timestamp, symbol, final_action, order_kind, action_type,
                       risk_verdict, risk_reason, market_context_json
                FROM decisions
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            recent = self._conn.execute(
                """
                SELECT timestamp, symbol, final_action, order_kind, action_type,
                       risk_reason, order_id
                FROM decisions
                ORDER BY id DESC
                LIMIT 8
                """
            ).fetchall()
        equity = None
        day_start = None
        context: dict[str, object] = {}
        if latest is not None:
            try:
                loaded = json.loads(latest["market_context_json"])
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                context = loaded
                equity = context.get("equity")
                day_start = context.get("day_start_equity")
        return {
            "total": total,
            "by_action": by_action,
            "submitted": by_action.get("submitted", 0),
            "dry_run": by_action.get("dry_run", 0),
            "no_trade": by_action.get("no_trade", 0),
            "rejected": by_action.get("rejected", 0),
            "send_aborted": by_action.get("send_aborted", 0),
            "send_failed": by_action.get("send_failed", 0),
            "equity": equity,
            "day_start_equity": day_start,
            "latest": None
            if latest is None
            else {
                "timestamp": latest["timestamp"],
                "symbol": latest["symbol"],
                "final_action": latest["final_action"],
                "order_kind": latest["order_kind"],
                "action_type": latest["action_type"],
                "risk_verdict": latest["risk_verdict"],
                "risk_reason": latest["risk_reason"],
            },
            "recent": [
                {
                    "timestamp": row["timestamp"],
                    "symbol": row["symbol"],
                    "final_action": row["final_action"],
                    "order_kind": row["order_kind"],
                    "action_type": row["action_type"],
                    "risk_reason": row["risk_reason"],
                    "order_id": row["order_id"],
                }
                for row in recent
            ],
        }
