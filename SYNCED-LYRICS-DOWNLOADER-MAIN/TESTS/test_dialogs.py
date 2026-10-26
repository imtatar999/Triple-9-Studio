import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import messagebox

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import studio_dialogs  # noqa: E402


class AppDialogTests(unittest.TestCase):
    """The replacement message boxes keep tkinter's return values and carry the icon."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"no display: {exc}")
        cls.root.geometry("400x300+100+100")
        cls.saved = {name: getattr(messagebox, name) for name in studio_dialogs.REPLACEMENTS}
        studio_dialogs.install(str(APP_DIR / "triplenine.ico"), {"bg": "#101010", "fg": "#eeeeee"})

    @classmethod
    def tearDownClass(cls):
        for name, function in cls.saved.items():
            setattr(messagebox, name, function)
        studio_dialogs._ORIGINAL.clear()
        cls.root.destroy()

    def answer(self, action, delay=150):
        """Run ``action(dialog_window)`` once the dialog is up."""
        seen = {}

        def go():
            def toplevels(widget):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Toplevel):
                        yield child
                        yield from toplevels(child)

            dialogs = [w for w in toplevels(self.root) if w.winfo_viewable() and w.title() not in ("modal",)]
            if not dialogs:
                self.root.after(50, go)
                return
            seen["window"] = dialogs[-1]
            seen["title"] = dialogs[-1].title()
            action(dialogs[-1])

        self.root.after(delay, go)
        return seen

    @staticmethod
    def press(label):
        def action(win):
            for widget in win.winfo_children():
                for child in widget.winfo_children():
                    if isinstance(child, tk.Button) and child.cget("text") == label:
                        child.invoke()
                        return
        return action

    @staticmethod
    def key(name):
        def action(win):
            win.event_generate(f"<{name}>")
        return action

    def test_return_values_match_tkinter(self):
        cases = [
            ("showinfo", "OK", "ok"), ("showwarning", "OK", "ok"), ("showerror", "OK", "ok"),
            ("askyesno", "Yes", True), ("askyesno", "No", False),
            ("askyesnocancel", "Yes", True), ("askyesnocancel", "No", False), ("askyesnocancel", "Cancel", None),
            ("askokcancel", "OK", True), ("askokcancel", "Cancel", False),
            ("askquestion", "Yes", "yes"), ("askquestion", "No", "no"),
            ("askretrycancel", "Retry", True), ("askretrycancel", "Cancel", False),
        ]
        for name, button, expected in cases:
            with self.subTest(name=name, button=button):
                seen = self.answer(self.press(button))
                result = getattr(messagebox, name)("Title " + name, "Message", parent=self.root)
                self.assertEqual(result, expected)
                self.assertEqual(seen.get("title"), "Title " + name)

    def test_enter_takes_default_and_escape_cancels(self):
        self.answer(self.key("Return"))
        self.assertTrue(messagebox.askyesno("Q", "Enter?", parent=self.root))
        self.answer(self.key("Escape"))
        self.assertIsNone(messagebox.askyesnocancel("Q", "Escape?", parent=self.root))
        self.answer(self.key("Escape"))
        self.assertFalse(messagebox.askyesno("Q", "Escape means No", parent=self.root))
        self.answer(self.key("Return"))
        self.assertFalse(messagebox.askyesno("Q", "default=no", parent=self.root, default="no"))

    def test_window_carries_the_icon_and_closing_cancels(self):
        calls = []
        original = tk.Toplevel.iconbitmap

        def spy(win, *args, **kwargs):
            calls.append(args)
            return original(win, *args, **kwargs)

        tk.Toplevel.iconbitmap = spy
        try:
            self.answer(lambda win: win.tk.call(win.protocol("WM_DELETE_WINDOW")))  # the title bar's ✕
            self.assertIsNone(messagebox.askyesnocancel("Q", "Close the window", parent=self.root))
        finally:
            tk.Toplevel.iconbitmap = original
        self.assertTrue(any("triplenine.ico" in str(args[0]) for args in calls if args))

    def test_modal_window_underneath_gets_its_grab_back(self):
        modal = tk.Toplevel(self.root)
        modal.title("modal")
        modal.geometry("200x100+150+150")
        self.root.update()
        modal.grab_set()
        self.answer(self.press("OK"))
        messagebox.showinfo("Info", "over a modal window", parent=modal)
        self.assertIs(modal.grab_current(), modal)
        modal.destroy()


if __name__ == "__main__":
    unittest.main()
