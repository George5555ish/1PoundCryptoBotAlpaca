"""n8n webhook tests. No network."""

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import config
import notify


def _settings_environ(**extra: str) -> dict[str, str]:
    base = {
        "ALPACA_PAPER": "true",
        "ALPACA_API_KEY": "PKtest",
        "ALPACA_SECRET_KEY": "secret",
        "OPENAI_API_KEY": "xai-test",
    }
    base.update(extra)
    return base


class WebhookTests(unittest.TestCase):
    def test_missing_url_warns_and_does_not_post(self) -> None:
        with patch("requests.post") as post:
            with self.assertLogs("notify", level="WARNING") as captured:
                notify.post_decision({"final_action": "no_trade"}, "  ")
        post.assert_not_called()
        self.assertIn("N8N_WEBHOOK_URL is not set", "\n".join(captured.output))

    def test_post_sends_json_with_a_timeout(self) -> None:
        response = Mock(ok=True, status_code=200)
        with patch("requests.post", return_value=response) as post:
            with self.assertLogs("notify", level="INFO") as captured:
                notify.post_decision({"final_action": "no_trade"}, "https://n8n.example/webhook/secret")
        post.assert_called_once_with(
            "https://n8n.example/webhook/secret",
            json={"final_action": "no_trade"},
            timeout=10,
        )
        self.assertIn("N8N_WEBHOOK ok status=200", "\n".join(captured.output))

    def test_http_error_and_network_error_do_not_raise(self) -> None:
        response = Mock(ok=False, status_code=500)
        with patch("requests.post", return_value=response):
            with self.assertLogs("notify", level="ERROR"):
                notify.post_decision({"final_action": "no_trade"}, "https://n8n.example/hook")
        with patch("requests.post", side_effect=TimeoutError("slow")):
            with self.assertLogs("notify", level="ERROR"):
                notify.post_decision({"final_action": "no_trade"}, "https://n8n.example/hook")

    def test_rows_use_the_sheet_columns(self) -> None:
        when = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)
        strategy = {
            "momentum_pullback": {
                "status": "fail",
                "reasons": ["no pullback", "extended"],
                "trend": "higher highs",
                "extended": True,
                "pullback": False,
                "distance_from_high": "0.001",
                "pullback_low": None,
                "suggested_stop": None,
                "target_low": None,
                "target_high": None,
            },
            "macd_divergence": {"status": "absent", "description": "No bullish divergence."},
        }
        rows = notify.build_rows(
            timestamp=when,
            allow_send=False,
            context={
                "equity": "100.00",
                "day_start_equity": "90.00",
                "day_pnl_usd": "10.00",
                "symbols": {
                    "BTC/USD": {"strategy": strategy, "chart_bars": [{"close": "1"}]},
                    "ETH/USD": {"strategy": strategy},
                },
            },
            suggestion={"action": "hold", "reason": "wait", "symbol": None, "direction": None, "stop_price": None, "target_price": None},
            verdict="no_trade",
            reason="hold",
            approved=False,
            final_action="no_trade",
            order_id=None,
            chart_filenames=["20261023T110000Z_BTC-USD.png", "20261023T110000Z_ETH-USD.png"],
            symbol=None,
            risk_decision={"verdict": "no_trade", "approved": False, "reason": "hold"},
        )
        self.assertEqual([row["symbol"] for row in rows], ["BTC/USD", "ETH/USD"])
        btc = rows[0]
        self.assertEqual(list(btc.keys()), list(notify.SHEET_FIELDS))
        self.assertEqual(btc["timestamp_utc"], "2026-10-23T11:00:00+00:00")
        self.assertEqual(btc["mode"], "dry-run")
        self.assertEqual(btc["equity"], "100.00")
        self.assertEqual(btc["day_start_equity"], "90.00")
        self.assertEqual(btc["day_pnl"], "10.00")
        self.assertEqual(btc["trend"], "higher highs")
        self.assertEqual(btc["momentum_status"], "fail")
        self.assertEqual(btc["momentum_reasons"], "no pullback | extended")
        self.assertTrue(btc["extended"])
        self.assertFalse(btc["pullback"])
        self.assertEqual(btc["distance_from_high"], "0.001")
        self.assertEqual(btc["macd_divergence_status"], "absent")
        self.assertEqual(btc["macd_divergence_description"], "No bullish divergence.")
        self.assertEqual(btc["ai_action"], "hold")
        self.assertEqual(btc["ai_reason"], "wait")
        self.assertEqual(btc["risk_gate_verdict"], "no_trade")
        self.assertFalse(btc["risk_gate_approved"])
        self.assertEqual(btc["risk_gate_reason"], "hold")
        self.assertEqual(btc["final_action"], "no_trade")
        self.assertIsNone(btc["order_id"])
        self.assertEqual(btc["chart_filename"], "20261023T110000Z_BTC-USD.png")
        self.assertEqual(rows[1]["chart_filename"], "20261023T110000Z_ETH-USD.png")
        self.assertNotIn("chart_bars", btc["raw_strategy_json"])
        self.assertIn("no pullback", btc["raw_strategy_json"])
        self.assertIn('"action": "hold"', btc["raw_ai_suggestion"])
        self.assertIn('"verdict": "no_trade"', btc["raw_risk_gate"])

    def test_settings_keep_a_blank_webhook_optional_and_mask_it(self) -> None:
        blank = config.load_settings(_settings_environ())
        self.assertIsNone(blank.n8n_webhook_url)
        secret = "https://n8n.example/webhook/abc123"
        set_up = config.load_settings(_settings_environ(N8N_WEBHOOK_URL=f"  {secret}  "))
        self.assertEqual(set_up.n8n_webhook_url, secret)
        self.assertNotIn(secret, repr(set_up))
        self.assertIn("n8n_webhook_url=set", repr(set_up))


if __name__ == "__main__":
    unittest.main()
