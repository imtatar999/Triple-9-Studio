"""Quick listen: go through suspicious passages one by one, hear each in a few seconds and decide.

For every flagged passage the window plays a short clip around it (looping) and shows the LRC lines with
the one that is playing highlighted. One key decides:

    O / Enter  sounds OK → the warning is ignored          F  needs fixing → Fix fragment opens
    W          the lyrics are wrong → View/Change Lyrics    S / →  skip for now      Space  replay
"""
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

import studio_waveform

CLIP_LEAD = 1.5      # seconds before the first context line
CLIP_TAIL = 3.0      # seconds after the last context line
CLIP_MIN, CLIP_MAX = 6.0, 20.0
CONTEXT = 2          # lines shown before and after the passage


def clip_bounds(entries, first_line, last_line):
    """(start, duration) in seconds of the clip that covers the passage and its neighbours."""
    indices = [i for i, entry in enumerate(entries) if first_line <= entry["file_line"] <= last_line]
    if not indices:
        return 0.0, CLIP_MIN
    start = max(0.0, entries[max(0, indices[0] - 1)]["time_ms"] / 1000 - CLIP_LEAD)
    end = entries[min(len(entries) - 1, indices[-1] + CONTEXT)]["time_ms"] / 1000 + CLIP_TAIL
    return start, min(CLIP_MAX, max(CLIP_MIN, end - start))


def decode_clip(audio, start, duration, rate=44100, channels=2):
    """Raw 16-bit PCM of ``duration`` seconds of ``audio`` from ``start`` (ffmpeg seeks fast in MP3)."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(audio), "-vn", "-f", "s16le", "-acodec", "pcm_s16le", "-ac", str(channels), "-ar", str(rate), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not result.stdout:
        raise RuntimeError("ffmpeg could not read the audio: " + result.stderr.decode("utf-8", "replace")[-200:])
    return result.stdout


def describe(item):
    kind = {"dense_lines": "lines squeezed together too tightly to be sung",
            "even_spacing": "lines with guessed timing (spread evenly)"}.get(item.get("kind"), "lines at the same time")
    return f"{item.get('count', '?')} {kind} — starting at {item.get('timestamp', '')}"


class QuickListen:
    """The Quick listen window. ``handlers`` (all provided by the Edit and Fix report):

    ``resolve(item)`` → the current row of that passage or None (it may be fixed/ignored meanwhile);
    ``ignore(item)``; ``fix(item, done)`` → opens Fix fragment, calls ``done(saved)`` afterwards;
    ``wrong(item, done)`` → View/Change Lyrics, calls ``done()``; ``audio(item)`` → the MP3 path;
    ``entries(item)`` → the timed lines of its LRC; ``name(item)`` → a readable song name.
    """

    def __init__(self, parent, theme, create_toplevel, items, handlers, volume=0.8):
        self.theme, self.items, self.h = theme, list(items), handlers
        self.position, self.volume = 0, min(1.0, max(0.0, float(volume)))
        self.counts = {"ok": 0, "fixed": 0, "wrong": 0, "skipped": 0}
        self.win = create_toplevel(parent)
        self.win.title("Quick listen — Edit and Fix")
        self.win.configure(bg=theme["bg"])
        self.win.minsize(900, 560)
        self.win.geometry("1000x640")
        self.player = None
        self.clip_start = 0.0
        self.entries = []
        self.item = None
        self._loading = None   # position being decoded
        self._cache = {}       # position -> (entries, start, pcm)
        self._results = queue.Queue()
        self._closed = False
        self._build()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.after(100, self._poll)
        self.win.after(60, self._tick)
        self.load(0)

    # ----- window ---------------------------------------------------------
    def _build(self):
        t, win = self.theme, self.win
        self.head_var, self.what_var = tk.StringVar(), tk.StringVar()
        tk.Label(win, textvariable=self.head_var, bg=t["bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 14, "bold")).pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(win, textvariable=self.what_var, bg=t["bg"], fg="#ffb000", anchor="w",
                 font=("Segoe UI", 11)).pack(fill="x", padx=16, pady=(2, 8))
        self.lines = tk.Listbox(win, height=10, exportselection=False, activestyle="none", bg=t["panel"], fg=t["fg"],
                                font=("Segoe UI", 13), selectbackground="#1f5130", selectforeground="#ffffff")
        self.lines.pack(fill="both", expand=True, padx=16)
        self.status_var = tk.StringVar(value="Loading the first clip…")
        tk.Label(win, textvariable=self.status_var, bg=t["bg"], fg=t.get("muted", t["fg"]), anchor="w",
                 wraplength=950, justify="left").pack(fill="x", padx=16, pady=(8, 4))
        self.loop_var = tk.BooleanVar(value=True)
        options = tk.Frame(win, bg=t["bg"])
        options.pack(fill="x", padx=16)
        tk.Checkbutton(options, text="Loop the clip until I decide", variable=self.loop_var, bg=t["bg"], fg=t["fg"],
                       selectcolor=t["panel"], activebackground=t["bg"], takefocus=0).pack(side="left")
        tk.Label(options, text="🔊 Volume", bg=t["bg"], fg=t["fg"]).pack(side="left", padx=(24, 4))
        self.volume_var = tk.IntVar(value=int(round(self.volume * 100)))
        self.volume_scale = tk.Scale(options, from_=0, to=100, orient="horizontal", length=200, showvalue=True,
                                     variable=self.volume_var, command=self._volume_changed, takefocus=0,
                                     bg=t["bg"], fg=t["fg"], troughcolor=t["panel"], highlightthickness=0,
                                     sliderlength=18, width=12)
        self.volume_scale.pack(side="left")
        row = tk.Frame(win, bg=t["bg"])
        row.pack(fill="x", padx=16, pady=(8, 14))

        def button(text, command, color=None, side="left"):
            b = tk.Button(row, text=text, command=command, takefocus=0, font=("Segoe UI", 11, "bold"), padx=12, pady=6,
                          bg=color or t.get("btn_bg", t["panel"]), fg="#ffffff" if color else t.get("btn_fg", t["fg"]))
            b.pack(side=side, padx=(0, 8))
            return b

        button("✔ Sounds OK — ignore  [O]", self.ok, "#1f5130")
        button("🔧 Needs fixing  [F]", self.fix, "#7a4b00")
        button("❓ Lyrics wrong  [W]", self.wrong, "#6b2a2a")
        button("⏭ Skip  [S]", self.skip)
        button("🔁 Replay  [Space]", self.replay)
        button("Close", self.close, side="right")
        for key, command in (("<space>", self.replay), ("<KeyPress-o>", self.ok), ("<KeyPress-O>", self.ok),
                             ("<Return>", self.ok), ("<KeyPress-f>", self.fix), ("<KeyPress-F>", self.fix),
                             ("<KeyPress-w>", self.wrong), ("<KeyPress-W>", self.wrong), ("<KeyPress-s>", self.skip),
                             ("<KeyPress-S>", self.skip), ("<Right>", self.skip), ("<Escape>", self.close)):
            win.bind(key, lambda _e, c=command: (c(), "break")[1])
        win.focus_force()

    def _volume_changed(self, _value=None):
        """Applies at once, stays for the next passages and is remembered for the next time."""
        self.volume = self.volume_var.get() / 100.0
        if self.player is not None:
            try:
                self.player.set_volume(self.volume)
            except Exception:
                pass
        if self.h.get("volume"):
            self.h["volume"](self.volume)

    # ----- loading / playing ----------------------------------------------
    def load(self, position):
        """Show passage ``position`` (skipping ones that are gone) and start its clip."""
        self.stop()
        while position < len(self.items):
            current = self.h["resolve"](self.items[position])
            if current is not None:
                self.items[position] = current
                break
            position += 1
        if position >= len(self.items):
            self.finish()
            return
        self.position, self.item = position, self.items[position]
        self.head_var.set(f"Passage {position + 1} of {len(self.items)} — {self.h['name'](self.item)}")
        self.what_var.set(describe(self.item))
        self.lines.delete(0, "end")
        self.status_var.set("Loading the clip…")
        self._request(position)

    def _request(self, position):
        if position in self._cache or position >= len(self.items) or self._loading == position:
            self._after_ready(position)
            return
        self._loading = position
        item = self.items[position]
        # The thread must not hold the window (or anything with Tk variables): if it were the last owner,
        # the variables would be freed in this thread and Tk would crash. It gets plain objects only.
        get_entries, get_audio, results = self.h["entries"], self.h["audio"], self._results

        def worker():
            try:
                entries = get_entries(item)
                start, duration = clip_bounds(entries, item["first_file_line"], item["last_file_line"])
                pcm = decode_clip(get_audio(item), start, duration)
                results.put((position, "ok", (entries, start, pcm)))
            except Exception as exc:
                results.put((position, "error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                position, kind, value = self._results.get_nowait()
                if self._loading == position:
                    self._loading = None
                if kind == "ok":
                    self._cache[position] = value
                elif position == self.position:
                    self.status_var.set(f"Could not play this passage: {value}")
                self._after_ready(position)
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _after_ready(self, position):
        if position == self.position and position in self._cache and self.player is None:
            self.entries, self.clip_start, pcm = self._cache[position]
            self._show_lines()
            self.play(pcm)
            nxt = position + 1  # decode the next clip while this one is judged
            if nxt < len(self.items) and nxt not in self._cache and self._loading is None:
                self._request(nxt)

    def _show_lines(self):
        first, last = self.item["first_file_line"], self.item["last_file_line"]
        indices = [i for i, e in enumerate(self.entries) if first <= e["file_line"] <= last]
        lo, hi = max(0, indices[0] - CONTEXT), min(len(self.entries) - 1, indices[-1] + CONTEXT)
        self.shown = list(range(lo, hi + 1))
        self.lines.delete(0, "end")
        for i in self.shown:
            entry = self.entries[i]
            flagged = first <= entry["file_line"] <= last
            m, s = divmod(entry["time_ms"] / 1000, 60)
            self.lines.insert("end", f"{'⚠' if flagged else '  '}  {int(m):02d}:{s:05.2f}   {entry['text']}")
            self.lines.itemconfig("end", fg="#ff5252" if flagged else self.theme["fg"])

    def play(self, pcm):
        try:
            self.player = studio_waveform.WaveOutPlayer(pcm, 44100, 2)
            self.player.set_volume(self.volume)
            self.player.play(0.0)
            self.duration = len(pcm) / (44100 * 4)
            self.status_var.set("Listen: the highlighted line is the one playing. Decide with O / F / W / S.")
        except Exception as exc:
            self.player = None
            self.status_var.set(f"Playback unavailable: {exc}")

    def _tick(self):
        if self._closed:
            return
        if self.player is not None and self.entries:
            try:
                now = self.clip_start + self.player.position()
                current = None
                for k, i in enumerate(self.shown):
                    if self.entries[i]["time_ms"] / 1000 <= now + 0.02:
                        current = k
                self.lines.selection_clear(0, "end")
                if current is not None:
                    self.lines.selection_set(current)
                    self.lines.see(current)
                if self.player.finished() or self.player.position() >= self.duration - 0.05:
                    if self.loop_var.get():
                        self.player.play(0.0)
                    else:
                        self.player.pause()
            except Exception:
                pass
        self.win.after(60, self._tick)

    def stop(self):
        if self.player is not None:
            self.player.close()
            self.player = None

    def replay(self):
        if self.player is not None:
            self.player.play(0.0)
        elif self.position in self._cache:
            self._after_ready(self.position)

    # ----- decisions --------------------------------------------------------
    def _next(self):
        self.win.focus_force()
        self.load(self.position + 1)

    def ok(self):
        if self.item is None:
            return
        self.h["ignore"](self.item)
        self.counts["ok"] += 1
        self._next()

    def skip(self):
        if self.item is None:
            return
        self.counts["skipped"] += 1
        self._next()

    def _hand_over(self, action, counter):
        """Run Fix fragment / View-Change Lyrics on top; carry on with the next passage afterwards."""
        if self.item is None:
            return
        self.stop()
        self.win.withdraw()

        def done(saved=True):
            if self._closed:
                return
            self.counts[counter if saved else "skipped"] += 1
            self.win.deiconify()
            self._next()

        action(self.item, done)

    def fix(self):
        self._hand_over(self.h["fix"], "fixed")

    def wrong(self):
        self._hand_over(self.h["wrong"], "wrong")

    def finish(self):
        self.stop()
        self.head_var.set("Quick listen finished")
        self.what_var.set(f"Ignored (sounded OK): {self.counts['ok']}   •   Fixed: {self.counts['fixed']}   •   "
                          f"Wrong lyrics: {self.counts['wrong']}   •   Skipped: {self.counts['skipped']}")
        self.lines.delete(0, "end")
        self.status_var.set("Nothing left in this list. Close the window.")
        self.item = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        self.win.destroy()
        if self.h.get("closed"):
            self.h["closed"](self.counts)
