import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import studio_windows as sw  # noqa: E402


class WindowKeyTests(unittest.TestCase):
    def test_titles_of_the_same_kind_share_a_key(self):
        self.assertEqual(sw.window_key("Fix Fragment Manually — Song A.lrc"), "Fix Fragment Manually")
        self.assertEqual(sw.window_key("[2/5] Fix Fragment Manually — Song B.lrc"), "Fix Fragment Manually")
        self.assertEqual(sw.window_key("View/Change Lyrics — x.mp3"), "View/Change Lyrics")
        self.assertEqual(sw.window_key("Edit Line 12"), "Edit Line")
        self.assertEqual(sw.window_key("Options"), "Options")
        self.assertEqual(sw.window_key("Triple 9 Studio - Lyrics Sync"), "Triple 9 Studio - Lyrics Sync")
        self.assertEqual(sw.window_key(""), "window")


class UsableTests(unittest.TestCase):
    screen = (0, 0, 1920, 1080)

    def test_a_reachable_window_is_restored(self):
        self.assertTrue(sw.usable("800x600+100+50", self.screen))
        self.assertTrue(sw.usable("800x600+1500+100", self.screen))  # partly off the right edge: still reachable

    def test_unreachable_or_broken_geometry_is_ignored(self):
        for geometry in ("800x600+3000+100", "800x600+100+2000", "800x600-2000+100", "800x600+100+-500",
                         "garbage", "", None, "50x40+10+10", "9000x600+0+0"):
            self.assertFalse(sw.usable(geometry, self.screen), geometry)

    def test_a_window_on_a_second_monitor_to_the_left_is_fine(self):
        self.assertTrue(sw.usable("800x600+-1500+100", (-1920, 0, 1920, 1080)))


class RememberTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"no display: {exc}")
        self.tmp = tempfile.TemporaryDirectory()
        sw.configure(Path(self.tmp.name) / "windows.json")

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()
        sw.configure(None)

    def pump(self, seconds=0.3):
        import time
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def open_window(self, title="Demo — one", geometry="500x300+120+90"):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry(geometry)
        sw.remember(win)
        self.pump(0.2)
        return win

    def test_a_moved_and_resized_window_reopens_in_the_same_place(self):
        first = self.open_window()
        first.geometry("640x420+210+160")
        self.pump(0.3)
        first.destroy()
        self.pump(0.1)
        second = self.open_window(title="Demo — two")  # a different song, same kind of window
        self.pump(0.3)
        self.assertEqual(second.geometry(), "640x420+210+160")
        second.destroy()

    def test_a_fixed_size_window_gets_only_its_place_back(self):
        first = self.open_window(title="Fixed — a")
        first.geometry("500x300+300+200")
        self.pump(0.3)
        first.destroy()
        second = tk.Toplevel(self.root)
        second.title("Fixed — b")
        second.geometry("400x250+10+10")
        second.resizable(False, False)
        sw.remember(second)
        self.pump(0.4)
        self.assertTrue(second.geometry().startswith("400x250+300+200"), second.geometry())
        second.destroy()

    def test_reset_forgets_the_places_and_centres_open_windows(self):
        win = self.open_window(geometry="500x300+5+5")
        win.geometry("500x300+7+9")
        self.pump(0.9)  # saved half a second after the last move
        self.assertTrue(sw._read())
        moved = sw.reset_all(self.root)
        self.pump(0.2)
        self.assertGreaterEqual(moved, 2)
        self.assertEqual(sw._read(), {})
        left, top, right, bottom = sw.screen_bounds(self.root)
        self.assertGreater(win.winfo_x(), left + 50)
        win.destroy()


if __name__ == "__main__":
    unittest.main()
