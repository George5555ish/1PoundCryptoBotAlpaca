"""Candle summary tests. No network."""

import unittest
from datetime import datetime, timezone

import ai_decision
import scanner


def _bar(high: str, low: str, close: str) -> dict[str, str]:
    return {"high": high, "low": low, "close": close}


class CandleSummaryTests(unittest.TestCase):
    def test_higher_highs(self) -> None:
        bars = [
            _bar("10", "8", "9"),
            _bar("11", "9", "10"),
            _bar("12", "10", "11"),
            _bar("13", "11", "12"),
        ]
        summary = scanner.summarize_candles(bars)
        self.assertEqual(summary["bar_count"], 4)
        self.assertEqual(summary["trend"], "higher highs")
        self.assertEqual(summary["last_close"], "12")
        self.assertEqual(summary["highest_high"], "13")
        self.assertEqual(summary["lowest_low"], "8")
        self.assertIsNone(summary["error"])

    def test_lower_lows(self) -> None:
        bars = [
            _bar("13", "11", "12"),
            _bar("12", "10", "11"),
            _bar("11", "9", "10"),
            _bar("10", "8", "9"),
        ]
        self.assertEqual(scanner.summarize_candles(bars)["trend"], "lower lows")

    def test_sideways(self) -> None:
        bars = [_bar("10", "9", "9.5") for _ in range(6)]
        summary = scanner.summarize_candles(bars)
        self.assertEqual(summary["trend"], "sideways")
        self.assertEqual(summary["bar_count"], 6)

    def test_mixed_when_range_expands_both_ways(self) -> None:
        bars = [
            _bar("10", "9", "9.5"),
            _bar("10", "9", "9.5"),
            _bar("12", "7", "8"),
            _bar("12", "7", "8"),
        ]
        self.assertEqual(scanner.summarize_candles(bars)["trend"], "mixed")

    def test_fewer_than_four_bars_is_mixed(self) -> None:
        summary = scanner.summarize_candles([_bar("10", "9", "9.5"), _bar("11", "10", "10.5")])
        self.assertEqual(summary["bar_count"], 2)
        self.assertEqual(summary["trend"], "mixed")

    def test_empty_bars(self) -> None:
        summary = scanner.summarize_candles([])
        self.assertEqual(summary["bar_count"], 0)
        self.assertIsNone(summary["last_close"])
        self.assertEqual(summary["error"], "no usable bars")

    def test_prompt_payload_drops_raw_bars(self) -> None:
        payload = ai_decision.prompt_payload(
            {
                "symbols": {
                    "BTC/USD": {
                        "bid": "1",
                        "bars": [{"close": "1"}],
                        "candle_summary": {"bar_count": 1, "trend": "mixed"},
                    }
                }
            }
        )
        self.assertNotIn("bars", payload["symbols"]["BTC/USD"])
        self.assertEqual(payload["symbols"]["BTC/USD"]["candle_summary"]["trend"], "mixed")

    def test_retrieved_candles_and_summary_are_logged(self) -> None:
        rows = [
            {
                "timestamp": datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc),
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
            }
        ]
        summary = scanner.summarize_candles(rows)
        with self.assertLogs("scanner", level="INFO") as captured:
            scanner._log_retrieved_candles("BTC/USD", summary)
        text = "\n".join(captured.output)
        self.assertNotIn("SCAN candle BTC/USD time=", text)
        self.assertIn("SCAN candle_summary BTC/USD bar_count=1", text)
        self.assertIn("last_close=10.5", text)
        self.assertIn("trend=mixed", text)


if __name__ == "__main__":
    unittest.main()
