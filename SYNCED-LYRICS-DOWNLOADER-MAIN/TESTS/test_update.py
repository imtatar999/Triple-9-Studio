import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_update as up


class UpdateTests(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(up.is_newer("v1.2.0", "1.1.9"))
        self.assertTrue(up.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(up.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(up.is_newer("junk", "1.0.0"))

    def test_latest_picks_highest_tag(self):
        def fetch(url):
            if url.endswith("per_page=100"):
                return json.dumps([{"name": "v1.0.0"}, {"name": "v1.10.0"}, {"name": "v1.9.5"}, {"name": "beta"}]).encode()
            raise OSError("no release page")
        found = up.latest("o/r", fetch)
        self.assertEqual((found["version"], found["tag"]), ("1.10.0", "v1.10.0"))
        self.assertTrue(found["zip"].endswith("/archive/refs/tags/v1.10.0.zip"))

    def test_apply_keeps_user_data_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            (root / "SYNCED-LYRICS-DOWNLOADER-MAIN").mkdir(parents=True)
            (root / "runtime").mkdir()
            (root / "runtime" / "keep.txt").write_text("mine")
            (root / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "lyrics_gui_config.json").write_text('{"token": "secret"}')
            (root / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "a.py").write_text("old")
            (root / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "same.py").write_text("same")
            archive = Path(tmp) / "new.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Triple-9-Studio-1.1.0/SYNCED-LYRICS-DOWNLOADER-MAIN/a.py", "new")
                z.writestr("Triple-9-Studio-1.1.0/SYNCED-LYRICS-DOWNLOADER-MAIN/same.py", "same")
                z.writestr("Triple-9-Studio-1.1.0/SYNCED-LYRICS-DOWNLOADER-MAIN/b.py", "added")
                z.writestr("Triple-9-Studio-1.1.0/SYNCED-LYRICS-DOWNLOADER-MAIN/lyrics_gui_config.json", "{}")
                z.writestr("Triple-9-Studio-1.1.0/runtime/keep.txt", "overwritten")
                z.writestr("Triple-9-Studio-1.1.0/SYNCED-LYRICS-DOWNLOADER-MAIN/studio_install.py", "pkgs")
            result = up.apply(archive, root, Path(tmp) / "bk")
            app = root / "SYNCED-LYRICS-DOWNLOADER-MAIN"
            self.assertEqual((app / "a.py").read_text(), "new")
            self.assertEqual((app / "b.py").read_text(), "added")
            self.assertIn("token", (app / "lyrics_gui_config.json").read_text())
            self.assertEqual((root / "runtime" / "keep.txt").read_text(), "mine")
            self.assertEqual(result["changed"], ["SYNCED-LYRICS-DOWNLOADER-MAIN/a.py"])
            self.assertTrue(result["install"])
            saved = list((Path(tmp) / "bk").glob("*.zip"))
            self.assertEqual(len(saved), 1)
            with zipfile.ZipFile(saved[0]) as z:
                self.assertEqual(z.read("SYNCED-LYRICS-DOWNLOADER-MAIN/a.py"), b"old")

    def test_git_checkout_is_never_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertTrue(up.is_development_copy(tmp))


if __name__ == "__main__":
    unittest.main()
