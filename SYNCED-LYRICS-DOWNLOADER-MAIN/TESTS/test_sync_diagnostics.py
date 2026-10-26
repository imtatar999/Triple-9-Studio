import ast
import importlib
from pathlib import Path
import re
import unittest

from syncalong.transcribe import WordTimestamp


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load_function(name):
    nodes = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in (name, "_parse_sync_lyrics")]
    import sys
    sys.path.insert(0, str(SOURCE.parent))
    import studio_resync
    namespace = {"Path": Path, "importlib": importlib, "re": re, "studio_resync": studio_resync, "config": {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<sync diagnostics>", "exec"), namespace)
    return namespace[name]


class FakeTranscriber:
    def transcribe(self, *_args, **_kwargs):
        return [
            WordTimestamp("all", "All", 11.18, 11.38),
            WordTimestamp("i", "I", 11.38, 11.48),
            WordTimestamp("know", "know", 11.48, 11.72),
            WordTimestamp("rockstar", "rockstar", 11.72, 12.10),
        ]


class FakeOpeningModel:
    def transcribe(self, *_args, **_kwargs):
        return {"segments": [{"words": [
            {"word": " Uh", "start": 0.62, "end": 0.90},
            {"word": " Uh-huh", "start": 1.10, "end": 1.55},
            {"word": " Yeah", "start": 2.20, "end": 2.55},
            {"word": " uh", "start": 2.90, "end": 3.10},
            {"word": " Yeah", "start": 4.30, "end": 4.60},
            {"word": " uh", "start": 4.75, "end": 5.00},
        ]}]}


class RecoveringTranscriber(FakeTranscriber):
    _model = FakeOpeningModel()


class FakeMiddleModel:
    def transcribe(self, *_args, **_kwargs):
        return {"segments": [{"words": [
            {"word": " alpha", "start": 3.0, "end": 3.3},
            {"word": " beta", "start": 4.0, "end": 4.3},
            {"word": " gamma", "start": 5.0, "end": 5.3},
        ]}]}


class MiddleFailureTranscriber:
    _model = FakeMiddleModel()

    def transcribe(self, *_args, **_kwargs):
        return [
            WordTimestamp("start", "Start", 1.0, 1.3),
            WordTimestamp("alpha", "alpha", 5.0, 5.0),
            WordTimestamp("beta", "beta", 5.0, 5.0),
            WordTimestamp("gamma", "gamma", 5.0, 5.0),
            WordTimestamp("end", "End", 10.0, 10.3),
        ]


class SyncDiagnosticsTests(unittest.TestCase):
    def test_explains_identical_opening_timestamps_when_whisper_starts_late(self):
        align = load_function("_syncalong_align_with_diagnostics")
        lyrics = "Uh\nUh-huh\nYeah, uh\nAll I know is rockstar\n"

        result, diagnostic = align(
            FakeTranscriber(), lyrics, "unused.mp3", "en", 55, True, False
        )

        non_blank_times = [timestamp for line, timestamp in result.timed_lines
                           if not line.is_blank]
        self.assertEqual(non_blank_times, [11.18, 11.18, 11.18, 11.18])
        report = "\n".join(diagnostic)
        self.assertIn("Whisper first word: 11.18s", report)
        self.assertIn("ESTIMATED before first direct match", report)
        self.assertIn("CAUSE OF IDENTICAL OPENING TIMES", report)
        self.assertIn("DIRECT match 'all'", report)

    def test_recovers_skipped_opening_from_short_original_audio_pass(self):
        align = load_function("_syncalong_align_with_diagnostics")
        lyrics = "Uh\nUh-huh\nYeah, uh\nYeah, uh\nAll I know is rockstar\n"

        result, diagnostic = align(
            RecoveringTranscriber(), lyrics, "unused.mp3", "en", 55, True, True
        )

        non_blank_times = [timestamp for line, timestamp in result.timed_lines
                           if not line.is_blank]
        self.assertEqual(non_blank_times[0], 0.62)
        self.assertLess(non_blank_times[1], 11.18)
        self.assertLess(non_blank_times[2], 11.18)
        self.assertEqual(non_blank_times[-1], 11.18)
        self.assertIn("heard 6 usable word(s)", result.t9_opening_recovery)
        self.assertIn("Original full-song Whisper start: 11.18s", "\n".join(diagnostic))

    def test_repairs_three_equal_timestamps_in_middle_with_focused_audio(self):
        align = load_function("_syncalong_align_with_diagnostics")
        lyrics = "Start\nAlpha\nBeta\nGamma\nEnd\n"

        result, _diagnostic = align(
            MiddleFailureTranscriber(), lyrics, "unused.mp3", "en", 55, False, True
        )

        times = [timestamp for line, timestamp in result.timed_lines
                 if not line.is_blank]
        self.assertEqual(times, [1.0, 3.0, 4.0, 5.0, 10.0])
        self.assertIn("Fragment recovery accepted", result.t9_fragment_recovery[0])
        self.assertIn("3 identical timestamps", result.t9_fragment_recovery[0])


if __name__ == "__main__":
    unittest.main()
