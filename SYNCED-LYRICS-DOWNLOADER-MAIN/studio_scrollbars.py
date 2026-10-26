"""Light grey scroll bars everywhere.

The standard Windows scroll bar of Tk is white and cannot be coloured, so on the dark theme the thumb is hard to see.
``install(root)`` replaces ``tk.Scrollbar`` with a themed one (light grey thumb on a dark grey track) and gives the
ttk scroll bars the same look; every window opened afterwards uses it.
"""
import tkinter as tk
from tkinter import ttk

THUMB, THUMB_ACTIVE, TRACK, ARROW = "#a9a9a9", "#c4c4c4", "#2b2b2b", "#2b2b2b"
_installed = False


def _grey_layout(style, orient):
    """The clam layout of a scroll bar with its elements copied under our own names (so only these change)."""
    current = style.theme_use()
    style.theme_use("clam")
    layout = style.layout(f"{orient}.TScrollbar")
    style.theme_use(current)

    def convert(items):
        result = []
        for name, options in items:
            options = dict(options)
            new = "Grey" + name
            if new not in style.element_names():
                style.element_create(new, "from", "clam", name)
            if "children" in options:
                options["children"] = convert(options["children"])
            result.append((new, options))
        return result

    return convert(layout)


def install(root):
    global _installed
    if _installed:
        return
    style = ttk.Style(root)
    for orient in ("Vertical", "Horizontal"):
        name = f"Grey.{orient}.TScrollbar"
        style.layout(name, _grey_layout(style, orient))
        style.configure(name, troughcolor=TRACK, background=THUMB, bordercolor=TRACK, lightcolor=THUMB,
                        darkcolor=THUMB, arrowcolor=ARROW, relief="flat", arrowsize=16)
        style.map(name, background=[("active", THUMB_ACTIVE), ("pressed", THUMB_ACTIVE)],
                  lightcolor=[("active", THUMB_ACTIVE)], darkcolor=[("active", THUMB_ACTIVE)])
        style.configure(f"{orient}.TScrollbar", troughcolor=TRACK, background=THUMB)  # plain ttk scroll bars too

    class GreyScrollbar(ttk.Scrollbar):
        """Accepts what tk.Scrollbar accepted (orient, command); colours come from the style."""

        def __init__(self, master=None, **options):
            orient = options.get("orient", "vertical")
            super().__init__(master, orient=orient, command=options.get("command"),
                             style=f"Grey.{'Horizontal' if str(orient).startswith('h') else 'Vertical'}.TScrollbar")

        def configure(self, cnf=None, **kw):
            kw = {k: v for k, v in dict(cnf or {}, **kw).items() if k in ("command", "orient", "style", "takefocus")}
            if kw:
                super().configure(**kw)

        config = configure

    tk.Scrollbar = GreyScrollbar
    _installed = True
