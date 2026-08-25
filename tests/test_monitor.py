import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

import hot_chips_monitor as monitor


class MonitorTests(unittest.TestCase):
    def test_relevance_prefers_specific_cerebras_claim(self):
        specific = {"text": "Cerebras rack-scale architecture for Wafer Scale Engine at Hot Chips", "public_metrics": {}}
        noise = {"text": "I ate hot chips today", "public_metrics": {}}
        specific_score, companies, products = monitor.relevance_score(specific)
        noise_score, _, _ = monitor.relevance_score(noise)
        self.assertGreater(specific_score, noise_score + 30)
        self.assertIn("Cerebras", companies)
        self.assertIn("WSE", products)

    def test_external_urls_excludes_x_redirect_targets(self):
        post = {"entities": {"urls": [
            {"expanded_url": "https://example.com/chip"},
            {"expanded_url": "https://x.com/example/status/1"},
        ]}}
        self.assertEqual(monitor.external_urls(post), ["https://example.com/chip"])

    def test_jalapeno_food_is_not_a_chip_signal(self):
        food = {"text": "jalapeño poppers and hot chips for lunch", "public_metrics": {}}
        chip = {"text": "OpenAI Jalapeño inference ASIC benchmark", "public_metrics": {}}
        food_score, _, food_products = monitor.relevance_score(food)
        chip_score, _, chip_products = monitor.relevance_score(chip)
        self.assertNotIn("Jalapeño", food_products)
        self.assertIn("Jalapeño", chip_products)
        self.assertGreater(chip_score, food_score + 30)

    def test_config_queries_fit_recent_search_limit(self):
        config = monitor.load_config(monitor.ROOT / "config.json")
        self.assertTrue(all(len(lane["query"]) <= 512 for lane in config["x"]["lanes"]))


if __name__ == "__main__":
    unittest.main()
