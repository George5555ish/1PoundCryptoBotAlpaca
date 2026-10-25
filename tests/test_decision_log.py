"""Audit log tests. The database is a temp file."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

import decision_log


class DecisionLogTests(unittest.TestCase):
    def test_round_trip_and_complete(self) -> None:
        store_path = Path(__file__).resolve().parent / "_tmp_decisions.db"
        if store_path.exists():
            store_path.unlink()
        log = decision_log.DecisionLog(store_path)
        now = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)
        try:
            start = log.day_start_equity(now, "100000.5")
            again = log.day_start_equity(now + timedelta(hours=1), "90000")
            self.assertEqual(start, again)
            nxt = log.day_start_equity(now + timedelta(days=1), "90000")
            self.assertEqual(nxt, "90000")
            row_id = log.write(
                timestamp=now,
                market_context={"equity": "100000.5", "positions": []},
                ai_suggestion={"action": "enter", "symbol": "BTC/USD"},
                risk_verdict="approve",
                risk_reason="sized",
                risk_decision={"approved": True},
                position_size="0.01000000",
                final_action="approved_awaiting_send",
                symbol="BTC/USD",
                order_kind="entry_market",
            )
            stored = log.get(row_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["position_size"], "0.01000000")
            self.assertEqual(stored["ai_suggestion"]["symbol"], "BTC/USD")
            self.assertIsNone(stored["order_id"])
            log.complete(row_id, final_action="submitted", order_id="order-1", detail="ok")
            finished = log.get(row_id)
            assert finished is not None
            self.assertEqual(finished["order_id"], "order-1")
            self.assertEqual(finished["final_action"], "submitted")
            with self.assertRaises(RuntimeError):
                log.complete(row_id, final_action="submitted", order_id="order-2")
        finally:
            log.close()
            store_path.unlink(missing_ok=True)

    def test_rejection_cannot_gain_an_order_id(self) -> None:
        store_path = Path(__file__).resolve().parent / "_tmp_reject.db"
        if store_path.exists():
            store_path.unlink()
        log = decision_log.DecisionLog(store_path)
        try:
            row_id = log.write(
                timestamp=datetime.now(timezone.utc),
                market_context={"equity": "1"},
                ai_suggestion=None,
                risk_verdict="reject",
                risk_reason="no",
                risk_decision={"approved": False},
                position_size=None,
                final_action="rejected",
                symbol="ETH/USD",
                order_kind="none",
            )
            with self.assertRaises(RuntimeError):
                log.complete(row_id, final_action="submitted", order_id="nope")
        finally:
            log.close()
            store_path.unlink(missing_ok=True)

    def test_naive_timestamp_is_refused(self) -> None:
        store_path = Path(__file__).resolve().parent / "_tmp_naive.db"
        log = decision_log.DecisionLog(store_path)
        try:
            with self.assertRaises(ValueError):
                log.write(
                    timestamp=datetime(2026, 10, 22, 21, 0),
                    market_context={},
                    ai_suggestion=None,
                    risk_verdict="reject",
                    risk_reason="no",
                    risk_decision={},
                    position_size=None,
                    final_action="rejected",
                )
        finally:
            log.close()
            store_path.unlink(missing_ok=True)

    def test_action_type_and_entry_levels_round_trip(self) -> None:
        store_path = Path(__file__).resolve().parent / "_tmp_levels.db"
        if store_path.exists():
            store_path.unlink()
        log = decision_log.DecisionLog(store_path)
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        try:
            self.assertEqual(log.latest_entry_levels("BTC/USD"), (None, None))
            row_id = log.write(
                timestamp=now,
                market_context={
                    "action_type": "position_review",
                    "symbol": "ETH/USD",
                    "current_price": "2500",
                    "unrealized_pl": "10",
                    "decision": "hold",
                    "reason": "hold; trend is higher highs and neither the stop nor the target has been hit",
                },
                ai_suggestion=None,
                risk_verdict="no_trade",
                risk_reason="hold",
                risk_decision={"approved": False},
                position_size="0.5",
                final_action="no_trade",
                symbol="ETH/USD",
                action_type="position_review",
            )
            stored = log.get(row_id)
            assert stored is not None
            self.assertEqual(stored["action_type"], "position_review")
            self.assertEqual(stored["market_context"]["decision"], "hold")
            self.assertEqual(stored["market_context"]["unrealized_pl"], "10")
            log.write(
                timestamp=now,
                market_context={"action_type": "entry_evaluation"},
                ai_suggestion={"action": "enter"},
                risk_verdict="approve",
                risk_reason="sized",
                risk_decision={"stop_price": "99000", "target_price": "102000", "approved": True},
                position_size="0.01",
                final_action="dry_run",
                symbol="BTC/USD",
                order_kind="entry_market",
                action_type="entry_evaluation",
            )
            self.assertEqual(log.latest_entry_levels("BTCUSD"), ("99000", "102000"))
            with self.assertRaises(ValueError):
                log.write(
                    timestamp=now,
                    market_context={},
                    ai_suggestion=None,
                    risk_verdict="no_trade",
                    risk_reason="no",
                    risk_decision={},
                    position_size=None,
                    final_action="no_trade",
                    action_type="guess",
                )
        finally:
            log.close()
            store_path.unlink(missing_ok=True)

    def test_submit_is_only_called_after_the_audit_insert(self) -> None:
        source = Path(__file__).resolve().parent.parent.joinpath("main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("execution.submit("), 1)
        insert_at = source.index('final_action="approved_awaiting_send"')
        submit_at = source.index("execution.submit(")
        self.assertLess(insert_at, submit_at)


if __name__ == "__main__":
    unittest.main()
