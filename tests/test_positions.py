"""Hold and close reviews. No broker and no model."""

from decimal import Decimal
import unittest

import config
import positions


class PositionReviewTests(unittest.TestCase):
    def test_lower_lows_closes(self) -> None:
        review = positions.review_position(
            symbol="BTC/USD",
            qty="0.01",
            current_price="80000",
            unrealized_pl="-5",
            trend="lower lows",
            stop_price="70000",
            target_price="90000",
        )
        self.assertEqual(review.decision, "close")
        self.assertIn("lower lows", review.reason)

    def test_target_closes(self) -> None:
        review = positions.review_position(
            symbol="ETH/USD",
            qty="1",
            current_price="3300",
            unrealized_pl="20",
            trend="higher highs",
            stop_price="2900",
            target_price="3300",
        )
        self.assertEqual(review.decision, "close")
        self.assertIn("target", review.reason)

    def test_stop_closes_before_the_trend(self) -> None:
        review = positions.review_position(
            symbol="BTC/USD",
            qty="0.01",
            current_price="69000",
            unrealized_pl="-40",
            trend="lower lows",
            stop_price="70000",
            target_price="90000",
        )
        self.assertEqual(review.decision, "close")
        self.assertIn("protective stop", review.reason)

    def test_hold_between_stop_and_target(self) -> None:
        review = positions.review_position(
            symbol="BTC/USD",
            qty="0.01",
            current_price="81000",
            unrealized_pl="1",
            trend="higher highs",
            stop_price="70000",
            target_price="90000",
        )
        self.assertEqual(review.decision, "hold")
        self.assertIn("higher highs", review.reason)
        self.assertEqual(review.current_price, "81000")
        self.assertEqual(review.unrealized_pl, "1")

    def test_stock_down_two_percent_from_entry_closes_immediately(self) -> None:
        self.assertEqual(config.POSITION_LOSS_CLOSE_FRACTION, Decimal("0.02"))
        review = positions.review_position(
            symbol="IWM",
            qty="10",
            current_price="98.00",
            unrealized_pl="-20",
            trend="higher highs",
            stop_price="90.00",
            target_price="110.00",
            side="long",
            avg_entry_price="100.00",
        )
        self.assertEqual(review.decision, "close")
        self.assertIn(config.POSITION_LOSS_CLOSE_REASON, review.reason)

    def test_stock_short_up_two_percent_from_entry_closes_immediately(self) -> None:
        review = positions.review_position(
            symbol="QQQ",
            qty="5",
            current_price="102.00",
            unrealized_pl="-10",
            trend="lower lows",
            stop_price="110.00",
            target_price="90.00",
            side="short",
            avg_entry_price="100.00",
        )
        self.assertEqual(review.decision, "close")
        self.assertIn(config.POSITION_LOSS_CLOSE_REASON, review.reason)

    def test_stock_under_two_percent_loss_does_not_cut(self) -> None:
        review = positions.review_position(
            symbol="DIA",
            qty="2",
            current_price="98.01",
            unrealized_pl="-3.98",
            trend="higher highs",
            stop_price="90.00",
            target_price="110.00",
            side="long",
            avg_entry_price="100.00",
        )
        self.assertEqual(review.decision, "hold")

    def test_crypto_is_not_cut_by_the_stock_loss_rule(self) -> None:
        review = positions.review_position(
            symbol="BTC/USD",
            qty="0.01",
            current_price="98000",
            unrealized_pl="-200",
            trend="higher highs",
            stop_price="90000",
            target_price="120000",
            avg_entry_price="100000",
        )
        self.assertEqual(review.decision, "hold")

    def test_missing_levels_hold_without_a_lower_low(self) -> None:
        review = positions.review_position(
            symbol="BTC/USD",
            qty="0.01",
            current_price=None,
            unrealized_pl=None,
            trend=None,
            stop_price=None,
            target_price=None,
        )
        self.assertEqual(review.decision, "hold")
        self.assertIn("no tracked stop or target", review.reason)


if __name__ == "__main__":
    unittest.main()
