import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import studio_hints as h  # noqa: E402


class HintLookupTests(unittest.TestCase):
    def test_icons_counters_and_trailing_marks_do_not_matter(self):
        same = ["Quick listen", "🎧 Quick listen", "🎧 Quick listen  [Q]", "Quick listen…"]
        self.assertEqual({h.hint_key(t) for t in same}, {"quick listen"})
        self.assertEqual(h.hint_key("Restore ignored (3)"), "restore ignored")
        self.assertEqual(h.hint_key("💾 Save & next song ▶  (2/5)"), "save & next song")
        self.assertEqual(h.hint_key("Find & Download Missing Lyrics TXT [0]"), "find & download missing lyrics txt")

    def test_plus_and_minus_buttons_stay_apart(self):
        self.assertNotEqual(h.hint_key("−0.1 s [,]"), h.hint_key("+0.1 s [.]"))
        self.assertIn("earlier", h.lookup("−0.1 s [,]"))
        self.assertIn("later", h.lookup("+0.1 s [.]"))

    def test_buttons_with_a_changing_tail_use_their_prefix(self):
        self.assertIn("Show the real errors", h.lookup("Errors (12 songs)") or "")
        self.assertIsNotNone(h.lookup("Save and Change Lyrics and Synced Lyrics + Update Embedded"))
        self.assertIn("Whisper", h.lookup("#1 Whisper, prompt OFF"))
        self.assertIn("earlier", h.lookup("#3 Copy earlier repeat"))

    def test_unknown_button_has_no_hint_and_every_hint_is_short(self):
        self.assertIsNone(h.lookup("Some button nobody described"))
        self.assertIsNone(h.lookup(""))
        too_long = [k for k, v in h.HINTS.items() if len(v) > 110]
        self.assertEqual(too_long, [], "hints should stay short and to the point")


class CloseWindowTests(unittest.TestCase):
    def test_one_close_closes_the_whole_window_after_hovering_buttons(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        try:
            h.Hints(root, None).install()
            win = tk.Toplevel(root)
            buttons = [tk.Button(win, text=text) for text in ("Quick listen", "Play", "Close")]
            for button in buttons:
                button.pack()
            root.update()
            for button in buttons:  # mouse passes over the buttons: hints are planned and cancelled
                button.event_generate("<Enter>")
                root.update()
                button.event_generate("<Leave>")
                root.update()
            win.destroy()  # used to stop half-way: "can't delete Tcl command"
            root.update()
            self.assertFalse(win.winfo_exists())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
