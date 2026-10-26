import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import studio_resync  # noqa: E402

LRC = (
    "[ar:Juice WRLD]\n"
    "[00:01.00] Yup, ooh\n"
    "[00:03.00] Get geeked, get geeked\n"
    "[00:05.00] I be sharper baby alligator teeth\n"
    "\n"
    "[00:09.00] Get geeked, get geeked\n"
    "[00:11.00] Last line\n"
)
TXT = (
    "[Intro]\n"
    "Yup, ooh\n"
    "\n"
    "[Chorus]\n"
    "Get geeked,  get geeked\n"
    "I be sharper baby alligator teeth\n"
    "\n"
    "Get geeked, get geeked\n"
    "Last line\n"
)


class RemoveLinesTests(unittest.TestCase):
    def test_lrc_keeps_every_other_line_byte_identical(self):
        crlf = LRC.replace("\n", "\r\n")
        new = studio_resync.remove_lrc_lines(crlf, [3, 6])
        self.assertEqual(new, crlf.replace("[00:03.00] Get geeked, get geeked\r\n", "", 1)
                         .replace("[00:09.00] Get geeked, get geeked\r\n", ""))

    def test_txt_removes_the_same_occurrence_of_a_repeated_line(self):
        # LRC line 6 is the SECOND "Get geeked, get geeked": the first one must stay in the TXT.
        new, missing = studio_resync.remove_txt_lines(TXT, LRC, [6])
        self.assertEqual(missing, [])
        self.assertEqual(new.count("Get geeked"), 1)
        self.assertIn("Get geeked,  get geeked\nI be sharper", new)  # the first chorus line is kept
        self.assertTrue(new.endswith("\n\nLast line\n"))
        self.assertIn("[Chorus]", new)  # headers and blank lines are untouched

    def test_txt_match_ignores_case_spaces_and_curly_quotes(self):
        lrc = "[00:01.00] I’m on they ASS\n[00:02.00] next\n"
        new, missing = studio_resync.remove_txt_lines("i'm on  they ass\nnext\n", lrc, [1])
        self.assertEqual((new, missing), ("next\n", []))

    def test_line_missing_from_txt_is_reported_and_txt_untouched(self):
        new, missing = studio_resync.remove_txt_lines("Something else\n", LRC, [7])
        self.assertEqual((new, missing), ("Something else\n", [7]))

    def test_write_keeps_bom_and_backup_has_restore_map(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            lrc = folder / "song.lrc"
            lrc.write_bytes(b"\xef\xbb\xbf" + LRC.encode("utf-8"))
            backup = studio_resync.backup_song_files([lrc], folder / "BACKUPS")
            studio_resync.write_keeping_bom(lrc, "new\n")
            self.assertEqual(lrc.read_bytes(), b"\xef\xbb\xbfnew\n")
            self.assertTrue(backup.name.startswith("fragment_"))
            self.assertEqual(json.loads((backup / "restore.json").read_text(encoding="utf-8")),
                             {str(lrc): str(backup / "song.lrc")})
            self.assertEqual((backup / "song.lrc").read_bytes(), b"\xef\xbb\xbf" + LRC.encode("utf-8"))
            self.assertEqual(list(folder.glob("*.tmp")), [])


class EditLineTests(unittest.TestCase):
    def test_edit_lrc_line_keeps_the_timestamp(self):
        new = studio_resync.edit_lrc_line(LRC, 4, "  I be sharper,   baby alligator teeth ")
        self.assertIn("[00:05.00] I be sharper, baby alligator teeth\n", new)
        self.assertEqual(new.replace("[00:05.00] I be sharper, baby alligator teeth", ""),
                         LRC.replace("[00:05.00] I be sharper baby alligator teeth", ""))

    def test_edit_txt_line_changes_the_same_occurrence(self):
        new, found = studio_resync.edit_txt_line(TXT, LRC, 6, "Get geeked (Yuh)")
        self.assertTrue(found)
        self.assertIn("Get geeked,  get geeked\nI be sharper", new)  # the first chorus line stays
        self.assertIn("\nGet geeked (Yuh)\nLast line\n", new)

    def test_edit_txt_line_not_found_leaves_text(self):
        self.assertEqual(studio_resync.edit_txt_line("Other\n", LRC, 7, "New"), ("Other\n", False))


if __name__ == "__main__":
    unittest.main()
