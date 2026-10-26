import sys
import unittest
from datetime import datetime
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_DIR))

from studio_eta import OperationETA, format_duration


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class EtaTests(unittest.TestCase):
    def test_duration_formatting(self):
        self.assertEqual(format_duration(42), "42s")
        self.assertEqual(format_duration(125), "2m 05s")
        self.assertEqual(format_duration(7260), "2h 01m")

    def test_estimate_uses_completed_items(self):
        clock = FakeClock()
        eta = OperationETA(5, clock=clock, now=lambda: datetime(2026, 9, 28, 12, 0))
        self.assertIn("calculating", eta.label())
        clock.value = 10
        eta.advance()
        self.assertEqual(eta.remaining_seconds(), 40)
        self.assertIn("40s", eta.label())
        self.assertIn("12:00", eta.label())

    def test_recent_items_adjust_estimate_without_polling(self):
        clock = FakeClock()
        eta = OperationETA(4, clock=clock)
        clock.value = 10
        eta.advance()
        clock.value = 30
        eta.advance()
        self.assertEqual(eta.completed, 2)
        self.assertAlmostEqual(eta.remaining_seconds(), 26.0)


if __name__ == "__main__":
    unittest.main()
