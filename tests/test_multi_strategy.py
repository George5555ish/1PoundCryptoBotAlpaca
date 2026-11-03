"""Multi-strategy gate and runner helpers. No broker."""

from datetime import datetime, timezone
from decimal import Decimal
import unittest

import config
import risk_gate
from tests.test_risk_gate import _request


NOW = datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc)


class MultiStrategyGateTests(unittest.TestCase):
    def test_universe_has_twenty_liquid_names(self) -> None:
        self.assertEqual(len(config.MULTI_STRATEGY_SYMBOLS), 20)
        self.assertIn("SPY", config.MULTI_STRATEGY_SYMBOLS)
        self.assertIn("NFLX", config.MULTI_STRATEGY_SYMBOLS)
        self.assertEqual(config.COMMODITY_SYMBOLS, ("GLD", "SLV", "USO"))

    def test_spy_long_goes_through_the_same_gate(self) -> None:
        decision = risk_gate.evaluate(
            _request(
                symbol="SPY",
                direction="long",
                bid="499.90",
                ask="500.00",
                stop_price="495.00",
                target_price="510.00",
                now=NOW,
                quote_time=NOW,
            )
        )
        self.assertTrue(decision.approved, decision.reason)
        self.assertEqual(decision.side, "buy")
        self.assertEqual(decision.symbol, "SPY")
        self.assertLessEqual(Decimal(decision.planned_loss or "0"), Decimal("1000"))
        qty = Decimal(decision.qty or "0")
        self.assertEqual(qty, qty.to_integral_value())
        self.assertGreater(qty, 0)

    def test_spy_short_is_sized_from_the_stop_above(self) -> None:
        decision = risk_gate.evaluate(
            _request(
                symbol="SPY",
                direction="short",
                bid="499.90",
                ask="500.00",
                stop_price="505.00",
                target_price="490.00",
                now=NOW,
                quote_time=NOW,
            )
        )
        self.assertTrue(decision.approved, decision.reason)
        self.assertEqual(decision.side, "sell")
        self.assertGreater(Decimal(decision.stop_price or "0"), Decimal("500"))
        self.assertLess(Decimal(decision.target_price or "0"), Decimal("500"))

    def test_crypto_short_is_still_rejected(self) -> None:
        decision = risk_gate.evaluate(
            _request(direction="short", stop_price="101000", target_price="98000")
        )
        self.assertFalse(decision.approved)
        self.assertIn("long only", decision.reason)

    def test_one_position_per_symbol(self) -> None:
        held = risk_gate.OpenPosition(symbol="SPY", qty=Decimal("1"), side="long")
        decision = risk_gate.evaluate(
            _request(
                symbol="SPY",
                direction="long",
                bid="499.90",
                ask="500.00",
                stop_price="495.00",
                target_price="510.00",
                open_positions=(held,),
                now=NOW,
                quote_time=NOW,
            )
        )
        self.assertFalse(decision.approved)
        self.assertIn("already have this symbol", decision.reason)

    def test_sol_is_still_outside_both_universes(self) -> None:
        decision = risk_gate.evaluate(
            _request(symbol="SOL/USD", ask="150", bid="149.9", stop_price="140", target_price="170")
        )
        self.assertFalse(decision.approved)
        self.assertIn("universe", decision.reason)
