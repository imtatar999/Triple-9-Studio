import ast
import importlib
from pathlib import Path
import re
import unicodedata
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load_functions():
    names = {"_lrc_timestamp_milliseconds", "_norm_match_text", "_lrc_timed_entries",
             "_find_lrc_duplicate_timestamp_runs",
             "_replace_lrc_line_times", "_lrc_repeated_block_candidate", "_retry_lrc_line_range",
             "_lrc_later_repeat_candidate", "_strong_line_times",
             "_auto_fix_lrc_error", "_parse_sync_lyrics"}
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in {"_LRC_TIMED_TEXT_RE", "_WEAK_MATCH_WORDS"}
                for t in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    import sys
    sys.path.insert(0, str(SOURCE.parent))
    import studio_resync
    namespace = {"re": re, "unicodedata": unicodedata, "importlib": importlib, "studio_resync": studio_resync,
                 "config": {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<fragment fix>", "exec"), namespace)
    return namespace


# Shape of "Chrome (Cardi B Chopper)": the last chorus collapsed after 03:23.82.
LRC = """[00:30.00] Intro line here

[01:58.90] Oh, I'm in my zone, hand on my chrome
[02:01.30] Ain't what you want (Pa-pow)
[02:02.58] Oh, I'm in my zone, hand on my chrome
[02:05.54] Ain't what you want (Uh-huh)

[02:15.90] I'm in my lane, swerve (Skrrt)

[03:23.82] Oh, I'm in my zone, hand on my chrome
[03:24.73] Ain't what you want (Pa-pow)
[03:25.64] Oh, I'm in my zone, hand on my chrome
[03:57.61] Ain't what you want (Yeah left, left)
"""


class Word:
    def __init__(self, word, start):
        self.word, self.raw, self.start, self.end = word, word, start, start + 0.2


class FragmentFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_functions()
        cls.entries = cls.ns["_lrc_timed_entries"](LRC)

    def test_repeated_chorus_timing_is_copied_from_the_anchor(self):
        start = next(i for i, e in enumerate(self.entries) if e["file_line"] == 11)
        times, source_line = self.ns["_lrc_repeated_block_candidate"](self.entries, start, len(self.entries))
        self.assertEqual(source_line, 3)
        self.assertEqual([round(t, 2) for t in times], [206.22, 207.5, 210.46])

    def test_no_candidate_without_an_identical_earlier_block(self):
        start = next(i for i, e in enumerate(self.entries) if e["file_line"] == 8)
        self.assertIsNone(self.ns["_lrc_repeated_block_candidate"](self.entries, start, start + 1))

    def test_replace_changes_only_selected_timestamps(self):
        new = self.ns["_replace_lrc_line_times"](LRC, {11: 206.22, 13: 210.456})
        old_lines, new_lines = LRC.splitlines(), new.splitlines()
        changed = [i + 1 for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(changed, [11, 13])
        self.assertEqual(new_lines[10], "[03:26.22] Ain't what you want (Pa-pow)")
        self.assertEqual(new_lines[12], "[03:30.46] Ain't what you want (Yeah left, left)")

    def test_retry_bounds_whisper_window_and_offers_all_candidates(self):
        calls = []

        def transcribe(clip_start, clip_end, prompt):
            calls.append((round(clip_start, 2), round(clip_end, 2), bool(prompt)))
            return [Word("aint", 206.3), Word("what", 206.5), Word("you", 206.6), Word("want", 206.7),
                    Word("oh", 207.6), Word("im", 207.8), Word("in", 207.9), Word("my", 208.0),
                    Word("zone", 208.2), Word("hand", 208.6), Word("on", 208.7), Word("my", 208.8),
                    Word("chrome", 209.0), Word("aint", 210.5), Word("what", 210.6),
                    Word("you", 210.7), Word("want", 210.9), Word("outside", 250.0)]

        try:
            result = self.ns["_retry_lrc_line_range"](LRC, 11, 13, audio_end=240.0, threshold=55,
                                                      transcribe=transcribe)
        except ModuleNotFoundError:
            self.skipTest("syncalong is not installed for this Python")
        self.assertEqual([line["file_line"] for line in result["lines"]], [11, 12, 13])
        self.assertEqual(round(result["region"][0], 2), 203.87)
        self.assertEqual(result["region"][1], 240.0)
        self.assertEqual([c[2] for c in calls], [True, False])
        labels = [c["label"] for c in result["candidates"]]
        self.assertIn("Copy earlier repeat", labels)
        self.assertFalse([l for l in labels if "rhythm" in l.lower() or "syllables" in l.lower()])  # removed method
        whisper = next(c for c in result["candidates"] if c["label"].startswith("Whisper"))
        self.assertTrue(all(203.87 <= t <= 240.0 for t in whisper["times"]))
        self.assertEqual(sorted(whisper["times"]), whisper["times"])


    def test_real_chrome_ending_passes_the_scan_after_copy_fix(self):
        path = Path(r"C:\Users\Admin\Downloads\Testjuice\Juice WRLD\Juice WRLD\SYNCED LYRICS"
                    r"\Chrome  (Cardi B Chopper) (JW3) .lrc")
        if not path.is_file():
            self.skipTest("local Chrome LRC not available")
        text = path.read_text(encoding="utf-8-sig")
        scan = self.ns["_find_lrc_duplicate_timestamp_runs"]
        if not scan(text):
            self.skipTest("Chrome LRC was already fixed")
        entries = self.ns["_lrc_timed_entries"](text)
        start = next(i for i, e in enumerate(entries) if e["file_line"] == 114)
        times, _ = self.ns["_lrc_repeated_block_candidate"](entries, start, len(entries))
        fixed = self.ns["_replace_lrc_line_times"](
            text, {e["file_line"]: t for e, t in zip(entries[start:], times)})
        self.assertEqual(scan(fixed), [])

    def test_racks_on_racks_intro_is_timed_from_the_later_chorus(self):
        # Opening chorus pinned to 0.5–1s by a stray "I"/"to"; the second chorus is good.
        lrc = """[00:00.15] DY Krazy
[00:00.54] Still gettin' racks on racks
[00:00.62] I put a bone on her breast (Ooh)
[00:00.83] Bulletproof doors on the back
[00:27.38] While we pop out his melanin
[02:11.36] Still gettin' racks on racks
[02:12.20] I put a bone on her breast (Ooh)
[02:14.96] Bulletproof doors on the back
[02:25.76] While we pop out his melanin
"""
        entries = self.ns["_lrc_timed_entries"](lrc)
        times, source_line, copied = self.ns["_lrc_later_repeat_candidate"](entries, 0, 4, 0.0)
        self.assertEqual((source_line, copied), (6, 3))
        self.assertEqual([round(t, 2) for t in times], [6.49, 12.98, 13.82, 16.58])

    def test_single_stop_word_does_not_pin_a_line(self):
        flat = ["i", "put", "a", "bone", "got", "the", "crest", "to", "the", "clown"]
        word_lines = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
        heard = [Word("i", 0.62), Word("to", 1.04), Word("bone", 14.2), Word("crest", 16.0)]
        mapping = {0: 0, 7: 1, 3: 2, 6: 3}
        times, anchored = self.ns["_strong_line_times"](flat, word_lines, mapping, heard, 2, 0.0, 30.0)
        self.assertEqual(anchored, 2)
        self.assertEqual(times, [14.2, 16.0])

    def test_auto_fix_tries_prompt_on_then_off_and_keeps_the_one_that_clears_the_error(self):
        lrc = ("[00:10.00] alpha morning sunshine\n[00:14.00] bravo evening thunder\n"
               "[00:18.00] charlie silver mountain\n[00:18.00] delta golden river\n"
               "[00:18.00] echo purple forest\n[00:30.00] foxtrot crystal ocean\n"
               "[00:34.00] golf marble castle\n[00:38.00] hotel final chorus\n")
        warning = self.ns["_find_lrc_duplicate_timestamp_runs"](lrc)[0]
        heard = {"alpha": 10, "morning": 10.3, "bravo": 14, "evening": 14.3, "charlie": 20, "silver": 20.3,
                 "delta": 23, "golden": 23.3, "echo": 26, "purple": 26.3, "foxtrot": 30, "crystal": 30.3,
                 "golf": 34, "marble": 34.3}
        prompts = []

        def transcribe(clip_start, clip_end, prompt):
            prompts.append(bool(prompt))
            if prompt:  # prompt ON hears nothing useful here
                return []
            return [Word(word, start) for word, start in heard.items()]

        try:
            new_text, label, notes = self.ns["_auto_fix_lrc_error"](lrc, warning, 45.0, 55, transcribe)
        except ModuleNotFoundError:
            self.skipTest("syncalong is not installed for this Python")
        self.assertEqual(prompts, [True, False])
        self.assertEqual(label, "Whisper, prompt OFF")
        self.assertEqual(self.ns["_find_lrc_duplicate_timestamp_runs"](new_text), [])
        self.assertIn("[00:20.00] charlie silver mountain", new_text)
        self.assertIn("[00:38.00] hotel final chorus", new_text)  # outside the passage: untouched

    def test_auto_fix_gives_up_when_neither_prompt_clears_the_error(self):
        lrc = ("[00:10.00] alpha one two\n[00:18.00] bravo three four\n"
               "[00:18.00] charlie five six\n[00:30.00] delta end\n")
        warning = self.ns["_find_lrc_duplicate_timestamp_runs"](lrc)[0]
        try:
            new_text, label, notes = self.ns["_auto_fix_lrc_error"](lrc, warning, 40.0, 55, lambda *a: [])
        except ModuleNotFoundError:
            self.skipTest("syncalong is not installed for this Python")
        self.assertIsNone(new_text)
        self.assertIsNone(label)
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
