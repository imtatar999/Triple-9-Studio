"""Waveform view with playback and LRC timing lanes, used by Fix fragment.

Audio is decoded once by ffmpeg into a temporary 16-bit WAV, so the playhead,
the waveform and Whisper's timings share exactly the same timeline (MP3 seeking
in system players can drift on VBR files). Playback uses the Windows MCI
"waveaudio" device through ctypes: no extra packages are needed.
"""
import bisect
import itertools
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import wave

try:
    import numpy as np
except ImportError:  # the app already depends on numpy (Whisper); keep import errors readable
    np = None

PEAKS_PER_SECOND = 200
ZOOM_SPANS = (5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
LABEL_WIDTH = 92
WAVE_HEIGHT = 140
RULER_HEIGHT = 16
LANE_HEIGHT = 26
ALERT_COLOR = "#ff5252"  # lines still flagged as errors
SELECTED_COLOR = "#2f5d8a"  # lines selected in the list
HIT_RADIUS = 10  # pixels around a marker that count as clicking it (its words to the right count too)


def decode_audio(path, keep_pcm=False):
    """Decode ``path`` to a temporary WAV; return (wav_path, peaks 0..1, duration_seconds).

    ``keep_pcm`` adds a 4th item, ``(pcm_bytes, rate, channels)``, for the in-memory player."""
    fd, wav_path = tempfile.mkstemp(prefix="t9_wave_", suffix=".wav")
    os.close(fd)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-vn", "-ac", "2", "-ar", "44100", "-acodec", "pcm_s16le", wav_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        os.remove(wav_path)
        raise RuntimeError("ffmpeg could not decode the audio: " + (result.stdout or "").strip()[-300:])
    with wave.open(wav_path, "rb") as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        raw = stream.readframes(frames)
        data = np.frombuffer(raw, dtype=np.int16)
    mono = np.abs(data.reshape(-1, channels).astype(np.int32)).max(axis=1)
    bucket = max(1, rate // PEAKS_PER_SECOND)
    usable = len(mono) // bucket * bucket
    peaks = mono[:usable].reshape(-1, bucket).max(axis=1).astype(np.float32)
    scale = float(np.percentile(peaks, 99.5)) if len(peaks) else 1.0
    peaks = np.clip(peaks / max(scale, 1.0), 0.0, 1.0)
    if keep_pcm:
        return wav_path, peaks, frames / float(rate), (raw, rate, channels)
    return wav_path, peaks, frames / float(rate)


class WaveOutPlayer:
    """Low-latency player: the whole song sits in memory and goes straight to the sound card.

    Play starts within a few milliseconds (no file reading, no MCI buffering), and the
    position comes from the samples the device has really played, so the red playhead —
    and every time set with T — matches what you hear.
    """

    def __init__(self, pcm, rate, channels):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._winmm = ctypes.windll.winmm

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                        ("cbSize", wintypes.WORD)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                        ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                        ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

        class MMTIME(ctypes.Structure):
            _fields_ = [("wType", wintypes.UINT), ("sample", wintypes.DWORD), ("_pad", wintypes.DWORD)]

        self._WAVEHDR, self._MMTIME = WAVEHDR, MMTIME
        self.rate, self.block = int(rate), int(channels) * 2
        self._pcm = ctypes.create_string_buffer(pcm, len(pcm))  # kept alive while the device reads it
        self._size = len(pcm)
        fmt = WAVEFORMATEX(1, int(channels), int(rate), int(rate) * self.block, self.block, 16, 0)
        self._handle = ctypes.c_void_p()
        self._check(self._winmm.waveOutOpen(ctypes.byref(self._handle), ctypes.c_uint(0xFFFFFFFF),
                                            ctypes.byref(fmt), 0, 0, 0), "waveOutOpen")
        self._header = None
        self._start = 0.0
        self._base, self._clock = 0.0, time.perf_counter()
        self._paused_at = 0.0
        self.playing = False

    def _check(self, error, what):
        if error:
            raise RuntimeError(f"{what} failed (MMRESULT {error})")

    def _release(self):
        """Stop the device and give the buffer back."""
        self._winmm.waveOutReset(self._handle)
        if self._header is not None:
            self._winmm.waveOutUnprepareHeader(self._handle, self._ctypes.byref(self._header),
                                               self._ctypes.sizeof(self._header))
            self._header = None

    def play(self, start_seconds):
        self._release()
        frame = min(max(0, int(start_seconds * self.rate)), self._size // self.block)
        offset = frame * self.block
        header = self._WAVEHDR()
        header.lpData = self._ctypes.addressof(self._pcm) + offset
        header.dwBufferLength = max(self.block, self._size - offset)
        self._check(self._winmm.waveOutPrepareHeader(self._handle, self._ctypes.byref(header),
                                                     self._ctypes.sizeof(header)), "waveOutPrepareHeader")
        self._header = header
        self._start = frame / self.rate
        self._base = self._device_seconds()  # some drivers do not reset their counter on waveOutReset
        self._clock = time.perf_counter()
        self._check(self._winmm.waveOutWrite(self._handle, self._ctypes.byref(header),
                                             self._ctypes.sizeof(header)), "waveOutWrite")
        self.playing = True

    def _device_seconds(self):
        when = self._MMTIME(2, 0, 0)  # ask for TIME_SAMPLES; a driver may answer in bytes or ms instead
        if self._winmm.waveOutGetPosition(self._handle, self._ctypes.byref(when), self._ctypes.sizeof(when)):
            return None
        if when.wType == 4:   # TIME_BYTES
            return when.sample / (self.rate * self.block)
        if when.wType == 1:   # TIME_MS
            return when.sample / 1000.0
        return when.sample / self.rate

    def _played_seconds(self):
        """Seconds played since Play — from the sound card, but never far from the real clock."""
        device = self._device_seconds()
        clock = time.perf_counter() - self._clock
        if device is None:
            return clock
        played = max(0.0, device - (self._base or 0.0))
        if abs(played - clock) > 1.0:  # a confused driver counter: trust the clock instead
            return clock
        return played

    def pause(self):
        if self.playing:
            self._paused_at = self._start + self._played_seconds()
            self._release()
        self.playing = False

    def position(self):
        if not self.playing:
            return self._paused_at
        return min(self._start + self._played_seconds(), self._size / self.block / self.rate)

    def finished(self):
        return self._header is None or bool(self._header.dwFlags & 0x1)  # WHDR_DONE

    def set_volume(self, fraction):
        """0.0 … 1.0 for this player only (left and right channel)."""
        level = int(0xFFFF * min(1.0, max(0.0, fraction)))
        self._winmm.waveOutSetVolume(self._handle, self._ctypes.c_uint((level << 16) | level))

    def close(self):
        try:
            self._release()
            self._winmm.waveOutClose(self._handle)
        except Exception:
            pass
        self.playing = False


class StretchedPlayer:
    """A slowed-down copy of the song (pitch kept); every position is reported in SONG seconds."""

    def __init__(self, inner, speed):
        self.inner, self.speed = inner, speed

    @property
    def playing(self):
        return self.inner.playing

    @playing.setter
    def playing(self, value):
        self.inner.playing = value

    def play(self, start_seconds):
        self.inner.play(start_seconds / self.speed)

    def pause(self):
        self.inner.pause()

    def position(self):
        return self.inner.position() * self.speed

    def finished(self):
        return self.inner.finished()

    def set_volume(self, fraction):
        self.inner.set_volume(fraction)

    def close(self):
        self.inner.close()


def stretch_audio(wav_path, speed, rate, channels):
    """Raw 16-bit PCM of ``wav_path`` played at ``speed`` (0.5–1.0) without changing the pitch."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(wav_path), "-filter:a", f"atempo={speed}",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", str(channels), "-ar", str(rate), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not result.stdout:
        raise RuntimeError("ffmpeg could not slow the audio down: " + result.stderr.decode("utf-8", "replace")[-200:])
    return result.stdout


class MciPlayer:
    """Minimal WAV player on the Windows MCI waveaudio device (play, pause, seek, position)."""
    _ids = itertools.count(1)

    def __init__(self, wav_path):
        import ctypes
        self._winmm = ctypes.windll.winmm
        self._ctypes = ctypes
        self.alias = f"t9wave{next(self._ids)}"
        self._send(f'open "{wav_path}" type waveaudio alias {self.alias}')
        self._send(f"set {self.alias} time format milliseconds")
        self.playing = False

    def _send(self, command):
        buffer = self._ctypes.create_unicode_buffer(256)
        error = self._winmm.mciSendStringW(command, buffer, 255, 0)
        if error:
            message = self._ctypes.create_unicode_buffer(256)
            self._winmm.mciGetErrorStringW(error, message, 255)
            raise RuntimeError(f"MCI error {error}: {message.value}")
        return buffer.value

    def play(self, start_seconds):
        self._send(f"play {self.alias} from {max(0, int(start_seconds * 1000))}")
        self.playing = True

    def pause(self):
        if self.playing:
            self._send(f"stop {self.alias}")
        self.playing = False

    def position(self):
        return int(self._send(f"status {self.alias} position") or 0) / 1000.0

    def finished(self):
        return self._send(f"status {self.alias} mode") != "playing"

    def set_volume(self, fraction):
        try:
            self._send(f"setaudio {self.alias} volume to {int(1000 * min(1.0, max(0.0, fraction)))}")
        except RuntimeError:
            pass

    def close(self):
        try:
            self._send(f"close {self.alias}")
        except Exception:
            pass
        self.playing = False


class WaveformView(tk.Frame):
    """Waveform + playhead + one marker lane per timing variant.

    Lanes are dicts ``{"name", "color", "times": {file_line: seconds}}``.
    Callbacks: ``on_tick(seconds)`` while the playhead moves,
    ``on_marker(lane_index, file_line)`` when a marker is clicked,
    ``on_marker_select(file_line, mode)`` when a marker is right-clicked (``"only"``: choose that line,
    the playhead stays) or Ctrl/Shift-clicked (``"toggle"`` / ``"range"``, like in a list).
    ``selected_lines()`` (optional) returns the lines selected in the list; their markers are highlighted.
    Several markers under the mouse open a small menu to pick the right one.
    """

    def __init__(self, parent, theme, on_tick=None, on_marker=None, on_marker_select=None, auto_scroll=True,
                 selected_lines=None, volume=80):
        super().__init__(parent, bg=theme["bg"])
        self.theme = theme
        self.on_tick, self.on_marker, self.on_marker_select = on_tick, on_marker, on_marker_select
        self._hand_scrolled = 0.0  # auto-scroll waits a moment after the user scrolls by hand
        self.peaks, self.duration, self.wav_path, self.player = None, 0.0, None, None
        self.lanes, self.focus_line = [], None
        self.line_texts = {}  # file_line -> lyric text; markers show the first words next to the number
        self.alert_lines = set()  # lines still flagged as errors: red markers in the first (Current) lane
        self.selected_lines = selected_lines or (lambda: set())
        self._lane_top = None  # y of the first lane, and where each marker sits: [(x, file_line), ...] per lane
        self._lane_markers = []
        self.view_start, self.span = 0.0, 20.0
        self.playhead = 0.0
        self.follow = tk.BooleanVar(value=bool(auto_scroll))
        self._results = queue.Queue()
        self._closed = False
        self._format = (44100, 2)
        self._players = {}  # speed -> player; 1.0 is the normal one
        self._stretch_results = queue.Queue()

        bar = tk.Frame(self, bg=theme["bg"])
        bar.pack(fill="x")

        def button(text, command, width=None):
            widget = tk.Button(bar, text=text, command=command, width=width,
                               bg=theme.get("btn_bg", theme["panel"]), fg=theme.get("btn_fg", theme["fg"]))
            widget.pack(side="left", padx=(0, 4))
            return widget

        button("⏪ 5 s", lambda: self.seek(self.playhead - 5))
        self.play_btn = button("▶ Play", self.toggle, 9)
        button("5 s ⏩", lambda: self.seek(self.playhead + 5))
        button("Zoom −", lambda: self.zoom(+1))
        button("Zoom +", lambda: self.zoom(-1))
        self.follow_btn = button("", self.toggle_follow)
        self.follow_btn.configure(font=("Segoe UI", 9, "bold"))
        self._show_follow()
        tk.Label(bar, text="🔊 Volume", bg=theme["bg"], fg=theme["fg"]).pack(side="left", padx=(14, 2))
        self.volume_var = tk.IntVar(value=int(min(100, max(0, volume))))
        self.volume_scale = tk.Scale(bar, from_=0, to=100, orient="horizontal", length=140, showvalue=True,
                                     variable=self.volume_var, command=lambda _v: self._apply_volume(),
                                     bg=theme["bg"], fg=theme["fg"], troughcolor=theme["panel"],
                                     highlightthickness=0, sliderlength=16, width=10, font=("Segoe UI", 8))
        self.volume_scale.pack(side="left")
        tk.Label(bar, text="⏩ Speed", bg=theme["bg"], fg=theme["fg"]).pack(side="left", padx=(14, 2))
        self.speed_var = tk.StringVar(value="1")
        self.speed_buttons = []
        for value, text in (("1", "1×"), ("0.75", "0.75×"), ("0.5", "0.5×")):
            radio = tk.Radiobutton(bar, text=text, value=value, variable=self.speed_var, indicatoron=False,
                                   command=self._speed_chosen, padx=7, pady=1, bg=theme["panel"], fg=theme["fg"],
                                   selectcolor=theme.get("accent", "#355a7a"), activebackground=theme["panel"],
                                   activeforeground=theme["fg"], state="disabled")
            radio.pack(side="left", padx=(0, 2))
            self.speed_buttons.append(radio)
        self.time_var = tk.StringVar(value="Loading waveform…")
        tk.Label(bar, textvariable=self.time_var, bg=theme["bg"], fg=theme["fg"],
                 font=("Consolas", 10)).pack(side="right")

        self.canvas = tk.Canvas(self, height=RULER_HEIGHT + WAVE_HEIGHT + LANE_HEIGHT,
                                bg=theme["panel"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))
        self.scroll = tk.Scrollbar(self, orient="horizontal", command=self._on_scrollbar)
        self.scroll.pack(fill="x")
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-3>", self._on_right_click)

    # ----- loading -------------------------------------------------------
    def load(self, audio_path):
        results = self._results  # the thread gets plain objects only, never the widget (see QuickListen)

        def worker():
            try:
                wav_path, peaks, duration, (pcm, rate, channels) = decode_audio(audio_path, keep_pcm=True)
                try:
                    player = WaveOutPlayer(pcm, rate, channels)
                except Exception:
                    player = None  # falls back to MCI below
                results.put(("ok", (wav_path, peaks, duration, player, (rate, channels))))
            except Exception as exc:
                results.put(("error", exc))
        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_load)

    def _poll_load(self):
        if self._closed:
            return
        try:
            kind, value = self._results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_load)
            return
        if kind == "error":
            self.time_var.set(f"Waveform unavailable: {value}")
            return
        self.wav_path, self.peaks, self.duration, fast_player, self._format = value
        if self._closed:
            if fast_player is not None:
                fast_player.close()
            self._remove_wav()
            return
        try:
            self.player = fast_player or MciPlayer(self.wav_path)
            self._players = {1.0: self.player}
            self._apply_volume()
            if fast_player is not None:  # slowing down needs the in-memory player
                for radio in self.speed_buttons:
                    radio.configure(state="normal")
        except Exception as exc:
            self.player = None
            self.time_var.set(f"Playback unavailable ({exc}); waveform and manual timing still work")
        self.redraw()
        self._tick()

    # ----- public API ----------------------------------------------------
    def set_lanes(self, lanes):
        self.lanes = lanes
        self.canvas.configure(height=RULER_HEIGHT + WAVE_HEIGHT + LANE_HEIGHT * max(1, len(lanes)))
        self.redraw()

    def set_alert_lines(self, lines):
        lines = set(lines)
        if lines != self.alert_lines:
            self.alert_lines = lines
            self.redraw()

    def set_line_texts(self, texts):
        self.line_texts = dict(texts)
        self.redraw()

    @staticmethod
    def marker_label(file_line, text, words=3):
        """“11 get geeked get…”: the line number and the first words, lower case."""
        parts = re.sub(r"\([^)]*\)", " ", text or "").split()
        if not parts:
            return str(file_line)
        return f"{file_line} " + " ".join(parts[:words]).lower() + ("…" if len(parts) > words else "")

    def toggle_follow(self):
        self.follow.set(not self.follow.get())
        self._show_follow()

    def _show_follow(self):
        self.follow_btn.configure(text="🎯 Auto-scroll: ON" if self.follow.get() else "🎯 Auto-scroll: OFF",
                                  fg="#2fbf71" if self.follow.get() else self.theme.get("btn_fg", self.theme["fg"]))

    def set_focus_line(self, file_line):
        self.focus_line = file_line
        self.redraw()

    def show_time(self, seconds, lead=0.25):
        """Scroll so ``seconds`` is visible (a quarter from the left edge)."""
        if not (self.view_start <= seconds <= self.view_start + self.span * 0.85):
            self.view_start = max(0.0, seconds - self.span * lead)
            self.redraw()

    def toggle(self):
        if self.player is None:
            return
        if self.player.playing:
            self.player.pause()
            self.play_btn.configure(text="▶ Play")
        else:
            if self.duration and self.playhead >= self.duration - 0.05:
                self.playhead = 0.0
            self.player.play(self.playhead)
            self.play_btn.configure(text="⏸ Pause")

    def seek(self, seconds):
        self.playhead = min(max(0.0, seconds), self.duration or seconds)
        if self.player is not None and self.player.playing:
            self.player.play(self.playhead)
        self.show_time(self.playhead)
        self._draw_playhead()
        if self.on_tick:
            self.on_tick(self.playhead)

    def _apply_volume(self):
        for player in self._players.values() if self._players else [self.player]:
            if player is not None and hasattr(player, "set_volume"):
                try:
                    player.set_volume(self.volume_var.get() / 100.0)
                except Exception:
                    pass

    # ----- playback speed -----------------------------------------------
    def set_speed(self, speed):
        """1.0, 0.75 or 0.5. The slowed copy is made in the background (about a second)."""
        speed = float(speed)
        self.speed_var.set("1" if speed == 1.0 else str(speed).rstrip("0").rstrip("."))
        if speed in self._players:
            self._switch_player(speed)
            return
        for radio in self.speed_buttons:
            radio.configure(state="disabled")
        self.time_var.set(f"Preparing {speed}× playback…")

        wav_path, (rate, channels), results = self.wav_path, self._format, self._stretch_results

        def worker():
            try:
                results.put((speed, "ok", stretch_audio(wav_path, speed, rate, channels)))
            except Exception as exc:
                results.put((speed, "error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_stretch)

    def _poll_stretch(self):
        if self._closed:
            return
        try:
            speed, kind, value = self._stretch_results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_stretch)
            return
        for radio in self.speed_buttons:
            radio.configure(state="normal")
        if kind == "error":
            self.speed_var.set("1")
            self.time_var.set(f"Slow playback unavailable: {value}")
            return
        rate, channels = self._format
        try:
            self._players[speed] = StretchedPlayer(WaveOutPlayer(value, rate, channels), speed)
        except Exception as exc:
            self.speed_var.set("1")
            self.time_var.set(f"Slow playback unavailable: {exc}")
            return
        self._apply_volume()
        self._switch_player(speed)

    def _speed_chosen(self):
        self.set_speed(float(self.speed_var.get()))

    def _switch_player(self, speed):
        """Continue from the same place in the song with the player for ``speed``."""
        target = self._players[speed]
        if target is self.player:
            return
        position = self.playhead
        was_playing = bool(self.player and self.player.playing)
        if self.player is not None:
            self.player.pause()
        self.player = target
        if was_playing:
            self.player.play(position)
        self._draw_playhead()

    def zoom(self, direction):
        spans = list(ZOOM_SPANS) + ([self.duration] if self.duration > ZOOM_SPANS[-1] else [])
        index = min(range(len(spans)), key=lambda i: abs(spans[i] - self.span))
        index = min(len(spans) - 1, max(0, index + direction))
        center = self.playhead if self.view_start <= self.playhead <= self.view_start + self.span \
            else self.view_start + self.span / 2
        self.span = spans[index]
        self.view_start = max(0.0, center - self.span / 2)
        self.redraw()

    def close(self):
        self._closed = True
        for player in {id(p): p for p in (list(self._players.values()) + [self.player]) if p is not None}.values():
            player.close()
        self._players.clear()
        self.player = None
        self._remove_wav()

    def _remove_wav(self):
        if self.wav_path:
            try:
                os.remove(self.wav_path)
            except OSError:
                pass
            self.wav_path = None

    # ----- drawing -------------------------------------------------------
    def _x(self, seconds, width):
        return LABEL_WIDTH + (seconds - self.view_start) / self.span * (width - LABEL_WIDTH)

    def _t(self, x, width):
        return self.view_start + (x - LABEL_WIDTH) / max(1, width - LABEL_WIDTH) * self.span

    def redraw(self):
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), LABEL_WIDTH + 50)
        # The waveform takes every pixel the lanes do not need, so it grows with the window.
        wave_height = max(60, canvas.winfo_height() - RULER_HEIGHT - LANE_HEIGHT * max(1, len(self.lanes)))
        top, mid = RULER_HEIGHT, RULER_HEIGHT + wave_height / 2
        muted = self.theme.get("muted", "#9aa0a6")
        canvas.create_rectangle(0, 0, LABEL_WIDTH, RULER_HEIGHT + wave_height, fill=self.theme["bg"], width=0)
        canvas.create_text(6, mid, text="Audio", anchor="w", fill=self.theme["fg"])
        if self.peaks is None:
            canvas.create_text(width / 2, mid, text="Loading waveform…", fill=muted)
            return
        self.view_start = min(max(0.0, self.view_start), max(0.0, self.duration - self.span))
        columns = max(1, int(width - LABEL_WIDTH))
        # Snap the view to whole pixels: while scrolling, the picture then moves in whole-pixel steps
        # and every column keeps the same slice of audio, so the waveform does not shimmer.
        pixel_seconds = self.span / columns
        first_column = int(round(self.view_start / pixel_seconds))
        self.view_start = first_column * pixel_seconds
        # Ruler: a tick every 1/2/5/10 s depending on zoom.
        step = next(s for s in (1, 2, 5, 10, 20, 30, 60) if self.span / s <= 16)
        first = int(self.view_start // step + 1) * step
        for tick in range(int(first), int(self.view_start + self.span) + 1, step):
            x = self._x(tick, width)
            canvas.create_line(x, 0, x, RULER_HEIGHT, fill=muted)
            canvas.create_text(x + 3, 2, text=f"{tick // 60}:{tick % 60:02d}", anchor="nw", fill=muted,
                               font=("Segoe UI", 7))
        # Waveform envelope as one polygon (fast even for long songs).
        # Column edges come from one fixed time grid (column k always covers the same audio).
        edges = np.floor((first_column + np.arange(columns + 1)) * pixel_seconds * PEAKS_PER_SECOND).astype(int)
        edges = np.clip(edges, 0, len(self.peaks))
        starts = np.minimum(edges[:-1], max(0, len(self.peaks) - 1))
        values = np.maximum.reduceat(self.peaks, starts) if len(self.peaks) else np.zeros(columns)
        values = np.where(edges[1:] > edges[:-1], values, 0.0).tolist()
        half = wave_height / 2 - 4
        upper = [(LABEL_WIDTH + i, mid - v * half) for i, v in enumerate(values)]
        lower = [(LABEL_WIDTH + i, mid + v * half) for i, v in reversed(list(enumerate(values)))]
        canvas.create_polygon(*[c for point in upper + lower for c in point], fill="#2fbf71", outline="#1f8f52")
        canvas.create_line(LABEL_WIDTH, mid, width, mid, fill="#1f8f52")
        # Lanes with one marker per lyric line.
        for index, lane in enumerate(self.lanes):
            y0 = RULER_HEIGHT + wave_height + index * LANE_HEIGHT
            canvas.create_rectangle(0, y0, width, y0 + LANE_HEIGHT, fill=self.theme["bg"] if index % 2 else self.theme["panel"], width=0)
            canvas.create_text(6, y0 + LANE_HEIGHT / 2, text=lane["name"][:14], anchor="w", fill=lane["color"],
                               font=("Segoe UI", 9, "bold"))
            label_end = -1.0  # right edge of the last drawn number; overlapping numbers are skipped
            visible = [(file_line, seconds) for file_line, seconds
                       in sorted(lane["times"].items(), key=lambda item: (item[1], item[0]))
                       if self.view_start - 1 <= seconds <= self.view_start + self.span]
            if index == 0:
                self._lane_top = y0
                self._lane_markers = []
                selected = self.selected_lines()
            hits = []  # (marker x, right end of its words, line): a click on the words picks the line too
            self._lane_markers.append(hits)
            for position, (file_line, seconds) in enumerate(visible):
                x = self._x(seconds, width)
                next_x = self._x(visible[position + 1][1], width) if position + 1 < len(visible) else width
                focused = file_line == self.focus_line
                color = ALERT_COLOR if index == 0 and file_line in self.alert_lines else lane["color"]
                right = x
                mark = canvas.create_line(x, y0 + 2, x, y0 + LANE_HEIGHT - 2, fill=color, width=14 if focused else 12,
                                          tags=("marker", f"lane{index}", f"line{file_line}"))
                if index == 0 and file_line in selected:  # selected in the list: a blue block behind the marker
                    canvas.tag_lower(canvas.create_rectangle(x - 9, y0 + 1, x + 9, y0 + LANE_HEIGHT - 1,
                                                             fill=SELECTED_COLOR, width=0, tags="marker"), mark)
                if focused or x + 2 > label_end:
                    style = dict(anchor="w", fill="#ffffff" if focused else color,
                                 font=("Segoe UI", 10, "bold" if focused else "normal"),
                                 tags=("marker", f"lane{index}", f"line{file_line}"))
                    label = canvas.create_text(x + 9, y0 + LANE_HEIGHT / 2, style,
                                               text=self.marker_label(file_line, self.line_texts.get(file_line)))
                    if canvas.bbox(label)[2] > next_x - 8 and not focused:
                        canvas.delete(label)  # the words do not fit before the next marker: number only
                        label = canvas.create_text(x + 9, y0 + LANE_HEIGHT / 2, style, text=str(file_line))
                    label_end = canvas.bbox(label)[2] + 2
                    right = label_end
                    if index == 0 and file_line in selected:
                        canvas.tag_lower(canvas.create_rectangle(x + 2, y0 + 2, label_end, y0 + LANE_HEIGHT - 2,
                                                                 fill=SELECTED_COLOR, width=0, tags="marker"), label)
                hits.append((x, right, file_line))
                if focused:  # guide line over the waveform for the focused line
                    canvas.create_line(x, top, x, top + wave_height, fill=lane["color"], dash=(3, 3))
        self._draw_playhead()
        total = max(self.duration, self.span)
        self.scroll.set(self.view_start / total, (self.view_start + self.span) / total)

    def _draw_playhead(self):
        canvas = self.canvas
        canvas.delete("playhead")
        width = max(canvas.winfo_width(), LABEL_WIDTH + 50)
        if self.view_start <= self.playhead <= self.view_start + self.span:
            x = self._x(self.playhead, width)
            canvas.create_line(x, 0, x, canvas.winfo_height(), fill="#ff3b30", width=2, tags="playhead")
        self.time_var.set(f"{self._fmt(self.playhead)} / {self._fmt(self.duration)}"
                          + ("" if self.player is not None else "  (no playback)"))

    @staticmethod
    def _fmt(seconds):
        return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"

    # ----- events --------------------------------------------------------
    def _tick(self):
        if self._closed:
            return
        if self.player is not None and self.player.playing:
            try:
                self.playhead = min(max(0.0, self.player.position()), self.duration or 0.0)
                if self.player.finished() or (self.duration and self.playhead >= self.duration - 0.01):
                    self.player.playing = False
                    self.play_btn.configure(text="▶ Play")
            except Exception:
                self.player.playing = False
            if self.follow.get() and time.monotonic() >= self._hand_scrolled:
                # Smooth: the waveform glides under a playhead kept at a third of the view.
                self.view_start = max(0.0, self.playhead - self.span / 3)
                self.redraw()
            else:
                self._draw_playhead()
            if self.on_tick:
                self.on_tick(self.playhead)
        self.after(40, self._tick)

    def markers_at(self, x, y):
        """(lane index, lines under the mouse, nearest first) — empty when (x, y) is not on a lane."""
        if self._lane_top is None or y < self._lane_top:
            return None, []
        lane = int((y - self._lane_top) // LANE_HEIGHT)
        if not 0 <= lane < len(self._lane_markers):
            return None, []
        near = sorted((abs(mx - x), line) for mx, right, line in self._lane_markers[lane]
                      if mx - HIT_RADIUS <= x <= max(mx + HIT_RADIUS, right))
        return lane, [line for _distance, line in near]

    def _pick(self, event, lines, action):
        """One line: act at once. Several stacked lines: a menu to choose the right one."""
        if len(lines) == 1:
            action(lines[0])
            return
        menu = tk.Menu(self, tearoff=0)
        lane_times = self.lanes[0]["times"] if self.lanes else {}
        for line in sorted(lines, key=lambda l: (lane_times.get(l, 0), l)):
            words = self.marker_label(line, self.line_texts.get(line), words=6)
            when = self._fmt(lane_times[line]) if line in lane_times else ""
            menu.add_command(label=f"{words}    {when}", command=lambda l=line: action(l))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _select_action(self, event, default):
        if event.state & 0x0004:
            return "toggle"  # Ctrl: add/remove this line, like in the list
        if event.state & 0x0001:
            return "range"   # Shift: everything from the last chosen line to this one
        return default

    def _on_click(self, event):
        if self.peaks is None or event.x < LABEL_WIDTH:
            return
        lane, lines = self.markers_at(event.x, event.y)
        if lines:
            mode = self._select_action(event, None)
            if mode and self.on_marker_select:
                self._pick(event, lines, lambda line: self.on_marker_select(line, mode))
            elif not mode and self.on_marker:
                self._pick(event, lines, lambda line: self.on_marker(lane, line))
            return
        self.seek(self._t(event.x, self.canvas.winfo_width()))

    def _on_right_click(self, event):
        _lane, lines = self.markers_at(event.x, event.y)
        if lines and self.on_marker_select:
            mode = self._select_action(event, "only")
            self._pick(event, lines, lambda line: self.on_marker_select(line, mode))
        return "break"

    def _on_wheel(self, event):
        self._hand_scrolled = time.monotonic() + 3.0
        if event.state & 0x0004:  # Ctrl + wheel zooms
            self.zoom(-1 if event.delta > 0 else +1)
        else:
            self.view_start = max(0.0, self.view_start - (event.delta / 120) * self.span * 0.2)
            self.redraw()

    def _on_scrollbar(self, *args):
        self._hand_scrolled = time.monotonic() + 3.0
        total = max(self.duration, self.span)
        if args[0] == "moveto":
            self.view_start = float(args[1]) * total
        elif args[0] == "scroll":
            amount = float(args[1]) * (self.span * 0.9 if args[2] == "pages" else self.span * 0.1)
            self.view_start += amount
        self.redraw()


def active_line(times, seconds):
    """file_line whose start is the latest one at or before ``seconds`` (None before the first)."""
    ordered = sorted((t, line) for line, t in times.items())
    index = bisect.bisect_right([t for t, _line in ordered], seconds) - 1
    return ordered[index][1] if index >= 0 else None
