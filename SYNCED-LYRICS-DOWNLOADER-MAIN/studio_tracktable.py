"""The Tracks list as a table (like Mp3tag): the status icons + file name on the left, the song's tags to the right.

``TrackTable`` is a ttk.Treeview that answers the same calls as the Listbox it replaces (insert / get / curselection /
selection_set / nearest / bbox …), so the rest of the program did not have to change. ``TagCache`` keeps the tags that were
already read (on disk, checked by file size and time), and ``TagReader`` reads the missing ones in the background in
small batches, so even a library of thousands of songs never freezes the window.
"""
import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

# (key, heading, width, anchor): "name" is the list entry itself (status icons + file name); the rest are tags
COLUMNS = (
    ("name", "Name", 380, "w"), ("filename", "File name", 220, "w"), ("title", "Title", 200, "w"),
    ("artist", "Artist", 150, "w"), ("albumartist", "Album artist", 150, "w"), ("album", "Album", 170, "w"),
    ("tracknumber", "Track", 50, "center"), ("date", "Year", 90, "center"), ("genre", "Genre", 100, "w"),
    ("comment", "Comment", 320, "w"), ("modified", "Date modified", 140, "w"),
)
TAG_KEYS = tuple(key for key, *_rest in COLUMNS[1:])
STYLE = "Tracks.Treeview"


class TrackTable(ttk.Treeview):
    def __init__(self, parent, **_ignored):
        self._font = tkfont.Font(family="Segoe UI", size=10)
        style = ttk.Style(parent)
        style.configure(STYLE, rowheight=self._font.metrics("linespace") + 6, font=self._font, borderwidth=0)
        style.configure(STYLE + ".Heading", font=("Segoe UI", 9, "bold"), relief="flat")
        # coloured field + headings: the elements of the "clam" theme, used only by this style
        for name, original in (("Tracks.field", "Treeview.field"), ("Tracks.heading", "Treeheading.border")):
            if name not in style.element_names():
                style.element_create(name, "from", "clam", original)
        style.layout(STYLE, [("Tracks.field", {"sticky": "nswe", "children": [
            ("Treeview.padding", {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]})]})])
        style.layout(STYLE + ".Heading", [("Tracks.heading", {"sticky": "nswe", "children": [
            ("Treeheading.padding", {"sticky": "nswe", "children": [("Treeheading.text", {"sticky": "we"})]})]})])
        super().__init__(parent, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended", style=STYLE)
        for key, heading, width, anchor in COLUMNS:
            self.heading(key, text=heading, anchor="w")
            self.column(key, width=width, minwidth=40, anchor=anchor, stretch=False)
        self._ids, self._index, self._counter = [], {}, 0
        self._quiet = None  # selection set by the program (not by the user): its <<ListboxSelect>> is not forwarded
        self._style = style
        self._order = [c[0] for c in COLUMNS]   # every column in display order (hidden ones keep their place)
        self.on_layout_changed = None           # called after the user dragged a heading or a column border
        self._pressed = None
        super().bind("<ButtonPress-1>", self._press, "+")
        super().bind("<ButtonRelease-1>", self._release, "+")

    # ---- Listbox-like API
    def size(self):
        return len(self._ids)

    def insert(self, index, text, values=()):
        self._counter += 1
        iid = f"r{self._counter}"
        row = (text,) + tuple(values) + ("",) * (len(COLUMNS) - 1 - len(values))
        super().insert("", "end", iid=iid, values=row)
        self._index[iid] = len(self._ids)
        self._ids.append(iid)

    def delete(self, first, last=None):
        if first in (0, "0") and last in (None, tk.END, "end"):
            if self._ids:
                super().delete(*self._ids)
            self._ids, self._index = [], {}
            return
        raise NotImplementedError("TrackTable only clears the whole list")

    def get(self, index):
        return self.set(self._ids[index], "name")

    def set_tags(self, index, values):
        """Fill the tag columns of one row (``values`` in TAG_KEYS order)."""
        iid = self._ids[index]
        super().item(iid, values=(self.set(iid, "name"),) + tuple(values))

    def curselection(self):
        return tuple(sorted(self._index[i] for i in self.selection() if i in self._index))

    def _range(self, first, last):
        first = len(self._ids) - 1 if first in (tk.END, "end") else int(first)
        last = first if last is None else (len(self._ids) - 1 if last in (tk.END, "end") else int(last))
        return self._ids[max(0, first):last + 1]

    def selection_set(self, first, last=None):
        self.selection_add_rows(self._range(first, last))

    def selection_add_rows(self, ids):
        if ids:
            super().selection_add(*ids)
        self._quiet = self.curselection()

    def selection_clear(self, first=0, last=None):
        if first in (0, "0") and last in (None, tk.END, "end"):
            if self.selection():
                super().selection_remove(*self.selection())
        else:
            ids = self._range(first, last)
            if ids:
                super().selection_remove(*ids)
        self._quiet = self.curselection()

    def see(self, index):
        if 0 <= index < len(self._ids):
            super().see(self._ids[index])

    def activate(self, index):
        if 0 <= index < len(self._ids):
            self.focus(self._ids[index])

    def nearest(self, y):
        iid = self.identify_row(y)
        return self._index.get(iid, -1)

    def bbox(self, index, column=None):
        if not 0 <= index < len(self._ids):
            return None
        return super().bbox(self._ids[index], column or "name") or None

    def bind(self, sequence=None, func=None, add=None):
        if sequence == "<<ListboxSelect>>":
            def forward(event):
                now = self.curselection()
                if self._quiet is not None and now == self._quiet:
                    return None  # the program set this selection itself
                self._quiet = None
                return func(event)
            return super().bind("<<TreeviewSelect>>", forward, add)
        return super().bind(sequence, func, add)

    def cget(self, key):
        return str(self._font) if key == "font" else super().cget(key)

    def configure(self, cnf=None, **kw):
        """Accepts the Listbox colour options (bg, fg, selectbackground …) and applies them to the table style."""
        kw = dict(cnf or {}, **kw)
        colours = {k: kw.pop(k) for k in ("bg", "fg", "selectbackground", "selectforeground") if k in kw}
        for key in ("highlightbackground", "highlightcolor", "width", "font"):
            kw.pop(key, None)
        if colours:
            self.style_colours(**colours)
        if kw:
            super().configure(**kw)

    config = configure

    def style_colours(self, bg=None, fg=None, selectbackground=None, selectforeground=None):
        opts = {}
        if bg:
            opts.update(background=bg, fieldbackground=bg)
        if fg:
            opts["foreground"] = fg
        self._style.configure(STYLE, **opts)
        if selectbackground or selectforeground:
            self._style.map(STYLE, background=[("selected", selectbackground or "#355a7a")],
                            foreground=[("selected", selectforeground or "#ffffff")])

    def set_heading_command(self, callback):
        """Clicking a column heading calls ``callback(column key)``."""
        for key, *_rest in COLUMNS:
            self.heading(key, command=lambda k=key: callback(k))

    def mark_sort(self, key=None, reverse=False):
        """Show ▲ / ▼ on the heading of the column the list is sorted by."""
        for column, heading, *_rest in COLUMNS:
            arrow = (" ▼" if reverse else " ▲") if column == key else ""
            self.heading(column, text=heading + arrow)

    def set_visible(self, keys):
        """Show only these tag columns (the Name column is always shown), keeping the order the user dragged them to."""
        wanted = set(keys) | {"name"}
        self["displaycolumns"] = [k for k in self._order if k in wanted]

    def _merged_order(self):
        """The full column order after the user moved visible columns: hidden columns keep their slot."""
        shown = [str(c) for c in self["displaycolumns"]]
        if shown == ["#all"]:
            return [c[0] for c in COLUMNS]
        slots = iter(shown)
        return [next(slots) if key in shown else key for key in self._order] + [
            k for k in shown if k not in self._order]

    def default_layout(self):
        """Every column back in the original order and width."""
        self._order = [c[0] for c in COLUMNS]
        for key, _heading, width, _anchor in COLUMNS:
            self.column(key, width=width)

    def layout(self):
        """{"order": every column in display order, "widths": {column: pixels}} — what the user arranged."""
        return {"order": self._merged_order(), "widths": {c[0]: int(self.column(c[0], "width")) for c in COLUMNS}}

    def apply_layout(self, layout):
        """Restore a saved ``layout()`` (unknown or missing columns are ignored)."""
        known = [c[0] for c in COLUMNS]
        order = [k for k in (layout or {}).get("order", []) if k in known]
        self._order = order + [k for k in known if k not in order]
        for key, width in ((layout or {}).get("widths") or {}).items():
            if key in known and isinstance(width, int) and 30 <= width <= 1600:
                self.column(key, width=width)

    def _press(self, event):
        self._pressed = self.identify_region(event.x, event.y)

    def _release(self, event):
        if self._pressed in ("separator", "heading") and self.on_layout_changed:
            self._order = self._merged_order()  # where the visible columns now are among the hidden ones
            self.after_idle(self.on_layout_changed)
        self._pressed = None

    def style_headings(self, bg, fg):
        self._style.configure(STYLE + ".Heading", background=bg, foreground=fg)
        self._style.map(STYLE + ".Heading", background=[("active", bg)])


class TagCache:
    """Tags already read, remembered on disk; a song is read again only when its size or time changed."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    @staticmethod
    def signature(path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return [stat.st_mtime_ns, stat.st_size]

    def lookup(self, path):
        """The tag values (TAG_KEYS order) when still valid, else None."""
        entry = self.data.get(path)
        valid = entry and entry[:2] == self.signature(path) and len(entry[2]) == len(TAG_KEYS)
        return entry[2] if valid else None

    def store(self, path, signature, values):
        with self.lock:
            self.data[path] = [signature[0], signature[1], list(values)]
            self.dirty = True

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            snapshot = json.dumps(self.data, ensure_ascii=False)
            self.dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(snapshot, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass


class TagReader:
    """Reads the tags of many songs one by one in a background thread and hands them over in small batches."""

    BATCH = 40

    def __init__(self, cache, read_one, deliver, finished=None):
        """``read_one(path)`` -> {key: text}; ``deliver(batch)`` is called (from the thread) with [(path, values)];
        ``finished()`` when the queue is empty."""
        self.cache, self.read_one, self.deliver, self.finished = cache, read_one, deliver, finished
        self.queue, self.queued, self.lock = [], set(), threading.Lock()
        self.running = False

    def request(self, paths):
        with self.lock:
            for path in paths:
                if path not in self.queued:
                    self.queued.add(path)
                    self.queue.append(path)
            if self.running or not self.queue:
                return
            self.running = True
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        batch = []
        while True:
            with self.lock:
                if not self.queue:
                    self.running = False
                    break
                path = self.queue.pop(0)  # songs on screen first: the newest request is at the end, so pop the oldest
                self.queued.discard(path)
            signature = TagCache.signature(path)
            if signature is None:
                continue
            try:
                tags = dict(self.read_one(path))
                tags["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(signature[0] / 1e9))  # from Windows
                values = [str(tags.get(key, "") or "") for key in TAG_KEYS]
            except Exception:
                values = [""] * len(TAG_KEYS)
            self.cache.store(path, signature, values)
            batch.append((path, values))
            if len(batch) >= self.BATCH:
                self.deliver(batch)
                batch = []
                time.sleep(0.02)  # leave the processor to the window between batches
        if batch:
            self.deliver(batch)
        self.cache.save()
        if self.finished:
            self.finished()
