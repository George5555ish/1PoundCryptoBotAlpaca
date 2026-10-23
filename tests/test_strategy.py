"""Momentum, MACD divergence, and chart snapshot tests. No network."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import ai_decision
import charts
import decision_log
import execution
import indicators
import main
import scanner
import strategy


def _bar(high: str, low: str, close: str, open_price: str | None = None) -> dict[str, str]:
    return {
        "open": open_price or close,
        "high": high,
        "low": low,
        "close": close,
        "timestamp": "2026-10-23T01:00:00+00:00",
    }


def _rising_pullback() -> list[dict[str, str]]:
    """Higher highs, then a close that has left the high and reached the 5-bar average."""

    return [
        _bar("100", "99", "99.5"),
        _bar("101", "99.5", "100.5"),
        _bar("102", "100", "101.5"),
        _bar("103", "101", "102.5"),
        _bar("106", "103", "105"),
        _bar("108", "104", "107"),
        _bar("110", "105", "109"),
        _bar("109", "104", "105"),
    ]


class MomentumTests(unittest.TestCase):
    def test_pullback_in_higher_highs_passes(self) -> None:
        bars = _rising_pullback()
        summary = scanner.summarize_candles(bars)
        result = strategy.evaluate_momentum(summary, bars)
        self.assertEqual(summary["trend"], "higher highs")
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["extended"])
        self.assertTrue(result["pullback"])
        self.assertEqual(result["pullback_low"], "104")
        self.assertEqual(result["suggested_stop"], "103.99")
        self.assertEqual(result["target_low"], "106.52")
        self.assertEqual(result["target_high"], "107.02")
        self.assertLess(Decimal(result["atr_stop"]), Decimal(summary["last_close"]))

    def test_extension_fails_the_long(self) -> None:
        bars = _rising_pullback()
        bars[-1] = _bar("109.9", "109", "109.80")
        result = strategy.evaluate_momentum(scanner.summarize_candles(bars), bars)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["extended"])
        self.assertTrue(any("0.3%" in reason for reason in result["reasons"]))

    def test_no_pullback_fails_when_price_is_still_above_the_average(self) -> None:
        bars = _rising_pullback()
        bars[-1] = _bar("109.6", "109", "109.50")
        result = strategy.evaluate_momentum(scanner.summarize_candles(bars), bars)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["extended"])
        self.assertFalse(result["pullback"])
        self.assertTrue(any("5-bar average" in reason for reason in result["reasons"]))

    def test_lower_lows_fail(self) -> None:
        bars = [
            _bar("13", "11", "12"),
            _bar("12", "10", "11"),
            _bar("11", "9", "10"),
            _bar("10", "8", "9"),
        ]
        result = strategy.evaluate_momentum(scanner.summarize_candles(bars), bars)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("higher highs" in reason for reason in result["reasons"]))

    def test_too_few_bars_fail(self) -> None:
        bars = [_bar("10", "9", "9.5"), _bar("11", "10", "10.5")]
        result = strategy.build_context(scanner.summarize_candles(bars), bars)
        self.assertEqual(result["momentum_pullback"]["status"], "fail")
        self.assertEqual(result["macd_divergence"]["status"], "absent")
        self.assertTrue(result["macd_divergence"]["insufficient_bars"])


class MacdTests(unittest.TestCase):
    def test_warmup_positions(self) -> None:
        closes = [Decimal("100")] * 40
        line, signal, histogram = indicators.macd(closes)
        self.assertIsNone(line[24])
        self.assertIsNotNone(line[25])
        self.assertIsNone(signal[32])
        self.assertIsNotNone(signal[33])
        self.assertIsNotNone(histogram[33])

    def test_bullish_divergence_on_the_last_two_swing_lows(self) -> None:
        lows = ["10", "9", "8", "5", "8", "9", "8", "4", "8", "9"]
        bars = [_bar(str(Decimal(low) + 2), low, low) for low in lows]
        macd_line = [Decimal("0")] * 10
        histogram = [Decimal("0")] * 10
        macd_line[3] = Decimal("-5")
        macd_line[7] = Decimal("-1")
        histogram[3] = Decimal("-2")
        histogram[7] = Decimal("-0.4")
        result = indicators.bullish_divergence(bars, macd_line, histogram)
        self.assertEqual(result["status"], "present")
        self.assertEqual(result["source"], "macd and histogram")
        self.assertEqual(result["price_swing_lows"][0]["price"], "5")
        self.assertEqual(result["price_swing_lows"][1]["price"], "4")
        self.assertIn("Bullish divergence", result["description"])

    def test_lower_price_without_a_higher_macd_low_is_absent(self) -> None:
        lows = ["10", "9", "8", "5", "8", "9", "8", "4", "8", "9"]
        bars = [_bar(str(Decimal(low) + 2), low, low) for low in lows]
        macd_line = [Decimal("-1")] * 10
        macd_line[7] = Decimal("-3")
        histogram = [Decimal("-1")] * 10
        histogram[7] = Decimal("-3")
        result = indicators.bullish_divergence(bars, macd_line, histogram)
        self.assertEqual(result["status"], "absent")
        self.assertIn("did not make a higher low", result["description"])
        self.assertEqual(len(result["price_swing_lows"]), 2)


class ChartTests(unittest.TestCase):
    def test_snapshot_marks_a_png(self) -> None:
        bars = []
        for index in range(20):
            price = Decimal(100 + index)
            bars.append(
                {
                    "time": f"2026-10-23T01:{index:02d}:00+00:00",
                    "open": format(price, "f"),
                    "high": format(price + 1, "f"),
                    "low": format(price - 1, "f"),
                    "close": format(price, "f"),
                }
            )
        context = {
            "momentum_pullback": {"status": "fail", "trend": "mixed", "reasons": ["trend is mixed"]},
            "macd_divergence": {
                "status": "present",
                "description": "Bullish divergence on the macd.",
                "price_swing_lows": [
                    {"bar_index": 4, "price": "103", "time": "t", "macd": "-2", "histogram": "-1"},
                    {"bar_index": 12, "price": "101", "time": "t", "macd": "-1", "histogram": "-0.2"},
                ],
            },
        }
        when = datetime(2026, 10, 23, 2, 50, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = charts.save_snapshot("BTC/USD", bars, context, when, Path(folder))
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.name.startswith("20261023T025000Z_BTC-USD"))
            self.assertGreater(path.stat().st_size, 1000)
            skipped = charts.save_snapshot("ETH/USD", [_bar("10", "9", "9.5")], {}, when, Path(folder))
            self.assertIsNone(skipped)


class StrategyLogTests(unittest.TestCase):
    def test_prompt_and_decision_log_receive_strategy_without_bars(self) -> None:
        self.assertIn('momentum_pullback.status is "pass"', ai_decision.SYSTEM_PROMPT)
        self.assertIn("does not override a failed momentum check", ai_decision.SYSTEM_PROMPT)
        view = scanner.SymbolView(
            symbol="BTC/USD",
            bid="100",
            ask="101",
            last="100.5",
            quote_time=None,
            candle_summary={"bar_count": 8, "trend": "higher highs"},
            strategy={
                "momentum_pullback": {"status": "pass", "reasons": ["trend is higher highs"]},
                "macd_divergence": {"status": "present", "description": "Bullish divergence on the macd."},
            },
            chart_bars=({"close": "100"},),
        )
        now = datetime(2026, 10, 23, 2, 50, tzinfo=timezone.utc)
        account = execution.AccountView("100000", "100000", False, "active")
        book = execution.PositionBook((), (), False, False)
        context = main._context(now, account, "100000", book, scanner.ScanResult((view,)), None)
        self.assertEqual(context["symbols"]["BTC/USD"]["strategy"]["momentum_pullback"]["status"], "pass")
        self.assertNotIn("chart_bars", context["symbols"]["BTC/USD"])
        payload = ai_decision.prompt_payload({"symbols": {"BTC/USD": {**context["symbols"]["BTC/USD"], "chart_bars": [{"close": "1"}]}}})
        self.assertNotIn("chart_bars", payload["symbols"]["BTC/USD"])
        self.assertEqual(payload["symbols"]["BTC/USD"]["strategy"]["macd_divergence"]["status"], "present")

        with tempfile.TemporaryDirectory() as folder:
            log = decision_log.DecisionLog(Path(folder) / "decisions.db")
            try:
                row_id = log.write(
                    timestamp=now,
                    market_context=context,
                    ai_suggestion={"action": "hold", "reason": "wait"},
                    risk_verdict="no_trade",
                    risk_reason="hold",
                    risk_decision={"verdict": "no_trade"},
                    position_size=None,
                    final_action="no_trade",
                )
                stored = log.get(row_id)
            finally:
                log.close()
        self.assertIsNotNone(stored)
        assert stored is not None
        stored_strategy = stored["market_context"]["symbols"]["BTC/USD"]["strategy"]
        self.assertEqual(stored_strategy["momentum_pullback"]["status"], "pass")
        self.assertEqual(stored_strategy["macd_divergence"]["status"], "present")
        self.assertNotIn("chart_bars", stored["market_context"]["symbols"]["BTC/USD"])


if __name__ == "__main__":
    unittest.main()
