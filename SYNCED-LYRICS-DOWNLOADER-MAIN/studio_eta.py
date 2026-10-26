"""Small, dependency-free ETA helper for multi-item Studio operations."""
import time
from datetime import datetime, timedelta


def format_duration(seconds):
    seconds = max(0, int(round(float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


class OperationETA:
    """Estimate remaining time from a cheap per-item exponential moving average."""

    def __init__(self, total, clock=None, now=None):
        self.total = max(0, int(total))
        self.completed = 0
        self._clock = clock or time.monotonic
        self._now = now or datetime.now
        self._item_started = self._clock()
        self._average = None

    def advance(self, count=1):
        count = max(1, int(count))
        current = self._clock()
        elapsed = max(0.0, current - self._item_started)
        per_item = elapsed / count
        if self._average is None:
            self._average = per_item
        else:
            # Favor recent tracks while avoiding large jumps after one outlier.
            self._average = self._average * 0.70 + per_item * 0.30
        self.completed = min(self.total, self.completed + count)
        self._item_started = current
        return self.remaining_seconds()

    def remaining_seconds(self):
        if self._average is None:
            return None
        return max(0.0, self._average * (self.total - self.completed))

    def label(self):
        remaining = self.remaining_seconds()
        if self.completed >= self.total and self.total:
            return "ETA: complete"
        if remaining is None:
            return "ETA: calculating after first item…"
        finish = self._now() + timedelta(seconds=remaining)
        return f"ETA: ~{format_duration(remaining)} • finish about {finish:%H:%M}"
