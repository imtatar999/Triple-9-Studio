import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_backups as b


class BackupCleanupTests(unittest.TestCase):
    def make(self, path, age_days=0, data=b"x" * 10):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def library(self, root):
        resync = root / "resync_abc"
        self.make(resync / "0" / "song.mp3", 60, b"m" * 100)
        self.make(resync / "1" / "song.lrc", 60, b"l" * 20)
        self.make(resync / "restore.json", 60, b"{}")
        self.make(root / "fragment_new" / "song.lrc", 1, b"l" * 20)
        self.make(root / "LYRICS-EDITS" / "20260101_old.txt", 90, b"t" * 30)
        code = self.make(root / "BUILDER_OLD_20250101" / "builder.py", 400)
        bak = self.make(root / "app.py.bak", 400)
        return resync, code, bak

    def test_usage_is_counted_per_type_and_code_backups_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BACKUPS"
            self.library(root)
            usage = b.usage_by_type([root])
            self.assertEqual(usage["mp3"], [1, 100])
            self.assertEqual(usage["lrc"], [2, 40])
            self.assertEqual(usage["txt"], [1, 30])
            self.assertEqual(usage["other"], [1, 2])  # restore.json; builder.py and app.py.bak are not counted
            self.assertEqual(b.usage_by_type([root], days=30)["lrc"], [1, 20])

    def test_deleting_mp3_keeps_lyrics_backups_and_code(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BACKUPS"
            resync, code, bak = self.library(root)
            deleted, freed, errors = b.delete_types([root], {"mp3"})
            self.assertEqual((deleted, freed, errors), (1, 100, []))
            self.assertFalse((resync / "0" / "song.mp3").exists())
            self.assertTrue((resync / "1" / "song.lrc").exists())
            self.assertTrue(code.exists() and bak.exists())

    def test_backup_folder_removed_when_only_restore_list_is_left(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "BACKUPS"
            resync, code, _bak = self.library(root)
            deleted, _freed, _errors = b.delete_types([root], {"mp3", "lrc"}, days=30)
            self.assertEqual(deleted, 2)  # the old resync backup only; the 1-day-old fragment stays
            self.assertFalse(resync.exists())
            self.assertTrue((root / "fragment_new" / "song.lrc").exists())
            self.assertTrue(code.exists())

    def test_missing_roots_are_ignored(self):
        self.assertEqual(b.data_backups([Path("does-not-exist-xyz")]), [])
        self.assertEqual(sum(v[0] for v in b.usage_by_type([Path("does-not-exist-xyz")]).values()), 0)


if __name__ == "__main__":
    unittest.main()
