"""Dashboard helpers. No live webhook."""

from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import config
import dashboard
import decision_log


class DashboardTests(unittest.TestCase):
    def test_normalize_trades_accepts_one_object_or_a_list(self) -> None:
        single = dashboard.normalize_trades({"time": "t", "equity": 1, "ai_action": "hold"})
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["ai_action"], "hold")
        wrapped = dashboard.normalize_trades({"trades": [{"ai_action": "enter"}, {"ai_action": "exit"}, {}, {}, {}, {"skip": True}]})
        self.assertEqual(len(wrapped), 5)
        self.assertEqual(dashboard.normalize_trades("nope"), [])

    def test_page_includes_counts_and_equity(self) -> None:
        page = dashboard.render_page(
            snapshot={
                "as_of": "2026-10-25T13:00:00+00:00",
                "mode": "paper-send",
                "equity": "99999.98",
                "day_start_equity": "100000",
                "day_pnl": "-0.02",
                "positions": [{"symbol": "BTC/USD", "qty": "0.01", "unrealized_pl": "1.5"}],
                "blocked": False,
            },
            stats={
                "total": 12,
                "submitted": 2,
                "dry_run": 3,
                "no_trade": 7,
                "send_aborted": 0,
                "send_failed": 0,
                "recent": [
                    {
                        "timestamp": "2026-10-25T13:00:00+00:00",
                        "symbol": "ETH/USD",
                        "final_action": "no_trade",
                        "action_type": "entry_evaluation",
                        "risk_reason": "hold",
                    }
                ],
                "latest": {},
            },
            trades=[{"time": "2026-10-25T13:12:05+00:00", "equity": 99999.98, "ai_action": "hold", "ai_reason": "wait"}],
            trades_error=None,
        )
        self.assertIn("99999.98", page)
        self.assertIn(">12<", page)
        self.assertIn("BTC/USD", page)
        self.assertIn("hold", page)
        self.assertIn("paper-send", page)
        self.assertNotIn("<script>", page)

    def test_fetch_recent_trades_does_not_raise(self) -> None:
        trades, error = dashboard.fetch_recent_trades("")
        self.assertEqual(trades, [])
        self.assertIn("not set", error or "")
        response = Mock(ok=True)
        response.json.return_value = {"ai_action": "hold", "equity": 1}
        with patch("requests.get", return_value=response) as get:
            trades, error = dashboard.fetch_recent_trades("https://example.test/hook")
        get.assert_called_once()
        self.assertIsNone(error)
        self.assertEqual(trades[0]["ai_action"], "hold")

    def test_settings_default_the_trades_webhook_and_port(self) -> None:
        settings = config.load_settings(
            {
                "ALPACA_PAPER": "true",
                "ALPACA_API_KEY": "PKtest",
                "ALPACA_SECRET_KEY": "secret",
                "OPENAI_API_KEY": "xai-test",
            }
        )
        self.assertEqual(settings.dashboard_trades_webhook, config.DEFAULT_DASHBOARD_TRADES_WEBHOOK)
        self.assertEqual(settings.dashboard_port, 8080)
        self.assertNotIn("10410755", repr(settings))

    def test_stats_round_trip_from_the_log(self) -> None:
        path = Path(__file__).resolve().parent / "_tmp_dash.db"
        if path.exists():
            path.unlink()
        log = decision_log.DecisionLog(path)
        try:
            empty = log.stats()
            self.assertEqual(empty["total"], 0)
            log.write(
                timestamp=datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc),
                market_context={"equity": "50", "day_start_equity": "40"},
                ai_suggestion=None,
                risk_verdict="no_trade",
                risk_reason="hold",
                risk_decision={"approved": False},
                position_size=None,
                final_action="no_trade",
            )
            summary = log.stats()
            self.assertEqual(summary["no_trade"], 1)
            self.assertEqual(summary["equity"], "50")
        finally:
            log.close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
