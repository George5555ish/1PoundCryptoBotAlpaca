"""Execution guards. These tests build request objects and do not call Alpaca."""

import unittest

import execution
import risk_gate


class ExecutionGuardTests(unittest.TestCase):
    def test_entry_is_an_ioc_market_and_the_stop_is_gtc(self) -> None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest

        entry = MarketOrderRequest(
            symbol="BTC/USD",
            qty=0.01,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.IOC,
            order_class=OrderClass.SIMPLE,
        )
        fields = entry.to_request_fields()
        self.assertEqual(fields["type"], "market")
        self.assertEqual(fields["time_in_force"], "ioc")
        self.assertEqual(fields["order_class"], "simple")
        self.assertNotIn("take_profit", fields)
        self.assertNotIn("stop_loss", fields)

        stop = StopLimitOrderRequest(
            symbol="BTC/USD",
            qty=0.01,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=99000.0,
            limit_price=98950.5,
            order_class=OrderClass.SIMPLE,
        )
        stop_fields = stop.to_request_fields()
        self.assertEqual(stop_fields["type"], "stop_limit")
        self.assertEqual(stop_fields["time_in_force"], "gtc")
        self.assertLess(stop_fields["limit_price"], stop_fields["stop_price"])

    def test_submit_refuses_a_dry_run_and_an_unapproved_decision(self) -> None:
        decision = risk_gate.RiskDecision(
            verdict="reject",
            approved=False,
            reason="no",
            model_reason="",
        )
        with self.assertRaises(RuntimeError):
            execution.submit(object(), decision, allow_send=False)
        approved = risk_gate.RiskDecision(
            verdict="approve",
            approved=True,
            reason="yes",
            model_reason="",
            symbol="BTC/USD",
            side="buy",
            qty="0.01",
            stop_price="99000.00",
            order_kind=risk_gate.ORDER_KIND_ENTRY,
        )
        with self.assertRaises(RuntimeError):
            execution.submit(object(), approved, allow_send=False)

    def test_paper_url_guard(self) -> None:
        import config

        config.assert_paper_trading_url("https://paper-api.alpaca.markets")
        config.assert_paper_trading_url("https://paper-api.alpaca.markets/v2")
        with self.assertRaises(RuntimeError):
            config.assert_paper_trading_url("https://api.alpaca.markets")


if __name__ == "__main__":
    unittest.main()
