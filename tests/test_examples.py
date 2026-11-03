import json
import unittest

from pribor.domain import markdown
from pribor.server import EXAMPLES, example_data


class ExampleTests(unittest.TestCase):
    def test_both_real_examples_are_bound_and_validated(self):
        for key in EXAMPLES:
            with self.subTest(product=key):
                data = example_data(key)
                card = data["card"]
                self.assertIsNotNone(card)
                self.assertEqual(card["generation"]["mode"], "live_api")
                self.assertTrue(card["generation"]["viewing_saved_result"])
                self.assertTrue(800 <= len(card["seo_description"]) <= 1200)
                self.assertLessEqual(len(card["seo_title"]), 70)
                self.assertEqual(card["characteristics"][-1]["value"], "данных нет")
                self.assertEqual(data["markdown"], markdown(card))
                self.assertNotIn("api_key", json.dumps(data).lower())
