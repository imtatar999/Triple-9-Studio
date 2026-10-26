import ast
from pathlib import Path
import re
import tempfile
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_resync

SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load_scan_functions():
    names = {
        "_lrc_timestamp_milliseconds",
        "_find_lrc_duplicate_timestamp_runs",
    }
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "_LRC_TIMED_TEXT_RE"
                   for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {"re": re, "Path": Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<lrc scan>", "exec"), namespace)
    return namespace


class LrcFailureScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_scan_functions()

    def test_detects_leading_duplicate_run_across_blank_spacing(self):
        text = """[00:30.00] first
[00:30.00] second

[00:30.00] third
[00:31.20] fourth
"""
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["count"], 3)
        self.assertTrue(failures[0]["at_start"])
        self.assertEqual(failures[0]["first_file_line"], 1)
        self.assertEqual(failures[0]["last_file_line"], 4)

    def test_two_equal_lines_are_reported(self):
        text = "[00:01.00] one\n[00:01.00] two\n[00:02.00] three\n"
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([(f["count"], f["first_file_line"]) for f in failures], [(2, 1)])
        self.assertEqual(failures[0]["severity"], "error")  # SAME TIME is always an error

    def test_two_long_lines_too_close_are_dense(self):
        # "Bulletproof doors on the back" cannot be sung in 0.27 s.
        text = ("[02:14.96] Bulletproof doors on the back today\n"
                "[02:15.23] Got the crest to the clown, T-Rex\n[02:18.00] Next line\n")
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([(f.get("kind"), f["count"]) for f in failures], [("dense_lines", 2)])
        self.assertEqual(failures[0]["severity"], "minor")  # only the sensitive 2+ line rule finds it

    def test_adlibs_in_parentheses_do_not_make_a_line_dense(self):
        # Sung: "In the McLaren" (3 words) in 0.54 s; "(Oh-oh, yeah, yeah)" is a background ad-lib.
        text = ("[00:16.88] In the McLaren (Oh-oh, yeah, yeah)\n"
                "[00:17.42] Got all the hoes starin' at me now\n[00:19.68] And glarin'\n")
        self.assertEqual(self.ns["_find_lrc_duplicate_timestamp_runs"](text), [])

    def test_dense_long_lines_with_different_times(self):
        text = "\n".join(f"[01:{stamp}] one two three four five six seven eight nine"
                         for stamp in ("54.16", "54.68", "54.83", "54.98", "55.40", "55.82", "55.88"))
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "dense_lines")
        self.assertEqual(failures[0]["count"], 7)
        self.assertAlmostEqual(failures[0]["span_seconds"], 1.72)
        self.assertEqual(failures[0]["severity"], "error")  # the strict 4+ line rule

    def test_short_adlibs_and_normal_lines_are_not_dense(self):
        for text in (
            "\n".join(f"[00:01.{i}0] Yeah uh huh" for i in (0, 1, 3, 6)),
            "\n".join(f"[00:{t}] one two three four five six seven eight nine"
                      for t in ("01.00", "04.10", "06.95", "10.30", "13.12")),
        ):
            self.assertEqual(self.ns["_find_lrc_duplicate_timestamp_runs"](text), [])

    def test_a_short_line_inside_a_dense_run_does_not_split_it(self):
        # Get Geeked lines 83-91: many lines in under 3 s; a 4-word line used to cut the run.
        text = "\n".join(f"[02:{t}] {line}" for t, line in (
            ("16.21", "Get geeked, get geeked, get geeked, get geeked"),
            ("16.69", "Get geeked, get geeked, get geeked, get geeked"),
            ("17.17", "Get geeked, get geeked, get geeked, get geeked"),
            ("17.40", "Get geeked, get geeked (Geek)"),
            ("17.65", "I be flyer than a bald eagle beak, get geeked"),
            ("21.03", "Get geeked, huh")))
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([(f["kind"], f["first_file_line"], f["last_file_line"], f["severity"]) for f in failures],
                         [("dense_lines", 1, 5, "error")])

    def test_three_lines_at_13_words_per_second_are_an_error(self):
        # 22 words in 1.46 s (Get Geeked lines 8-11) cannot be sung: an error, not a smaller warning.
        text = ("[00:10.14] Get geeked, get geeked, get geeked\n"
                "[00:10.68] I be sharper baby alligator teeth (Sharp, sharp), get geeked\n"
                "[00:10.84] Get geeked, get geeked, get geeked, get geeked\n"
                "[00:11.60] I get flyer than a bald eagle beak (Who?), get geeked (Who?)\n"
                "[00:15.00] Next\n")
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([(f["kind"], f["count"], f["severity"]) for f in failures], [("dense_lines", 4, "error")])

    def test_evenly_spaced_lines_are_guessed(self):
        # Syncalong interpolates lines Whisper did not hear: exactly equal steps.
        def lrc(count, step, extra=""):
            lines = []
            for i in range(count):
                cs = 1000 + round(i * step * 100)
                lines.append(f"[{cs // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}] line number {i} of the verse")
            return "\n".join(lines) + extra

        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](lrc(10, 1.86))
        self.assertEqual([(f["kind"], f["count"], f["severity"]) for f in failures], [("even_spacing", 10, "error")])
        self.assertAlmostEqual(failures[0]["step_seconds"], 1.86)
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](lrc(5, 2.5, "\n[01:00.00] end"))
        # 5 guessed lines + "end", which comes 47 s later and so joins the passage.
        self.assertEqual([(f["kind"], f["count"], f["severity"]) for f in failures], [("even_spacing", 6, "minor")])
        # Four even lines are normal (a steady beat).
        self.assertEqual(self.ns["_find_lrc_duplicate_timestamp_runs"](lrc(4, 2.5, "\n[01:00.00] end")), [])

    def test_two_lines_after_a_passage_join_it_when_the_next_line_is_far(self):
        # The screenshot case: 82–84 squeezed into 0.3 s, then 85, 86, 87 every ~6 s — also wrong.
        text = "\n".join(f"[03:{t}] {line}" for t, line in (
            ("30.00", "I can't focus in school, need Adderall"),
            ("42.02", "Money in sacks, spend a couple racks"),
            ("42.17", "Smoking thrax, big gun on my lap"),
            ("42.32", "That's the truth, yeah, yeah, that's facts"),
            ("48.35", "Who are you? Yeah, yeah, get back"),
            ("54.39", "If you wanna sneak diss, fuck a diss track"),
            ("58.00", "Where are you at? I'ma hit you in your face")))
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([(f["first_file_line"], f["last_file_line"], f.get("extended_lines")) for f in failures],
                         [(2, 6, 2)])  # 2–4 detected, 5 and 6 added; 7 stays
        self.assertEqual(failures[0]["end_timestamp"], "[03:54.39]")
        # A close next line (≤ 4 s) adds nothing.
        close = text.replace("[03:48.35]", "[03:45.00]")
        self.assertEqual([f["last_file_line"] for f in self.ns["_find_lrc_duplicate_timestamp_runs"](close)], [4])

    def test_guessed_run_inside_a_dense_run_is_reported_once(self):
        text = "\n".join(f"[00:{10 + i * 0.3:05.2f}] one two three four five six seven" for i in range(6))
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual([f["kind"] for f in failures], ["dense_lines"])

    def test_dense_rule_does_not_duplicate_identical_run(self):
        text = "\n".join("[00:01.00] one two three four five six seven eight nine" for _ in range(5))
        self.assertEqual(len(self.ns["_find_lrc_duplicate_timestamp_runs"](text)), 1)

    def test_equivalent_fraction_spellings_share_one_time(self):
        text = "[00:30] one\n[00:30.0] two\n[00:30.000] three\n"
        failures = self.ns["_find_lrc_duplicate_timestamp_runs"](text)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["time_ms"], 30000)

    def test_library_scan_is_recursive_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "album" / "SYNCED LYRICS"
            nested.mkdir(parents=True)
            bad = nested / "bad.LRC"
            good = root / "good.lrc"
            bad_text = "[00:05.00] a\n[00:05.00] b\n[00:05.00] c\n"
            bad.write_text(bad_text, encoding="utf-8")
            good.write_text("[00:01.00] a\n[00:02.00] b\n[00:03.00] c\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as state:
                result = studio_resync.scan(root, Path(state) / "scan_cache.json",
                                            self.ns["_find_lrc_duplicate_timestamp_runs"])

            self.assertEqual(result["files_scanned"], 2)
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(Path(result["failures"][0]["path"]), bad)
            self.assertEqual(bad.read_text(encoding="utf-8"), bad_text)


if __name__ == "__main__":
    unittest.main()
