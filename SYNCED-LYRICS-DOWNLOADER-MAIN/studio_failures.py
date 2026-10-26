"""LRC Failures report, Fix fragment and the fix queue of the Lyrics Sync window.

Moved out of ``open_syncalong_window``. ``app`` gives live access to the main
program's globals (``app.MUSIC_DIR`` is read when used); ``ctx`` holds the
Lyrics Sync window's own widgets, variables and helpers. The window fills
``ctx`` right after building its widgets, before any of these tools can run.
"""
import importlib
import os
import threading
import time
import tkinter as tk
from collections.abc import Mapping
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

import studio_triage
import studio_waveform


class GlobalsView:
    """Attribute access to a live globals() dict."""

    def __init__(self, namespace):
        object.__setattr__(self, "_namespace", namespace)

    def __getattr__(self, name):
        try:
            return self._namespace[name]
        except KeyError:
            raise AttributeError(name) from None


class VisibleTimes(Mapping):
    """A live view of ``times`` without the ``hidden`` lines (the Current lane skips cleared lines)."""

    def __init__(self, times, hidden):
        self._times, self._hidden = times, hidden

    def __getitem__(self, line):
        if line in self._hidden:
            raise KeyError(line)
        return self._times[line]

    def __iter__(self):
        return (line for line in self._times if line not in self._hidden)

    def __len__(self):
        return sum(1 for line in self._times if line not in self._hidden)


def install(app, ctx):
    """Create the tools for one Lyrics Sync window; returns the ones its buttons call."""
    def remember(action, path, detail=""):
        """Add one entry to Edit and Fix history; never let history break a save."""
        try:
            app.studio_resync.record_history(Path(ctx.resync_dir) / "fix_history.json", action, path,
                                             detail, app.BACKUP_ROOT)
        except Exception as exc:
            ctx.slog(f"   (Edit and Fix history not updated: {exc})")

    def fragment_transcriber(tx, audio, audio_end, language, separate_vocals, cached):
        """Whisper on one clip of ``audio``; Demucs vocals are cut once per clip and kept in ``cached``."""
        transcribe_module = importlib.import_module("syncalong.transcribe")

        def transcribe(clip_start, clip_end, prompt):
            options = {"word_timestamps": True, "condition_on_previous_text": False}
            if language:
                options["language"] = language
            if prompt:
                options["initial_prompt"] = prompt
            source, offset = str(audio), 0.0
            # Whisper loops forever when a clip ends after the audio
            # (segment size becomes 0). At the song end, pass only the
            # start so Whisper uses the real end of the decoded audio.
            to_song_end = clip_end >= audio_end - 1.0
            if separate_vocals:
                key = (round(clip_start, 2), round(clip_end, 2))
                if key not in cached:
                    cached[key] = app._separate_fragment_vocals(
                        audio, clip_start, min(clip_end, audio_end), ctx.slog)
                vocals, offset, _cleanup = cached[key]
                source = str(vocals)
                # The Demucs file already is exactly the window; an explicit
                # end could exceed its decoded length by a frame.
                options["clip_timestamps"] = "0"
            else:
                options["clip_timestamps"] = (f"{clip_start:.2f}" if to_song_end
                                              else f"{clip_start:.2f},{clip_end:.2f}")
            words = transcribe_module._extract_words(tx._model.transcribe(source, **options))
            for word in words:
                word.start += offset
                word.end += offset
            return words

        return transcribe

    def cleanup_vocals(cached):
        for vocals in cached.values():
            try:
                vocals[2]()
            except Exception:
                pass
        cached.clear()

    def open_marked_sync():
        if ctx.running["value"]:
            return
        dialog = app.create_toplevel(ctx.win)
        dialog.title("Marked for Lyrics Sync")
        dialog.configure(bg=ctx.theme["bg"])
        dialog.geometry("820x450")
        entries = list(app.studio_resync.read(ctx.queue_path).values())
        box = tk.Listbox(dialog, selectmode="extended", exportselection=False,
                         bg=ctx.theme["panel"], fg=ctx.theme["fg"])
        box.pack(fill="both", expand=True, padx=12, pady=12)
        for item in entries:
            box.insert("end", item["audio"])
        def load():
            ctx.batch_jobs.clear()
            for index in box.curselection():
                item = entries[index]
                if Path(item["audio"]).is_file() and Path(item["lyrics"]).is_file():
                    ctx.batch_jobs.append(dict(audio=item["audio"], lyrics=item["lyrics"],
                        output=app._default_synced_output(item["audio"], item["lyrics"])))
            if ctx.batch_jobs:
                ctx.audio_var.set(ctx.batch_jobs[0]["audio"])
                ctx.lyrics_var.set(ctx.batch_jobs[0]["lyrics"])
                ctx.output_var.set(ctx.batch_jobs[0]["output"])
                ctx._refresh_batch_ui()
                dialog.destroy()
            else:
                messagebox.showinfo("Marked tracks", "Select tracks with existing audio and TXT.", parent=dialog)
        row = tk.Frame(dialog, bg=ctx.theme["bg"])
        row.pack(fill="x", padx=12, pady=12)
        ctx.styled_button(row, "Select all", lambda: box.selection_set(0, "end")).pack(side="left")
        ctx.styled_button(row, "Load selected for sync", load).pack(side="right")


    def open_fragment_fix(lrc_path, first_line, last_line, parent, on_saved=None,
                          auto_retry=False, on_finished=None, title_prefix="", queue=None):
        """Retime only user-selected LRC lines; everything else stays untouched.

        ``on_saved(path, new_text)`` re-checks the saved file in the failure
        report and returns the warnings that are still shown for it.
        ``auto_retry`` starts Retry immediately; ``on_finished(saved)`` runs
        once when the window closes. ``queue`` ({"position", "total", "advance"}) switches on the
        "Save & next song" / "Skip song" buttons: they set ``queue["advance"]`` before closing.
        """
        try:
            audio = app.studio_resync.audio_for_lrc(lrc_path)
            lrc_text = Path(lrc_path).read_text(encoding="utf-8-sig")
        except (ValueError, OSError, UnicodeError) as exc:
            messagebox.showerror("Fix fragment", str(exc), parent=parent)
            if on_finished:
                on_finished(False)
            return
        entries = app._lrc_timed_entries(lrc_text)
        current_times = {entry["file_line"]: entry["time_ms"] / 1000 for entry in entries}
        text_of = {entry["file_line"]: entry["text"] for entry in entries}
        dialog = app.create_toplevel(parent)
        dialog.title(title_prefix + "Fix Fragment Manually — " + Path(lrc_path).name)
        dialog.configure(bg=ctx.theme["bg"])
        dialog.minsize(820, 600)
        dialog.resizable(True, True)
        # Window size, full screen and the pane dividers are remembered between sessions.
        layout_path = Path(ctx.resync_dir) / "fix_fragment_layout.json"
        layout = app.studio_resync.read(layout_path)
        if layout.get("geometry"):
            dialog.geometry(layout["geometry"])
        else:
            app.popup_over_root(dialog, 1240, 940)
        state = {"result": None, "busy": False, "closed": False, "focus": None, "hidden": False, "applied": None}
        undo_stack = []  # snapshots of Current before each change, for “Undo last change”
        cleared = set()  # lines whose time was cleared here: listed, but no marker until T sets a new time
        manual = dict(current_times)
        lanes = []
        lane_colors = ("#4fc3f7", "#ba68c8", "#81c784", "#ff8a65", "#f06292")

        def save_layout():
            try:
                zoomed = dialog.state() == "zoomed"
                data = dict(layout, zoomed=zoomed, tap_mode=bool(tap_mode.get()), console=console["visible"],
                            t_reaction=reaction_seconds(),
                            auto_scroll=bool(waveform.follow.get()), volume=int(waveform.volume_var.get()),
                            sashes=[panes.sash_coord(i)[1] for i in range(len(panes.panes()) - 1)])
                if not zoomed:
                    data["geometry"] = dialog.geometry()
                app.studio_resync.write(layout_path, data)
            except Exception:
                pass  # the layout is a convenience; never block closing

        def finish_dialog(saved):
            if state["closed"]:
                return
            state["closed"] = True
            save_layout()
            waveform.close()
            dialog.destroy()
            if on_finished:
                on_finished(saved)

        def toggle_full_screen(_event=None):
            dialog.state("normal" if dialog.state() == "zoomed" else "zoomed")
            return "break"

        header = tk.Frame(dialog, bg=ctx.theme["bg"])
        header.pack(fill="x", padx=14, pady=(10, 4))
        ctx.styled_button(header, "⛶ Full screen (F11)", toggle_full_screen).pack(side="right", anchor="n", padx=(10, 0))
        tk.Label(header, text=(
            "💡 FASTEST MANUAL SYNC: play the song, RIGHT-CLICK the line number under the waveform, "
            "and press T the moment that line starts. Repeat for the next line, then Save.\n"
            "1. Click a line in the list (it is only chosen, nothing moves).   "
            "2. Put the red playhead where that line really starts: click the waveform, use ⏪/⏩ or play (Space).   "
            "3. Click “Set new timecode to playhead” (T).   "
            "−0.1 s / +0.1 s (, and .) move every selected line — Shift/Ctrl+click to select several.   "
            "Right-click a line number under the waveform to choose it there.  "
            "Double-click a line to listen from it.  “Fix lines automatically” tries several methods; each shows as a coloured lane — "
            "“Use selected fix” puts the chosen one into Current, then fine-tune it and Save.  "
            "Drag the grey bars between the panels to resize them."),
            bg=ctx.theme["bg"], fg=ctx.theme["fg"], justify="left", anchor="w", wraplength=1000,
        ).pack(side="left", fill="x", expand=True)
        bottom = tk.Frame(dialog, bg=ctx.theme["bg"])
        bottom.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
        panes = tk.PanedWindow(dialog, orient="vertical", sashwidth=8, sashrelief="raised",
                               bg=ctx.theme.get("muted", "#555555"), bd=0, opaqueresize=True)
        panes.pack(fill="both", expand=True, padx=14)
        top_pane = tk.Frame(panes, bg=ctx.theme["bg"])
        audio_pane = tk.Frame(panes, bg=ctx.theme["bg"])
        result_pane = tk.Frame(panes, bg=ctx.theme["bg"])
        panes.add(top_pane, minsize=90, stretch="always")
        panes.add(audio_pane, minsize=230, stretch="always")
        panes.add(result_pane, minsize=70, stretch="always")

        def restore_layout():
            if layout.get("zoomed"):
                dialog.state("zoomed")
                dialog.update_idletasks()
            for index, y in enumerate(layout.get("sashes", [])[:len(panes.panes()) - 1]):
                try:
                    panes.sash_place(index, 0, int(y))
                except (tk.TclError, ValueError):
                    pass
            # The lyrics list always opens small: about 20 lines, never more than a third of the
            # window, so the waveform and its buttons get the rest (drag the bar to change it).
            dialog.update_idletasks()
            try:
                linespace = tkfont.Font(font=lines_box.cget("font")).metrics("linespace")
                wanted = 20 * linespace + actions.winfo_reqheight() + 24
                panes.sash_place(0, 0, int(min(wanted, panes.winfo_height() / 3)))
            except (tk.TclError, ValueError):
                pass

        lines_box = tk.Listbox(top_pane, selectmode="extended", exportselection=False, height=8,
                               bg=ctx.theme["panel"], fg=ctx.theme["fg"], font=("Consolas", 10),
                               selectbackground=ctx.theme.get("accent", "#355a7a"))
        lines_box.pack(fill="both", expand=True)

        def format_time(seconds):
            return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"

        def row_text(entry):
            line = entry["file_line"]
            if line in cleared:
                return f"line {line:>3}  {format_time(current_times[line])} → --:--.--  {entry['text']}"
            moved = abs(manual[line] - current_times[line]) >= 0.005
            return (f"line {line:>3}  {format_time(current_times[line])}"
                    + (f" → {format_time(manual[line])}" if moved else " " * 11) + f"  {entry['text']}")

        alerts = {"lines": set()}  # every line inside a scan warning of this LRC (ignored warnings excluded)

        def compute_alerts():
            runs = [dict(run, path=str(Path(lrc_path).resolve()))
                    for run in app._find_lrc_duplicate_timestamp_runs(lrc_text)]
            visible, _hidden = app.studio_resync.split_ignored(runs, ctx.ignored_path)
            alerts["lines"] = {entry["file_line"] for entry in entries for run in visible
                               if run["first_file_line"] <= entry["file_line"] <= run["last_file_line"]}

        def is_red(index):
            """Flagged by the scan and not retimed here yet."""
            line = entries[index]["file_line"]
            return line in alerts["lines"] and abs(manual[line] - current_times[line]) < 0.005

        def paint_row(index):
            red = is_red(index)
            focused = state["focus"] == index
            fg = "#ff5252" if red else ("#ffb000" if focused else ctx.theme["fg"])
            lines_box.itemconfig(index, fg=fg, selectforeground=fg,
                                 background="#4a3b00" if focused else ctx.theme["panel"],
                                 selectbackground="#7a6200" if focused else ctx.theme.get("accent", "#355a7a"))

        def update_error_ui():
            """Recolour the rows, the red markers and the Skip button after any change."""
            state["paint_pending"] = False
            if not dialog.winfo_exists():
                return
            red = [i for i in range(len(entries)) if is_red(i)]
            for index in range(len(entries)):
                paint_row(index)
            waveform.set_alert_lines({entries[i]["file_line"] for i in red})
            passages = sum(1 for k, i in enumerate(red) if k == 0 or red[k - 1] != i - 1)
            skip_btn.configure(state="normal" if red else "disabled",
                               text=(f"⏭ Skip to next error line ({passages} left)" if red
                                     else "✔ No error lines left"))

        def schedule_error_ui():
            if not state.get("paint_pending"):
                state["paint_pending"] = True
                dialog.after_idle(update_error_ui)

        compute_alerts()
        for entry in entries:
            lines_box.insert("end", row_text(entry))
        preselect = [i for i, entry in enumerate(entries) if first_line <= entry["file_line"] <= last_line]
        if preselect:
            # Two lines of context on each side: damage usually starts a bit before the flagged
            # passage, and the good lines around it are safer anchors when retimed with it.
            preselect = list(range(max(0, preselect[0] - 2), min(len(entries), preselect[-1] + 3)))
        for index in preselect:
            lines_box.selection_set(index)
        if preselect:
            lines_box.see(preselect[-1])
        actions = tk.Frame(top_pane, bg=ctx.theme["bg"])
        actions.pack(fill="x", pady=6)

        # Audio pane: manual tools on top, then the waveform (it takes all spare height).
        manual_row = tk.Frame(audio_pane, bg=ctx.theme["bg"])
        manual_row.pack(fill="x", pady=(6, 2))
        choice_row = tk.Frame(audio_pane, bg=ctx.theme["bg"])
        choice_row.pack(fill="x", pady=(2, 2))
        focus_var = tk.StringVar(value="Click a line in the list above to choose it.")
        tk.Label(audio_pane, textvariable=focus_var, bg=ctx.theme["bg"], fg="#ffb000", anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", pady=(0, 2))
        status_var = tk.StringVar(value="")
        tk.Label(audio_pane, textvariable=status_var, bg=ctx.theme["bg"], fg=ctx.theme["fg"], justify="left",
                 anchor="w", wraplength=1180).pack(fill="x", pady=(0, 4))
        # What each lane sings at the playhead, and the next line — above the waveform, centred.
        live_var = tk.StringVar(value="")
        # The Live tap bar: on top, in its own box, Current (what is playing — the line you tapped last);
        # under it the line waiting for T; a small hint at the right.
        live_label = tk.Frame(audio_pane, bg="#1f5130", cursor="hand2")
        live_label.columnconfigure(0, weight=1)
        current_box = tk.Frame(live_label, bg="#173d24", cursor="hand2", highlightthickness=1,
                               highlightbackground="#6fbf8a", highlightcolor="#6fbf8a")
        current_box.grid(row=0, column=0, sticky="ew", padx=(16, 16), pady=(8, 4))
        live_current = tk.Label(current_box, text="", bg="#173d24", fg="#ffcf70", font=("Segoe UI", 15),
                                anchor="center", justify="center", cursor="hand2", padx=10, pady=4)
        live_current.pack(fill="x", expand=True)
        next_box = tk.Frame(live_label, bg="#1f5130", cursor="hand2")
        next_box.grid(row=1, column=0, sticky="ew", padx=(16, 16), pady=(4, 8))
        next_inner = tk.Frame(next_box, bg="#1f5130", cursor="hand2")  # prompt + line, centred together
        next_inner.pack(anchor="center")
        live_prompt = tk.Label(next_inner, text="🎤 Press T when it starts:", bg="#1f5130", fg="#bfe3c9",
                               font=("Segoe UI", 11), cursor="hand2")
        live_prompt.pack(side="left")
        live_line = tk.Label(next_inner, textvariable=live_var, bg="#1f5130", fg="#ffffff", anchor="center",
                             justify="center", font=("Segoe UI", 19, "bold"), cursor="hand2")
        live_line.pack(side="left", padx=(16, 0))
        tk.Label(live_label, text="LIVE TAP — click here\nor Esc to stop", bg="#1f5130", fg="#bfe3c9",
                 font=("Segoe UI", 9), justify="right", cursor="hand2").grid(row=0, column=1, rowspan=2, padx=(4, 16))
        # Long lines wrap instead of being cut off.
        current_box.bind("<Configure>", lambda e: live_current.configure(wraplength=max(200, e.width - 24)))
        next_box.bind("<Configure>", lambda e: live_line.configure(
            wraplength=max(200, e.width - live_prompt.winfo_reqwidth() - 30)))

        def bind_tree(widget):
            widget.bind("<Button-1>", lambda _e: toggle_live(), add="+")
            for child in widget.winfo_children():
                bind_tree(child)

        bind_tree(live_label)
        now_frame = tk.Frame(audio_pane, bg=ctx.theme["bg"], cursor="hand2", highlightthickness=2,
                             highlightbackground=ctx.theme["bg"], highlightcolor=ctx.theme["bg"])
        now_frame.pack(fill="x", pady=(2, 4))
        now_frame.bind("<Button-1>", lambda _e: toggle_live())
        now_labels = []
        waveform = studio_waveform.WaveformView(
            audio_pane, ctx.theme, on_tick=lambda seconds: update_now(seconds),
            on_marker=lambda lane, line: marker_clicked(lane, line),
            on_marker_select=lambda line, mode="only": choose_marker_line(line, mode),
            auto_scroll=layout.get("auto_scroll", True), volume=layout.get("volume", 80),
            selected_lines=lambda: {entries[i]["file_line"] for i in lines_box.curselection() if i < len(entries)})
        waveform.pack(fill="both", expand=True)

        tk.Label(result_pane, text="Console — details of the automatic fixes (times per line, notes)",
                 bg=ctx.theme["bg"], fg=ctx.theme.get("muted", ctx.theme["fg"]), anchor="w").pack(fill="x", pady=(6, 0))
        results = tk.Text(result_pane, height=5, wrap="none", bg=ctx.theme["panel"], fg=ctx.theme["fg"], font=("Consolas", 10))
        results.pack(fill="both", expand=True, pady=(4, 4))
        results.configure(state="disabled")
        choice_var = tk.IntVar(value=0)
        console = {"visible": True}
        tap_mode = tk.BooleanVar(value=bool(layout.get("tap_mode", False)))
        reaction = tk.DoubleVar(value=float(layout.get("t_reaction", 0.0)))  # seconds T is pressed late

        # ----- lanes, "now singing" and manual timing ----------------------
        def rebuild_lanes():
            # "Current" is the working copy: the LRC timing plus every change made here (not saved yet).
            lanes[:] = [{"name": "Current", "color": "#ffb000", "times": VisibleTimes(manual, cleared)}]
            result = state["result"]
            for i, candidate in enumerate(result["candidates"] if result and not state["hidden"] else []):
                times = dict(manual)
                times.update({entry["file_line"]: t for entry, t in zip(result["lines"], candidate["times"])})
                lanes.append({"name": f"#{i + 1} {candidate['label']}", "color": lane_colors[i % len(lane_colors)],
                              "times": times})
            waveform.set_lanes(lanes)
            for row in now_frame.winfo_children():
                row.destroy()
            now_labels.clear()
            choosing = len(lanes) > 1  # automatic fixes shown: one line per lane, no "Next line"
            for position, lane in enumerate(lanes):
                main = lane["name"] == "Current"
                if position:
                    tk.Frame(now_frame, bg=ctx.theme.get("muted", "#555555"), height=1, width=760).pack(
                        anchor="center", pady=3)  # a thin rule between the lanes so they do not run together
                row = tk.Frame(now_frame, bg=ctx.theme["bg"], cursor="hand2")
                row.pack(anchor="center")
                now_label = tk.Label(row, text="", bg=ctx.theme["bg"], fg=lane["color"], cursor="hand2",
                                     font=("Segoe UI", 14 if main else 11, "bold"))
                now_label.pack(side="left")
                next_label = tk.Label(row, text="", bg=ctx.theme["bg"], fg="#7fb3ff" if main else lane["color"],
                                      font=("Segoe UI", 12 if main else 10, "normal"), cursor="hand2")
                for widget in (row, now_label, next_label):  # click the caption: Live tap on / off
                    widget.bind("<Button-1>", lambda _e: toggle_live())
                if not choosing:
                    next_label.pack(side="left", padx=(18, 0))
                now_labels.append((now_label, next_label))
            update_now(waveform.playhead)

        def update_now(seconds):
            last = state.get("last_now", seconds)
            state["last_now"] = seconds
            if state.get("live") is not None and (seconds < last - 0.3 or seconds > last + 1.5) and not cleared:
                state["live"] = live_start_index(seconds)  # you jumped: T now times the next line from here
                show_live()
            if state.get("live") is not None:
                playing = studio_waveform.active_line(VisibleTimes(manual, cleared), seconds)
                live_current.configure(text=f"Current:  {playing}  {text_of[playing]}"
                                       if playing is not None else "Current:  —")  # the whole line: where it ends
            for lane, (now_label, next_label) in zip(lanes, now_labels):
                line = studio_waveform.active_line(lane["times"], seconds)
                later = sorted((t, l) for l, t in lane["times"].items() if t > seconds + 1e-6)
                upcoming = later[0][1] if later else None
                now_label.configure(text=f"{lane['name']}:  " + (f"{line}  {text_of[line]}" if line is not None else "—"))
                next_label.configure(text=f"Next line:  {upcoming}  {text_of[upcoming]}" if upcoming is not None else "")

        def refresh_row(index):
            selected = lines_box.selection_includes(index)
            lines_box.delete(index)
            lines_box.insert(index, row_text(entries[index]))
            if selected:
                lines_box.selection_set(index)
            paint_row(index)
            schedule_error_ui()

        def set_focus(index, seek=False, scroll=True):
            if not 0 <= index < len(entries):
                return
            previous = state["focus"]
            state["focus"] = index
            if state.get("live") is not None:
                state["live"] = index  # in Live tap the chosen line is the one T times next
                show_live()
            if previous is not None and previous != index and previous < len(entries):
                paint_row(previous)
            paint_row(index)
            lines_box.see(index)
            line = entries[index]["file_line"]
            focus_var.set(selection_text(index))
            waveform.set_focus_line(line)
            if scroll:
                waveform.show_time(manual[line])  # scroll the view to it; the playhead does not move
            if seek:
                waveform.seek(max(0.0, manual[line] - 0.5))

        def marker_clicked(lane_index, line):
            index = next((i for i, entry in enumerate(entries) if entry["file_line"] == line), None)
            if index is None:
                return
            set_focus(index, seek=False)
            waveform.seek(lanes[lane_index]["times"][line])

        def remember_undo(description):
            undo_stack.append((dict(manual), state["hidden"], state["applied"], description, set(cleared)))
            del undo_stack[:-200]

        def undo_last(_event=None):
            if not undo_stack:
                status_var.set("Nothing to undo.")
                return "break"
            times, hidden, applied, description, was_cleared = undo_stack.pop()
            manual.clear()
            manual.update(times)
            cleared.clear()
            cleared.update(was_cleared)
            if (hidden, applied) != (state["hidden"], state["applied"]):
                state["hidden"], state["applied"] = hidden, applied
                rebuild_lanes()
                rebuild_choices()
            for index in range(len(entries)):
                refresh_row(index)
            waveform.redraw()
            if state["focus"] is not None:
                focus_var.set(selection_text(state["focus"]))
            status_var.set(f"Undone: {description}." + (f" {len(undo_stack)} more step(s) can be undone."
                                                        if undo_stack else ""))
            return "break"

        def skip_to_next_error():
            """Choose the next passage of red lines after the chosen line (wrapping round) and play from it."""
            red = [i for i in range(len(entries)) if is_red(i)]
            if not red:
                update_error_ui()
                return "break"
            starts = [i for k, i in enumerate(red) if k == 0 or red[k - 1] != i - 1]
            after = state["focus"] if state["focus"] is not None else -1
            while after in red and after + 1 in red:
                after += 1  # the chosen line is inside a red passage: skip the rest of that passage
            start = next((i for i in starts if i > after), starts[0])  # none left below: back to the first
            end = start
            while end + 1 in red:
                end += 1
            lines_box.selection_clear(0, "end")
            lines_box.selection_set(start, end)
            set_focus(start, scroll=False)
            line = entries[start]["file_line"]
            waveform.seek(max(0.0, manual[line] - 1.0))  # one second before the passage, to hear it come in
            status_var.set(f"Error passage: lines {line}–{entries[end]['file_line']} "
                           f"({end - start + 1} line(s)). Right-click a number below and press T, or use "
                           "Fix lines automatically on the selected lines.")
            dialog.focus_set()
            return "break"

        def choose_marker_line(line, mode="only"):
            """A line number under the waveform: right-click chooses it (the playhead stays);
            Ctrl+click adds/removes it, Shift+click selects from the last chosen line to it — like the list."""
            index = next((i for i, entry in enumerate(entries) if entry["file_line"] == line), None)
            if index is None:
                return
            if mode == "toggle":
                if lines_box.selection_includes(index):
                    lines_box.selection_clear(index)
                else:
                    lines_box.selection_set(index)
                state["anchor"] = index
            elif mode == "range":
                anchor = state.get("anchor", state["focus"] if state["focus"] is not None else index)
                lines_box.selection_clear(0, "end")
                lines_box.selection_set(min(anchor, index), max(anchor, index))
            else:
                lines_box.selection_clear(0, "end")
                lines_box.selection_set(index)  # only this line: −0.1 / +0.1 s move just it
                state["anchor"] = index
            set_focus(index, scroll=False)
            count = len(lines_box.curselection())
            status_var.set(f"{count} line(s) selected under the waveform — −0.1 s / +0.1 s move all of them."
                           if mode != "only" else
                           f"Line {line} chosen — press T (Set new timecode to playhead) at the moment it starts.")
            dialog.focus_set()

        def cleared_indices():
            return [i for i, entry in enumerate(entries) if entry["file_line"] in cleared]

        def live_start_index(seconds):
            """Where Live tap starts: the first line whose time was cleared (list order) — those are
            the lines waiting for T. With none cleared: the line after the one Current shows now."""
            waiting = cleared_indices()
            if waiting:
                return waiting[0]
            active = studio_waveform.active_line(VisibleTimes(manual, cleared), seconds)
            if active is None:
                return 0
            return next((i + 1 for i, entry in enumerate(entries) if entry["file_line"] == active), 0)

        def live_next_after(index):
            """The line the next T times: the next cleared line in list order (wrapping round),
            or simply the following line once no line is waiting any more."""
            waiting = cleared_indices()
            if waiting:
                return next((i for i in waiting if i > index), waiting[0])
            return index + 1

        def show_live():
            index = state.get("live")
            if index is None:  # back to the normal caption (Current / Next line)
                live_label.pack_forget()
                if not now_frame.winfo_manager():
                    now_frame.pack(fill="x", pady=(2, 4), before=waveform)
                return
            # Live tap shows ONLY the line waiting for T — not what the old timing plays at the moment.
            if index < len(entries):
                entry = entries[index]
                live_prompt.configure(text="🎤 Press T when it starts:")
                live_var.set(f"line {entry['file_line']}   {entry['text'][:80]}")
            else:
                live_prompt.configure(text="🎤 Live tap:")
                live_var.set("end of the lyrics")
            update_now(waveform.playhead)  # fills "Current" on the right of the bar
            if now_frame.winfo_manager():
                now_frame.pack_forget()
            if not live_label.winfo_manager():
                live_label.pack(fill="x", pady=(2, 4), before=waveform)

        def toggle_live(_event=None, on=None):
            on = (state.get("live") is None) if on is None else on
            state["live"] = live_start_index(waveform.playhead) if on else None
            if on and state["live"] < len(entries):
                lines_box.selection_clear(0, "end")
                lines_box.selection_set(state["live"])
                set_focus(state["live"], scroll=False)
            show_live()
            status_var.set("Live tap on: play the song and press T each time the next line starts — no clicking. "
                           "Rewind or click a line to start somewhere else." if on else "Live tap off.")
            dialog.focus_set()
            return "break"

        def live_tap():
            index = state["live"]
            if index >= len(entries):
                status_var.set("Live tap: that was the last line.")
                return
            line = entries[index]["file_line"]
            was_waiting = bool(cleared)
            when = tap_time()
            set_manual(line, when)
            following = live_next_after(index)
            update_now(waveform.playhead)  # the tapped line is Current now
            lines_box.selection_clear(0, "end")
            if following < len(entries):
                lines_box.selection_set(following)  # the line T times next is chosen in the list right away
                set_focus(following, scroll=False)  # (this also points Live tap at it)
            else:
                state["live"] = len(entries)
            state["last_now"] = waveform.playhead
            show_live()
            done = " All cleared lines have a time now." if was_waiting and not cleared else ""
            status_var.set(f"Line {line} now starts at {format_time(when)} (not saved yet).{done}")

        def reaction_seconds():
            try:
                return min(1.0, max(0.0, float(reaction.get())))
            except (tk.TclError, ValueError):
                return 0.0

        def tap_time():
            """Where T puts a line: the playhead minus your reaction time (people press T a little late)."""
            return max(0.0, waveform.playhead - reaction_seconds())

        def set_manual(line, seconds):
            remember_undo(f"line {line} set to {format_time(seconds)}")
            manual[line] = max(0.0, seconds)
            cleared.discard(line)  # a cleared line gets its marker back with the new time
            index = next(i for i, entry in enumerate(entries) if entry["file_line"] == line)
            refresh_row(index)
            waveform.redraw()
            focus_var.set(selection_text(index))

        def set_to_playhead(_event=None):
            if state.get("live") is not None:
                live_tap()
                return "break"
            if state["focus"] is None:
                status_var.set("Click a line first, put the playhead where it starts, then Set new timecode.")
                return "break"
            line = entries[state["focus"]]["file_line"]
            when = tap_time()
            set_manual(line, when)
            status_var.set(f"Line {line} now starts at {format_time(when)} (not saved yet)."
                           + (f" [T −{reaction_seconds():.2f} s reaction compensation]" if reaction_seconds() else ""))
            if tap_mode.get() and state["focus"] + 1 < len(entries):
                set_focus(state["focus"] + 1)  # tap mode: the next press times the next line
            return "break"

        def nudge(delta):
            """Move every selected line (or the chosen one) by ``delta`` seconds."""
            indices = list(lines_box.curselection())
            if not indices and state["focus"] is not None:
                indices = [state["focus"]]
            if not indices:
                return "break"
            remember_undo(f"{len(indices)} line(s) moved by {delta:+.1f} s")
            for index in indices:
                line = entries[index]["file_line"]
                if line in cleared:
                    continue  # no time to move: press T to give it one
                manual[line] = max(0.0, manual[line] + delta)
                refresh_row(index)
            waveform.redraw()
            if state["focus"] is not None:
                line = entries[state["focus"]]["file_line"]
                focus_var.set(selection_text(state["focus"]))
            status_var.set(f"Moved {len(indices)} line(s) by {delta:+.1f} s (not saved yet).")
            return "break"

        def selection_text(index):
            line = entries[index]["file_line"]
            count = len(lines_box.curselection())
            when = "(no time — press T)" if line in cleared else f"at {format_time(manual[line])}"
            return (f"Chosen: line {line} {when} — {entries[index]['text'][:70]}"
                    + (f"     •  {count} lines selected: −0.1 s / +0.1 s move all of them" if count > 1 else ""))

        def copy_candidate_to_manual():
            result = state["result"]
            if not result or not result["candidates"]:
                status_var.set("Click “Fix lines automatically” first, then pick the method that matches the song.")
                return
            chosen = choice_var.get()
            candidate = result["candidates"][chosen]
            remember_undo(f"Use selected fix #{chosen + 1} {candidate['label']}")
            for entry, t in zip(result["lines"], candidate["times"]):
                manual[entry["file_line"]] = t
                cleared.discard(entry["file_line"])
            for index in range(len(entries)):
                refresh_row(index)
            state["hidden"] = True  # the fix is now Current: the coloured lanes are not needed any more
            state["applied"] = chosen
            rebuild_lanes()
            rebuild_choices()
            status_var.set(f"#{chosen + 1} {candidate['label']} is now Current (not saved yet). Fine-tune lines "
                           "if needed (Set new timecode, −0.1 / +0.1 s), then Save.")

        def show_all_fixes():
            state["hidden"] = False
            rebuild_lanes()
            rebuild_choices()

        def hide_all_fixes():
            state["hidden"] = True
            state["applied"] = None
            rebuild_lanes()
            rebuild_choices()
            status_var.set("Automatic fixes hidden — none of them fit. Set the lines manually, then Save.")

        def rebuild_choices():
            for child in choice_row.winfo_children():
                child.destroy()
            result = state["result"]
            candidates = result["candidates"] if result else []
            if not candidates:
                return
            if state["hidden"]:
                applied = state["applied"]
                text = (f"✔ #{applied + 1} {candidates[applied]['label']} applied to Current."
                        if applied is not None else "Automatic fixes hidden.")
                tk.Label(choice_row, text=text, bg=ctx.theme["bg"], fg=ctx.theme["fg"],
                         font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 10))
                ctx.styled_button(choice_row, "Show automatic fixes again", show_all_fixes).pack(side="left")
                return
            tk.Label(choice_row, text="Automatic fixes — pick the colour that matches the song:",
                     bg=ctx.theme["bg"], fg=ctx.theme["fg"], font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 10))
            for i, candidate in enumerate(candidates):
                tk.Radiobutton(choice_row, text=f"#{i + 1} {candidate['label']}", variable=choice_var, value=i,
                               bg=ctx.theme["bg"], fg=lane_colors[i % len(lane_colors)], selectcolor=ctx.theme["panel"],
                               activebackground=ctx.theme["bg"], font=("Segoe UI", 11, "bold")
                               ).pack(side="left", padx=(0, 14))
            dialog.after_idle(lambda: state.get("route_space") and state["route_space"](choice_row))
            use_btn = ctx.styled_button(choice_row, "✔ Use selected fix", copy_candidate_to_manual)
            use_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4)
            use_btn.pack(side="left", padx=(4, 0))
            ctx.styled_button(choice_row, "✖ Hide all fixes (none fit)", hide_all_fixes).pack(side="left", padx=(8, 0))

        def reset_manual():
            remember_undo("Undo all changes")
            manual.clear()
            manual.update(current_times)
            cleared.clear()
            for index in range(len(entries)):
                refresh_row(index)
            waveform.redraw()
            schedule_error_ui()
            status_var.set("All changes undone — Current is the timing saved in the LRC again.")

        def on_line_click(event):
            # One click only chooses the line: the waveform view and the playhead stay where they are.
            # A double click jumps there (and plays from the line).
            set_focus(lines_box.nearest(event.y), scroll=False)

        def on_line_double_click(event):
            index = lines_box.nearest(event.y)
            set_focus(index)
            waveform.seek(max(0.0, manual[entries[index]["file_line"]] - 0.5))  # listen from this line

        lines_box.bind("<ButtonRelease-1>", on_line_click)
        lines_box.bind("<<ListboxSelect>>", lambda _e: waveform.redraw())  # highlight selected markers
        dialog.bind("<Escape>", lambda _e: toggle_live(on=False) if state.get("live") is not None else None)
        lines_box.bind("<Double-Button-1>", on_line_double_click)
        for widget in (dialog, lines_box):
            widget.bind("<space>", lambda _e: (waveform.toggle(), "break")[1])
            widget.bind("<KeyPress-t>", set_to_playhead)
            widget.bind("<KeyPress-T>", set_to_playhead)
            widget.bind("<comma>", lambda _e: nudge(-0.1))
            widget.bind("<period>", lambda _e: nudge(+0.1))
            widget.bind("<Left>", lambda _e: (waveform.seek(waveform.playhead - 2), "break")[1])
            widget.bind("<Right>", lambda _e: (waveform.seek(waveform.playhead + 2), "break")[1])

        def show_result(result):
            state["result"] = result
            state["hidden"] = False
            state["applied"] = None
            candidates = result["candidates"]
            header = f"{'line':>5}  {'current':>8}  " + "  ".join(f"{'#' + str(i + 1):>8}" for i in range(len(candidates)))
            rows = [header, "-" * len(header)]
            for k, entry in enumerate(result["lines"]):
                rows.append(f"{entry['file_line']:>5}  {format_time(entry['time_ms'] / 1000):>8}  "
                            + "  ".join(f"{format_time(c['times'][k]):>8}" for c in candidates)
                            + f"   {entry['text'][:48]}")
            rows.append("")
            rows.extend(f"#{i + 1} {c['label']}: {c['note']}" for i, c in enumerate(candidates))
            rows.extend(result["notes"])
            results.configure(state="normal")
            results.delete("1.0", "end")
            results.insert("1.0", "\n".join(rows))
            results.configure(state="disabled")
            choice_var.set(0)
            rebuild_choices()
            status_var.set(
                f"Search window {format_time(result['region'][0])}–{format_time(result['region'][1])}. "
                + (f"{len(candidates)} automatic fix(es) — play the song and compare each coloured lane with "
                   "the waveform, pick one and click Use selected fix (it becomes Current). "
                   "If none fits, click Hide all fixes."
                   if candidates else "No method found a usable timing; set the lines manually or keep the LRC."))
            rebuild_lanes()
            if result["lines"]:
                waveform.show_time(result["lines"][0]["time_ms"] / 1000)

        def set_busy(value):
            state["busy"] = value
            retry_btn.configure(state="disabled" if value else "normal")
            save_btn.configure(state="disabled" if value else "normal")

        def explain_auto_fix():
            """Say what Fix lines automatically does; returns False when the user cancels."""
            if layout.get("skip_fix_explanation"):
                return True
            box = app.create_toplevel(dialog)
            box.title("Fix lines automatically")
            box.configure(bg=ctx.theme["bg"])
            box.transient(dialog)
            tk.Label(box, text=(
                "The program will now try several methods to retime the selected lines:\n\n"
                "  •  Whisper with the lyrics prompt ON\n"
                "  •  Whisper with the lyrics prompt OFF\n"
                "  •  Copy of an identical earlier or later part of the song (when there is one)\n\n"
                "Each method gets its own colour: a lane of markers under the waveform and the line it would show "
                "while the song plays. No method is always right — play the song, compare the coloured markers with "
                "the waveform and pick the colour that matches the real song best.\n\n"
                "Then click “Use selected fix”: it becomes Current, which you can fine-tune and Save. If none fits, “Hide all fixes”. Nothing is saved before you click Save."),
                bg=ctx.theme["bg"], fg=ctx.theme["fg"], justify="left", wraplength=560).pack(padx=18, pady=(16, 8))
            dont_show = tk.BooleanVar(value=False)
            tk.Checkbutton(box, text="Don't show this again", variable=dont_show, bg=ctx.theme["bg"], fg=ctx.theme["fg"],
                           selectcolor=ctx.theme["panel"], activebackground=ctx.theme["bg"]).pack(anchor="w", padx=18)
            answer = {"go": False}
            row = tk.Frame(box, bg=ctx.theme["bg"])
            row.pack(fill="x", padx=18, pady=(8, 16))

            def close(go):
                answer["go"] = go
                box.destroy()

            start = ctx.styled_button(row, "🔧 Start", lambda: close(True))
            start.configure(font=("Segoe UI", 10, "bold"))
            start.pack(side="left")
            ctx.styled_button(row, "Cancel", lambda: close(False), 10).pack(side="right")
            box.protocol("WM_DELETE_WINDOW", lambda: close(False))
            app.popup_over_root(box, 620, 400)
            box.grab_set()
            dialog.wait_window(box)
            if dont_show.get():
                layout["skip_fix_explanation"] = True
                save_layout()
            return answer["go"]

        def retry():
            if state["busy"] or ctx.running["value"] or ctx.setup_running["value"]:
                messagebox.showinfo("Fix fragment", "Finish the current operation first.", parent=dialog)
                return
            selection = lines_box.curselection()
            if not selection:
                messagebox.showinfo("Fix fragment", "Select the lines with wrong timing first.", parent=dialog)
                return
            if list(selection) != list(range(selection[0], selection[-1] + 1)):
                messagebox.showinfo("Fix fragment", "Select one continuous block of lines.", parent=dialog)
                return
            if not explain_auto_fix():
                return
            threshold, model_name, required_vram, language = ctx._common_sync_settings()
            if not ctx._preflight_sync(model_name, required_vram):
                return
            first_time = manual.get(entries[selection[0]]["file_line"], current_times[entries[selection[0]]["file_line"]])
            waveform.seek(max(0.0, first_time - 2.0))  # ready to listen from just before the lines being fixed
            separate_vocals = bool(ctx.vocals_var.get())
            first = entries[selection[0]]["file_line"]
            last = entries[selection[-1]]["file_line"]
            try:
                from mutagen import File as MutagenFile
                audio_end = float(MutagenFile(str(audio)).info.length)
            except Exception:
                audio_end = entries[-1]["time_ms"] / 1000 + 30.0
            set_busy(True)
            ctx.set_running(True)
            status_var.set("Loading Whisper and retrying the selected lines… (see the log)")
            ctx.slog(f"Fix fragment: {Path(lrc_path).name}, LRC lines {first}–{last}")

            def worker():
                result, error, cached = None, "", {}
                try:
                    app._ensure_windowless_standard_streams()
                    tx = ctx.get_transcriber(model_name)
                    transcribe = fragment_transcriber(tx, audio, audio_end, language, separate_vocals, cached)
                    result = app._retry_lrc_line_range(lrc_text, first, last, audio_end, threshold,
                                                   transcribe, progress=ctx.slog)
                    for candidate in result["candidates"]:
                        ctx.slog(f"   Candidate {candidate['label']}: {candidate['note']}")
                    for note in result["notes"]:
                        ctx.slog("   " + note)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    ctx.slog("✗ Fix fragment failed: " + error)
                finally:
                    cleanup_vocals(cached)
                    app.ui_call(finish, result, error)

            def finish(result, error):
                ctx.set_running(False)
                if not dialog.winfo_exists():
                    return
                set_busy(False)
                if error:
                    status_var.set("Retry failed: " + error)
                    return
                show_result(result)

            threading.Thread(target=worker, daemon=True).start()

        def save_times(changes, label):
            """Write ``changes`` ({file_line: seconds}) into the LRC; back up, re-embed, re-check."""
            try:
                if Path(lrc_path).read_text(encoding="utf-8-sig") != lrc_text:
                    raise ValueError("The LRC changed on disk after this window was opened. Reopen Fix fragment.")
                new_text = app._replace_lrc_line_times(lrc_text, changes)
                embedded_before = app._synced_lyrics_sources(str(audio), force=True)[0]
                backup = app.studio_resync.save_fragment_repair(
                    lrc_path, new_text, app.BACKUP_ROOT)
                embed_note = ""
                if embedded_before:
                    app._embed_synced_lrc_as_mp3tag_unsyncedlyrics(str(audio), new_text,
                                                               ctx._common_sync_settings()[3] or "en")
                    embed_note = "\nEmbedded MP3 lyrics were updated too."
            except Exception as exc:
                messagebox.showerror("Fix fragment", str(exc), parent=dialog)
                return
            app._SYNCED_LYRICS_CACHE.clear()
            ctx.slog(f"✓ Fix fragment saved ({label}): {lrc_path}\n   Backup: {backup}")
            remember(f"Fix fragment — {label}", lrc_path, f"{len(changes)} line(s) retimed")
            if on_saved:
                remaining = on_saved(lrc_path, new_text)
            else:
                remaining, _hidden = app.studio_resync.split_ignored(
                    [dict(run, path=str(Path(lrc_path).resolve()))
                     for run in app._find_lrc_duplicate_timestamp_runs(new_text)], ctx.ignored_path)
            saved_note = (f"Saved {len(changes)} retimed line(s) using {label}.\n"
                          f"Backup: {backup}{embed_note}")
            if remaining:
                ctx.slog(f"   ⚠ {len(remaining)} scan warning(s) still detected in this LRC.")
                messagebox.showwarning(
                    "Fix fragment",
                    saved_note + f"\n\nThe song still has {len(remaining)} suspicious passage(s), so it stays "
                    "in Failures (its rows were updated). Try Fix fragment again on the remaining lines "
                    "or ignore the warning if it sounds right.",
                    parent=dialog)
            else:
                ctx.slog("   ✓ No scan warnings left — removed from Failures.")
                if not (queue and queue.get("advance")):
                    messagebox.showinfo("Song fixed",
                                        "Song successfully fixed and removed from Failures.\n\n" + saved_note,
                                        parent=dialog)
            finish_dialog(True)

        def changed_lines():
            return {line: t for line, t in manual.items() if abs(t - current_times[line]) >= 0.005}

        def save_manual():
            if state["busy"]:
                return
            if cleared:
                waiting = sorted(cleared)
                if not messagebox.askyesno(
                        "Lines without a time",
                        f"{len(waiting)} line(s) still have no time (line {', '.join(map(str, waiting[:10]))}"
                        + ("…" if len(waiting) > 10 else "") + ").\n\nThey keep their previous time. Save anyway?",
                        parent=dialog):
                    return
            changes = changed_lines()
            if not changes and state.get("files_changed"):
                if not (queue and queue.get("advance")):
                    messagebox.showinfo("Save", "Your removed / edited lines are saved (LRC, TXT and the MP3's "
                                        "embedded lyrics). No timing was changed.", parent=dialog)
                finish_dialog(True)
                return
            if not changes:
                messagebox.showinfo("Save", "Nothing has changed yet. Pick an automatic fix and click "
                                    "Use selected fix, or click a line and press T at the right moment "
                                    "while the song plays.", parent=dialog)
                return
            order = [entry["file_line"] for entry in entries]
            out_of_order = [b for a, b in zip(order, order[1:]) if manual[b] < manual[a] - 0.005]
            if out_of_order and not messagebox.askyesno(
                    "Manual timing",
                    f"{len(out_of_order)} line(s) now start before the line above them "
                    f"(first: line {out_of_order[0]}). Save anyway?", parent=dialog):
                return
            label = "manual timing"
            if state["applied"] is not None and state["result"]:
                label = state["result"]["candidates"][state["applied"]]["label"] + " + manual timing"
            save_times(changes, label)

        retry_btn = ctx.styled_button(actions, "🔧 Fix lines automatically", retry)
        retry_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4)
        retry_btn.pack(side="left")
        ctx.styled_button(actions, "Open in system player",
                          lambda: app.play_song(str(audio), dialog)).pack(side="left", padx=(8, 0))
        skip_btn = ctx.styled_button(manual_row, "⏭ Skip to next error line", skip_to_next_error)
        skip_btn.configure(font=("Segoe UI", 10, "bold"))
        skip_btn.pack(side="left", padx=(0, 10))
        set_btn = ctx.styled_button(manual_row, "⏱ Set new timecode to playhead [T]", set_to_playhead)
        set_btn.configure(font=("Segoe UI", 10, "bold"))
        set_btn.pack(side="left", padx=(0, 6))
        save_manual_btn = ctx.styled_button(manual_row, "💾 Save manual timing", save_manual)
        save_manual_btn.configure(font=("Segoe UI", 10, "bold"))
        save_manual_btn.pack(side="left", padx=(0, 14))
        def move_to_playhead():
            """Move the selected line(s) so the first one starts at the playhead; the others keep their
            distance to it (a cleared line in the selection just gets the playhead time when it is first)."""
            indices = chosen_indices()
            if not indices:
                status_var.set("Select the line(s) to move first.")
                return "break"
            timed = [i for i in indices if entries[i]["file_line"] not in cleared]
            playhead = waveform.playhead
            first_line = entries[indices[0]]["file_line"]
            remember_undo(f"{len(indices)} line(s) moved to the playhead")
            if not timed or indices[0] not in timed:
                manual[first_line] = playhead  # the first line had no time: it simply starts here
                cleared.discard(first_line)
            if timed:
                delta = playhead - manual[entries[timed[0]]["file_line"]] if indices[0] in timed else 0.0
                for i in timed:
                    line = entries[i]["file_line"]
                    manual[line] = max(0.0, manual[line] + delta)
            for i in indices:
                refresh_row(i)
            waveform.redraw()
            update_now(playhead)
            if state["focus"] is not None:
                focus_var.set(selection_text(state["focus"]))
            status_var.set(f"Line {first_line}" + (f" and {len(indices) - 1} more" if len(indices) > 1 else "")
                           + f" moved: it now starts at {format_time(playhead)} (not saved yet).")
            return "break"

        for text, command in (("−0.1 s [,]", lambda: nudge(-0.1)), ("+0.1 s [.]", lambda: nudge(+0.1)),
                              ("⇤ Move selected to playhead", move_to_playhead),
                              ("↶ Undo last change [Ctrl+Z]", undo_last), ("↺ Undo all changes", reset_manual)):
            ctx.styled_button(manual_row, text, command).pack(side="left", padx=(0, 6))
        tk.Checkbutton(manual_row, text="Tap mode (after Set, go to the next line)", variable=tap_mode,
                       bg=ctx.theme["bg"], fg=ctx.theme["fg"], selectcolor=ctx.theme["panel"],
                       activebackground=ctx.theme["bg"]).pack(side="left", padx=(6, 0))
        reaction_label = tk.Label(manual_row, text="T reaction −", bg=ctx.theme["bg"], fg=ctx.theme["fg"])
        reaction_label.pack(side="left", padx=(14, 2))
        reaction_box = tk.Spinbox(manual_row, from_=0.0, to=0.6, increment=0.05, width=5, format="%.2f",
                                  textvariable=reaction, bg=ctx.theme["panel"], fg=ctx.theme["fg"],
                                  buttonbackground=ctx.theme["panel"], insertbackground=ctx.theme["fg"])
        reaction_box.pack(side="left")
        tk.Label(manual_row, text="s", bg=ctx.theme["bg"], fg=ctx.theme["fg"]).pack(side="left", padx=(2, 0))
        for widget in (reaction_label, reaction_box):
            create_hint = getattr(app, "create_tooltip", None)
            if create_hint:
                create_hint(widget, "People press T a little after they hear a line start (about 0.15–0.25 s).\n"
                                    "Set how much earlier T should place the line: 0.15 usually fits.")
        dialog.bind("<F11>", toggle_full_screen)
        for key in ("<minus>", "<KP_Subtract>"):  # zoom out / in from the keyboard, no Ctrl needed
            dialog.bind(key, lambda _e: (waveform.zoom(+1), "break")[1])
        for key in ("<equal>", "<plus>", "<KP_Add>"):
            dialog.bind(key, lambda _e: (waveform.zoom(-1), "break")[1])
        for widget in (dialog, lines_box):
            widget.bind("<Control-z>", undo_last)
            widget.bind("<Control-Z>", undo_last)
        waveform.set_line_texts(text_of)
        def wrong_lyrics():
            if state["busy"]:
                messagebox.showinfo("Wrong lyrics", "Wait until Retry finishes.", parent=dialog)
                return
            if not messagebox.askyesno(
                    "Wrong lyrics",
                    "The lyrics text does not match this song?\n\n"
                    "The LRC and embedded MP3 lyrics will be backed up and deleted, the song will be "
                    "marked for Lyrics Sync, and View/Change Lyrics opens so you can find the correct text.",
                    parent=dialog):
                return
            txt = app._find_plain_txt_for_audio(str(audio)) or app.missing_txt_path(str(audio))
            try:
                backup, removed, embedded = app.studio_resync.clear_sync(
                    audio, txt, ctx.queue_path, app.BACKUP_ROOT,
                    app.remove_embedded_lyrics)
            except Exception as exc:
                messagebox.showerror("Wrong lyrics", f"Could not remove the old sync: {exc}", parent=dialog)
                return
            app._SYNCED_LYRICS_CACHE.clear()
            remember("Wrong lyrics — sync removed, new text requested", lrc_path)
            ctx.slog(f"Wrong lyrics: removed {len(removed)} LRC file(s)"
                 + (" and embedded MP3 lyrics" if embedded else "")
                 + f" for {Path(audio).name}" + (f"\n   Backup: {backup}" if backup else ""))
            if on_saved:
                on_saved(lrc_path, "")  # no LRC any more: drop the song's rows from Failures
            state["closed"] = True
            save_layout()
            waveform.close()
            dialog.destroy()
            replace_window = app.open_replace_lyrics_for_song(str(audio), parent=ctx.win)
            if replace_window is not None:
                replace_window.wait_window()
            if queue:
                queue["advance"] = True  # the lyrics were dealt with: carry on with the next song
            if on_finished:
                on_finished(False)

        ctx.styled_button(actions, "Wrong lyrics — delete sync & find new text…",
                      wrong_lyrics).pack(side="left", padx=(8, 0))

        def chosen_indices():
            return sorted(lines_box.curselection()) or ([state["focus"]] if state["focus"] is not None else [])

        def write_song_files(title, new_lrc, change_txt=None):
            """Back up and write the LRC (+ TXT via ``change_txt(text) -> (new_text, info)``) and the
            MP3's embedded lyrics. Returns ``(txt_path, embedded, txt_info)`` or None after an error."""
            nonlocal lrc_text
            try:
                if Path(lrc_path).read_text(encoding="utf-8-sig") != lrc_text:
                    raise ValueError("The LRC changed on disk after this window was opened. Reopen Fix fragment.")
                txt = app._find_plain_txt_for_audio(str(audio)) if change_txt else ""
                new_txt, txt_info = None, None
                if txt:
                    new_txt, txt_info = change_txt(Path(txt).read_text(encoding="utf-8-sig"))
                embedded = bool(app._synced_lyrics_sources(str(audio), force=True)[0])
                files = [lrc_path] + ([txt] if txt else []) + ([str(audio)] if embedded else [])
                backup = app.studio_resync.backup_song_files(files, app.BACKUP_ROOT)
                app.studio_resync.write_keeping_bom(lrc_path, new_lrc)
                if txt:
                    app.studio_resync.write_keeping_bom(txt, new_txt)
                if embedded:
                    app._embed_synced_lrc_as_mp3tag_unsyncedlyrics(str(audio), new_lrc,
                                                               ctx._common_sync_settings()[3] or "en")
            except Exception as exc:
                messagebox.showerror(title, str(exc), parent=dialog)
                return None
            lrc_text = new_lrc
            state["files_changed"] = True  # Remove / Edit Line already wrote the files: Save may just close
            app._SYNCED_LYRICS_CACHE.clear()
            ctx.slog(f"✓ {title}: {Path(lrc_path).name}" + (" + TXT" if txt else "")
                     + (" + embedded MP3 lyrics" if embedded else "") + f"\n   Backup: {backup}")
            if on_saved:
                on_saved(lrc_path, new_lrc)  # the report re-checks this song
            return txt, embedded, txt_info

        def drop_from_window(gone, renumber, focus_index):
            """Forget lines that are no longer timed lines (``renumber`` maps the file lines that stay)."""
            def remap(times):
                return {renumber(line): t for line, t in times.items() if line not in gone}

            kept_entries = [dict(entry, file_line=renumber(entry["file_line"]))
                            for entry in entries if entry["file_line"] not in gone]
            new_current, new_manual, new_text_of = remap(current_times), remap(manual), {
                renumber(line): text for line, text in text_of.items() if line not in gone}
            entries[:] = kept_entries
            for target, values in ((current_times, new_current), (manual, new_manual), (text_of, new_text_of)):
                target.clear()
                target.update(values)
            undo_stack[:] = [(remap(times), False, None, description,
                              {renumber(line) for line in was_cleared if line not in gone})
                             for times, _hidden, _applied, description, was_cleared in undo_stack]
            still_cleared = {renumber(line) for line in cleared if line not in gone}
            cleared.clear()
            cleared.update(still_cleared)
            state["result"], state["hidden"], state["applied"] = None, False, None  # fixes were for old lines
            focus_index = min(focus_index, len(entries) - 1)
            state["focus"] = None
            compute_alerts()
            lines_box.delete(0, "end")
            for entry in entries:
                lines_box.insert("end", row_text(entry))
            schedule_error_ui()
            lines_box.selection_set(focus_index)
            waveform.line_texts = dict(text_of)
            rebuild_lanes()  # redraws the markers; playhead, zoom, scroll and playback stay as they are
            rebuild_choices()
            set_focus(focus_index, scroll=False)

        def txt_note(txt, embedded, done, missing):
            note = done + (", the TXT" if txt else "") + (" and the embedded MP3 lyrics" if embedded else "") + "."
            if txt and missing:
                note += f" Not found in the TXT (left there): line(s) {', '.join(map(str, missing))}."
            if txt is not None and not txt:
                note += " No TXT lyrics file was found for this song."
            return note

        def remove_lines():
            """Remove Line from song: delete the selected lines from the LRC, the TXT and the MP3's
            embedded lyrics, then update this window in place (the waveform and playback keep going)."""
            if state["busy"]:
                messagebox.showinfo("Remove Line from song", "Wait until the automatic fix finishes.", parent=dialog)
                return
            indices = chosen_indices()
            if not indices:
                messagebox.showinfo("Remove Line from song", "Select the line(s) to remove first.", parent=dialog)
                return
            doomed = [entries[i]["file_line"] for i in indices]
            if len(doomed) >= len(entries):
                messagebox.showinfo("Remove Line from song", "Every line is selected — keep at least one line "
                                    "(use “Wrong lyrics” to remove the whole sync).", parent=dialog)
                return
            preview = "\n".join(f"  line {entries[i]['file_line']}: {entries[i]['text'][:70]}" for i in indices[:8])
            if len(indices) > 8:
                preview += f"\n  … and {len(indices) - 8} more"
            if not messagebox.askyesno(
                    "Remove Line from song",
                    f"Remove {len(indices)} line(s) from this song?\n\n{preview}\n\n"
                    "They are deleted from the LRC, the TXT lyrics and the MP3's embedded lyrics. "
                    "All three files are backed up first (Options → Restore from backups).", parent=dialog):
                return
            old_lrc = lrc_text
            written = write_song_files(
                "Remove Line from song", app.studio_resync.remove_lrc_lines(old_lrc, doomed),
                lambda text: app.studio_resync.remove_txt_lines(text, old_lrc, doomed))
            if written is None:
                return
            txt, embedded, missing = written
            remember("Removed line(s) from song", lrc_path, "line(s) " + ", ".join(map(str, doomed)))
            drop_from_window(set(doomed), lambda line: line - sum(1 for d in doomed if d < line), indices[0])
            status_var.set(txt_note(txt, embedded, f"Removed {len(doomed)} line(s) from the LRC", missing or []))

        def clear_times():
            """Clear time of selected: the lines stay in the list, but have no time until you press T.
            Their markers leave the waveform and they no longer show as Current, so close markers
            stop getting in the way. Nothing is written to the files until Save."""
            indices = chosen_indices()
            if not indices:
                status_var.set("Select the line(s) whose time you want to clear first.")
                return "break"
            lines = [entries[i]["file_line"] for i in indices]
            remember_undo(f"time cleared on {len(lines)} line(s)")
            cleared.update(lines)
            for index in indices:
                refresh_row(index)
            waveform.redraw()
            update_now(waveform.playhead)
            if state["focus"] is not None:
                focus_var.set(selection_text(state["focus"]))
            status_var.set(f"Time cleared on {len(lines)} line(s) — hidden under the waveform. To time one again, "
                           "click it in the list (it has no marker now) and press T where it starts.")
            return "break"

        def edit_line():
            """Edit Line: change the text of one line in the LRC, the TXT and the embedded lyrics."""
            if state["busy"]:
                messagebox.showinfo("Edit Line", "Wait until the automatic fix finishes.", parent=dialog)
                return
            indices = chosen_indices()
            if len(indices) != 1:
                if len(indices) > 1 and state["focus"] in indices:
                    indices = [state["focus"]]
                else:
                    messagebox.showinfo("Edit Line", "Select one line to edit.", parent=dialog)
                    return
            index = indices[0]
            line = entries[index]["file_line"]
            old_text = entries[index]["text"]
            box = app.create_toplevel(dialog)
            box.title(f"Edit Line {line}")
            box.configure(bg=ctx.theme["bg"])
            box.transient(dialog)
            tk.Label(box, text=f"Line {line} — the new text is saved to the LRC, the TXT and the MP3's embedded "
                               "lyrics (all backed up first). The timestamp stays.",
                     bg=ctx.theme["bg"], fg=ctx.theme["fg"], wraplength=640, justify="left").pack(
                fill="x", padx=16, pady=(14, 6))
            text_var = tk.StringVar(value=old_text)
            field = tk.Entry(box, textvariable=text_var, font=("Segoe UI", 12), width=70, bg=ctx.theme["panel"],
                             fg=ctx.theme["fg"], insertbackground=ctx.theme["fg"])
            field.pack(fill="x", padx=16, pady=6)
            answer = {"text": None}

            def accept(_event=None):
                answer["text"] = " ".join(text_var.get().split())
                box.destroy()

            row = tk.Frame(box, bg=ctx.theme["bg"])
            row.pack(fill="x", padx=16, pady=(6, 14))
            ctx.styled_button(row, "Cancel", box.destroy, 10).pack(side="right")
            save = ctx.styled_button(row, "💾 Save line", accept, 12)
            save.configure(font=("Segoe UI", 10, "bold"))
            save.pack(side="right", padx=(0, 8))
            field.bind("<Return>", accept)
            box.bind("<Escape>", lambda _e: box.destroy())
            app.popup_over_root(box, 700, 170)
            box.grab_set()
            field.focus_set()
            field.select_range(0, "end")
            dialog.wait_window(box)
            new_text = answer["text"]
            if new_text is None or new_text == old_text:
                return
            if not new_text:
                messagebox.showinfo("Edit Line", "The line cannot be empty — use Remove Line from song instead.",
                                    parent=dialog)
                return
            old_lrc = lrc_text
            written = write_song_files(
                "Edit Line", app.studio_resync.edit_lrc_line(old_lrc, line, new_text),
                lambda text: (lambda r: (r[0], [] if r[1] else [line]))(
                    app.studio_resync.edit_txt_line(text, old_lrc, line, new_text)))
            if written is None:
                return
            txt, embedded, missing = written
            remember("Edited line text", lrc_path, f"line {line}: “{old_text}” → “{new_text}”")
            entries[index] = dict(entries[index], text=new_text)
            text_of[line] = new_text
            compute_alerts()  # the number of words changed, so the dense check may change
            refresh_row(index)
            waveform.set_line_texts(text_of)
            update_now(waveform.playhead)
            focus_var.set(selection_text(index))
            status_var.set(txt_note(txt, embedded, f"Line {line} now reads “{new_text}” in the LRC", missing or []))

        line_tools = tk.Frame(actions, bg=ctx.theme["bg"])
        line_tools.pack(side="left", padx=(8, 0))
        ctx.styled_button(line_tools, "🗑 Remove Line from song", remove_lines).pack(side="left")
        ctx.styled_button(line_tools, "✏ Edit Line", edit_line).pack(side="left", padx=(8, 0))
        ctx.styled_button(line_tools, "⏱✖ Clear time of selected [Del]", clear_times).pack(side="left", padx=(8, 0))
        for widget in (dialog, lines_box):
            widget.bind("<Delete>", lambda _e: clear_times())
        def close_or_ask():
            """Close; when Current has unsaved changes, ask whether to save them first."""
            if state["busy"] or not changed_lines():
                finish_dialog(False)
                return
            answer = messagebox.askyesnocancel(
                "Unsaved changes",
                f"{len(changed_lines())} line(s) have a new timecode that is not saved yet.\n\n"
                "Save the changes before closing?", parent=dialog)
            if answer is None:
                return  # Cancel: stay in the window
            if answer:
                save_manual()  # closes the window when the save worked
            else:
                finish_dialog(False)

        close_btn = ctx.styled_button(bottom, "Close", close_or_ask, 10)
        close_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4)
        close_btn.pack(side="right")
        save_btn = ctx.styled_button(bottom, "💾 Save", save_manual, 10)
        save_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4)
        save_btn.pack(side="right", padx=(0, 8))

        if queue:
            def save_and_next():
                queue["advance"] = True
                save_manual()  # closes the window (and the queue opens the next song) when the save worked
                if not state["closed"]:
                    queue["advance"] = False

            def skip_song():
                queue["advance"] = True
                close_or_ask()  # asks about unsaved changes; Cancel keeps this song open
                if not state["closed"]:
                    queue["advance"] = False

            where = f"{queue['position']}/{queue['total']}"
            next_btn = ctx.styled_button(bottom, f"💾 Save & next song ▶  ({where})", save_and_next)
            next_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4, bg="#1f5130", fg="#ffffff")
            next_btn.pack(side="right", padx=(0, 16))
            skip_btn_q = ctx.styled_button(bottom, "Skip song ▶", skip_song)
            skip_btn_q.configure(font=("Segoe UI", 11), padx=12, pady=4)
            skip_btn_q.pack(side="right", padx=(0, 8))
            dialog.bind("<Control-Return>", lambda _e: (save_and_next(), "break")[1])

        def set_console(visible):
            """Slide the console (bottom panel) up into view or hide it again."""
            if visible == console["visible"]:
                return
            if visible:
                panes.add(result_pane, minsize=70, stretch="always")
                dialog.update_idletasks()
                try:
                    panes.sash_place(len(panes.panes()) - 2, 0, max(260, panes.winfo_height() - 200))
                except tk.TclError:
                    pass
            else:
                panes.forget(result_pane)
            console["visible"] = visible
            console_btn.configure(text="▼ Hide console" if visible else "▲ Show console")

        console_btn = ctx.styled_button(bottom, "▼ Hide console", lambda: set_console(not console["visible"]))
        console_btn.configure(font=("Segoe UI", 11, "bold"), padx=14, pady=4)
        console_btn.pack(side="right", padx=(0, 8))
        set_console(bool(layout.get("console", False)))
        dialog.protocol("WM_DELETE_WINDOW", close_or_ask)
        rebuild_lanes()
        waveform.load(audio)
        set_focus(preselect[0] if preselect else 0)
        update_error_ui()
        dialog.after(200, restore_layout)

        def route_space(widget):
            """Space = Play / Pause everywhere in this window, even when a button or slider has focus."""
            if isinstance(widget, (tk.Button, tk.Checkbutton, tk.Radiobutton, tk.Scale)):
                widget.bind("<space>", lambda _e: (waveform.toggle(), "break")[1])
            for child in widget.winfo_children():
                route_space(child)

        route_space(dialog)
        state["route_space"] = route_space
        dialog.focus_set()
        if auto_retry and lines_box.curselection():
            dialog.after(300, retry)

    def open_fix_history(parent):
        """Edit and Fix history: songs that were changed, the most recently changed first."""
        history_file = Path(ctx.resync_dir) / "fix_history.json"
        window = app.create_toplevel(parent)
        window.title("Edit and Fix history")
        window.configure(bg=ctx.theme["bg"])
        app.popup_over_root(window, 1000, 640)
        window.minsize(700, 420)
        tk.Label(window, text=("Songs you changed, newest change first. Select a song to see every change; "
                               "open it again in Fix fragment or View/Change Lyrics. Entries marked "
                               "“from backup” were reconstructed from older backups."),
                 bg=ctx.theme["bg"], fg=ctx.theme["fg"], justify="left", anchor="w",
                 wraplength=960).pack(fill="x", padx=14, pady=(12, 6))
        buttons = tk.Frame(window, bg=ctx.theme["bg"])
        buttons.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
        panes = tk.PanedWindow(window, orient="vertical", sashwidth=8, sashrelief="raised",
                               bg=ctx.theme.get("muted", "#555555"), bd=0, opaqueresize=True)
        panes.pack(fill="both", expand=True, padx=14)
        list_frame = tk.Frame(panes, bg=ctx.theme["bg"])
        songs_box = tk.Listbox(list_frame, selectmode="extended", exportselection=False, bg=ctx.theme["panel"], fg=ctx.theme["fg"],
                               font=("Consolas", 10), selectbackground=ctx.theme.get("accent", "#355a7a"))
        songs_scroll = tk.Scrollbar(list_frame, command=songs_box.yview)
        songs_box.configure(yscrollcommand=songs_scroll.set)
        songs_scroll.pack(side="right", fill="y")
        songs_box.pack(fill="both", expand=True)
        details = tk.Text(panes, height=8, wrap="none", bg=ctx.theme["panel"], fg=ctx.theme["fg"],
                          font=("Consolas", 10))
        panes.add(list_frame, minsize=120, stretch="always")
        panes.add(details, minsize=60, stretch="always")
        state = {"songs": [], "index": None}

        def build_index():
            """Old backup entries only know a file name; find those files in the library once."""
            index = {}
            folder = app.MUSIC_DIR
            if folder and os.path.isdir(folder):
                for directory, _dirs, files in os.walk(folder):
                    for name in files:
                        if name.lower().endswith((".lrc", ".mp3")):
                            index.setdefault(name.casefold(), Path(directory) / name)
            return index

        def resolve(path):
            candidate = Path(path)
            if candidate.is_file():
                return candidate
            if state["index"] is None:
                state["index"] = build_index()
            return state["index"].get(candidate.name.casefold())

        def stamp(event):
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(event.get("time", 0)))

        def load():
            events = app.studio_resync.history(history_file, app.BACKUP_ROOT)
            groups = {}
            for event in events:  # newest first, so each song's first event is its latest change
                groups.setdefault(app.studio_resync.song_key(event["path"]), []).append(event)
            state["songs"] = list(groups.values())
            songs_box.delete(0, "end")
            for group in state["songs"]:
                latest = group[0]
                name = Path(latest["path"]).stem
                songs_box.insert("end", f"{stamp(latest)}   {name[:52]:<52}   {latest['action'][:44]}"
                                        + (f"   ({len(group)} changes)" if len(group) > 1 else ""))
            if state["songs"]:
                songs_box.selection_set(0)
                show_details()
            else:
                details.delete("1.0", "end")
                details.insert("1.0", "No changes recorded yet.")

        def selected_group():
            selection = songs_box.curselection()
            return state["songs"][selection[0]] if selection else None

        def show_details(_event=None):
            group = selected_group()
            details.delete("1.0", "end")
            if not group:
                return
            lines = [f"{Path(group[0]['path']).stem} — {len(group)} change(s), newest first", ""]
            for event in group:
                lines.append(f"{stamp(event)}   {event['action']}" + (f"   — {event['detail']}" if event.get("detail") else ""))
                lines.append(f"                   {event['path']}")
            details.insert("1.0", "\n".join(lines))

        def song_files(group):
            """(lrc, audio) for the selected song; either may be None."""
            for event in group:
                path = resolve(event["path"])
                if path is None:
                    continue
                if path.suffix.lower() == ".mp3":
                    lrc = next((p for p in app.studio_resync.lrc_locations(path) if p.is_file()), None)
                    return lrc, path
                try:
                    return path, app.studio_resync.audio_for_lrc(path)
                except (ValueError, OSError):
                    return path, None
            return None, None

        def fix_again():
            group = selected_group()
            if not group:
                return
            lrc, _audio = song_files(group)
            if lrc is None:
                messagebox.showinfo("Edit and Fix history", "This song has no LRC file now (it may be waiting "
                                    "for Lyrics Sync). Use View/Change Lyrics or resync it first.", parent=window)
                return
            open_fragment_fix(str(lrc), 0, 0, window, on_finished=lambda _saved: load())

        def replace_again():
            group = selected_group()
            if not group:
                return
            _lrc, audio = song_files(group)
            if audio is None:
                messagebox.showinfo("Edit and Fix history", "The MP3 of this song was not found in the library.",
                                    parent=window)
                return
            replace_window = app.open_replace_lyrics_for_song(str(audio), parent=window)
            if replace_window is not None:
                replace_window.wait_window()
                if window.winfo_exists():
                    load()

        def open_folder():
            group = selected_group()
            if not group:
                return
            lrc, audio = song_files(group)
            target = lrc or audio
            if target is not None:
                os.startfile(str(Path(target).parent))

        def remove_selected():
            groups = [state["songs"][i] for i in songs_box.curselection()]
            if not groups:
                messagebox.showinfo("Edit and Fix history", "Select one or more songs first "
                                    "(Shift/Ctrl+click selects several).", parent=window)
                return
            names = ", ".join(Path(group[0]["path"]).stem for group in groups[:3]) + ("…" if len(groups) > 3 else "")
            if not messagebox.askyesno(
                    "Remove from history",
                    f"Remove {len(groups)} song(s) from Edit and Fix history?\n\n{names}\n\n"
                    "Only the history entries are removed; lyrics, LRC files and backups are not touched.",
                    parent=window):
                return
            keys = {app.studio_resync.song_key(group[0]["path"]) for group in groups}
            removed = app.studio_resync.remove_history(history_file, keys, app.BACKUP_ROOT)
            load()
            details.delete("1.0", "end")
            details.insert("1.0", f"Removed {removed} history entr{'y' if removed == 1 else 'ies'}.")

        songs_box.bind("<<ListboxSelect>>", show_details)
        songs_box.bind("<Double-Button-1>", lambda _e: fix_again())
        songs_box.bind("<Delete>", lambda _e: remove_selected())
        for text, command in (("Fix Fragment Manually…", fix_again), ("View/Change Lyrics…", replace_again),
                              ("Open containing folder", open_folder), ("Refresh", load)):
            ctx.styled_button(buttons, text, command).pack(side="left", padx=(0, 8))
        ctx.styled_button(buttons, "Close", window.destroy, 10).pack(side="right")
        ctx.styled_button(buttons, "Remove from history", remove_selected).pack(side="right", padx=(0, 8))
        load()


    KIND_LABELS = {"dense_lines": "DENSE LINES", "even_spacing": "GUESSED", "ends_early": "ENDS EARLY"}
    FILTER_KINDS = {"same": "same_time", "dense": "dense_lines", "guessed": "even_spacing"}

    def show_lrc_failure_report(result):
        visible, ignored_count = app.studio_resync.split_ignored(result["failures"], ctx.ignored_path)
        failures = sorted(
            visible,
            key=lambda item: (-sum(x["count"] for x in visible if x["path"] == item["path"]), item["path"].casefold(), item["time_ms"]),
        )
        unreadable = result["unreadable"]
        scanned = result["files_scanned"]
        ignored = {"count": ignored_count}
        # The report opens even with no warnings, so "Fix fragment in any LRC…"
        # and "Restore ignored" stay one click away.
        no_warnings_text = (
            f"No suspicious passages found in {scanned} LRC file(s): no repeated timestamps and no "
            "unusually dense long-line runs. This does not confirm alignment with the audio."
            + (f" {ignored_count} ignored warning(s) are hidden." if ignored_count else ""))

        report = app.create_toplevel(ctx.win)
        report.title("Edit and Fix Synced Lyrics — Possible Sync Failures")
        report.configure(bg=ctx.theme["bg"])
        app.popup_over_root(report, 980, 620)
        report.minsize(720, 420)

        heading = tk.Label(
            report, font=("Segoe UI", 14, "bold"), bg=ctx.theme["bg"], fg=ctx.theme["fg"],
            anchor="w", justify="left",
        )
        heading.pack(fill="x", padx=14, pady=(14, 4))

        def own_severity(item):
            """How bad one passage is on its own: SAME TIME and strict DENSE are errors."""
            if item.get("kind") == "ends_early":
                return "ends"  # has its own tab and never counts as an error of the song
            if item.get("unreadable") or item.get("kind") not in ("dense_lines", "even_spacing"):
                return "errors"  # SAME TIME
            level = item.get("severity") or ("error" if item.get("count", 0) >= 4 else "minor")
            return "errors" if level == "error" else "minor"

        def error_songs():
            return {os.path.normcase(item["path"]) for item in failures if own_severity(item) == "errors"}

        def severity(item, songs=None):
            """Which tab a passage belongs to. A song with any error keeps ALL its passages in Errors:
            in an already broken song the smaller warnings can be dangerous too."""
            if own_severity(item) == "ends":
                return "ends"
            if own_severity(item) == "errors":
                return "errors"
            songs = error_songs() if songs is None else songs
            return "errors" if os.path.normcase(item["path"]) in songs else "minor"

        def update_heading():
            warnings = [item for item in failures if not item.get("unreadable")]
            songs = error_songs()
            errors = [item for item in warnings if severity(item, songs) == "errors"]
            minor = [item for item in warnings if severity(item, songs) == "minor"]
            ends = [item for item in warnings if severity(item, songs) == "ends"]
            heading.configure(text=(
                f"Errors: {len({i['path'] for i in errors})} songs ({len(errors)} passages)   •   "
                f"Potential smaller errors: {len({i['path'] for i in minor})} songs ({len(minor)} passages)   •   "
                f"Ends early: {len(ends)} songs\n"
                f"{scanned} LRC files scanned"
                + (f"   •   ignored warnings: {ignored['count']}" if ignored["count"] else "")))
            tab_errors.configure(text=f"Errors ({len({i['path'] for i in errors})} songs)")
            tab_minor.configure(text=f"Potential Smaller Errors ({len({i['path'] for i in minor})} songs)")
            tab_ends.configure(text=f"Ends Early ({len(ends)} songs)")

        explanation = tk.Label(
            report,
            text=("Errors: lines sharing one timestamp (SAME TIME), lines packed too tightly to be sung (DENSE: 4+ "
                  "long lines, or 13+ words per second) and lines Whisper did not hear whose times were guessed — "
                  "spread exactly evenly (GUESSED: 8+ lines, or less than 1 s apart). A song with any error keeps all "
                  "its passages in Errors (smaller ones are marked “(small)”). Potential Smaller Errors: only short "
                  "dense or guessed runs in otherwise clean songs; worth a listen, but rarely a broken song. Ends Early tab (no ⚠ in Tracks): the last lyric line starts 30+ s before the song ends - the sync may have "
                  "stopped half way (or the song just has a long outro). Songs with the most suspicious "
                  "lines are at the top. Each row is one passage; Ctrl/Shift selects several. START = first lyric line."),
            bg=ctx.theme["bg"], fg=ctx.theme.get("muted", ctx.theme["fg"]), anchor="w",
            justify="left", wraplength=930,
        )
        explanation.pack(fill="x", padx=14, pady=(0, 10))

        tab_var = tk.StringVar(value="errors")
        type_var = tk.StringVar(value="all")
        search_var = tk.StringVar(value="")
        tabs_row = tk.Frame(report, bg=ctx.theme["bg"])
        tabs_row.pack(fill="x", padx=14, pady=(0, 2))

        def tab_button(value, text):
            return tk.Radiobutton(tabs_row, text=text, variable=tab_var, value=value, indicatoron=False,
                                  command=lambda: render(), font=("Segoe UI", 11, "bold"), padx=16, pady=6,
                                  bg=ctx.theme["panel"], fg=ctx.theme["fg"], selectcolor=ctx.theme.get("accent", "#355a7a"),
                                  activebackground=ctx.theme["panel"], activeforeground=ctx.theme["fg"])

        tab_errors = tab_button("errors", "Errors")
        tab_errors.pack(side="left")
        tab_minor = tab_button("minor", "Potential Smaller Errors")
        tab_minor.pack(side="left", padx=(6, 0))
        tab_ends = tab_button("ends", "Ends Early")
        tab_ends.pack(side="left", padx=(6, 0))
        filter_row = tk.Frame(report, bg=ctx.theme["bg"])
        filter_row.pack(fill="x", padx=14, pady=(4, 6))
        tk.Label(filter_row, text="Show:", bg=ctx.theme["bg"], fg=ctx.theme["fg"]).pack(side="left")
        for value, text in (("all", "All types"), ("same", "SAME TIME"), ("dense", "DENSE"), ("guessed", "GUESSED")):
            tk.Radiobutton(filter_row, text=text, variable=type_var, value=value, command=lambda: render(),
                           bg=ctx.theme["bg"], fg=ctx.theme["fg"], selectcolor=ctx.theme["panel"],
                           activebackground=ctx.theme["bg"]).pack(side="left", padx=(6, 0))
        search_entry = tk.Entry(filter_row, textvariable=search_var, width=34, bg=ctx.theme["panel"],
                                fg=ctx.theme["fg"], insertbackground=ctx.theme["fg"])
        search_entry.pack(side="right")
        tk.Label(filter_row, text="🔎 Search song:", bg=ctx.theme["bg"], fg=ctx.theme["fg"]).pack(side="right", padx=(0, 6))
        search_var.trace_add("write", lambda *_args: render())

        list_frame = tk.Frame(report, bg=ctx.theme["bg"])
        list_frame.pack(fill="both", expand=True, padx=14)
        results_list = tk.Listbox(
            list_frame, selectmode="extended", exportselection=False, bg=ctx.theme["panel"], fg=ctx.theme["fg"],
            selectbackground=ctx.theme.get("accent", "#355a7a"),
            selectforeground=ctx.theme["fg"], font=("Consolas", 10),
        )
        vertical = tk.Scrollbar(list_frame, orient="vertical", command=results_list.yview)
        horizontal = tk.Scrollbar(list_frame, orient="horizontal", command=results_list.xview)
        results_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        results_list.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        root_path = Path(result["folder"])

        def row_text(item):
            if item.get("unreadable"):
                return f"[READ ] could not read  |  {item['path']}"
            try:
                display_path = str(Path(item["path"]).relative_to(root_path))
            except (ValueError, OSError):
                display_path = item["path"]
            location = "START" if item["at_start"] else "LATER"
            kind = KIND_LABELS.get(item.get("kind"), "SAME TIME")
            if own_severity(item) == "minor":
                kind += " (small)"  # in Errors only because the same song has a real error
            if item.get("kind") == "ends_early":
                return (f"[END  ] {item['timestamp']} + {item['gap_seconds']:>5.0f}s left "
                        f"| {kind:19} | {display_path}")
            return (f"[{location:5}] {item['timestamp']} × {item['count']:>2} lines "
                    f"| {kind:19} | {display_path}")

        for path, error in unreadable:
            failures.append({"path": path, "unreadable": True, "error": error})
        view = []  # the rows currently shown (tab + type filter + search), in list order

        def render(select=0):
            needle = search_var.get().strip().casefold()
            kind = type_var.get()
            songs = error_songs()
            items = [item for item in failures
                     if severity(item, songs) == tab_var.get()
                     and (kind == "all" or (not item.get("unreadable")
                                            and FILTER_KINDS[kind] == item.get("kind", "same_time")))
                     and (not needle or needle in Path(item["path"]).name.casefold())]
            totals = {}
            for item in items:
                totals[item["path"]] = totals.get(item["path"], 0) + item.get("count", 0)
            if tab_var.get() == "ends":  # the longer the song goes on after the lyrics, the higher it is
                items.sort(key=lambda item: (-item.get("gap_seconds", 0), -item.get("duration_seconds", 0),
                                             item["path"].casefold()))
            else:
                items.sort(key=lambda item: (-totals[item["path"]], -item.get("gap_seconds", 0),
                                             item["path"].casefold(), item.get("time_ms", 0)))
            view[:] = items
            results_list.delete(0, "end")
            for item in view:
                results_list.insert("end", row_text(item))
            update_heading()
            if view:
                results_list.selection_set(min(max(0, select), len(view) - 1))
                results_list.see(min(max(0, select), len(view) - 1))
                update_detail()
            elif not failures:
                detail_var.set(no_warnings_text)
            else:
                detail_var.set("Nothing to show here — try the other tab, another type or a different search.")

        def same_file(left, right):
            try:
                return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))
            except OSError:
                return False

        def refresh_fixed_file(path, new_text):
            """Re-check one saved LRC and update its rows. Returns warnings still shown."""
            positions = [i for i, item in enumerate(failures)
                         if not item.get("unreadable") and same_file(item["path"], path)]
            key = positions and failures[positions[0]]["path"] or str(Path(path).resolve())
            fresh, hidden = app.studio_resync.split_ignored(
                [dict(run, path=key) for run in app._find_lrc_runs_for_file(Path(path), new_text)], ctx.ignored_path)
            insert_at = positions[0] if positions else None
            for i in reversed(positions):
                del failures[i]
            if insert_at is not None and report.winfo_exists():
                failures[insert_at:insert_at] = fresh
                selection = results_list.curselection()
                render(selection[0] if selection else 0)
            try:
                app.refresh_track_badges()
            except Exception:
                pass
            return fresh

        detail_var = tk.StringVar(value="Select an item to see its details.")
        detail = tk.Label(
            report, textvariable=detail_var, bg=ctx.theme["bg"], fg=ctx.theme["fg"],
            anchor="w", justify="left", wraplength=930,
        )
        detail.pack(fill="x", padx=14, pady=(8, 4))

        def selected_item():
            selection = results_list.curselection()
            return view[selection[0]] if selection else None

        def selected_items():
            return [view[i] for i in results_list.curselection() if not view[i].get("unreadable")]

        def update_detail(_event=None):
            item = selected_item()
            if not item:
                return
            if item.get("unreadable"):
                detail_var.set(f"File: {item['path']}\nRead error: {item['error']}")
            else:
                where = ("at the start of the lyrics" if item["at_start"]
                         else "at the end of the lyrics" if item.get("kind") == "ends_early" else "later in the lyrics")
                if item.get("kind") == "dense_lines":
                    timing = (f"{item['count']} lines start within {item['span_seconds']:.2f}s "
                              f"({item['timestamp']}–{item['end_timestamp']}) — too fast to sing")
                elif item.get("kind") == "ends_early":
                    def clock(seconds):
                        return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
                    timing = (f"the lyrics end at {item['timestamp']} but the song lasts {clock(item['duration_seconds'])} "
                              f"- {item['gap_seconds']:.0f}s without any lyric line. Possible cut-off sync: "
                              "listen to the end (it may also be a real outro)")
                elif item.get("kind") == "even_spacing":
                    timing = (f"{item['count']} lines start exactly every {item.get('step_seconds', 0):.2f}s "
                              f"({item['timestamp']}–{item['end_timestamp']}) — Whisper did not hear them; "
                              "the times were guessed (spread evenly)")
                else:
                    timing = f"{item['count']} lines share {item['timestamp']}"
                detail_var.set(
                    f"{timing} {where}; "
                    f"file lines {item['first_file_line']}–{item['last_file_line']}.\n"
                    f"First line: {item['first_text']}\nFile: {item['path']}"
                )

        def open_selected_file(_event=None):
            item = selected_item()
            if item and Path(item["path"]).is_file():
                try:
                    os.startfile(item["path"])
                except OSError as exc:
                    messagebox.showerror("Open LRC", str(exc), parent=report)

        def open_selected_folder():
            item = selected_item()
            if item and Path(item["path"]).parent.is_dir():
                try:
                    os.startfile(str(Path(item["path"]).parent))
                except OSError as exc:
                    messagebox.showerror("Open folder", str(exc), parent=report)

        results_list.bind("<<ListboxSelect>>", update_detail)
        results_list.bind("<Double-Button-1>", open_selected_file)

        report_buttons = tk.Frame(report, bg=ctx.theme["bg"])
        report_buttons.pack(fill="x", padx=14, pady=(6, 14))
        ctx.styled_button(report_buttons, "Open selected LRC", open_selected_file).pack(side="left")
        ctx.styled_button(report_buttons, "Open containing folder", open_selected_folder).pack(side="left", padx=(8, 0))

        def replace_selected_lyrics():
            item = selected_item()
            if not item or item.get("unreadable"):
                messagebox.showinfo("View/Change Lyrics", "Select one warning row first.", parent=report)
                return
            try:
                audio = app.studio_resync.audio_for_lrc(item["path"])
            except (ValueError, OSError) as exc:
                messagebox.showerror("View/Change Lyrics", str(exc), parent=report)
                return
            app.open_replace_lyrics_for_song(str(audio), parent=report)

        ctx.styled_button(report_buttons, "View/Change Lyrics…", replace_selected_lyrics).pack(side="left", padx=(8, 0))

        def swap_selected_songs():
            """Put new audio (e.g. a version with the long beat at the end cut off) in place of the selected songs:
            all metadata, lyrics and cover move over (Swap Song and Transfer Metadata), then each song is checked again."""
            import difflib
            lrcs = []
            for item in selected_items():
                if item["path"] not in lrcs:
                    lrcs.append(item["path"])
            songs = [(lrc, app.studio_resync.song_for_lrc(lrc)) for lrc in lrcs]
            songs = [(lrc, song) for lrc, song in songs if song]
            if not songs:
                messagebox.showinfo("Swap Song", "Select the warning rows of the songs you want to replace first.",
                                    parent=report)
                return
            files = filedialog.askopenfilenames(
                parent=report, title=f"Choose the new version of {len(songs)} song(s)",
                filetypes=[("Songs", "*.mp3 *.flac"), ("All files", "*.*")])
            if not files:
                return

            def name_key(path):
                return app.studio_swap.strip_brackets(Path(path).stem).casefold()

            pairs, unmatched, free = [], [], list(files)
            for lrc, song in songs:
                if len(songs) == 1 and len(files) == 1:
                    best = files[0]
                else:
                    scored = sorted(((difflib.SequenceMatcher(None, name_key(song), name_key(f)).ratio(), f) for f in free),
                                    reverse=True)
                    best = scored[0][1] if scored and scored[0][0] >= 0.6 else None
                if not best:
                    unmatched.append(song.name)
                    continue
                free.remove(best) if best in free else None
                pairs.append((lrc, song, Path(best)))
            if not pairs:
                messagebox.showinfo("Swap Song", "None of the chosen files has a name like the selected songs.",
                                    parent=report)
                return
            listing = "\n".join(f"• {song.name}  ←  {new.name}" for _lrc, song, new in pairs[:15])
            if len(pairs) > 15:
                listing += f"\n… and {len(pairs) - 15} more"
            if unmatched:
                listing += "\n\nNo matching file (skipped): " + ", ".join(unmatched[:8])
            if not messagebox.askyesno(
                    "Swap Song", f"Replace {len(pairs)} song(s) with the new file?\n\n{listing}\n\n"
                    "Every tag, the embedded lyrics and the front cover move to the new file, which takes the old "
                    "file's name and place (the TXT / LRC stay with it). The old file goes to "
                    f"“{app.studio_swap.OLD_SONGS_FOLDER}” next to the new file. Both are backed up first.",
                    parent=report):
                return
            keys = [key for key, _label in app.studio_swap.FIELDS] + ["lyrics", "cover"]
            lines = []
            for lrc, song, new in pairs:
                if song.suffix.lower() != new.suffix.lower():
                    lines.append(f"✗ {song.name}: the new file must also be {song.suffix.upper()[1:]}")
                    continue
                try:
                    new_final, old_final, backup = app.studio_swap.swap(song, new, keys, app.BACKUP_ROOT)
                except Exception as exc:
                    lines.append(f"✗ {song.name}: {exc}")
                    continue
                app._remember_fix("Swap Song and Transfer Metadata", str(new_final), f"old song moved to {old_final}")
                app.log(f"Swap Song: {new.name} → {new_final}; old song → {old_final}. Backup: {backup}")
                try:
                    app.WARNING_INDEX.forget(lrc)
                    remaining = refresh_fixed_file(lrc, Path(lrc).read_text(encoding="utf-8-sig"))
                except Exception:
                    remaining = None
                length = app.studio_resync.song_length(new_final)
                when = f" ({int(length) // 60}:{int(length) % 60:02d})" if length else ""
                if remaining is None:
                    lines.append(f"✓ {song.name}{when}: swapped")
                elif not remaining:
                    lines.append(f"✓ {song.name}{when}: swapped — no warnings left")
                else:
                    kinds = sorted({KIND_LABELS.get(run.get("kind"), "SAME TIME") for run in remaining})
                    lines.append(f"⚠ {song.name}{when}: swapped — still: {', '.join(kinds)}")
            app._SYNCED_LYRICS_CACHE.clear()
            try:
                app.refresh_tracks_from_disk()
            except Exception:
                pass
            messagebox.showinfo("Swap Song", "\n".join(lines), parent=report)

        ctx.styled_button(report_buttons, "Swap song (new audio)…", swap_selected_songs).pack(side="left", padx=(8, 0))

        def fix_selected_fragment():
            item = selected_item()
            if not item or item.get("unreadable"):
                messagebox.showinfo("Fix fragment", "Select one warning row first.", parent=report)
                return
            open_fragment_fix(item["path"], item["first_file_line"], item["last_file_line"], report,
                              on_saved=refresh_fixed_file)

        warning_tools = tk.Frame(report, bg=ctx.theme["bg"])
        warning_tools.pack(fill="x", padx=14, pady=(6, 0), before=report_buttons)
        ctx.styled_button(warning_tools, "Fix Fragment Manually…", fix_selected_fragment).pack(side="left")

        def fix_songs_one_by_one():
            """Open every listed song one after another (from the selected one): at its first error,
            with Save & next song / Skip song — no clicking back to this list in between."""
            order, seen = [], set()
            for item in view:
                if not item.get("unreadable") and item["path"] not in seen:
                    seen.add(item["path"])
                    order.append(item["path"])
            if not order:
                messagebox.showinfo("Fix songs one by one", "There are no songs in this list.", parent=report)
                return
            chosen = selected_item()
            start = order.index(chosen["path"]) if chosen and chosen.get("path") in order else 0
            queue_songs = order[start:]

            def first_warning(path):
                rows = [item for item in failures if not item.get("unreadable") and same_file(item["path"], path)
                        and severity(item) == tab_var.get()] or [
                    item for item in failures if not item.get("unreadable") and same_file(item["path"], path)]
                return min(rows, key=lambda item: item["first_file_line"]) if rows else None

            def open_next(i):
                if not report.winfo_exists():
                    return
                while i < len(queue_songs) and first_warning(queue_songs[i]) is None:
                    i += 1  # already fixed (or ignored) meanwhile
                if i >= len(queue_songs):
                    messagebox.showinfo("Fix songs one by one", "That was the last song in the list.", parent=report)
                    return
                item = first_warning(queue_songs[i])
                ctl = {"position": i + 1, "total": len(queue_songs), "advance": False}

                def finished(saved, i=i, ctl=ctl):
                    if ctl["advance"] and report.winfo_exists():
                        report.after(150, lambda: open_next(i + 1))

                open_fragment_fix(item["path"], item["first_file_line"], item["last_file_line"], report,
                                  on_saved=refresh_fixed_file, on_finished=finished,
                                  title_prefix=f"[{i + 1}/{len(queue_songs)}] ", queue=ctl)

            open_next(0)

        ctx.styled_button(warning_tools, "▶ Fix songs one by one", fix_songs_one_by_one).pack(side="left", padx=(8, 0))

        def quick_listen():
            """Quick listen: hear each listed passage for a few seconds and decide with one key."""
            rows = [item for item in view if not item.get("unreadable")]
            if not rows:
                messagebox.showinfo("Quick listen", "There are no passages in this list.", parent=report)
                return
            chosen = selected_item()
            start = next((i for i, item in enumerate(rows) if item is chosen), 0)

            def resolve(item):
                for row in failures:
                    if (not row.get("unreadable") and row["first_file_line"] == item["first_file_line"]
                            and same_file(row["path"], item["path"])):
                        return row
                return None

            def ignore(item):
                app.studio_resync.ignore([item], ctx.ignored_path)
                failures[:] = [row for row in failures if row is not item]
                ignored["count"] += 1
                restore_button.configure(text=f"Restore ignored ({ignored['count']})")
                try:
                    app.refresh_track_badges()
                except Exception:
                    pass

            def fix(item, done):
                open_fragment_fix(item["path"], item["first_file_line"], item["last_file_line"], report,
                                  on_saved=refresh_fixed_file, on_finished=lambda saved: done(saved))

            def wrong(item, done):
                audio = app.studio_resync.audio_for_lrc(item["path"])
                window = app.open_replace_lyrics_for_song(str(audio), parent=report)
                if window is None:
                    done(False)
                    return
                window.wait_window()
                done(True)

            def entries(item):
                return app._lrc_timed_entries(Path(item["path"]).read_text(encoding="utf-8-sig"))

            volume_path = Path(ctx.resync_dir) / "quick_listen.json"

            def remember_volume(volume):
                try:
                    app.studio_resync.write(volume_path, {"volume": round(volume, 3)})
                except OSError:
                    pass

            saved_volume = app.studio_resync.read(volume_path).get("volume", 0.8)
            handlers = {"resolve": resolve, "ignore": ignore, "fix": fix, "wrong": wrong, "volume": remember_volume,
                        "audio": lambda item: app.studio_resync.audio_for_lrc(item["path"]),
                        "entries": entries, "name": lambda item: Path(item["path"]).stem,
                        "closed": lambda counts: render(0) if report.winfo_exists() else None}
            studio_triage.QuickListen(report, ctx.theme, app.create_toplevel, rows[start:], handlers,
                                      volume=saved_volume)

        ctx.styled_button(warning_tools, "🎧 Quick listen", quick_listen).pack(side="left", padx=(8, 0))

        def fix_any_lrc():
            path = filedialog.askopenfilename(
                parent=report, title="Choose an LRC to fix",
                initialdir=app.MUSIC_DIR if app.MUSIC_DIR and Path(app.MUSIC_DIR).is_dir() else None,
                filetypes=(("LRC files", "*.lrc"), ("All files", "*.*")))
            if path:
                open_fragment_fix(path, 0, 0, report, on_saved=refresh_fixed_file)

        ctx.styled_button(warning_tools, "Fix Fragment Manually (any LRC)…", fix_any_lrc).pack(side="left", padx=(8, 0))
        ctx.styled_button(warning_tools, "🕘 Edit and Fix history",
                          lambda: open_fix_history(report)).pack(side="right")

        def ignore_selected_warnings():
            rows = selected_items()
            if not rows:
                messagebox.showinfo("Ignore warning", "Select one or more warning rows first.", parent=report)
                return
            if not messagebox.askyesno(
                    "Ignore warning",
                    f"Hide {len(rows)} selected warning(s) from future scans?\n\n"
                    "Nothing is changed in the LRC files. If a song is resynced and the "
                    "warning comes back with different timings, it is shown again.",
                    parent=report):
                return
            try:
                app.studio_resync.ignore(rows, ctx.ignored_path)
            except OSError as exc:
                messagebox.showerror("Ignore warning", str(exc), parent=report)
                return
            first_row = results_list.curselection()[0]
            failures[:] = [item for item in failures if not any(item is row for row in rows)]
            ignored["count"] += len(rows)
            restore_button.configure(text=f"Restore ignored ({ignored['count']})")
            render(first_row)

        def restore_ignored():
            if not ignored["count"] and not app.studio_resync.read(ctx.ignored_path):
                messagebox.showinfo("Restore ignored", "There are no ignored warnings.", parent=report)
                return
            if not messagebox.askyesno(
                    "Restore ignored",
                    "Show all ignored warnings again?\nThey appear after the next scan.",
                    parent=report):
                return
            try:
                app.studio_resync.write(ctx.ignored_path, {})
            except OSError as exc:
                messagebox.showerror("Restore ignored", str(exc), parent=report)
                return
            report.destroy()
            scan_lrc_library()

        ctx.styled_button(warning_tools, "Ignore selected warning", ignore_selected_warnings).pack(side="left", padx=(8, 0))
        restore_button = ctx.styled_button(warning_tools, f"Restore ignored ({ignored['count']})", restore_ignored)
        restore_button.pack(side="left", padx=(8, 0))


        def mark_selected(paths=None):
            """Delete LRC + embedded lyrics of the given (or selected) songs and mark them for Lyrics Sync."""
            if ctx.running["value"]:
                messagebox.showinfo("Busy", "Finish the current sync first.", parent=report)
                return
            selected = set(paths or [item["path"] for item in selected_items()])
            if not selected:
                return
            ctx.set_running(True)
            def work():
                errors = []
                for path in selected:
                    try:
                        app.studio_resync.mark(path, ctx.queue_path,
                            app.BACKUP_ROOT,
                            app._find_plain_txt_for_audio, app.remove_embedded_lyrics)
                    except Exception as exc:
                        errors.append(str(exc))
                def finish():
                    ctx.set_running(False)
                    app._SYNCED_LYRICS_CACHE.clear()
                    app.refresh_tracks_from_disk()
                    report.destroy()
                    messagebox.showinfo("Marked for Lyrics Sync",
                        f"Marked: {len(selected)-len(errors)}\n" + "\n".join(errors), parent=ctx.win)
                app.ui_call(finish)
            threading.Thread(target=work, daemon=True).start()
        def delete_current_lyrics(paths=None):
            """Back up and delete the songs' LRC + embedded lyrics; they go to Marked for Lyrics Sync."""
            if ctx.running["value"] or ctx.setup_running["value"]:
                messagebox.showinfo("Busy", "Finish the current operation first.", parent=report)
                return
            songs = list(dict.fromkeys(paths or [item["path"] for item in selected_items()]))
            if not songs:
                messagebox.showinfo("Delete Current Lyrics", "Select one or more warning rows first.", parent=report)
                return
            if not messagebox.askyesno(
                    "Delete Current Lyrics",
                    f"Delete the current synced lyrics of {len(songs)} song(s)?\n\n"
                    "The LRC and embedded MP3 lyrics are backed up and deleted; the plain TXT lyrics are kept. "
                    "The songs go to the “Marked for Lyrics Sync” list so you can sync them again later.",
                    parent=report):
                return
            mark_selected(songs)


        ctx.styled_button(report_buttons, "Close", report.destroy, 10).pack(side="right")
        ctx.styled_button(report_buttons, "🗑 Delete Current .lrc and Embedded Lyrics",
                          lambda: delete_current_lyrics()).pack(side="right", padx=(0, 8))
        if failures and not any(severity(item) == "errors" for item in failures):
            # nothing in Errors: open the tab that has something
            tab_var.set("minor" if any(severity(item) == "minor" for item in failures) else "ends")
        render()

    def scan_lrc_library():
        if ctx.running["value"] or ctx.setup_running["value"] or ctx.lrc_scan_running["value"]:
            return
        library = app.MUSIC_DIR
        if not library or not Path(library).is_dir():
            messagebox.showinfo(
                "LRC Library Scan",
                "Open your music folder in the Downloader window first.",
                parent=ctx.win,
            )
            return

        ctx.lrc_scan_running["value"] = True
        ctx.scan_lrc_btn.configure(state="disabled", text="Scanning LRC library…")
        ctx.sync_status_var.set("Scanning LRC library for suspicious timings…")
        ctx.slog(f"Scanning LRC library: {library}")

        def finish(result=None, error=None):
            ctx.lrc_scan_running["value"] = False
            if not ctx.win.winfo_exists():
                return
            ctx.scan_lrc_btn.configure(state="normal", text="Edit and Fix Synced Lyrics")
            if error:
                ctx.sync_status_var.set("LRC library scan failed — see log")
                ctx.slog("✗ LRC library scan failed: " + error)
                messagebox.showerror("LRC Library Scan", error, parent=ctx.win)
                return
            visible, ignored_count = app.studio_resync.split_ignored(result["failures"], ctx.ignored_path)
            count = len(visible)
            flagged_files = len({item["path"] for item in visible})
            ctx.sync_status_var.set(
                f"LRC scan done — {flagged_files} of {result['files_scanned']} LRC files "
                f"have warnings; {count} suspicious passages total."
            )
            ctx.slog(
                f"LRC scan complete: {result['files_scanned']} file(s), "
                f"{flagged_files} LRC file(s) with warnings, {count} suspicious passage(s) total "
                f"({sum(item.get('kind') == 'dense_lines' for item in visible)} dense-line, "
                f"{sum(item.get('kind') == 'even_spacing' for item in visible)} guessed, "
                f"{sum(item.get('kind', 'same_time') == 'same_time' for item in visible)} same-timestamp), "
                f"{ignored_count} ignored, {len(result['unreadable'])} unreadable."
            )
            show_lrc_failure_report(result)
            try:
                app.refresh_track_badges()  # ⚠ marks in Tracks follow the fresh scan
            except Exception:
                pass

        def worker():
            try:
                result = app.studio_resync.scan(library, ctx.resync_dir / "scan_cache.json",
                                           app._find_lrc_duplicate_timestamp_runs,
                                           app._find_lrc_runs_for_file)
                app.ui_call(finish, result, None)
            except Exception as exc:
                app.ui_call(finish, None, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    return {"scan_lrc_library": scan_lrc_library, "open_marked_sync": open_marked_sync,
            "open_fragment_fix": open_fragment_fix}
