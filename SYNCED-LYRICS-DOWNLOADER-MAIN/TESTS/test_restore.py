import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_restore as r


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.music = base / "music"
        (self.music / "SYNCED LYRICS").mkdir(parents=True)
        (self.music / "LYRICS").mkdir()
        self.song = self.music / "Song A.mp3"
        self.song.write_bytes(b"audio now")
        self.lrc = self.music / "SYNCED LYRICS" / "Song A.lrc"
        self.lrc.write_text("[00:01.00] now", encoding="utf-8")
        self.backups = base / "BACKUPS"
        self.index = None

    def make(self, path, text, age=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        os.utime(path.parent, (stamp, stamp))
        return path

    def items(self):
        return r.list_backups([self.backups], r.library_index(self.music))

    def test_restore_json_backup_goes_back_to_its_exact_place(self):
        folder = self.backups / "resync_abc"
        copy = self.make(folder / "1" / "Song A.lrc", "[00:01.00] old")
        (folder / "restore.json").write_text(json.dumps({str(self.lrc): str(copy)}), encoding="utf-8")
        items = self.items()
        self.assertEqual(items[0]["kind"], "Before resync / marked for sync")
        restored, skipped, safety = r.restore(items[:1], self.backups)
        self.assertEqual((restored, skipped), ([self.lrc], []))
        self.assertEqual(self.lrc.read_text(encoding="utf-8"), "[00:01.00] old")
        # The replaced file was kept, so the restore itself can be undone.
        kept = json.loads((safety / "restore.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(kept[str(self.lrc)]).read_text(encoding="utf-8"), "[00:01.00] now")
        self.assertIn("Before a restore", [i["kind"] for i in self.items()])

    def test_name_only_backups_find_their_place_in_the_library(self):
        self.make(self.backups / "fragment_x" / "Song A.lrc", "[00:01.00] fragment backup", age=30)
        self.make(self.backups / "LYRICS-EDITS" / "20260930_101010_abcdef12_Song A.txt", "old text", age=20)
        self.make(self.backups / "deleted_lyrics_y" / "0" / "Song A.txt", "deleted text", age=10)
        targets = {i["kind"]: i["files"][0][1] for i in self.items()}
        self.assertEqual(targets["Before Fix fragment / lyrics edit"], self.lrc)
        self.assertEqual(targets["Before lyrics text edit (TXT)"], self.music / "LYRICS" / "Song A.txt")
        self.assertEqual(targets["Deleted lyrics"], self.music / "LYRICS" / "Song A.txt")
        newest = self.items()[0]
        r.restore([newest], self.backups)
        self.assertEqual((self.music / "LYRICS" / "Song A.txt").read_text(encoding="utf-8"), "deleted text")

    def test_marked_for_transcription_names_are_decoded(self):
        self.make(self.backups / "MARKED-FOR-TRANSCRIPTION" / "20260907_1" / "01_Lyrics_Song A.txt", "marked")
        item = self.items()[0]
        self.assertEqual(item["files"][0][1], self.music / "Lyrics" / "Song A.txt"
                         if os.name != "nt" else item["files"][0][1])
        self.assertEqual(item["files"][0][1].name, "Song A.txt")

    def test_unknown_songs_are_skipped_not_guessed(self):
        self.make(self.backups / "fragment_z" / "Unknown Song.lrc", "x")
        restored, skipped, safety = r.restore(self.items(), self.backups)
        self.assertEqual(restored, [])
        self.assertTrue(skipped and "unknown" in skipped[0])
        self.assertIsNone(safety)

    def test_code_backups_are_never_listed(self):
        self.make(self.backups / "BUILDER_X" / "Song A.lrc", "x")
        self.make(self.backups / "app.py.bak", "x")
        self.assertEqual(self.items(), [])


if __name__ == "__main__":
    unittest.main()
