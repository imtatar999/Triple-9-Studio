"""Every window remembers where it was and how big: reopened, it appears in the same place.

``configure(path)`` once at start-up, then ``remember(window)`` for each window (``create_toplevel`` of the
program does it). The window's kind is taken from its title ("Fix Fragment Manually — song.lrc" and
"Fix Fragment Manually — other.lrc" are the same kind). ``reset_all()`` forgets everything and brings the
open windows back to the middle of the screen — for a window that ended up somewhere unreachable.
"""
import json
import os
import re
import tkinter as tk
from pathlib import Path

_STATE = {"path": None, "enabled": True}
_GEOMETRY = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")
MIN_VISIBLE = (120, 60)  # a window must show at least this much on the screen to be restored there


def configure(path):
    _STATE["path"] = Path(path) if path else None


def window_key(title):
    """'[2/5] Fix Fragment Manually — Song.lrc' → 'Fix Fragment Manually'."""
    text = re.sub(r"^\[\d+/\d+\]\s*", "", str(title or "")).strip()
    text = re.split(r"\s+[—–]\s+", text, maxsplit=1)[0]
    text = re.sub(r"\s+\d+$", "", text).strip()
    return text or "window"


def _read():
    path = _STATE["path"]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    path = _STATE["path"]
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # remembering positions is a convenience; it must never break a window


def screen_bounds(window):
    """(left, top, right, bottom) of the whole desktop, all monitors."""
    try:
        import ctypes
        metrics = ctypes.windll.user32.GetSystemMetrics
        left, top = metrics(76), metrics(77)
        width, height = metrics(78), metrics(79)
        if width > 0 and height > 0:
            return left, top, left + width, top + height
    except Exception:
        pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def usable(geometry, bounds):
    """The saved geometry is valid and the window would be reachable on the current screens."""
    match = _GEOMETRY.match(str(geometry or ""))
    if not match:
        return False
    width, height, x, y = (int(v) for v in match.groups())
    left, top, right, bottom = bounds
    if width < 100 or height < 60 or width > right - left + 40 or height > bottom - top + 40:
        return False
    visible_w = min(x + width, right) - max(x, left)
    visible_h = min(y + height, bottom) - max(y, top)
    return visible_w >= MIN_VISIBLE[0] and visible_h >= MIN_VISIBLE[1] and y >= top - 10


def remember(window, key=None):
    """Restore this window's saved place when it is shown and save every move / resize from now on."""
    if _STATE["path"] is None:
        return
    info = {"key": key, "normal": None, "zoomed": False, "job": None}

    def current_key():
        if not info["key"]:
            info["key"] = window_key(window.title())
        return info["key"]

    def apply():
        try:
            if not window.winfo_exists():
                return
            saved = _read().get(current_key())
            if saved and usable(saved.get("geometry"), screen_bounds(window)):
                geometry = saved["geometry"]
                try:
                    fixed = window.resizable() == (False, False)
                except tk.TclError:
                    fixed = False
                if fixed:  # size is fixed by the window itself: only the place is restored
                    geometry = "+".join(["", *geometry.split("+")[1:]])
                window.geometry(geometry)
                if saved.get("zoomed"):
                    window.state("zoomed")
        except tk.TclError:
            pass
        finally:
            info["armed"] = True

    def snapshot():
        try:
            zoomed = window.state() == "zoomed"
            if not zoomed:
                info["normal"] = window.geometry()
            info["zoomed"] = zoomed
        except tk.TclError:
            pass

    def save():
        info["job"] = None
        if not info.get("armed") or not info["normal"]:
            return
        data = _read()
        data[current_key()] = {"geometry": info["normal"], "zoomed": info["zoomed"]}
        _write(data)

    def on_configure(event):
        if event.widget is not window or not info.get("armed"):
            return
        snapshot()
        if info["job"] is not None:
            try:
                window.after_cancel(info["job"])
            except tk.TclError:
                pass
        try:
            info["job"] = window.after(500, save)
        except tk.TclError:
            pass

    def on_destroy(event):
        if event.widget is window:
            save()  # the last remembered geometry — the window itself is already gone

    window.bind("<Configure>", on_configure, add="+")
    window.bind("<Destroy>", on_destroy, add="+")
    try:
        window.after_idle(apply)  # after the caller set the title and its own size
    except tk.TclError:
        pass


def reset_all(root):
    """Forget every saved place and bring each open window to the middle of the screen."""
    _write({})
    moved = 0
    left, top, right, bottom = screen_bounds(root)
    windows = [root] + [w for w in _walk(root) if isinstance(w, tk.Toplevel)]
    for window in windows:
        try:
            if window.overrideredirect():
                continue
            if window.state() in ("zoomed", "iconic"):
                window.state("normal")
            window.update_idletasks()
            width = min(window.winfo_width(), right - left - 80)
            height = min(window.winfo_height(), bottom - top - 120)
            window.geometry(f"{width}x{height}+{left + (right - left - width) // 2}+{top + (bottom - top - height) // 3}")
            moved += 1
        except tk.TclError:
            pass
    return moved


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)
