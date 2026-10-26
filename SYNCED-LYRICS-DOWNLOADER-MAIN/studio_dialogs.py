"""Message boxes drawn by the app itself, so they carry the triplenine icon.

Windows' native message boxes (what ``tkinter.messagebox`` shows) never show
the application icon in their title bar. ``install()`` replaces the
``tkinter.messagebox`` functions with Tk dialogs that do, keeping the same
names, arguments and return values, so every module (and every dialog added
later) gets them without changes.
"""

import tkinter as tk
from tkinter import messagebox

_ORIGINAL = {}
_SETTINGS = {"icon": None, "palette": None}

KIND_SYMBOLS = {"info": ("ℹ", "#4fc3f7"), "warning": ("⚠", "#ffb000"),
                "error": ("✖", "#ff5252"), "question": ("?", "#81c784")}


def _palette():
    getter = _SETTINGS["palette"]
    try:
        palette = dict(getter() if callable(getter) else (getter or {}))
    except Exception:
        palette = {}
    palette.setdefault("bg", "#1e1e1e")
    palette.setdefault("fg", "#e8e8e8")
    palette.setdefault("panel", "#2a2a2a")
    return palette


def _owner(parent):
    """The window the dialog belongs to: ``parent`` or the app's focused/main window."""
    root = tk._default_root
    if parent is not None:
        try:
            return parent.winfo_toplevel()
        except tk.TclError:
            pass
    if root is None:
        return None
    try:
        focused = root.focus_get()
        if focused is not None:
            return focused.winfo_toplevel()
    except (tk.TclError, KeyError):
        pass
    return root


def show(kind, title, message, buttons, default, cancel, parent=None, detail=None):
    """Show one modal dialog; returns the key of the clicked button (``cancel`` when closed)."""
    owner = _owner(parent)
    if owner is None:
        raise RuntimeError("no Tk root")
    palette = _palette()
    previous_grab = owner.grab_current()
    win = tk.Toplevel(owner)
    win.withdraw()
    win.title(title or "")
    if _SETTINGS["icon"]:
        try:
            win.iconbitmap(_SETTINGS["icon"])
        except tk.TclError:
            pass
    win.configure(bg=palette["bg"])
    win.resizable(False, False)
    try:
        if owner.winfo_viewable():
            win.transient(owner)
    except tk.TclError:
        pass
    body = tk.Frame(win, bg=palette["bg"])
    body.pack(fill="both", expand=True, padx=18, pady=(16, 10))
    symbol, color = KIND_SYMBOLS.get(kind, KIND_SYMBOLS["info"])
    tk.Label(body, text=symbol, fg=color, bg=palette["bg"], font=("Segoe UI", 22, "bold")).pack(
        side="left", anchor="n", padx=(0, 14))
    text = str(message or "") + (f"\n\n{detail}" if detail else "")
    tk.Label(body, text=text, fg=palette["fg"], bg=palette["bg"], justify="left", anchor="w",
             wraplength=560, font=("Segoe UI", 10)).pack(side="left", fill="both", expand=True)
    row = tk.Frame(win, bg=palette["bg"])
    row.pack(fill="x", padx=18, pady=(0, 14))
    result = {"value": cancel}

    def close(value):
        result["value"] = value
        win.destroy()

    widgets = {}
    for key, label in reversed(buttons):  # packed from the right: the first button ends up leftmost
        button = tk.Button(row, text=label, width=10, command=lambda k=key: close(k),
                           bg=palette.get("btn_bg", palette["panel"]), fg=palette.get("btn_fg", palette["fg"]),
                           activebackground=palette.get("accent", palette["panel"]),
                           font=("Segoe UI", 10, "bold" if key == default else "normal"))
        button.pack(side="right", padx=(8, 0))
        widgets[key] = button
    def press_enter(_event=None):
        focused = win.focus_get()
        close(next((key for key, button in widgets.items() if button is focused), default))

    win.bind("<Return>", press_enter)  # Enter presses the focused button (the default one at first)
    win.bind("<Escape>", lambda _e: close(cancel))
    for key, button in widgets.items():
        button.bind("<space>", lambda _e, k=key: close(k))
    win.protocol("WM_DELETE_WINDOW", lambda: close(cancel))
    # Centre over the owner (or the screen when the owner is hidden).
    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    try:
        if owner.winfo_viewable():
            x = owner.winfo_rootx() + (owner.winfo_width() - width) // 2
            y = owner.winfo_rooty() + (owner.winfo_height() - height) // 3
        else:
            raise tk.TclError
    except tk.TclError:
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.deiconify()
    win.lift()
    try:
        win.grab_set()
    except tk.TclError:
        pass
    (widgets.get(default) or win).focus_set()
    win.wait_window()
    try:  # give a modal window that was waiting underneath its grab back
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
    except tk.TclError:
        pass
    return result["value"]


def _make(name, kind, buttons, default, cancel, convert):
    def dialog(title=None, message=None, **options):
        try:
            choice = show(options.get("icon", kind) if options.get("icon") in KIND_SYMBOLS else kind,
                          title, message, buttons, options.get("default", default), cancel,
                          parent=options.get("parent"), detail=options.get("detail"))
        except (RuntimeError, tk.TclError):
            return _ORIGINAL[name](title, message, **options)  # no usable Tk window: native box
        return convert(choice)
    dialog.__name__ = name
    dialog.__doc__ = f"Triplenine replacement for tkinter.messagebox.{name}."
    return dialog


OK = [("ok", "OK")]
YES_NO = [("yes", "Yes"), ("no", "No")]
YES_NO_CANCEL = [("yes", "Yes"), ("no", "No"), ("cancel", "Cancel")]
OK_CANCEL = [("ok", "OK"), ("cancel", "Cancel")]
RETRY_CANCEL = [("retry", "Retry"), ("cancel", "Cancel")]

REPLACEMENTS = {
    "showinfo": ("info", OK, "ok", "ok", lambda c: "ok"),
    "showwarning": ("warning", OK, "ok", "ok", lambda c: "ok"),
    "showerror": ("error", OK, "ok", "ok", lambda c: "ok"),
    "askyesno": ("question", YES_NO, "yes", "no", lambda c: c == "yes"),
    "askyesnocancel": ("question", YES_NO_CANCEL, "yes", "cancel",
                       lambda c: None if c == "cancel" else c == "yes"),
    "askokcancel": ("question", OK_CANCEL, "ok", "cancel", lambda c: c == "ok"),
    "askquestion": ("question", YES_NO, "yes", "no", lambda c: c),
    "askretrycancel": ("warning", RETRY_CANCEL, "retry", "cancel", lambda c: c == "retry"),
}


def install(icon_path=None, palette=None):
    """Replace tkinter.messagebox's functions (once) and remember the icon and colours."""
    _SETTINGS["icon"] = icon_path
    _SETTINGS["palette"] = palette
    for name, (kind, buttons, default, cancel, convert) in REPLACEMENTS.items():
        if name not in _ORIGINAL:
            _ORIGINAL[name] = getattr(messagebox, name)
        setattr(messagebox, name, _make(name, kind, buttons, default, cancel, convert))


def heading_icon(font, icon_path, scale=1.45):
    """The triplenine logo sized to a heading font (a bit taller than its capitals); None without Pillow."""
    try:
        from PIL import Image, ImageTk
        from tkinter import font as tkfont
        size = max(16, int(tkfont.Font(font=font).metrics("ascent") * scale))
        with Image.open(icon_path) as icon:
            if hasattr(icon, "ico"):  # use the smallest layer that is big enough: the sharpest result
                layers = sorted(icon.ico.sizes())
                icon.size = next((layer for layer in layers if layer[0] >= size), layers[-1])
            picture = icon.convert("RGBA").resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(picture)
    except Exception:
        return None


def add_heading_logo(label, icon_path, scale=1.45):
    """Put the logo left of a heading label's text (tk or ttk). Returns True when it was added."""
    picture = heading_icon(label.cget("font"), icon_path, scale) if icon_path else None
    if picture is None:
        return False
    label.configure(image=picture, compound="left", text="  " + str(label.cget("text")).strip())
    label.image = picture  # keep a reference, or Tk drops the picture
    return True
