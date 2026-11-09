"""Edge cases for the benchmark metrics helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.metrics import quantile  # noqa: E402


class QuantileEdgeTests(unittest.TestCase):
    def test_quantile_rejects_probabilities_outside_unit_interval(self):
        for probability in (-0.1, 1.1):
            with self.subTest(probability=probability):
                with self.assertRaisesRegex(ValueError, "p"):
                    quantile([1, 2, 3], probability)

    def test_quantile_rejects_nan_probability(self):
        with self.assertRaisesRegex(ValueError, "p"):
            quantile([1, 2, 3], float("nan"))

    def test_quantile_rejects_infinite_probabilities(self):
        for probability in (float("-inf"), float("inf")):
            with self.subTest(probability=probability):
                with self.assertRaisesRegex(ValueError, "p.*finite.*0.*1"):
                    quantile([1, 2, 3], probability)

    def test_quantile_validates_probability_before_sample_shortcuts(self):
        for values in ([], [2]):
            for probability in (-0.1, 1.1, float("nan")):
                with self.subTest(values=values, probability=probability):
                    with self.assertRaisesRegex(ValueError, "p.*finite.*0.*1"):
                        quantile(values, probability)

    def test_quantile_keeps_endpoints_and_empty_sample_behavior(self):
        self.assertEqual(quantile([1, 2, 3], 0), 1.0)
        self.assertEqual(quantile([1, 2, 3], 1), 3.0)
        self.assertIsNone(quantile([], 0.5))


if __name__ == "__main__":
    unittest.main()
