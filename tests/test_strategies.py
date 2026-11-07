"""Rule-based paper strategies. No broker and no model."""

from decimal import Decimal
import unittest

import config
import strategies
from strategies import common
from strategies import current_momentum_pullback
from strategies import ema_stack_pullback
from strategies import anchored_vwap_mean_reversion
from strategies import vwap_squeeze_breakout
import multi_runner
import scanner


def _bars(closes: list[float], volume: float = 1000.0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for close in closes:
        price = Decimal(str(close))
        rows.append(
            {
                "open": str(price),
                "high": str(price + Decimal("0.20")),
                "low": str(price - Decimal("0.20")),
                "close": str(price),
                "volume": str(volume),
            }
        )
    return rows


class StrategyEngineTests(unittest.TestCase):
    def test_all_seven_load_and_hold_on_empty_candles(self) -> None:
        self.assertEqual(
            config.MULTI_STRATEGY_IDS,
            ("anchored_vwap_mean_reversion", "ema_stack_pullback"),
        )
        for strategy_id in config.MULTI_STRATEGY_CATALOG:
            evaluate = strategies.load_evaluate(strategy_id)
            payload = evaluate("SPY", [], {})
            self.assertEqual(set(payload), {"action", "direction", "stop_price", "target_price", "reason", "strategy_id"})
            self.assertEqual(payload["strategy_id"], strategy_id)
            self.assertEqual(payload["action"], "hold")
            self.assertIsInstance(payload["reason"], str)

    def test_unknown_strategy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            strategies.enabled_ids(("not_a_strategy",))

    def test_mean_reversion_shorts_a_stretch_above_vwap(self) -> None:
        candles = _bars([100.0] * 20 + [109.0])
        payload = anchored_vwap_mean_reversion.evaluate("SPY", candles, {})
        self.assertEqual(payload["action"], "enter")
        self.assertEqual(payload["direction"], "short")
        self.assertGreater(payload["stop_price"], 109.0)
        self.assertLess(payload["target_price"], 109.0)

    def test_mean_reversion_longs_a_stretch_below_vwap(self) -> None:
        candles = _bars([100.0] * 20 + [91.0])
        payload = anchored_vwap_mean_reversion.evaluate("QQQ", candles, {})
        self.assertEqual(payload["action"], "enter")
        self.assertEqual(payload["direction"], "long")
        self.assertLess(payload["stop_price"], 91.0)
        self.assertGreater(payload["target_price"], 91.0)

    def test_ema_stack_holds_when_the_stack_is_inverted(self) -> None:
        candles = _bars(list(range(80, 20, -1)))
        payload = ema_stack_pullback.evaluate("AAPL", candles, {})
        self.assertEqual(payload["action"], "hold")

    def test_squeeze_breakout_enters_after_a_tight_range(self) -> None:
        candles = _bars([100.0] * 14 + [100.40])
        payload = vwap_squeeze_breakout.evaluate("NVDA", candles, {})
        self.assertEqual(payload["action"], "enter")
        self.assertEqual(payload["direction"], "long")

    def test_current_momentum_wrapper_holds_when_the_existing_rule_fails(self) -> None:
        candles = _bars([100.0] * 30)
        payload = current_momentum_pullback.evaluate("MSFT", candles, {})
        self.assertEqual(payload["action"], "hold")
        self.assertEqual(payload["strategy_id"], "current_momentum_pullback")

    def test_first_signal_skips_exit_when_flat(self) -> None:
        view = scanner.SymbolView(
            symbol="SPY",
            bid="499.9",
            ask="500",
            last="500",
            quote_time=None,
            candle_summary={},
            strategy={},
            chart_bars=tuple(_bars([100.0] * 20 + [109.0])),
        )

        def _exit(symbol, candles, context):
            return common.exit_position("ema_stack_pullback", "leave")

        def _enter(symbol, candles, context):
            return common.enter(
                "vwap_orb_pullback",
                direction="long",
                close=Decimal("100"),
                stop=Decimal("99"),
                reason="take it",
            )

        original = strategies.load_evaluate
        mapping = {
            "ema_stack_pullback": _exit,
            "vwap_orb_pullback": _enter,
        }

        def _load(strategy_id):
            return mapping.get(strategy_id) or original(strategy_id)

        strategies.load_evaluate = _load  # type: ignore[method-assign]
        try:
            payload = multi_runner.first_signal(
                "SPY",
                view,
                ("ema_stack_pullback", "vwap_orb_pullback"),
                held=False,
            )
        finally:
            strategies.load_evaluate = original  # type: ignore[method-assign]
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "enter")
        self.assertEqual(payload["strategy_id"], "vwap_orb_pullback")

    def test_cash_session_weekday_and_weekend(self) -> None:
        from datetime import datetime, timezone

        monday_open = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
        saturday = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(multi_runner.cash_session_open(monday_open))
        self.assertFalse(multi_runner.cash_session_open(saturday))

    def test_closed_session_idles_until_next_open(self) -> None:
        from datetime import datetime, timezone

        friday_night = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc)  # 17:00 EDT
        monday_open = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)  # 09:30 EDT
        self.assertEqual(multi_runner.session_status(friday_night), "closed")
        self.assertEqual(multi_runner.next_cash_open(friday_night), monday_open)
        self.assertEqual(
            multi_runner.cycle_sleep_seconds(friday_night, 300, "closed"),
            multi_runner.CLOSED_HEARTBEAT_SECONDS,
        )

    def test_flatten_window_before_the_bell(self) -> None:
        from datetime import datetime, timezone

        flatten = datetime(2026, 10, 26, 19, 50, tzinfo=timezone.utc)  # 15:50 EDT
        after_close = datetime(2026, 10, 26, 20, 5, tzinfo=timezone.utc)  # 16:05 EDT
        self.assertEqual(multi_runner.session_status(flatten), "flatten")
        self.assertEqual(multi_runner.session_status(after_close), "closed")
        self.assertEqual(multi_runner.session_status(flatten, broker_open=False), "flatten")
        self.assertEqual(
            multi_runner.cycle_sleep_seconds(flatten, 300, "flatten"),
            config.MIN_LOOP_INTERVAL_SECONDS,
        )
        self.assertEqual(
            multi_runner.session_status(after_close, holding_equity=True),
            "flatten",
        )
        self.assertEqual(
            multi_runner.cycle_sleep_seconds(after_close, 300, "flatten"),
            multi_runner.CLOSED_HEARTBEAT_SECONDS,
        )

    def test_holiday_clock_overrides_the_weekday_calendar(self) -> None:
        from datetime import datetime, timezone

        thursday_noon = datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc)  # Thanksgiving
        self.assertTrue(multi_runner.cash_session_open(thursday_noon))
        self.assertEqual(multi_runner.session_status(thursday_noon, broker_open=False), "closed")
