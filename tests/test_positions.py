"""Hold and close reviews. No broker and no model."""

import unittest

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
