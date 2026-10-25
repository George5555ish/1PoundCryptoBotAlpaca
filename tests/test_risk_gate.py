"""Unit tests for the crypto risk gate. No broker and no model."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import unittest

import config
import risk_gate


NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)  # Saturday


def _request(**overrides) -> risk_gate.GateRequest:
    base = dict(
        action="enter",
        symbol="BTC/USD",
        direction="long",
        stop_price="99000",
        target_price="102000",
        reason="test",
        suggestion_valid=True,
        parse_error=None,
        bid="99990",
        ask="100000",
        quote_time=NOW,
        equity="100000",
        day_start_equity="100000",
        buying_power="100000",
        open_positions=(),
        pending_entry_while_flat=False,
        other_position_open=False,
        trading_blocked=False,
        now=NOW,
    )
    base.update(overrides)
    return risk_gate.GateRequest(**base)


class RiskGateTests(unittest.TestCase):
    def test_constants_are_the_crypto_rules(self) -> None:
        self.assertEqual(config.ALLOWED_SYMBOLS, ("BTC/USD", "ETH/USD"))
        self.assertEqual(config.MAX_RISK_FRACTION, Decimal("0.01"))
        self.assertEqual(config.DAILY_LOSS_FRACTION, Decimal("0.03"))
        self.assertEqual(config.MAX_OPEN_POSITIONS, 5)

    def test_gate_does_not_read_the_soft_target(self) -> None:
        source = Path(risk_gate.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SOFT_DAILY_PROFIT_TARGET", source)
        self.assertNotIn("format_soft_target_note", source)

    def test_weekend_long_is_allowed(self) -> None:
        decision = risk_gate.evaluate(_request())
        self.assertTrue(decision.approved)
        self.assertEqual(decision.order_kind, risk_gate.ORDER_KIND_ENTRY)
        self.assertEqual(decision.side, "buy")
        self.assertEqual(decision.symbol, "BTC/USD")
        qty = Decimal(decision.qty or "0")
        self.assertGreater(qty, 0)
        budget = Decimal("1000")
        self.assertLessEqual(Decimal(decision.planned_loss or "0"), budget)
        self.assertLessEqual(Decimal(decision.planned_loss_with_slippage or "0"), budget)
        self.assertLessEqual(qty * Decimal("100000"), Decimal("100000"))

    def test_short_is_rejected(self) -> None:
        decision = risk_gate.evaluate(
            _request(direction="short", stop_price="101000", target_price="98000")
        )
        self.assertFalse(decision.approved)
        self.assertIn("long only", decision.reason)

    def test_symbol_outside_the_universe_is_rejected(self) -> None:
        decision = risk_gate.evaluate(_request(symbol="SOL/USD", ask="150", bid="149.9", stop_price="140", target_price="170"))
        self.assertFalse(decision.approved)
        self.assertIn("universe", decision.reason)

    def test_btcusd_without_slash_is_the_same_pair(self) -> None:
        decision = risk_gate.evaluate(_request(symbol="BTCUSD"))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.symbol, "BTC/USD")

    def test_daily_loss_halts_at_three_percent(self) -> None:
        under = risk_gate.evaluate(_request(equity="9700.01", day_start_equity="10000", buying_power="9700.01"))
        exact = risk_gate.evaluate(_request(equity="9700", day_start_equity="10000", buying_power="9700"))
        self.assertTrue(under.approved)
        self.assertFalse(exact.approved)
        self.assertTrue(risk_gate.daily_loss_halted("9700", "10000"))
        self.assertFalse(risk_gate.daily_loss_halted("9700.01", "10000"))
        self.assertTrue(risk_gate.daily_loss_halted(None, "10000"))

    def test_exit_still_allowed_during_halt(self) -> None:
        position = risk_gate.OpenPosition(symbol="ETH/USD", qty=Decimal("0.5"))
        decision = risk_gate.evaluate(
            _request(
                action="exit",
                symbol="ETH/USD",
                direction="long",
                equity="9000",
                day_start_equity="10000",
                buying_power="9000",
                open_positions=(position,),
                stop_price=None,
                target_price=None,
            )
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.order_kind, risk_gate.ORDER_KIND_EXIT)
        self.assertEqual(Decimal(decision.qty or "0"), Decimal("0.5"))

    def test_second_symbol_is_allowed(self) -> None:
        held = risk_gate.OpenPosition(symbol="ETH/USD", qty=Decimal("1"))
        decision = risk_gate.evaluate(_request(open_positions=(held,)))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.symbol, "BTC/USD")

    def test_same_symbol_is_not_added_to(self) -> None:
        held = risk_gate.OpenPosition(symbol="BTC/USD", qty=Decimal("0.01"))
        decision = risk_gate.evaluate(_request(open_positions=(held,)))
        self.assertFalse(decision.approved)
        self.assertIn("already long", decision.reason)

    def test_fifth_position_fills_the_book(self) -> None:
        held = tuple(
            risk_gate.OpenPosition(symbol=f"X{index}/USD", qty=Decimal("1"))
            for index in range(5)
        )
        decision = risk_gate.evaluate(_request(open_positions=held))
        self.assertFalse(decision.approved)
        self.assertIn("max open positions is 5", decision.reason)

    def test_pending_buy_blocks_only_that_symbol(self) -> None:
        blocked = risk_gate.evaluate(_request(pending_symbols=("BTC/USD",)))
        allowed = risk_gate.evaluate(_request(symbol="ETH/USD", ask="3000", bid="2999", stop_price="2900", target_price="3300", pending_symbols=("BTC/USD",)))
        self.assertFalse(blocked.approved)
        self.assertIn("working entry", blocked.reason)
        self.assertTrue(allowed.approved)

    def test_other_account_position_blocks_entry(self) -> None:
        decision = risk_gate.evaluate(_request(other_position_open=True))
        self.assertFalse(decision.approved)
        self.assertIn("another position", decision.reason)

    def test_hold_does_not_size_prices(self) -> None:
        decision = risk_gate.evaluate(_request(action="hold", stop_price="1", target_price="1"))
        self.assertEqual(decision.verdict, risk_gate.VERDICT_NO_TRADE)
        self.assertIsNone(decision.qty)

    def test_naive_time_is_rejected(self) -> None:
        decision = risk_gate.evaluate(_request(now=datetime(2026, 10, 24, 18, 0)))
        self.assertFalse(decision.approved)
        self.assertIn("timezone-aware", decision.reason)

    def test_wide_spread_is_rejected(self) -> None:
        decision = risk_gate.evaluate(_request(bid="100000", ask="100300"))
        self.assertFalse(decision.approved)
        self.assertIn("spread", decision.reason)

    def test_tight_stop_is_capped_by_equity(self) -> None:
        decision = risk_gate.evaluate(
            _request(
                equity="10000",
                day_start_equity="10000",
                buying_power="10000",
                bid="49990",
                ask="50000",
                stop_price="49750",
                target_price="51000",
            )
        )
        self.assertTrue(decision.approved)
        self.assertTrue(decision.size_capped)
        self.assertEqual(Decimal(decision.qty or "0"), Decimal("0.2"))

    def test_notional_below_one_dollar_is_rejected(self) -> None:
        decision = risk_gate.evaluate(
            _request(
                equity="50",
                buying_power="50",
                day_start_equity="50",
                bid="99990",
                ask="100000",
                stop_price="50000",
                target_price="120000",
            )
        )
        self.assertFalse(decision.approved)
        self.assertTrue(
            "zero" in decision.reason or "one-dollar" in decision.reason
        )

    def test_two_protected_longs_stay_open(self) -> None:
        btc = risk_gate.OpenPosition("BTC/USD", Decimal("0.1"))
        eth = risk_gate.OpenPosition("ETH/USD", Decimal("1"))
        orders = (
            risk_gate.WorkingOrder("s1", "BTC/USD", "sell"),
            risk_gate.WorkingOrder("s2", "ETH/USD", "sell"),
        )
        self.assertEqual(risk_gate.plan_repairs((btc, eth), orders), ())

    def test_buy_for_the_other_symbol_is_kept(self) -> None:
        btc = risk_gate.OpenPosition("BTC/USD", Decimal("0.1"))
        orders = (
            risk_gate.WorkingOrder("stop-1", "BTC/USD", "sell"),
            risk_gate.WorkingOrder("buy-eth", "ETH/USD", "buy"),
        )
        self.assertEqual(risk_gate.plan_repairs((btc,), orders), ())

    def test_duplicate_buy_is_cancelled(self) -> None:
        orders = (
            risk_gate.WorkingOrder("buy-1", "BTC/USD", "buy"),
            risk_gate.WorkingOrder("buy-2", "BTC/USD", "buy"),
        )
        actions = risk_gate.plan_repairs((), orders)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].order_id, "buy-2")
        self.assertEqual(actions[0].kind, risk_gate.ORDER_KIND_CANCEL)

    def test_unprotected_long_is_closed(self) -> None:
        btc = risk_gate.OpenPosition("BTC/USD", Decimal("0.01"))
        actions = risk_gate.plan_repairs((btc,), ())
        self.assertEqual(actions[0].reason, "open long has no protective sell order")

    def test_protected_long_is_left_alone(self) -> None:
        btc = risk_gate.OpenPosition("BTC/USD", Decimal("0.01"))
        orders = (risk_gate.WorkingOrder("stop-1", "BTC/USD", "sell"),)
        self.assertEqual(risk_gate.plan_repairs((btc,), orders), ())

    def test_stray_sell_while_flat_is_cancelled(self) -> None:
        orders = (risk_gate.WorkingOrder("stop-1", "BTC/USD", "sell"),)
        actions = risk_gate.plan_repairs((), orders)
        self.assertEqual(actions[0].kind, risk_gate.ORDER_KIND_CANCEL)
        self.assertEqual(actions[0].order_id, "stop-1")

    def test_same_order_allows_a_small_quote_move(self) -> None:
        first = risk_gate.evaluate(_request())
        second = risk_gate.evaluate(_request(equity="100000"))
        drifted = risk_gate.evaluate(_request(stop_price="98950", target_price="102050"))
        changed = risk_gate.evaluate(_request(stop_price="98000"))
        rejected = risk_gate.evaluate(_request(direction="short", stop_price="101000", target_price="98000"))
        self.assertTrue(risk_gate.same_order(first, second))
        self.assertTrue(risk_gate.same_order(first, drifted))
        self.assertIsNone(risk_gate.second_pass_mismatch(first, drifted))
        self.assertFalse(risk_gate.same_order(first, changed))
        self.assertIn("stop moved", risk_gate.second_pass_mismatch(first, changed) or "")
        self.assertFalse(risk_gate.same_order(first, rejected))
        self.assertIn("second pass rejected", risk_gate.second_pass_mismatch(first, rejected) or "")


if __name__ == "__main__":
    unittest.main()
