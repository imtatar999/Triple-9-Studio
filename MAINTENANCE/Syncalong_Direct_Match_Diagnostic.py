import os
import re
import sys
import math
import queue
import threading
import difflib
import warnings
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

warnings.filterwarnings(
    "ignore",
    message=r"Failed to launch Triton kernels.*",
    category=UserWarning,
    module=r"whisper\.timing",
)

APP_NAME = "Triplenine Hybrid Sessions Lyrics Builder"

SECTION_RE = re.compile(
    r"^\s*[\[\(\{]\s*(intro|verse|chorus|hook|bridge|outro|refrain|pre[- ]?chorus|"
    r"post[- ]?chorus|interlude|break|instrumental|spoken|skit)(?:[^\]\)\}]*)[\]\)\}]\s*$",
    re.I,
)

FEATURE_RE = re.compile(r"(?i)\b(?:ft\.?|feat\.?|featuring|with)\b")


def norm(s):
    s = (s or "").lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"\[[^\]]+\]|\([^\)]+\)|\{[^\}]+\}", " ", s)
    s = re.sub(r"[^a-z0-9']+", " ", s)
    return " ".join(s.split())


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    aw, bw = a.split(), b.split()
    aset, bset = set(aw), set(bw)
    jac = len(aset & bset) / max(1, len(aset | bset))
    # Sequence similarity matters most; token overlap helps with punctuation
    # and small Whisper insertions.
    return 0.72 * seq + 0.28 * jac


def parse_reference(text):
    """Return ordered reference items: section headers and lyric lines."""
    items = []
    current_section = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if SECTION_RE.match(line):
            current_section = line
            items.append({"kind": "section", "text": line})
        else:
            items.append({
                "kind": "lyric",
                "text": line,
                "section": current_section,
            })
    return items


def group_whisper_words(words, pause=0.85, max_words=14, max_seconds=8.5):
    """Turn Syncalong WordTimestamp objects into readable transcript lines."""
    if not words:
        return []

    groups, cur = [], []
    start = None
    prev_end = None

    def flush():
        nonlocal cur, start, prev_end
        if cur:
            raw = "".join(
                (("" if i == 0 or str(getattr(w, "raw", "")).startswith(("'", ",", ".", "!", "?", ":", ";"))
                  else " ") + str(getattr(w, "raw", getattr(w, "word", ""))).strip())
                for i, w in enumerate(cur)
            )
            raw = re.sub(r"\s+([,.!?;:])", r"\1", raw).strip()
            groups.append({
                "text": raw,
                "start": float(getattr(cur[0], "start", 0.0)),
                "end": float(getattr(cur[-1], "end", getattr(cur[-1], "start", 0.0))),
            })
        cur, start, prev_end = [], None, None

    for w in words:
        ws = float(getattr(w, "start", 0.0))
        we = float(getattr(w, "end", ws))
        raw = str(getattr(w, "raw", getattr(w, "word", ""))).strip()
        gap = (ws - prev_end) if prev_end is not None else 0.0

        if cur and (
            gap >= pause
            or len(cur) >= max_words
            or (start is not None and we - start >= max_seconds)
        ):
            flush()

        if not cur:
            start = ws
        cur.append(w)
        prev_end = we

        # Whisper punctuation is useful as a soft line boundary.
        if raw.endswith((".", "!", "?")) and len(cur) >= 5:
            flush()

    flush()
    return groups


def build_reference_units(items):
    """Attach section headers to the next lyric line for later reconstruction."""
    units = []
    pending_sections = []
    for item in items:
        if item["kind"] == "section":
            pending_sections.append(item["text"])
        else:
            units.append({
                "text": item["text"],
                "headers": pending_sections[:],
            })
            pending_sections.clear()
    return units, pending_sections


def _join_texts(items):
    return " ".join(x["text"].strip() for x in items if x.get("text", "").strip())


def _block_similarity(ref_block, trans_block):
    """Similarity for 1..3 reference lines against 1..3 Whisper chunks."""
    return similarity(_join_texts(ref_block), _join_texts(trans_block))


def align_reference_to_transcript(ref_units, trans_lines, threshold=0.46):
    """Block-aware monotonic alignment.

    V2 allows 1-3 reference lines to align against 1-3 Whisper chunks.
    This handles line-boundary drift where Whisper merges/splits Genius lines.
    Extra Sessions-only audio can still be skipped cheaply and preserved.
    """
    n, m = len(ref_units), len(trans_lines)
    if not n or not m:
        return []

    neg = -10**9
    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0

    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] - 0.02
        back[0][j] = ("skip_t", 0, j - 1)

    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] - 0.70
        back[i][0] = ("skip_r", i - 1, 0)

    block_sizes = (1, 2, 3)

    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]
            if cur <= neg / 2:
                continue

            if j < m:
                val = cur - 0.02
                if val > dp[i][j + 1]:
                    dp[i][j + 1] = val
                    back[i][j + 1] = ("skip_t", i, j)

            if i < n:
                val = cur - 0.70
                if val > dp[i + 1][j]:
                    dp[i + 1][j] = val
                    back[i + 1][j] = ("skip_r", i, j)

            for rb in block_sizes:
                if i + rb > n:
                    continue
                for tb in block_sizes:
                    if j + tb > m:
                        continue

                    sim = _block_similarity(
                        ref_units[i:i + rb],
                        trans_lines[j:j + tb],
                    )

                    block_bonus = 0.10 if (rb > 1 or tb > 1) else 0.0

                    if sim >= threshold:
                        gain = sim * 2.70 + block_bonus
                    else:
                        gain = sim * 1.05 - 1.00

                    val = cur + gain
                    if val > dp[i + rb][j + tb]:
                        dp[i + rb][j + tb] = val
                        back[i + rb][j + tb] = (
                            "match", i, j, rb, tb, sim
                        )

    matches = []
    i, j = n, m
    while i > 0 or j > 0:
        step = back[i][j]
        if step is None:
            break
        kind = step[0]
        if kind == "match":
            _, pi, pj, rb, tb, sim = step
            if sim >= threshold:
                matches.append({
                    "ref_start": pi,
                    "ref_count": rb,
                    "trans_start": pj,
                    "trans_count": tb,
                    "similarity": sim,
                })
            i, j = pi, pj
        elif kind == "skip_t":
            _, i, j = step
        else:
            _, i, j = step

    matches.reverse()
    return matches


def _best_local_reference_similarity(ref_text, trans_lines, center, span=3):
    lo = max(0, center - span)
    hi = min(len(trans_lines), center + span + 1)
    best = 0.0
    for j in range(lo, hi):
        for count in (1, 2, 3):
            if j + count > len(trans_lines):
                continue
            best = max(
                best,
                similarity(ref_text, _join_texts(trans_lines[j:j + count]))
            )
    return best


def hybridize(ref_items, trans_lines, threshold=0.46):
    """Merge trusted reference into the full Sessions transcript."""
    ref_units, trailing_headers = build_reference_units(ref_items)
    matches = align_reference_to_transcript(ref_units, trans_lines, threshold)

    output = []
    diagnostics = []
    used_ref = set()
    matched_trans = set()
    blocks_by_start = {m["trans_start"]: m for m in matches}

    tj = 0
    while tj < len(trans_lines):
        block = blocks_by_start.get(tj)

        if block is None:
            if tj not in matched_trans:
                t = trans_lines[tj]
                output.append(t["text"])
                diagnostics.append(
                    f'[WHISPER ONLY     ] {t["text"]}  '
                    f'({t["start"]:.2f}s–{t["end"]:.2f}s)'
                )
            tj += 1
            continue

        ri = block["ref_start"]
        rb = block["ref_count"]
        tb = block["trans_count"]
        sim = block["similarity"]

        refs = ref_units[ri:ri + rb]
        trans = trans_lines[tj:tj + tb]

        for offset, unit in enumerate(refs):
            for header in unit["headers"]:
                output.append(header)

            output.append(unit["text"])
            used_ref.add(ri + offset)

            local_score = _best_local_reference_similarity(
                unit["text"], trans_lines, tj, span=max(3, tb + 1)
            )
            diagnostics.append(
                f'[REFERENCE BLOCK {sim*100:5.1f}% | line≈{local_score*100:5.1f}%] '
                f'{unit["text"]}'
            )

        diagnostics.append(
            f'                 Whisper block ({rb} ref ↔ {tb} chunk): '
            f'{_join_texts(trans)}'
        )

        for k in range(tj, min(len(trans_lines), tj + tb)):
            matched_trans.add(k)
        tj += tb

    unmatched = [
        (i, u["text"])
        for i, u in enumerate(ref_units)
        if i not in used_ref
    ]

    return output, diagnostics, matches, unmatched, trailing_headers


def _make_reference_prompt(ref_units):
    """Use the shorter lyrics as a stronger Whisper vocabulary/context hint."""
    text = " ".join(u["text"] for u in ref_units)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 6500:
        return text
    return text[:4500] + " " + text[-2000:]


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1120x850")
        self.root.minsize(900, 700)
        self.q = queue.Queue()
        self.running = False
        self.last_result = ""

        self.audio_var = tk.StringVar()
        self.model_var = tk.StringVar(value="turbo")
        self.language_var = tk.StringVar(value="en")
        self.threshold_var = tk.DoubleVar(value=0.46)
        self.demucs_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self.build_ui()
        self.root.after(100, self.poll_queue)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Hybrid Sessions Lyrics Builder",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Whisper transcribes the full audio. V2 block-matches pasted reference lyrics "
                "against Whisper, so Genius line breaks do not have to match Whisper chunks."
            ),
        ).pack(anchor="w", pady=(2, 10))

        audio = ttk.LabelFrame(outer, text="1. Sessions audio", padding=8)
        audio.pack(fill="x")
        ttk.Entry(audio, textvariable=self.audio_var).pack(side="left", fill="x", expand=True)
        ttk.Button(audio, text="Browse…", command=self.browse_audio).pack(side="left", padx=(8, 0))

        settings = ttk.Frame(outer)
        settings.pack(fill="x", pady=8)
        ttk.Label(settings, text="Whisper model:").pack(side="left")
        ttk.Combobox(
            settings, textvariable=self.model_var, width=12, state="readonly",
            values=("tiny", "tiny.en", "base", "base.en", "small", "small.en",
                    "medium", "medium.en", "turbo", "large-v3"),
        ).pack(side="left", padx=(5, 15))
        ttk.Label(settings, text="Language:").pack(side="left")
        ttk.Entry(settings, textvariable=self.language_var, width=7).pack(side="left", padx=(5, 15))
        ttk.Label(settings, text="Reference match threshold:").pack(side="left")
        ttk.Spinbox(
            settings, from_=0.35, to=0.85, increment=0.01,
            textvariable=self.threshold_var, width=6
        ).pack(side="left", padx=(5, 15))
        ttk.Checkbutton(
            settings, text="Separate vocals (Demucs)", variable=self.demucs_var
        ).pack(side="left")

        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(0, 8))

        left = ttk.LabelFrame(panes, text="2. Paste shorter reference lyrics here", padding=7)
        right = ttk.LabelFrame(panes, text="3. Hybrid result", padding=7)
        panes.add(left, weight=1)
        panes.add(right, weight=1)

        self.reference = tk.Text(left, wrap="word", undo=True, font=("Consolas", 10))
        self.reference.pack(fill="both", expand=True)
        self.result = tk.Text(right, wrap="word", undo=True, font=("Consolas", 10))
        self.result.pack(fill="both", expand=True)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 8))
        self.start_btn = ttk.Button(actions, text="▶ BUILD HYBRID LYRICS", command=self.start)
        self.start_btn.pack(side="left")
        ttk.Button(actions, text="Save result as TXT…", command=self.save).pack(side="left", padx=8)
        ttk.Button(actions, text="Clear result", command=self.clear_result).pack(side="left")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        diag_frame = ttk.LabelFrame(
            outer,
            text="Diagnostic log — shows Reference corrections vs Whisper-only Sessions lines",
            padding=7,
        )
        diag_frame.pack(fill="both", expand=False)
        self.log = tk.Text(diag_frame, height=12, wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def browse_audio(self):
        path = filedialog.askopenfilename(
            title="Select Sessions audio",
            filetypes=[
                ("Audio", "*.mp3 *.flac *.wav *.m4a *.ogg *.opus"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.audio_var.set(path)

    def clear_result(self):
        self.result.delete("1.0", "end")
        self.log.delete("1.0", "end")
        self.last_result = ""

    def save(self):
        text = self.result.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo(APP_NAME, "There is no hybrid result to save.")
            return
        audio = self.audio_var.get().strip()
        initial = (Path(audio).stem + " - Hybrid.txt") if audio else "Hybrid Lyrics.txt"
        path = filedialog.asksaveasfilename(
            title="Save Hybrid Lyrics",
            defaultextension=".txt",
            initialfile=initial,
            filetypes=[("Text file", "*.txt")],
        )
        if path:
            Path(path).write_text(text + "\n", encoding="utf-8")
            self.status_var.set(f"Saved: {Path(path).name}")

    def putlog(self, text):
        self.q.put(("log", text))

    def start(self):
        if self.running:
            return
        audio = self.audio_var.get().strip()
        reference = self.reference.get("1.0", "end").strip()
        if not audio or not os.path.isfile(audio):
            messagebox.showerror(APP_NAME, "Select a valid audio file first.")
            return
        if not reference:
            messagebox.showerror(APP_NAME, "Paste the shorter reference lyrics first.")
            return

        self.clear_result()
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status_var.set("Working…")

        args = (
            audio, reference, self.model_var.get().strip(),
            self.language_var.get().strip(),
            float(self.threshold_var.get()),
            bool(self.demucs_var.get()),
        )
        threading.Thread(target=self.worker, args=args, daemon=True).start()

    def worker(self, audio, reference, model, language, threshold, separate_vocals):
        try:
            self.putlog(f"Audio: {audio}")
            self.putlog(f"Model: {model} | Language: {language or 'auto'}")
            self.putlog("Loading Syncalong / Whisper…")

            try:
                import syncalong
            except Exception as exc:
                raise RuntimeError(
                    'Syncalong is not installed. Install with: pip install "syncalong[whisper]"'
                ) from exc

            ref_items = parse_reference(reference)
            ref_units, _ = build_reference_units(ref_items)
            if not ref_units:
                raise RuntimeError("The pasted reference contains no lyric lines.")

            # Use the trusted shorter text as Whisper's vocabulary/context hint.
            # This does NOT force Whisper to output only those lyrics.
            initial_prompt = _make_reference_prompt(ref_units)

            self.putlog(
                f"Reference: {len(ref_units)} lyric lines. "
                "Using it as Whisper initial_prompt."
            )
            self.putlog("Transcribing the FULL audio…")

            tx = syncalong.Transcriber(model_name=model)
            words = tx.transcribe(
                Path(audio),
                language=language or None,
                initial_prompt=initial_prompt or None,
                separate_vocals=separate_vocals,
            )
            self.putlog(f"Whisper returned {len(words)} timestamped words.")

            trans_lines = group_whisper_words(words)
            self.putlog(f"Grouped into {len(trans_lines)} transcript lines.")
            self.putlog("Aligning shorter reference inside full Sessions transcript…")

            output, diagnostics, mapping, unmatched, trailing = hybridize(
                ref_items, trans_lines, threshold
            )

            result = "\n".join(output).strip()
            total = len(ref_units)
            matched = total - len(unmatched)
            pct = (100.0 * matched / total) if total else 0.0

            self.q.put(("result", result))
            self.putlog("")
            self.putlog(
                f"Reference lines used: {matched}/{total} ({pct:.1f}%). "
                f"Matched blocks: {len(mapping)}. "
                "Extra Sessions audio remains as Whisper-only text."
            )
            self.putlog("")
            for line in diagnostics:
                self.putlog(line)

            if unmatched:
                self.putlog("")
                self.putlog("REFERENCE LINES NOT CONFIDENTLY FOUND IN AUDIO:")
                for _, line in unmatched:
                    self.putlog(f"  [NOT INSERTED] {line}")
                self.putlog(
                    "These were deliberately NOT forced into the result, so the "
                    "program does not invent lyrics that may not exist in this Sessions audio."
                )

            if trailing:
                self.putlog("")
                self.putlog(
                    "Trailing reference section header(s) had no following matched lyric "
                    "and were not inserted: " + " | ".join(trailing)
                )

            self.q.put(("done", f"Done — {matched}/{total} reference lines anchored"))
        except Exception as exc:
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    def poll_queue(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", value + "\n")
                    self.log.see("end")
                elif kind == "result":
                    self.result.delete("1.0", "end")
                    self.result.insert("1.0", value)
                    self.last_result = value
                elif kind == "done":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.status_var.set(value)
                elif kind == "error":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.status_var.set("Error")
                    self.log.insert("end", "\nERROR: " + value + "\n")
                    self.log.see("end")
                    messagebox.showerror(APP_NAME, value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
