"""Parser tests. No network."""

import unittest

import ai_decision


class ParseTests(unittest.TestCase):
    def test_enter_drops_quantity(self) -> None:
        suggestion = ai_decision.parse_suggestion(
            '{"action":"enter","symbol":"btc/usd","direction":"long",'
            '"stop_price":99000,"target_price":102000,"reason":"trend","qty":3}'
        )
        self.assertTrue(suggestion.valid)
        self.assertEqual(suggestion.symbol, "BTC/USD")
        self.assertEqual(suggestion.stop_price, "99000.00")
        self.assertIn("qty", suggestion.ignored_fields)
        self.assertFalse(hasattr(suggestion, "qty"))

    def test_hold_discards_prices(self) -> None:
        suggestion = ai_decision.parse_suggestion(
            '{"action":"hold","symbol":"ETH/USD","direction":"long",'
            '"stop_price":3000,"target_price":3200,"reason":"wait"}'
        )
        self.assertTrue(suggestion.valid)
        self.assertIsNone(suggestion.symbol)
        self.assertIsNone(suggestion.stop_price)

    def test_prose_is_invalid(self) -> None:
        suggestion = ai_decision.parse_suggestion("buy bitcoin")
        self.assertFalse(suggestion.valid)


if __name__ == "__main__":
    unittest.main()
