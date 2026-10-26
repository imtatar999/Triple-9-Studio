import os
import ctypes
import re
import sys
import math
import queue
import threading
import difflib
import warnings
import json
import shutil
import subprocess
import tempfile as _tempfile
import importlib.util
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import triplenine_reference_queue as reference_queue
import studio_preferences
import studio_windows
import studio_scrollbars
import studio_runtime
studio_runtime.prepare_environment()  # the program's own ffmpeg (runtime\\ffmpeg) on PATH
XINGYU_ZIP = "https://github.com/wangjiqing/xingyu-lyrics-aligner/archive/HEAD.zip"  # no Git needed
from triplenine_builder_runtime import TranscriptionProcess
import re
import time

warnings.filterwarnings(
    "ignore",
    message=r"Failed to launch Triton kernels.*",
    category=UserWarning,
    module=r"whisper\.timing",
)

APP_NAME = "Lyric Builder"


def _find_app_icon():
    """Use the same application icon as the Downloader."""
    candidates = [
        Path(__file__).resolve().with_name("triplenine.ico"),
        Path(sys.executable).resolve().with_name("triplenine.ico"),
    ]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "triplenine.ico")
    return next((str(path) for path in candidates if path.is_file()), "")


ICON_FILE = _find_app_icon()
# Downloader → View/Change Lyrics → "Open in Lyric Builder" writes this; the Builder loads it.
OPEN_REQUEST_FILE = Path(__file__).resolve().with_name("lyrics_sync_state") / "builder_request.json"


def _set_titlebar_theme(window, dark):
    """Match the Downloader's Windows title-bar appearance."""
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        enabled = ctypes.c_int(1 if dark else 0)
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0:
                break
    except Exception:
        pass

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


def similarity(a, b, *, autojunk=True):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b, autojunk=autojunk).ratio()
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

        # Genius-style [Open Verse] means "missing/unwritten verse here".
        # It is a placeholder, not a lyric and not a literal output header.
        # We skip it completely; if Sessions audio contains material in that
        # gap, the existing structure inference will create a real [Verse].
        if re.match(
            r"^\s*[\[\(\{]\s*open\s+verse(?:[^\]\)\}]*)[\]\)\}]\s*$",
            line,
            flags=re.I,
        ):
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


def group_whisper_words(words, pause=0.38, max_words=10, max_seconds=5.0):
    """Build audio-aware phrases from Whisper word timestamps.

    V5 uses real inter-word pauses as the primary line-break signal. This is
    much better for freestyle lyrics than capitalization or arbitrary text
    width. Short ad-libs such as "huh"/"ayy" are attached backwards.
    """
    if not words:
        return []

    ADLIBS = {
        "huh", "uh", "ayy", "ay", "yeah", "yo", "woo", "woah", "whoa",
        "oh", "ooh", "uh-huh", "mm", "mmm"
    }

    groups = []
    cur = []
    prev_end = None
    start_time = None

    def raw_word(w):
        return str(getattr(w, "raw", getattr(w, "word", ""))).strip()

    def clean_token(x):
        return re.sub(r"[^a-z0-9-]+", "", x.lower())

    def flush():
        nonlocal cur, prev_end, start_time
        if not cur:
            return
        parts = [raw_word(w) for w in cur]
        text = " ".join(x for x in parts if x)
        text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        text = re.sub(r"\bOff\s+-\s*white\b", "Off-white", text, flags=re.I)
        text = re.sub(r"\buh\s+-\s*huh\b", "uh-huh", text, flags=re.I)
        groups.append({
            "text": text.strip(),
            "start": float(getattr(cur[0], "start", 0.0)),
            "end": float(getattr(cur[-1], "end", getattr(cur[-1], "start", 0.0))),
        })
        cur = []
        prev_end = None
        start_time = None

    for idx, w in enumerate(words):
        ws = float(getattr(w, "start", 0.0))
        we = float(getattr(w, "end", ws))
        token = raw_word(w)
        token_clean = clean_token(token)
        gap = ws - prev_end if prev_end is not None else 0.0

        # Never start a new line with a short vocal ad-lib if there is a
        # preceding phrase. It belongs to the phrase before it.
        is_adlib = token_clean in ADLIBS
        should_break = False

        if cur and not is_adlib:
            elapsed = we - (start_time if start_time is not None else ws)

            # Real pause is strongest evidence.
            if gap >= pause and len(cur) >= 3:
                should_break = True
            # Strong punctuation plus at least a small phrase.
            elif raw_word(cur[-1]).endswith((".", "!", "?", ";", ":")) and len(cur) >= 3:
                should_break = True
            # Prevent giant rows, but only as a fallback.
            elif len(cur) >= max_words or elapsed >= max_seconds:
                should_break = True

        if should_break:
            flush()

        if not cur:
            start_time = ws
        cur.append(w)
        prev_end = we

        # If an ad-lib closes a phrase and the next word comes after a pause,
        # the next iteration will flush it together with this phrase.

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
                # A strong Chorus must not validate a weak line from the
                # preceding Verse merely because both share one ASR chunk.
                if any(unit.get("headers") for unit in ref_units[i + 1:i + rb]):
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


def _section_name(header):
    m = SECTION_RE.match((header or "").strip())
    return m.group(1).strip().lower() if m else ""


def _is_verse_header(header):
    return bool(re.match(r"(?i)^\s*\[\s*verse\b", header or ""))


def _split_whisper_only_line(text):
    """Conservative V5 cleanup for an already audio-segmented phrase."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []

    text = re.sub(r"\bOff\s+-\s*white\b", "Off-white", text, flags=re.I)
    text = re.sub(r"\buh\s+-\s*huh\b", "uh-huh", text, flags=re.I)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)

    # Attach stranded ad-libs to the previous phrase at the caller level.
    # Do not split on capitalization. Do not split on every comma.
    words = text.split()
    if len(words) <= 14:
        return [text]

    # Only very long phrases get a secondary punctuation split. Prefer a comma
    # near the middle and guarantee >=4 words on both sides.
    candidates = [
        i for i, w in enumerate(words[:-1], 1)
        if w.endswith((",", ";", ":")) and 4 <= i <= len(words) - 4
    ]
    if candidates:
        target = len(words) / 2
        cut = min(candidates, key=lambda i: abs(i - target))
        return [
            " ".join(words[:cut]).strip(),
            " ".join(words[cut:]).strip(),
        ]

    # No punctuation: balanced fallback, never a 1-3 word fragment.
    cut = len(words) // 2
    cut = max(4, min(cut, len(words) - 4))
    return [
        " ".join(words[:cut]).strip(),
        " ".join(words[cut:]).strip(),
    ]


def _is_adlib_only(text):
    t = re.sub(r"[^a-z0-9-]+", "", (text or "").lower())
    return t in {
        "huh", "uh", "ayy", "ay", "yeah", "yo", "woo", "woah", "whoa",
        "oh", "ooh", "uh-huh", "mm", "mmm"
    }


def _append_whisper_phrase(output, diagnostics, phrase, start, end):
    """Append phrase while preventing ad-libs from starting their own line."""
    phrase = (phrase or "").strip()
    if not phrase:
        return

    if _is_adlib_only(phrase) and output and output[-1] and not SECTION_RE.match(output[-1]):
        output[-1] = output[-1].rstrip(", ") + ", " + phrase
        diagnostics.append(
            f'[WHISPER ADLIB    ] attached "{phrase}" to previous line '
            f'({start:.2f}s–{end:.2f}s)'
        )
        return

    output.append(phrase)
    diagnostics.append(
        f'[WHISPER PHRASE   ] {phrase} ({start:.2f}s–{end:.2f}s)'
    )

def _reference_section_map(ref_items):
    """Map every reference lyric index to its current structural section."""
    section_for_ref = {}
    current = None
    ri = 0
    first_header = None
    for item in ref_items:
        if item["kind"] == "section":
            current = item["text"]
            if first_header is None:
                first_header = current
        else:
            section_for_ref[ri] = current
            ri += 1
    return section_for_ref, first_header


def _append_section(output, header):
    """Append a section with exactly one blank line before it."""
    if not header:
        return
    while output and output[-1] == "":
        output.pop()
    if output:
        output.append("")
    output.append(header)


def _format_sections_and_renumber_verses(lines):
    """Guarantee blank lines between sections and sequential Verse numbering."""
    cleaned = []
    verse_no = 0
    for line in lines:
        line = (line or "").rstrip()
        if SECTION_RE.match(line):
            while cleaned and cleaned[-1] == "":
                cleaned.pop()
            if cleaned:
                cleaned.append("")
            if _is_verse_header(line):
                verse_no += 1
                line = f"[Verse {verse_no}]"
            cleaned.append(line)
        else:
            if line:
                cleaned.append(line)

    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def _intro_duplicate_similarity(a, b):
    def clean(text):
        text = (text or "").lower()
        text = text.replace("’", "'").replace("‘", "'")
        text = re.sub(r"[^a-z0-9']+", " ", text)
        return " ".join(text.split())

    a, b = clean(a), clean(b)
    if not a or not b:
        return 0.0

    seq = difflib.SequenceMatcher(None, a, b).ratio()
    aw, bw = a.split(), b.split()
    aset, bset = set(aw), set(bw)
    jac = len(aset & bset) / max(1, len(aset | bset))
    return 0.72 * seq + 0.28 * jac


def _is_duplicate_of_upcoming_trusted_intro(candidate_text, next_block, ref_units):
    """Suppress only Session-ASR lines before the first trusted anchor.

    This fixes the common intro case where Xingyu hears a spoken line first and
    the same line appears a moment later as the trusted Genius/reference lyric.
    We deliberately keep this rule limited to the initial pre-anchor run so
    repeated hooks/verses later in the song are never removed accidentally.
    """
    if not next_block:
        return False, 0.0, None

    ri = next_block["ref_start"]
    # Look at the upcoming matched block plus a couple of nearby trusted lines.
    start = max(0, ri - 2)
    end = min(len(ref_units), ri + next_block["ref_count"] + 3)

    best_score = 0.0
    best_text = None
    for unit in ref_units[start:end]:
        score = _intro_duplicate_similarity(candidate_text, unit["text"])
        if score > best_score:
            best_score = score
            best_text = unit["text"]

    # 0.60 catches "G-faces" vs "G-Fazos / 1's" while remaining conservative,
    # and the rule only runs before the first anchored reference block.
    return best_score >= 0.60, best_score, best_text


def _recover_upcoming_section_prefix(
    run, next_block, ref_units, section_for_ref, used_ref, emitted_section, threshold,
    xingyu_lines=None
):
    """Recover the complete missing beginning of the next trusted section.

    Important rule: once audio clearly identifies the BEGINNING of an upcoming
    trusted section, the reference owns the wording and structure. We therefore
    restore ALL contiguous trusted lines before the next anchor, even if ASR
    missed one or more lines in the middle.

    This is specifically meant for cases such as:
        [Intro] 808
        [Chorus] line 1 ... line 7
    where Whisper/Xingyu hears Chorus lines 1-2, misses 3-4, and the global
    aligner first anchors on Chorus line 5. Lines 1-4 must still come from the
    trusted reference, never from rough ASR under [Intro].
    """
    if not run or not next_block:
        return None

    ri = next_block["ref_start"]
    if ri <= 0:
        return None

    target_section = section_for_ref.get(ri)
    if not target_section or target_section == emitted_section:
        return None

    indices = []
    p = ri - 1
    while p >= 0 and len(indices) < 12:
        if p in used_ref or section_for_ref.get(p) != target_section:
            break
        indices.append(p)
        p -= 1
    indices.reverse()
    if not indices:
        return None

    # Do NOT require ASR to contain every missing trusted line. It is enough
    # to identify the START of the trusted section strongly. V49 checks BOTH
    # Whisper chunks and Xingyu candidates. This matters when Whisper completely
    # misses Chorus lines 3-4 but Xingyu clearly hears lines 1-2.
    sources = [("Whisper", run)]

    if run and xingyu_lines:
        interval_start = float(run[0]["start"])
        interval_end = float(run[-1]["end"])
        x_probe_lines = _xingyu_lines_for_interval(
            xingyu_lines, interval_start, interval_end
        )
        if x_probe_lines:
            sources.append(("Xingyu", x_probe_lines))

    best = None
    max_ref_probe = min(3, len(indices))
    for source_name, source_lines in sources:
        for ref_count in range(1, max_ref_probe + 1):
            ref_probe = " ".join(ref_units[i]["text"] for i in indices[:ref_count])
            ref_words = set(norm(ref_probe).split())
            if not ref_words:
                continue

            for start_i in range(len(source_lines)):
                for audio_count in range(
                    1, min(3, len(source_lines) - start_i) + 1
                ):
                    audio_probe = " ".join(
                        x.get("text", "")
                        for x in source_lines[start_i:start_i + audio_count]
                    ).strip()
                    if not audio_probe:
                        continue

                    sim = similarity(ref_probe, audio_probe)
                    aw = set(norm(audio_probe).split())
                    ref_coverage = len(ref_words & aw) / max(1, len(ref_words))
                    combined = 0.76 * sim + 0.24 * ref_coverage

                    # For a combined Xingyu sentence containing two reference
                    # lines, word coverage is especially useful.
                    candidate = (
                        combined, sim, ref_coverage, ref_count, audio_count,
                        start_i, source_name, source_lines
                    )
                    if best is None or candidate[:3] > best[:3]:
                        best = candidate

    if not best:
        return None

    (
        combined, seq_score, coverage, ref_count, audio_count,
        start_i, source_name, source_lines
    ) = best

    # Strong evidence that this audio is the start of the upcoming trusted
    # section. Once established, restore its ENTIRE missing prefix. We do not
    # require lines 3-4 to have been heard by ASR: the reference is trusted.
    if combined < 0.47 or coverage < 0.47:
        return None

    # Convert the selected source position back into a time boundary. The main
    # unmatched run is trimmed by time, so genuine Intro material before the
    # Chorus remains intact even when Xingyu supplied the winning evidence.
    section_start_time = float(source_lines[start_i]["start"])
    run_start_index = len(run)
    for k, item in enumerate(run):
        if float(item["end"]) >= section_start_time - 0.35:
            run_start_index = k
            break

    return {
        "indices": indices,
        "section": target_section,
        "run_start_index": run_start_index,
        "score": combined,
        "sequence_score": seq_score,
        "coverage": coverage,
        "probe_ref_count": ref_count,
        "probe_audio_count": audio_count,
        "source": source_name,
        "section_start_time": section_start_time,
    }


def _should_suppress_unmatched_intro_before_recovery(
    run, recovered_prefix, emitted_section, ref_items
):
    """Suppress an unverified opening Intro when its trusted Intro lyrics were not found.

    If the reference begins with [Intro] but none of that Intro's trusted lyric
    lines are confidently aligned, and we have already recognized that the next
    real section starts later (for example [Chorus]), raw Whisper hallucinations
    before that boundary must not be printed as an invented [Intro].

    This intentionally prefers omission over fabrication. Genuine Intro text is
    still preserved when reference Intro lines were actually matched elsewhere
    by the normal trusted-reference alignment.
    """
    if not run or not recovered_prefix:
        return False
    if emitted_section not in (None, "[Intro]"):
        return False
    if recovered_prefix.get("section") == "[Intro]":
        return False

    # This rule is only for a reference that really starts with Intro.
    first_section = None
    for item in ref_items:
        if item.get("kind") == "section":
            first_section = item.get("text")
            break
    return first_section == "[Intro]"


def _is_boundary_duplicate_of_output_tail(candidate, output):
    """Catch Xingyu overlap containing the last 1-3 trusted lines.

    Example candidate:
        "Avoid them, break hearts, destroy them"
    after trusted Chorus tail:
        "Don't follow rules, avoid 'em"
        "Break hearts, destroy 'em"

    Comparing only the final single line is too strict, so compare against a
    short tail and measure how much of the candidate is already represented.
    """
    lyrics = [
        line for line in output
        if line and not SECTION_RE.match(line)
    ]
    if not lyrics:
        return False, 0.0, None

    cand_words = set(norm(candidate).split())
    if not cand_words:
        return False, 0.0, None

    best_score = 0.0
    best_tail = None
    for count in range(1, min(3, len(lyrics)) + 1):
        tail = " ".join(lyrics[-count:])
        tail_words = set(norm(tail).split())
        overlap = len(cand_words & tail_words) / max(1, len(cand_words))
        seq = _intro_duplicate_similarity(candidate, tail)
        score = 0.58 * overlap + 0.42 * seq
        if score > best_score:
            best_score = score
            best_tail = tail

    return best_score >= 0.58, best_score, best_tail


def _reference_gap_trace(diagnostics, run, x_candidates, next_block, ref_units):
    """Verbose diagnostic for every unmatched reference-mode audio gap."""
    if not run:
        return

    run_start = float(run[0].get("start", 0.0))
    run_end = float(run[-1].get("end", run_start))
    diagnostics.append("")
    diagnostics.append(
        f"[REFERENCE GAP TRACE] {run_start:.2f}s-{run_end:.2f}s | "
        f"{len(run)} Whisper phrase(s) | {len(x_candidates or [])} Xingyu segment(s)"
    )

    for i, t in enumerate(run, 1):
        clean_text = re.sub(r"\s+", " ", t.get("text", "")).strip()
        diagnostics.append(
            f"[GAP WHISPER {i:02d}] "
            f"{float(t.get('start', 0.0)):.2f}s-"
            f"{float(t.get('end', t.get('start', 0.0))):.2f}s | "
            f"{clean_text}"
        )

    if x_candidates:
        for i, x in enumerate(x_candidates, 1):
            clean_text = re.sub(r"\s+", " ", x.get("text", "")).strip()
            diagnostics.append(
                f"[GAP XINGYU  {i:02d}] "
                f"{float(x.get('start', 0.0)):.2f}s-"
                f"{float(x.get('end', x.get('start', 0.0))):.2f}s | "
                f"{clean_text}"
            )
    else:
        diagnostics.append("[GAP XINGYU] no overlapping Xingyu segment")

    if next_block:
        ri = int(next_block.get("ref_start", 0))
        rc = int(next_block.get("ref_count", 1))
        preview = " | ".join(
            ref_units[k]["text"]
            for k in range(ri, min(len(ref_units), ri + max(3, rc)))
        )
        diagnostics.append(
            f"[GAP NEXT REFERENCE] ref_index={ri} | {preview}"
        )


def _preanchor_unverified_whisper(run, x_candidates, next_block, ref_units):
    """Suppress an early Whisper-only phrase when the second ASR does not hear it.

    Limited strictly to audio before the very first trusted reference anchor.
    This avoids inventing lines such as a Whisper startup hallucination while
    preserving anything corroborated by Xingyu or resembling the upcoming
    trusted reference.
    """
    if not run or x_candidates or not next_block:
        return False, 0.0

    heard = " ".join(x.get("text", "") for x in run).strip()
    ri = int(next_block.get("ref_start", 0))
    best = 0.0
    for k in range(ri, min(len(ref_units), ri + 5)):
        best = max(best, similarity(heard, ref_units[k]["text"]))

    duration = float(run[-1].get("end", 0.0)) - float(run[0].get("start", 0.0))
    # Keep this conservative: only short/early unsupported material, and only
    # when it does not look like the upcoming reference either.
    return duration <= 12.0 and best < 0.36, best


def _first_reference_post_chorus(ref_items):
    """Return the first trusted Post-Chorus header and all its lyric lines."""
    header = None
    lines = []
    collecting = False

    for item in ref_items:
        if item.get("kind") == "section":
            kind = _section_name(item.get("text"))
            if collecting:
                break
            if kind in ("post-chorus", "post chorus"):
                collecting = True
                header = item.get("text")
        elif collecting and item.get("kind") == "lyric":
            text = item.get("text", "").strip()
            if text:
                lines.append(text)

    return header, lines


def _known_section_start_score(lines, pos, section_lines, ref_offset=0):
    """Start fingerprint for a trusted repeated section, allowing line merges."""
    if pos >= len(lines) or ref_offset >= len(section_lines):
        return 0.0

    target = " ".join(section_lines[ref_offset:ref_offset + 2])
    if not target:
        return 0.0

    best = 0.0
    for take in range(1, min(3, len(lines) - pos) + 1):
        heard_parts = lines[pos:pos + take]
        if any(SECTION_RE.match(x) for x in heard_parts):
            break
        heard = " ".join(heard_parts)
        rw = set(norm(target).split())
        hw = set(norm(heard).split())
        coverage = len(rw & hw) / max(1, len(rw))
        score = 0.72 * similarity(target, heard) + 0.28 * coverage
        best = max(best, score)
    return best


def _consume_known_section_from_offset(lines, pos, section_lines, ref_offset=0):
    """Align rough ASR to trusted section lines, allowing 1<->2 line drift."""
    j = pos
    ri = ref_offset
    matched_ref = 0

    while ri < len(section_lines) and j < len(lines):
        if SECTION_RE.match(lines[j]):
            break

        best = None
        for rtake in (1, 2):
            if ri + rtake > len(section_lines):
                continue
            ref_text = " ".join(section_lines[ri:ri + rtake])

            for atake in (1, 2):
                if j + atake > len(lines):
                    continue
                parts = lines[j:j + atake]
                if any(SECTION_RE.match(x) for x in parts):
                    continue

                heard = " ".join(parts)
                rw = set(norm(ref_text).split())
                hw = set(norm(heard).split())
                coverage = len(rw & hw) / max(1, len(rw))
                score = 0.72 * similarity(ref_text, heard) + 0.28 * coverage
                cand = (score, rtake, atake)

                if best is None or cand > best:
                    best = cand

        if not best or best[0] < 0.40:
            break

        ri += best[1]
        matched_ref += best[1]
        j += best[2]

    return j - pos, matched_ref


def _detect_repeated_post_chorus(lines, pos, post_lines):
    """Recognize a repeated Post-Chorus immediately after a repeated Chorus.

    The first Post-Chorus line may have been merged into the final Chorus ASR
    segment, so we explicitly try both offset 0 and offset 1.
    """
    best = None
    for ref_offset in (0, 1):
        if ref_offset >= len(post_lines):
            continue

        start_score = _known_section_start_score(
            lines, pos, post_lines, ref_offset=ref_offset
        )
        consumed, matched = _consume_known_section_from_offset(
            lines, pos, post_lines, ref_offset=ref_offset
        )

        available = len(post_lines) - ref_offset
        coverage = matched / max(1, available)
        candidate = (0.70 * start_score + 0.30 * coverage,
                     start_score, coverage, consumed, matched, ref_offset)

        if best is None or candidate > best:
            best = candidate

    if not best:
        return None

    combined, start_score, coverage, consumed, matched, ref_offset = best
    required = min(4, max(2, len(post_lines) - ref_offset))

    if (
        consumed > 0
        and matched >= required
        and start_score >= 0.50
        and coverage >= 0.52
    ):
        return {
            "score": combined,
            "start_score": start_score,
            "coverage": coverage,
            "consumed": consumed,
            "matched": matched,
            "ref_offset": ref_offset,
        }

    return None


def _skip_following_post_chorus_suffix(lines, pos, post_lines, diagnostics):
    """Skip an already-emitted trusted Post-Chorus suffix after full recovery."""
    k = pos
    while k < len(lines) and lines[k] == "":
        k += 1

    if k >= len(lines) or _section_name(lines[k]) not in ("post-chorus", "post chorus"):
        return pos

    block_start = k
    k += 1
    lyrics = []
    while k < len(lines) and not SECTION_RE.match(lines[k]):
        if lines[k].strip():
            lyrics.append(lines[k])
        k += 1

    if not lyrics:
        return pos

    heard = " ".join(lyrics)
    best = 0.0
    best_count = 0
    for count in range(1, min(len(post_lines), len(lyrics) + 2) + 1):
        suffix = " ".join(post_lines[-count:])
        sc = similarity(suffix, heard)
        if sc > best:
            best = sc
            best_count = count

    if best >= 0.52:
        diagnostics.append(
            f"[POST-CHORUS MERGE] Removed following duplicate trusted suffix "
            f"after full Post-Chorus recovery ({best*100:.1f}%, "
            f"suffix≈{best_count} line(s))."
        )
        return k

    return pos

def _first_reference_chorus_lines(ref_items):
    lines = []
    collecting = False
    for item in ref_items:
        if item.get("kind") == "section":
            if collecting:
                break
            collecting = _section_name(item.get("text")) in ("chorus", "hook", "refrain")
        elif collecting and item.get("kind") == "lyric":
            text = item.get("text", "").strip()
            if text:
                lines.append(text)
    return lines


def _chorus_start_fingerprint(lines, pos, chorus):
    """Score whether lines[pos:] starts another copy of the known Chorus."""
    if len(chorus) < 2:
        return 0.0
    # Later Chorus words cannot validate an unrelated first Verse line.
    if pos >= len(lines) or _local_phrase_evidence(chorus[0], [{"text": lines[pos]}]) < 0.50:
        return 0.0
    target2 = " ".join(chorus[:2])
    target3 = " ".join(chorus[:3]) if len(chorus) >= 3 else target2
    best = 0.0
    for take in range(1, min(3, len(lines) - pos) + 1):
        heard_parts = lines[pos:pos + take]
        if any(SECTION_RE.match(x) for x in heard_parts):
            break
        heard = " ".join(heard_parts)
        rw = set(norm(target2).split())
        hw = set(norm(heard).split())
        coverage = len(rw & hw) / max(1, len(rw))
        score = 0.72 * max(similarity(target2, heard), similarity(target3, heard)) + 0.28 * coverage
        best = max(best, score)
    return best


def _consume_known_chorus(lines, pos, chorus):
    """Align rough ASR to a known Chorus, allowing 1 ASR line = 1-2 ref lines."""
    j = pos
    ri = 0
    while ri < len(chorus) and j < len(lines):
        if SECTION_RE.match(lines[j]):
            break
        best = None
        for rtake in (1, 2):
            if ri + rtake > len(chorus):
                continue
            ref_text = " ".join(chorus[ri:ri + rtake])
            for atake in (1, 2):
                if j + atake > len(lines):
                    continue
                parts = lines[j:j + atake]
                if any(SECTION_RE.match(x) for x in parts):
                    continue
                heard = " ".join(parts)
                rw = set(norm(ref_text).split())
                hw = set(norm(heard).split())
                coverage = len(rw & hw) / max(1, len(rw))
                score = 0.72 * similarity(ref_text, heard) + 0.28 * coverage
                cand = (score, rtake, atake)
                if best is None or cand > best:
                    best = cand
        if not best or best[0] < 0.43:
            break
        ri += best[1]
        j += best[2]
    return j - pos, ri


def _restore_repeated_choruses(output, ref_items, diagnostics):
    """Detect repeated trusted Chorus and its repeated Post-Chorus continuation."""
    chorus = _first_reference_chorus_lines(ref_items)
    post_header, post_lines = _first_reference_post_chorus(ref_items)
    if len(chorus) < 2:
        return output

    rebuilt = []
    current_section = None
    i = 0
    while i < len(output):
        line = output[i]
        if SECTION_RE.match(line):
            current_section = line
            rebuilt.append(line)
            i += 1
            continue

        kind = _section_name(current_section)
        if line and kind not in ("chorus", "hook", "refrain"):
            start_score = _chorus_start_fingerprint(output, i, chorus)
            if start_score >= 0.58:
                consumed, ref_count = _consume_known_chorus(output, i, chorus)
                if consumed and ref_count >= min(4, len(chorus)):
                    while rebuilt and rebuilt[-1] == "":
                        rebuilt.pop()
                    _append_section(rebuilt, "[Chorus]")
                    rebuilt.extend(chorus[:ref_count])
                    diagnostics.append(
                        f"[REPEATED CHORUS {start_score*100:.1f}%] "
                        f"Detected known Chorus inside Sessions-only passage; "
                        f"restored {ref_count}/{len(chorus)} trusted lines."
                    )
                    i += consumed
                    current_section = "[Chorus]"

                    k = i
                    while k < len(output) and output[k] == "":
                        k += 1

                    # V52 (V49.1 baseline): a repeated Post-Chorus has the same structural
                    # recurrence as a Chorus and normally follows it directly.
                    # Check it BEFORE labeling the next material as a Verse.
                    post_hit = None
                    if (
                        post_header
                        and len(post_lines) >= 3
                        and k < len(output)
                        and not SECTION_RE.match(output[k])
                    ):
                        post_hit = _detect_repeated_post_chorus(
                            output, k, post_lines
                        )

                    if post_hit:
                        _append_section(rebuilt, post_header)
                        rebuilt.extend(post_lines)
                        diagnostics.append(
                            f"[REPEATED POST-CHORUS {post_hit['score']*100:.1f}%] "
                            f"Detected trusted Post-Chorus after repeated Chorus; "
                            f"restored {len(post_lines)}/{len(post_lines)} trusted "
                            f"lines (ASR matched {post_hit['matched']} line(s), "
                            f"start offset={post_hit['ref_offset']})."
                        )

                        i = k + post_hit["consumed"]
                        i = _skip_following_post_chorus_suffix(
                            output, i, post_lines, diagnostics
                        )
                        current_section = post_header
                        continue

                    if k < len(output) and not SECTION_RE.match(output[k]):
                        _append_section(rebuilt, "[Verse]")
                        current_section = "[Verse]"
                        diagnostics.append(
                            "[STRUCTURE] Inferred new Sessions verse after repeated Chorus."
                        )
                    continue

        rebuilt.append(line)
        i += 1
    return rebuilt


def _reference_section_blocks(ref_items):
    """Return ordered trusted reference sections with their exact lyric lines."""
    blocks = []
    current = None
    for item in ref_items:
        if item.get("kind") == "section":
            current = {
                "header": item.get("text", ""),
                "kind": _section_name(item.get("text")),
                "lines": [],
            }
            blocks.append(current)
        elif current is not None and item.get("kind") == "lyric":
            text = item.get("text", "").strip()
            if text:
                current["lines"].append(text)
    return blocks


def _block_word_stats(reference_text, heard_text):
    rw = set(norm(reference_text).split())
    hw = set(norm(heard_text).split())
    if not rw or not hw:
        return 0.0, 0.0
    common = len(rw & hw)
    return common / max(1, len(rw)), common / max(1, len(hw))


def _trusted_block_score(reference_lines, heard_lines):
    """Whole-block similarity that tolerates Xingyu merge/split differences."""
    if not reference_lines or not heard_lines:
        return 0.0, 0.0, 0.0, 0.0

    ref_text = " ".join(reference_lines)
    heard_text = " ".join(heard_lines)

    # Whole sections exceed difflib's 200-character autojunk threshold.
    # Common letters must remain evidence rather than being discarded as junk.
    seq = similarity(ref_text, heard_text, autojunk=False)
    coverage, purity = _block_word_stats(ref_text, heard_text)

    ref_start = " ".join(reference_lines[:2])
    heard_start = " ".join(heard_lines[:2])
    start_score = similarity(ref_start, heard_start, autojunk=False)

    score = (
        0.44 * seq
        + 0.28 * coverage
        + 0.20 * purity
        + 0.08 * start_score
    )
    return score, seq, coverage, purity


def _find_timed_trusted_block(source_lines, reference_lines, start_time=0.0, end_time=None):
    """Find a trusted section inside raw Whisper/Xingyu by whole-text family match."""
    if not source_lines or not reference_lines:
        return None

    candidates = [
        x for x in source_lines
        if float(x.get("end", x.get("start", 0.0))) >= start_time
        and (end_time is None or float(x.get("start", 0.0)) <= end_time)
    ]
    if not candidates:
        return None

    best = None
    for i in range(len(candidates)):
        for take in range(1, min(6, len(candidates) - i) + 1):
            heard = [x.get("text", "") for x in candidates[i:i + take]]
            score, seq, cov, pur = _trusted_block_score(reference_lines, heard)

            # Prefer compact matches. A window that drags a large amount of the
            # next section into the candidate should not beat a cleaner block.
            compact_penalty = max(0, take - 4) * 0.012
            final_score = score - compact_penalty

            rec = (
                final_score, seq, cov, pur,
                i, take, candidates[i:i + take]
            )
            if best is None or rec[:4] > best[:4]:
                best = rec

    if not best:
        return None

    score, seq, cov, pur, i, take, chosen = best
    if score < 0.52 or cov < 0.50 or pur < 0.50:
        return None

    return {
        "score": score,
        "sequence": seq,
        "coverage": cov,
        "purity": pur,
        "start": float(chosen[0].get("start", 0.0)),
        "end": float(chosen[-1].get("end", chosen[-1].get("start", 0.0))),
        "count": take,
        "lines": chosen,
    }


def _repair_broken_opening_from_trusted_reference(
    output, ref_items, trans_lines, xingyu_lines, diagnostics
):
    """Restore a skipped Intro/Chorus/Post-Chorus prefix using trusted structure.

    This only activates when the output is structurally broken BEFORE the first
    Verse (for example the first Chorus vanished and output starts in
    Post-Chorus).  It then requires raw ASR evidence for the trusted first
    Chorus.  The main aligner is not modified.
    """
    blocks = _reference_section_blocks(ref_items)
    if not blocks or not output:
        return output

    # Trusted prefix ends when the first Verse begins.
    prefix_blocks = []
    for block in blocks:
        if block["kind"] == "verse":
            break
        if block["lines"]:
            prefix_blocks.append(block)

    chorus_block = next(
        (b for b in prefix_blocks if b["kind"] in ("chorus", "hook", "refrain")),
        None
    )
    if not chorus_block:
        return output

    # Locate first Verse in produced output.
    first_verse_i = None
    for i, line in enumerate(output):
        if _section_name(line) == "verse":
            first_verse_i = i
            break
    if first_verse_i is None:
        return output

    produced_prefix = output[:first_verse_i]
    produced_kinds = [
        _section_name(x) for x in produced_prefix if SECTION_RE.match(x)
    ]

    # Do nothing when the beginning already has a proper trusted Chorus.
    if any(k in ("chorus", "hook", "refrain") for k in produced_kinds):
        return output

    sources = []
    if xingyu_lines:
        sources.append(("Xingyu", xingyu_lines))
    if trans_lines:
        sources.append(("Whisper", trans_lines))

    best_chorus = None
    best_source = None
    for source_name, source in sources:
        hit = _find_timed_trusted_block(
            source, chorus_block["lines"], start_time=0.0, end_time=70.0
        )
        if hit and (best_chorus is None or hit["score"] > best_chorus["score"]):
            best_chorus = hit
            best_source = source_name

    if not best_chorus:
        return output

    # Extra confidence because replacing the whole opening is a strong action.
    if best_chorus["score"] < 0.56 or best_chorus["coverage"] < 0.58:
        return output

    # Check whether a trusted Post-Chorus is supported too. Existing broken
    # output containing [Post-Chorus] is itself useful structural evidence.
    post_block = next(
        (b for b in prefix_blocks if b["kind"] in ("post-chorus", "post chorus")),
        None
    )
    post_supported = False
    post_hit = None
    if post_block:
        if any(k in ("post-chorus", "post chorus") for k in produced_kinds):
            post_supported = True
        else:
            for source_name, source in sources:
                hit = _find_timed_trusted_block(
                    source,
                    post_block["lines"],
                    start_time=max(0.0, best_chorus["start"] + 5.0),
                    end_time=75.0,
                )
                if hit and (post_hit is None or hit["score"] > post_hit["score"]):
                    post_hit = hit
            post_supported = bool(
                post_hit
                and post_hit["score"] >= 0.50
                and post_hit["coverage"] >= 0.50
            )

    rebuilt_prefix = []

    # If the trusted reference has an Intro before Chorus, restore it only when
    # there is actual transcribed activity before the detected Chorus start.
    has_pre_chorus_audio = any(
        float(x.get("start", 0.0)) < best_chorus["start"] - 0.75
        and float(x.get("end", x.get("start", 0.0))) > 0.0
        for x in (trans_lines or [])
    )

    for block in prefix_blocks:
        kind = block["kind"]

        if kind == "intro":
            if not has_pre_chorus_audio:
                continue
        elif kind in ("post-chorus", "post chorus"):
            if not post_supported:
                continue
        elif kind not in ("chorus", "hook", "refrain"):
            # Keep other trusted pre-Verse sections only if they were already
            # present in produced structure. Avoid inventing unusual sections.
            if kind not in produced_kinds:
                continue

        _append_section(rebuilt_prefix, block["header"])
        rebuilt_prefix.extend(block["lines"])

    if not any(
        _section_name(x) in ("chorus", "hook", "refrain")
        for x in rebuilt_prefix if SECTION_RE.match(x)
    ):
        return output

    diagnostics.append(
        f"[TRUSTED OPENING GUARDIAN {best_chorus['score']*100:.1f}% via "
        f"{best_source}] First Chorus was skipped by global alignment; "
        f"rebuilt trusted opening before Verse 1. "
        f"coverage={best_chorus['coverage']*100:.1f}%, "
        f"purity={best_chorus['purity']*100:.1f}%."
    )
    if has_pre_chorus_audio:
        diagnostics.append(
            "[TRUSTED OPENING GUARDIAN] Pre-Chorus audio activity exists, so "
            "trusted Intro was allowed to remain."
        )
    if post_block and post_supported:
        diagnostics.append(
            "[TRUSTED OPENING GUARDIAN] Trusted Post-Chorus restored as part "
            "of the opening family."
        )

    # Preserve everything from the first real Verse onward exactly as built.
    result = list(rebuilt_prefix)
    while result and result[-1] == "":
        result.pop()
    if result:
        result.append("")
    result.extend(output[first_verse_i:])
    return result


def _find_known_block_in_plain_lines(lines, pos, reference_lines, max_take=5):
    """Whole-block detector for rough Sessions text already placed in a Verse."""
    if pos >= len(lines) or not reference_lines:
        return None

    best = None
    for take in range(1, min(max_take, len(lines) - pos) + 1):
        heard = lines[pos:pos + take]
        if any(SECTION_RE.match(x) for x in heard):
            break

        score, seq, cov, pur = _trusted_block_score(reference_lines, heard)
        word_count = len(norm(" ".join(heard)).split())
        if word_count < 8:
            continue

        rec = (score, seq, cov, pur, take)
        if best is None or rec[:4] > best[:4]:
            best = rec

    if not best:
        return None

    score, seq, cov, pur, take = best

    # Designed for ASR where 6 trusted Chorus lines may collapse to 2 rough
    # lines. Require broad block coverage and good candidate purity.
    if score < 0.56 or cov < 0.40 or pur < 0.58:
        return None

    # Beginning of the heard block should resemble the beginning of the Chorus
    # OR contain enough of its first two trusted lines.
    heard_start = " ".join(lines[pos:pos + min(2, take)])
    ref_start = " ".join(reference_lines[:2])
    start_score = similarity(ref_start, heard_start)

    ref_start_words = set(norm(ref_start).split())
    heard_words = set(norm(heard_start).split())
    start_cov = len(ref_start_words & heard_words) / max(1, len(ref_start_words))

    if max(start_score, start_cov) < 0.42:
        return None

    return {
        "score": score,
        "sequence": seq,
        "coverage": cov,
        "purity": pur,
        "take": take,
        "start_score": start_score,
        "start_coverage": start_cov,
    }


def _repair_chorus_postchorus_family_inside_verses(output, ref_items, diagnostics):
    """Borrow no-reference whole-block family logic for reference mode.

    If a long Sessions Verse contains a rough repetition of the trusted Chorus,
    split the Verse at that point and restore the exact trusted Chorus.  Then
    detect the expected repeated Post-Chorus immediately after it.

    This specifically handles cases where Xingyu merged the entire Chorus into
    only 2 lines or missed the first few words, which defeats line-by-line
    matching.
    """
    chorus_header = None
    chorus_lines = []
    post_header, post_lines = _first_reference_post_chorus(ref_items)

    collecting = False
    for item in ref_items:
        if item.get("kind") == "section":
            if collecting:
                break
            if _section_name(item.get("text")) in ("chorus", "hook", "refrain"):
                collecting = True
                chorus_header = item.get("text")
        elif collecting and item.get("kind") == "lyric":
            text = item.get("text", "").strip()
            if text:
                chorus_lines.append(text)

    if not chorus_lines:
        return output

    lines = list(output)
    i = 0

    while i < len(lines):
        if _section_name(lines[i]) != "verse":
            i += 1
            continue

        verse_header = i
        block_end = i + 1
        while block_end < len(lines) and not SECTION_RE.match(lines[block_end]):
            block_end += 1

        # Candidate must leave at least one real Verse lyric before it whenever
        # possible; this avoids relabeling an entire already-correct Verse.
        lyric_positions = [
            p for p in range(verse_header + 1, block_end)
            if lines[p].strip()
        ]
        if not lyric_positions:
            i = block_end
            continue

        hit = None
        hit_pos = None

        for p in lyric_positions:
            cand = _find_known_block_in_plain_lines(
                lines, p, chorus_lines, max_take=5
            )
            if cand and (hit is None or cand["score"] > hit["score"]):
                hit = cand
                hit_pos = p

        if not hit or hit_pos is None:
            i = block_end
            continue

        # Require a meaningful Verse body before the recovered Chorus unless
        # the match is exceptionally strong.
        verse_before = [
            x for x in lines[verse_header + 1:hit_pos] if x.strip()
        ]
        if not verse_before and hit["score"] < 0.66:
            i = block_end
            continue

        consume_end = hit_pos + hit["take"]

        # Build a replacement for this section only.
        replacement = [lines[verse_header]]
        replacement.extend(lines[verse_header + 1:hit_pos])

        while replacement and replacement[-1] == "":
            replacement.pop()

        _append_section(replacement, chorus_header or "[Chorus]")
        replacement.extend(chorus_lines)

        diagnostics.append(
            f"[TRUSTED CHORUS FAMILY {hit['score']*100:.1f}%] "
            f"Split Sessions Verse at rough repeated Chorus; consumed "
            f"{hit['take']} rough line(s), coverage={hit['coverage']*100:.1f}%, "
            f"purity={hit['purity']*100:.1f}%."
        )

        # Try the trusted Post-Chorus directly after the recovered Chorus.
        post_consumed_to = consume_end
        if post_header and len(post_lines) >= 3:
            k = consume_end
            while k < len(lines) and not lines[k].strip():
                k += 1

            if k < len(lines) and not SECTION_RE.match(lines[k]):
                post_hit = _detect_repeated_post_chorus(lines, k, post_lines)
                if post_hit:
                    _append_section(replacement, post_header)
                    replacement.extend(post_lines)
                    post_consumed_to = k + post_hit["consumed"]

                    diagnostics.append(
                        f"[TRUSTED POST-CHORUS FAMILY {post_hit['score']*100:.1f}%] "
                        f"Recovered full trusted Post-Chorus after the repeated "
                        f"Chorus (rough matched {post_hit['matched']} line(s), "
                        f"offset={post_hit['ref_offset']})."
                    )

                    # A later partial trusted Post-Chorus header/suffix may
                    # already exist because the global aligner anchored only its
                    # final lines. Remove that duplicate suffix.
                    post_consumed_to = _skip_following_post_chorus_suffix(
                        lines, post_consumed_to, post_lines, diagnostics
                    )

        # Preserve any remainder before the next original section header.
        remainder_start = post_consumed_to
        remainder = lines[remainder_start:block_end]

        # If we recovered a Post-Chorus and nothing meaningful remains, this
        # Verse ends there. Otherwise keep leftover material as a new Verse.
        if any(x.strip() for x in remainder):
            _append_section(replacement, "[Verse]")
            replacement.extend(remainder)

        lines = (
            lines[:verse_header]
            + replacement
            + lines[block_end:]
        )

        # Restart slightly after the replacement; formatting/renumbering later
        # will clean all Verse numbers.
        i = verse_header + len(replacement)

    return lines


def _reference_post_chorus_variants(ref_items):
    """Collect every trusted Post-Chorus occurrence from the shorter reference."""
    variants = []
    current_header = None
    current_lines = None

    def flush():
        nonlocal current_header, current_lines
        if current_header and current_lines:
            variants.append((current_header, list(current_lines)))
        current_header = None
        current_lines = None

    for item in ref_items:
        if item.get("kind") == "section":
            if current_header is not None:
                flush()

            if _section_name(item.get("text")) in ("post-chorus", "post chorus"):
                current_header = item.get("text")
                current_lines = []

        elif current_header is not None and item.get("kind") == "lyric":
            text = item.get("text", "").strip()
            if text:
                current_lines.append(text)

    if current_header is not None:
        flush()

    return variants


def _repair_truncated_post_chorus_before_verse(output, ref_items, diagnostics):
    """Move a rough ASR Post-Chorus tail out of the following Verse.

    This is deliberately a *post-processing* repair.  It never changes the
    reference alignment itself.

    Safe pattern required:
      [Post-Chorus]  -> looks like a trusted Post-Chorus PREFIX
      [Verse]        -> its first 1-3 rough lines match the missing trusted SUFFIX

    Then:
      - append the exact trusted missing suffix to Post-Chorus
      - remove only those matching rough lines from the start of Verse
      - leave the real new Verse beginning untouched

    This fixes cases such as:
      "30 on me while I suck codeine and smoke drugs."
      "I got my life on the money."
    being placed at the start of Verse even though they are rough versions of
    the final two trusted Post-Chorus lines.
    """
    variants = _reference_post_chorus_variants(ref_items)
    if not variants:
        return output

    lines = list(output)
    i = 0

    while i < len(lines):
        if _section_name(lines[i]) not in ("post-chorus", "post chorus"):
            i += 1
            continue

        # Collect current Post-Chorus lyrics up to the next structural header.
        j = i + 1
        post_lyrics = []
        while j < len(lines) and not SECTION_RE.match(lines[j]):
            if lines[j].strip():
                post_lyrics.append(lines[j])
            j += 1

        if j >= len(lines) or _section_name(lines[j]) != "verse":
            i = j
            continue

        verse_header_index = j

        # First actual Verse lyric positions, allowing blank lines.
        verse_positions = []
        k = verse_header_index + 1
        while k < len(lines) and not SECTION_RE.match(lines[k]):
            if lines[k].strip():
                verse_positions.append(k)
                if len(verse_positions) >= 3:
                    break
            k += 1

        if not verse_positions:
            i = j + 1
            continue

        best = None

        for variant_header, variant_lines in variants:
            missing_count = len(variant_lines) - len(post_lyrics)

            # Very conservative: only repair a short missing tail.
            if missing_count < 1 or missing_count > 3:
                continue

            trusted_prefix = variant_lines[:len(post_lyrics)]
            trusted_suffix = variant_lines[len(post_lyrics):]

            if not trusted_prefix or not trusted_suffix:
                continue

            prefix_score = similarity(
                " ".join(trusted_prefix),
                " ".join(post_lyrics),
            )
            if prefix_score < 0.58:
                continue

            suffix_text = " ".join(trusted_suffix)
            suffix_words = set(norm(suffix_text).split())
            if not suffix_words:
                continue

            for take in range(1, min(3, len(verse_positions)) + 1):
                rough_lines = [lines[p] for p in verse_positions[:take]]
                rough_text = " ".join(rough_lines)

                sim = similarity(suffix_text, rough_text)
                rough_words = set(norm(rough_text).split())
                coverage = len(suffix_words & rough_words) / max(1, len(suffix_words))

                score = 0.72 * sim + 0.28 * coverage

                candidate = (
                    score,
                    sim,
                    coverage,
                    prefix_score,
                    take,
                    variant_header,
                    variant_lines,
                    trusted_suffix,
                    rough_lines,
                )
                if best is None or candidate[:4] > best[:4]:
                    best = candidate

        if best is None:
            i = j + 1
            continue

        (
            score,
            sim,
            coverage,
            prefix_score,
            take,
            variant_header,
            variant_lines,
            trusted_suffix,
            rough_lines,
        ) = best

        # Strong evidence required because we are moving lines across a section
        # boundary.  In the Buck 50 example the combined match is comfortably
        # above this threshold.
        if score < 0.56 or sim < 0.52 or coverage < 0.42:
            i = j + 1
            continue

        # Remove ONLY the rough Verse-prefix lines that matched the missing
        # Post-Chorus suffix.  Delete from the end so indexes remain valid.
        consumed_positions = verse_positions[:take]
        for pos in reversed(consumed_positions):
            del lines[pos]

        # Find the Verse header again after deletion.
        verse_header_index = None
        scan = i + 1
        while scan < len(lines):
            if SECTION_RE.match(lines[scan]):
                verse_header_index = scan
                break
            scan += 1

        if verse_header_index is None:
            break

        # Insert exact trusted wording immediately before the blank/header that
        # starts the Verse.
        insert_pos = verse_header_index
        while insert_pos > i + 1 and not lines[insert_pos - 1].strip():
            insert_pos -= 1

        for offset, trusted_line in enumerate(trusted_suffix):
            lines.insert(insert_pos + offset, trusted_line)

        diagnostics.append(
            f"[POST-CHORUS TAIL FIX {score*100:.1f}%] "
            f"Recovered {len(trusted_suffix)} trusted tail line(s) before "
            f"the following Verse; removed {take} rough ASR line(s) from "
            f"Verse start. Prefix={prefix_score*100:.1f}%, "
            f"suffix similarity={sim*100:.1f}%, coverage={coverage*100:.1f}%."
        )
        for trusted_line in trusted_suffix:
            diagnostics.append(
                f"[POST-CHORUS TRUSTED TAIL] {trusted_line}"
            )
        for rough_line in rough_lines:
            diagnostics.append(
                f"[POST-CHORUS ROUGH REMOVED] {rough_line}"
            )

        # Continue after this repaired section.
        i = verse_header_index + len(trusted_suffix) + 1

    return lines


def _merge_adjacent_partial_chorus_artifacts(output, ref_items, diagnostics):
    """Collapse: short Chorus + empty Verse + full repeated Chorus."""
    chorus = _first_reference_chorus_lines(ref_items)
    if not chorus:
        return output

    lines = list(output)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(lines):
            if _section_name(lines[i]) not in ("chorus", "hook", "refrain"):
                i += 1
                continue

            a_start = i
            a_lyrics = []
            j = i + 1
            while j < len(lines) and not SECTION_RE.match(lines[j]):
                if lines[j].strip():
                    a_lyrics.append(lines[j])
                j += 1

            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1

            if k < len(lines) and _section_name(lines[k]) == "verse":
                q = k + 1
                while q < len(lines) and not lines[q].strip():
                    q += 1
                # Only remove a Verse that has zero lyrics.
                if q < len(lines) and SECTION_RE.match(lines[q]):
                    k = q
                else:
                    i = j
                    continue

            if k >= len(lines) or _section_name(lines[k]) not in ("chorus", "hook", "refrain"):
                i = j
                continue

            b_lyrics = []
            b_end = k + 1
            while b_end < len(lines) and not SECTION_RE.match(lines[b_end]):
                if lines[b_end].strip():
                    b_lyrics.append(lines[b_end])
                b_end += 1

            if not a_lyrics or len(a_lyrics) > 2 or not b_lyrics:
                i = j
                continue

            prefix_score = similarity(
                " ".join(chorus[:len(a_lyrics)]), " ".join(a_lyrics)
            )
            second_score = similarity(chorus[0], b_lyrics[0])

            if prefix_score >= 0.62 and second_score >= 0.58:
                lines[a_start:b_end] = ["[Chorus]"] + b_lyrics
                diagnostics.append(
                    f"[CHORUS MERGE] Removed {len(a_lyrics)}-line duplicate "
                    f"Chorus and empty inferred Verse before full Chorus."
                )
                changed = True
                break

            i = j

    return lines


def _is_thanks_hallucination(text):
    """Common end-of-audio ASR hallucinations; only used on the trailing tail."""
    t = norm(text)
    if not t:
        return False
    starters = (
        "thank", "thanks", "thank you", "thank you very much",
        "thanks for watching", "thank you for watching",
        "thanks for listening", "thank you for listening"
    )
    return (
        any(t == x or t.startswith(x + " ") for x in starters)
        or t.endswith(" thank you")
        or t.endswith(" thanks")
    )


def _trim_full_mode_trailing_hallucinations(source, diagnostics):
    """Remove only trailing 'thank/thanks...' lines, never occurrences mid-song."""
    removed = []
    while source and _is_thanks_hallucination(source[-1]["text"]):
        removed.append(source.pop()["text"])
    if removed:
        diagnostics.append(
            "[END HALLUCINATION] Removed trailing ASR line(s): "
            + " | ".join(reversed(removed))
        )
    return source


def _chorus_word_coverage(reference, candidate):
    rw = set(norm(reference).split())
    cw = set(norm(candidate).split())
    return len(rw & cw) / max(1, len(rw))


def _chorus_word_purity(reference, candidate):
    rw = set(norm(reference).split())
    cw = set(norm(candidate).split())
    return len(rw & cw) / max(1, len(cw))


def _split_text_like_two_reference_lines(text, left_ref, right_ref):
    """Split one merged ASR line using two cleaner repeated Chorus lines."""
    spans = list(re.finditer(r"[A-Za-z0-9']+", text or ""))
    if len(spans) < 7:
        return None

    best = None
    for cut in range(3, len(spans) - 2):
        char_pos = spans[cut].start()
        left = text[:char_pos].strip(" ,")
        right = text[char_pos:].strip()
        if len(norm(left).split()) < 3 or len(norm(right).split()) < 3:
            continue

        ls = similarity(left, left_ref)
        rs = similarity(right, right_ref)
        score = 0.5 * ls + 0.5 * rs
        if best is None or score > best[0]:
            best = (score, left, right, ls, rs)

    if best and best[0] >= 0.62 and best[3] >= 0.54 and best[4] >= 0.54:
        return best
    return None


def _consensus_split_canonical_chorus(canonical_lines, occurrence_blocks, diagnostics):
    """Use repeated Chorus copies to repair merged line breaks.

    Example learned from the supplied songs:
      one ASR copy:  "Mama told me ... same All they do ... cocaine"
      other copies:  "Mama told me ... same"
                     "All they do ... cocaine"

    We keep the canonical WORDING, but borrow the line boundary supported by
    another occurrence.  Verse lines are never touched.
    """
    if not canonical_lines or len(occurrence_blocks) < 2:
        return canonical_lines

    current = list(canonical_lines)
    changed = True
    passes = 0

    while changed and passes < 3:
        changed = False
        passes += 1
        rebuilt = []

        for line in current:
            best_support = None

            for block in occurrence_blocks:
                for i in range(len(block) - 1):
                    a = block[i].strip()
                    b = block[i + 1].strip()
                    if len(norm(a).split()) < 3 or len(norm(b).split()) < 3:
                        continue

                    joined = a + " " + b
                    merged_score = similarity(line, joined)
                    if merged_score < 0.72:
                        continue

                    split = _split_text_like_two_reference_lines(line, a, b)
                    if not split:
                        continue

                    split_score, left, right, ls, rs = split
                    candidate = (
                        0.72 * merged_score + 0.28 * split_score,
                        left, right, merged_score, split_score
                    )
                    if best_support is None or candidate[0] > best_support[0]:
                        best_support = candidate

            if best_support and len(norm(line).split()) >= 8:
                _, left, right, merged_score, split_score = best_support
                rebuilt.extend([left, right])
                diagnostics.append(
                    f"[CHORUS LINE CONSENSUS] Split merged canonical line "
                    f"({merged_score*100:.1f}% repeated-block match, "
                    f"{split_score*100:.1f}% split confidence): {line}"
                )
                changed = True
            else:
                rebuilt.append(line)

        current = rebuilt

    return current


def _chorus_template_variants(block):
    """Full Chorus plus conservative one-line edge omissions.

    This models examples such as Cold Winters, where the last Chorus can omit
    its first lyric while the rest of the repeated group remains the same.
    """
    variants = [("full", 0, 0, list(block))]
    if len(block) >= 5:
        variants.append(("missing-first", 1, 0, list(block[1:])))
        variants.append(("missing-last", 0, 1, list(block[:-1])))
    return variants


def _full_mode_extra_chorus_ranges(texts, seed_ranges, diagnostics):
    """Find additional members of the same Chorus family.

    V49 rules learned from the supplied correct songs:
    - prefer a whole repeated block, not a tiny repeated sub-hook;
    - line count may differ because Xingyu merges/splits phrases;
    - one edge lyric may genuinely be omitted in a later Chorus;
    - one repeated lyric alone is never enough to define a Chorus.
    """
    if len(seed_ranges) < 2:
        return seed_ranges

    n = len(texts)
    seed_ranges = sorted(seed_ranges)
    seed_blocks = [
        texts[st:en] for st, en in seed_ranges
        if 0 <= st < en <= n
    ]
    if len(seed_blocks) < 2:
        return seed_ranges

    variants = []
    for block_no, block in enumerate(seed_blocks, 1):
        for kind, miss_pre, miss_post, lines in _chorus_template_variants(block):
            if len(lines) < 3:
                continue
            joined = " ".join(lines)
            variants.append({
                "block_no": block_no,
                "kind": kind,
                "miss_pre": miss_pre,
                "miss_post": miss_post,
                "lines": lines,
                "joined": joined,
                "start": " ".join(lines[:2]),
                "end": " ".join(lines[-2:]),
            })

    def overlaps(st, en, ranges):
        return any(st < r_en and en > r_st for r_st, r_en in ranges)

    seed_len = max(1, round(sum(len(x) for x in seed_blocks) / len(seed_blocks)))
    min_len = max(3, seed_len - 4)
    max_len = min(14, seed_len + 4)
    candidates = []

    for st in range(n):
        for take in range(min_len, max_len + 1):
            en = st + take
            if en > n or overlaps(st, en, seed_ranges):
                continue

            cand_lines = texts[st:en]
            cand = " ".join(cand_lines)

            best_variant = None
            for v in variants:
                whole = similarity(v["joined"], cand)
                cov = _chorus_word_coverage(v["joined"], cand)
                purity = _chorus_word_purity(v["joined"], cand)

                cand_start1 = cand_lines[0]
                cand_start2 = " ".join(cand_lines[:2])
                cand_end1 = cand_lines[-1]
                cand_end2 = " ".join(cand_lines[-2:])

                start_score = max(
                    similarity(v["start"], cand_start1),
                    similarity(v["start"], cand_start2),
                )
                end_score = max(
                    similarity(v["end"], cand_end1),
                    similarity(v["end"], cand_end2),
                )

                score = (
                    0.46 * whole
                    + 0.22 * cov
                    + 0.11 * purity
                    + 0.105 * start_score
                    + 0.105 * end_score
                )

                # A partial edge variant must be extremely strong in the rest
                # of the block; otherwise we prefer the full family member.
                if v["kind"] != "full":
                    score -= 0.015

                rec = (
                    score, whole, cov, purity, start_score, end_score,
                    st, en, v["kind"], v["miss_pre"], v["miss_post"]
                )
                if best_variant is None or rec > best_variant:
                    best_variant = rec

            if not best_variant:
                continue

            (
                score, whole, cov, purity, start_score, end_score,
                st2, en2, kind, miss_pre, miss_post
            ) = best_variant

            full_ok = (
                score >= 0.78 and whole >= 0.69 and cov >= 0.75
                and purity >= 0.71 and start_score >= 0.56 and end_score >= 0.56
            )
            partial_ok = (
                kind != "full"
                and score >= 0.80 and whole >= 0.74 and cov >= 0.82
                and purity >= 0.76 and start_score >= 0.60 and end_score >= 0.60
            )

            if full_ok or partial_ok:
                candidates.append(best_variant)

    candidates.sort(reverse=True)
    selected = list(seed_ranges)
    extras = []

    for cand in candidates:
        st, en = cand[6], cand[7]
        if overlaps(st, en, selected):
            continue
        selected.append((st, en))
        extras.append(cand)

    selected.sort()

    for cand in sorted(extras, key=lambda x: x[6]):
        (
            score, whole, cov, purity, start_sc, end_sc,
            st, en, kind, miss_pre, miss_post
        ) = cand
        diagnostics.append(
            f"[EXTRA CHORUS {score*100:.1f}%] family={kind} "
            f"segments {st+1}-{en}; whole={whole*100:.1f}%, "
            f"coverage={cov*100:.1f}%, start={start_sc*100:.1f}%, "
            f"end={end_sc*100:.1f}%."
        )

    return selected

def _refine_chorus_ranges_and_choose_canonical(texts, seed_ranges, all_ranges, diagnostics):
    """Refine Chorus family ranges and choose the cleanest common line layout.

    Unlike V45/V49, V49 allows a later Chorus to be a strong PARTIAL family
    member (one missing first/last lyric), instead of hallucinating the omitted
    line back into the song.
    """
    if not all_ranges:
        return all_ranges, [], {}

    def joined(st, en):
        return " ".join(texts[st:en]).strip()

    def wc(text):
        return len(norm(text).split())

    usable_seeds = [
        (st, en) for st, en in seed_ranges
        if 0 <= st < en <= len(texts)
    ] or list(all_ranges)

    template_range = max(usable_seeds, key=lambda r: wc(joined(*r)))
    template_block = list(texts[template_range[0]:template_range[1]])

    if not template_block:
        return all_ranges, [], {}

    template_variants = _chorus_template_variants(template_block)

    refined_meta = []

    for occ_index, (approx_st, approx_en) in enumerate(sorted(all_ranges), 1):
        best = None
        st_lo = max(0, approx_st - 3)
        st_hi = min(len(texts) - 1, approx_st + 3)

        for st in range(st_lo, st_hi + 1):
            for take in range(2, 15):
                en = st + take
                if en > len(texts):
                    break

                cand_lines = texts[st:en]
                cand = " ".join(cand_lines).strip()
                if not cand:
                    continue

                for kind, miss_pre, miss_post, vlines in template_variants:
                    ref = " ".join(vlines)
                    rw = norm(ref).split()
                    if not rw:
                        continue

                    whole = similarity(ref, cand)
                    cov = _chorus_word_coverage(ref, cand)
                    pur = _chorus_word_purity(ref, cand)

                    start_probe = " ".join(rw[:min(10, len(rw))])
                    end_probe = " ".join(rw[-min(10, len(rw)):])

                    cstart1 = cand_lines[0]
                    cstart2 = " ".join(cand_lines[:2])
                    cend1 = cand_lines[-1]
                    cend2 = " ".join(cand_lines[-2:])

                    start_sc = max(
                        similarity(start_probe, cstart1),
                        similarity(start_probe, cstart2),
                    )
                    end_sc = max(
                        similarity(end_probe, cend1),
                        similarity(end_probe, cend2),
                    )

                    score = (
                        0.43 * whole
                        + 0.25 * cov
                        + 0.10 * pur
                        + 0.11 * start_sc
                        + 0.11 * end_sc
                    )

                    if kind != "full":
                        score -= 0.012

                    if start_sc < 0.40:
                        score -= 0.10
                    if end_sc < 0.40:
                        score -= 0.10

                    rec = (
                        score, whole, cov, pur, start_sc, end_sc,
                        st, en, kind, miss_pre, miss_post
                    )
                    if best is None or rec > best:
                        best = rec

        if best is None:
            refined_meta.append({
                "st": approx_st, "en": approx_en,
                "kind": "full", "miss_pre": 0, "miss_post": 0,
                "score": 0.0,
            })
            continue

        (
            score, whole, cov, pur, start_sc, end_sc,
            st, en, kind, miss_pre, miss_post
        ) = best

        # Do not accept a partial edge merely because it is a little better.
        # Require strong evidence in the remaining repeated material.
        if kind != "full" and not (
            whole >= 0.72 and cov >= 0.80 and pur >= 0.74
            and start_sc >= 0.56 and end_sc >= 0.56
        ):
            kind = "full"
            miss_pre = miss_post = 0

        refined_meta.append({
            "st": st, "en": en, "kind": kind,
            "miss_pre": miss_pre, "miss_post": miss_post,
            "score": score,
            "whole": whole, "cov": cov, "pur": pur,
            "start_sc": start_sc, "end_sc": end_sc,
        })

        diagnostics.append(
            f"[CHORUS RANGE #{occ_index}] {approx_st+1}-{approx_en} -> "
            f"{st+1}-{en} | family={kind} | score={score*100:.1f}% | "
            f"coverage={cov*100:.1f}% | start={start_sc*100:.1f}% | "
            f"end={end_sc*100:.1f}%."
        )

    refined_meta.sort(key=lambda x: x["st"])
    clean = []
    for meta in refined_meta:
        if clean and meta["st"] < clean[-1]["en"]:
            meta = dict(meta)
            meta["st"] = clean[-1]["en"]
        if meta["st"] < meta["en"]:
            clean.append(meta)
    refined_meta = clean

    if not refined_meta:
        return all_ranges, [], {}

    occurrence_blocks = [
        list(texts[m["st"]:m["en"]]) for m in refined_meta
    ]

    # Prefer a complete family member for canonical text/layout.
    full_indices = [
        i for i, m in enumerate(refined_meta)
        if m["kind"] == "full"
    ]
    candidate_indices = full_indices or list(range(len(refined_meta)))

    occurrence_texts = [
        joined(m["st"], m["en"]) for m in refined_meta
    ]
    max_wc = max(wc(t) for t in occurrence_texts) or 1

    best_can = None
    for i in candidate_indices:
        text = occurrence_texts[i]
        sims = [
            similarity(text, other)
            for j, other in enumerate(occurrence_texts)
            if j != i
        ]
        consensus = sum(sims) / max(1, len(sims))
        completeness = wc(text) / max_wc
        interior_bonus = 0.02 if 0 < i < len(refined_meta) - 1 else 0.0

        # Prefer a stable complete copy over an unusually short/high-sim copy.
        score = 0.70 * consensus + 0.30 * completeness + interior_bonus
        rec = (score, consensus, completeness, i)
        if best_can is None or rec > best_can:
            best_can = rec

    _, consensus, completeness, canonical_i = best_can
    canonical_lines = list(occurrence_blocks[canonical_i])

    # Repair only Chorus line breaks by voting across its repetitions.
    canonical_lines = _consensus_split_canonical_chorus(
        canonical_lines, occurrence_blocks, diagnostics
    )

    diagnostics.append(
        f"[CANONICAL CHORUS] Using Chorus #{canonical_i+1} as common wording/"
        f"layout for full family members (consensus={consensus*100:.1f}%, "
        f"completeness={completeness*100:.1f}%)."
    )
    diagnostics.append("[CANONICAL CHORUS LINES]")
    for i, line in enumerate(canonical_lines, 1):
        diagnostics.append(f"[CANONICAL {i:02d}] {line}")

    # V49: recover accidentally excluded edge lyrics using the canonical
    # first/last line. A partial Chorus is only allowed to remain partial when
    # the adjacent raw segment does NOT match the omitted edge lyric.
    recovered_meta = []
    for m in refined_meta:
        m = dict(m)

        if m["kind"] == "missing-last" and canonical_lines and m["en"] < len(texts):
            next_line = texts[m["en"]]
            last_ref = canonical_lines[-1]
            sc = similarity(last_ref, next_line)

            # Require a clear match. In the No coaster trace this catches:
            # "My brother got a gun on him..." and
            # "My brother got it goin' on with him..."
            if sc >= 0.56:
                old_en = m["en"]
                m["en"] += 1
                m["kind"] = "full"
                m["miss_post"] = 0
                diagnostics.append(
                    f"[CHORUS EDGE RECOVERY] Extended Chorus end from source "
                    f"segment {old_en} to {m['en']} because next segment matches "
                    f"canonical final lyric ({sc*100:.1f}%): {next_line}"
                )

        if m["kind"] == "missing-first" and canonical_lines and m["st"] > 0:
            prev_line = texts[m["st"] - 1]
            first_ref = canonical_lines[0]
            sc = similarity(first_ref, prev_line)
            if sc >= 0.56:
                old_st = m["st"]
                m["st"] -= 1
                m["kind"] = "full"
                m["miss_pre"] = 0
                diagnostics.append(
                    f"[CHORUS EDGE RECOVERY] Extended Chorus start from source "
                    f"segment {old_st+1} to {m['st']+1} because previous segment "
                    f"matches canonical first lyric ({sc*100:.1f}%): {prev_line}"
                )

        recovered_meta.append(m)

    refined_meta = sorted(recovered_meta, key=lambda x: x["st"])

    # Resolve accidental overlaps again after edge recovery.
    clean2 = []
    for m in refined_meta:
        if clean2 and m["st"] < clean2[-1]["en"]:
            # Keep the earlier Chorus intact and move only the later start.
            m = dict(m)
            m["st"] = clean2[-1]["en"]
        if m["st"] < m["en"]:
            clean2.append(m)
    refined_meta = clean2

    render_map = {}
    for m in refined_meta:
        lines = list(canonical_lines)

        # Preserve a REAL one-line edge omission only if V49 did not recover
        # an adjacent matching canonical edge lyric.
        if m["miss_pre"] and len(lines) > m["miss_pre"]:
            lines = lines[m["miss_pre"]:]
        if m["miss_post"] and len(lines) > m["miss_post"]:
            lines = lines[:-m["miss_post"]]

        key = (m["st"], m["en"])
        render_map[key] = lines

        if m["kind"] != "full":
            diagnostics.append(
                f"[PARTIAL CHORUS] segments {m['st']+1}-{m['en']} rendered as "
                f"{m['kind']} family member; omitted edge lyric was NOT invented."
            )

    refined = [(m["st"], m["en"]) for m in refined_meta]
    return refined, canonical_lines, render_map

def _append_detailed_asr_trace(diagnostics, trans_lines, xingyu_lines):
    """Append a full, human-readable trace of both ASR sources.

    The Result+Log testing export should make it possible to compare exactly
    what Xingyu heard versus what Whisper grouped, without changing either
    source's text.  This is diagnostic-only.
    """
    diagnostics.append("")
    diagnostics.append("================ DETAILED ASR TRACE ================")

    if xingyu_lines:
        diagnostics.append(
            f"[TRACE] XINGYU RAW SEGMENTS: {len(xingyu_lines)}"
        )
        for i, x in enumerate(xingyu_lines, 1):
            text = re.sub(r"\s+", " ", x.get("text", "")).strip()
            st = float(x.get("start", 0.0))
            en = float(x.get("end", st))
            diagnostics.append(
                f"[XINGYU {i:03d}] {st:8.2f}s - {en:8.2f}s | {text}"
            )
    else:
        diagnostics.append("[TRACE] XINGYU RAW SEGMENTS: none")

    diagnostics.append("")
    if trans_lines:
        diagnostics.append(
            f"[TRACE] WHISPER GROUPED PHRASES: {len(trans_lines)}"
        )
        for i, x in enumerate(trans_lines, 1):
            text = re.sub(r"\s+", " ", x.get("text", "")).strip()
            st = float(x.get("start", 0.0))
            en = float(x.get("end", st))
            diagnostics.append(
                f"[WHISPER {i:03d}] {st:8.2f}s - {en:8.2f}s | {text}"
            )
    else:
        diagnostics.append("[TRACE] WHISPER GROUPED PHRASES: none")

    diagnostics.append("============== END DETAILED ASR TRACE ==============")
    diagnostics.append("")

def _full_transcription_structure(trans_lines, xingyu_lines=None):
    """No-reference mode: transcribe whole song and infer repeated Chorus boundaries."""
    diagnostics = []
    source = []
    raw = xingyu_lines if xingyu_lines else trans_lines
    source_name = "Xingyu" if xingyu_lines else "Whisper"

    for x in raw:
        text = re.sub(r"\s+", " ", x.get("text", "")).strip()
        if text:
            source.append({
                "text": text,
                "start": float(x.get("start", 0.0)),
                "end": float(x.get("end", x.get("start", 0.0))),
            })

    diagnostics.append(
        f"[FULL TRANSCRIPTION] No reference pasted. Using {len(source)} "
        f"{source_name} segments as primary lyrics."
    )
    source = _trim_full_mode_trailing_hallucinations(source, diagnostics)

    # V49: diagnostics only.  Dump both engines exactly as they were received
    # so Result + Log shows why a line was present/missing in one engine.
    _append_detailed_asr_trace(diagnostics, trans_lines, xingyu_lines)

    if not source:
        return [], diagnostics

    texts = [x["text"] for x in source]
    n = len(texts)

    # Find repeated sequences using flexible local alignment. Unlike V36/V49,
    # the Chorus is allowed to start several lines BEFORE the strongest exact
    # 3-line fingerprint. This matters when ASR line breaks/wording differ.
    best = None
    for a in range(n):
        for b in range(a + 6, n):
            # Search forward for a compact repeated core.
            pairs = []
            ia, ib = a, b
            while ia < min(n, a + 12) and ib < min(n, b + 12):
                sc = similarity(texts[ia], texts[ib])
                if sc >= 0.50:
                    pairs.append((ia, ib, sc))
                    ia += 1; ib += 1
                else:
                    # tolerate one segmentation mismatch on either side
                    opts = []
                    if ia+1 < n:
                        opts.append((similarity(texts[ia+1], texts[ib]), ia+1, ib))
                    if ib+1 < n:
                        opts.append((similarity(texts[ia], texts[ib+1]), ia, ib+1))
                    if opts:
                        osc, nia, nib = max(opts)
                        if osc >= 0.56:
                            ia, ib = nia, nib
                            continue
                    break
            if len(pairs) >= 3:
                avg = sum(p[2] for p in pairs)/len(pairs)
                # Prefer the longest coherent repeated block.  This protects songs
                # whose Chorus contains its own smaller repeated half (e.g. 4/5-line
                # internal repetitions) from being mistaken for the whole Chorus.
                time_gap = max(0.0, source[b]["start"] - source[a]["start"])
                distance_bonus = min(0.10, time_gap / 600.0)
                cand=(avg + .034*len(pairs) + distance_bonus, pairs)
                if best is None or cand[0] > best[0]:
                    best=cand

    if best is None:
        diagnostics.append(
            "[STRUCTURE] No reliable repeated Chorus found; returning clean "
            "transcription without invented section labels."
        )
        return texts, diagnostics

    pairs=best[1]
    core_a=pairs[0][0]
    core_b=pairs[0][1]
    end_a=pairs[-1][0]+1
    end_b=pairs[-1][1]+1

    # Expand BACKWARD from the repeated core. Require similarity, but allow
    # weaker ASR wording. This is what recovers lines such as "Mama told me..."
    # that occur before the previously detected 3-line core.
    back=0
    while core_a>0 and core_b>0 and back<6:
        sc=similarity(texts[core_a-1], texts[core_b-1])
        if sc < 0.43:
            break
        core_a-=1
        core_b-=1
        back+=1

    # Expand forward similarly so both occurrences share the same end boundary.
    fwd=0
    while end_a<n and end_b<n and fwd<4:
        sc=similarity(texts[end_a], texts[end_b])
        if sc < 0.43:
            break
        end_a+=1
        end_b+=1
        fwd+=1

    # Normalize both Chorus occurrences to the same structural span length.
    # Keep each occurrence's own transcription wording; without a trusted
    # reference we must not fabricate one version as ground truth.
    chorus_len=min(end_a-core_a, end_b-core_b)
    end_a=core_a+chorus_len
    end_b=core_b+chorus_len

    diagnostics.append(
        f"[STRUCTURE] Repeated Chorus expanded from matching core: "
        f"{chorus_len} segment(s), 2 occurrence(s), "
        f"backward expansion={back}."
    )

    seed_ranges=[(core_a,end_a),(core_b,end_b)]
    ranges = _full_mode_extra_chorus_ranges(texts, seed_ranges, diagnostics)
    ranges, canonical_chorus, chorus_render_map = (
        _refine_chorus_ranges_and_choose_canonical(
            texts, seed_ranges, ranges, diagnostics
        )
    )
    out=[]
    cursor=0
    verse_no=1
    for cs,ce in ranges:
        if cs<cursor:
            continue
        before=texts[cursor:cs]
        if before:
            if cursor == 0:
                # From the supplied reference songs: an Intro can contain many
                # short ad-lib lines, while a song may also start directly with
                # Chorus.  Use EARLY AUDIO POSITION, not a 3-line limit.
                first_chorus_time = source[cs]["start"] if cs < len(source) else 999.0
                pre_start = source[0]["start"] if source else 0.0
                pre_duration = max(0.0, first_chorus_time - pre_start)

                if first_chorus_time <= 22.5 and pre_duration <= 22.5:
                    _append_section(out, "[Intro]")
                    diagnostics.append(
                        f"[STRUCTURE] Opening material classified as Intro by "
                        f"early-Chorus timing ({pre_duration:.1f}s before Chorus)."
                    )
                else:
                    _append_section(out, f"[Verse {verse_no}]")
                    verse_no += 1
                    diagnostics.append(
                        f"[STRUCTURE] Opening material kept as Verse because first "
                        f"Chorus begins later ({first_chorus_time:.1f}s)."
                    )
            else:
                _append_section(out,f"[Verse {verse_no}]")
                verse_no+=1
            out.extend(before)
        _append_section(out,"[Chorus]")
        # Full family members share the canonical wording/layout.  A strongly
        # detected one-line edge omission remains partial (Cold-Winters-style)
        # instead of inventing a lyric that was not sung in that occurrence.
        render_lines = chorus_render_map.get((cs, ce))
        if render_lines:
            out.extend(render_lines)
        elif canonical_chorus:
            out.extend(canonical_chorus)
        else:
            out.extend(texts[cs:ce])
        cursor=ce

    if cursor<n:
        tail=texts[cursor:]
        if tail:
            _append_section(out,f"[Verse {verse_no}]")
            out.extend(tail)

    return out, diagnostics

def _guard_trusted_repeated_sections(output, ref_items, diagnostics):
    """Keep confirmed repeated blocks intact, without moving actual verses."""
    reference_blocks = _reference_section_blocks(ref_items)
    kinds = ("chorus", "hook", "refrain", "post-chorus", "post chorus")
    templates = []
    for block in reference_blocks:
        if block["kind"] not in kinds or len(block["lines"]) < 2:
            continue
        supporters = [other for other in reference_blocks
                      if other["kind"] == block["kind"]
                      and len(other["lines"]) == len(block["lines"])
                      and similarity(" ".join(block["lines"]), " ".join(other["lines"])) >= 0.90]
        if len(supporters) >= 2:
            templates.append(block)
    if not templates:
        return output

    # Opening ASR belongs inside Intro, before its trusted reference lines.
    first_header = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    preamble = list(output[:first_header])
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first_header:])))
    if preamble and blocks and blocks[0]["kind"] == "intro":
        blocks[0]["lines"] = [line for line in preamble if line.strip()] + blocks[0]["lines"]
        diagnostics.append("[TRUSTED INTRO] Kept opening ASR inside Intro.")
        preamble = []

    def duplicate(line, trusted):
        return max((similarity(line, text) for text in trusted), default=0.0) >= 0.45

    result = preamble
    previous_kind = ""
    for block_index, block in enumerate(blocks):
        chosen = None
        if block["kind"] in kinds:
            # Require every trusted line, in order, already present verbatim.
            # This removes only extra duplicates; it cannot invent a missing line.
            for template in templates:
                if template["kind"] != block["kind"]:
                    continue
                cursor, positions = 0, []
                for text in template["lines"]:
                    position = next((i for i in range(cursor, len(block["lines"]))
                                     if block["lines"][i] == text), None)
                    if position is None:
                        break
                    positions.append(position)
                    cursor = position + 1
                if len(positions) != len(template["lines"]):
                    continue
                extras = [line for i, line in enumerate(block["lines"]) if i not in positions]
                novel = [(i, line) for i, line in enumerate(block["lines"])
                         if i not in positions and not duplicate(line, template["lines"])]
                if (novel and all(i > positions[-1] for i, _ in novel)
                        and block_index + 1 < len(blocks)
                        and blocks[block_index + 1]["kind"] == "verse"):
                    following = blocks[block_index + 1]
                    following["lines"] = [line for _, line in novel] + following["lines"]
                    chosen = template
                    diagnostics.append(f"[TRUSTED REPEAT BOUNDARY] Moved {len(novel)} new trailing line(s) from {block['header']} to the following Verse; retained the complete repeated template.")
                    break
                if extras and all(duplicate(line, template["lines"]) for line in extras):
                    chosen = template
                    diagnostics.append(f"[TRUSTED REPEAT] Removed {len(extras)} duplicate ASR/reference line(s) from {block['header']}.")
                    break
        elif block["kind"] == "verse" and previous_kind in ("chorus", "hook", "refrain"):
            # Recognize a complete post-chorus even if an earlier pass already
            # extracted the preceding Chorus. Preserve genuinely new Verse text.
            for template in templates:
                if template["kind"] not in ("post-chorus", "post chorus"):
                    continue
                hit = _detect_repeated_post_chorus(block["lines"], 0, template["lines"])
                if not hit or hit["matched"] < len(template["lines"]) - hit["ref_offset"]:
                    continue
                remainder = block["lines"][hit["consumed"]:]
                if not all(duplicate(line, template["lines"]) for line in remainder):
                    continue
                chosen = template
                diagnostics.append(f"[TRUSTED POST-CHORUS] Replaced rough Verse label with {template['header']}; complete repeated pattern confirmed.")
                break
        final = chosen or block
        _append_section(result, final["header"])
        result.extend(final["lines"])
        previous_kind = final["kind"]
    return result


def _repair_chorus_across_headers(output, ref_items, diagnostics, trans_lines=None, xingyu_lines=None):
    """Join confirmed Chorus pieces even when an inferred Verse splits them.

    Search word boundaries so a single ASR line can end the Chorus and begin
    the next Verse without losing its new words.
    """
    templates = [b for b in _reference_section_blocks(ref_items)
                 if b["kind"] in ("chorus", "hook", "refrain") and len(b["lines"]) >= 4]
    first = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first:])))
    # Two partial Chorus blocks may overlap by one or two trusted lines.
    # Only join if their ordered union covers a complete known template;
    # never collapse two full repetitions into one.
    for left, right in zip(blocks, blocks[1:]):
        for template in templates:
            trusted = template["lines"]
            a, b = len(left["lines"]), len(right["lines"])
            if (left["kind"] == right["kind"] == template["kind"]
                    and 2 <= a < len(trusted) and 2 <= b < len(trusted)
                    and 0 <= a + b - len(trusted) <= 2
                    and all(similarity(x, y) >= 0.94 for x, y in zip(left["lines"], trusted[:a]))
                    and all(similarity(x, y) >= 0.94 for x, y in zip(right["lines"], trusted[-b:]))):
                left["lines"] = list(trusted)
                right["lines"] = []
                diagnostics.append("[CHORUS BOUNDARY] Joined overlapping partial Chorus blocks; emitted the shared lines once.")
                break
    for i, block in enumerate(blocks):
        if block["kind"] not in ("chorus", "hook", "refrain"):
            continue
        for template in templates:
            trusted = template["lines"]
            size = len(block["lines"])
            if not 2 <= size < len(trusted):
                continue
            if all(similarity(a, b) >= 0.94 for a, b in zip(block["lines"], trusted[-size:])):
                missing = trusted[:-size]
                if i and blocks[i - 1]["kind"] == "verse":
                    previous = blocks[i - 1]["lines"]
                    # ASR may merge several opening lines into one phrase.
                    opening = None
                    for take in range(1, min(len(previous), len(missing) + 2) + 1):
                        heard = previous[-take:]
                        score, _, coverage, purity = _trusted_block_score(missing, heard)
                        if (score >= 0.65 and coverage >= 0.60 and purity >= 0.65
                                and similarity(missing[0], heard[0]) >= 0.65):
                            if opening is None or score > opening[0]:
                                opening = (score, take)
                    if opening:
                        del previous[-opening[1]:]
                        block["lines"] = list(trusted)
                        diagnostics.append("[CHORUS BOUNDARY] Recovered merged opening phrases from the preceding Verse.")
                        break
                    if len(previous) >= len(missing) and all(
                            similarity(a, b) >= 0.85 for a, b in zip(previous[-len(missing):], missing)):
                        del previous[-len(missing):]
                        block["lines"] = list(trusted)
                        diagnostics.append("[CHORUS BOUNDARY] Moved recognized Chorus opening out of the preceding Verse.")
                        break
            if not all(similarity(a, b) >= 0.94 for a, b in zip(block["lines"], trusted[:size])):
                continue
            if i + 1 >= len(blocks) or blocks[i + 1]["kind"] != "verse":
                continue
            following = blocks[i + 1]
            remaining = trusted[size:]
            # A short rough middle can be bracketed by two exact Chorus
            # fragments. Verify that middle before joining all three blocks.
            if i + 2 < len(blocks) and blocks[i + 2]["kind"] == block["kind"]:
                ending = blocks[i + 2]
                tail_count = len(ending["lines"])
                middle = trusted[size:len(trusted) - tail_count]
                if (tail_count >= 2 and middle and size + tail_count >= 0.60 * len(trusted)
                        and all(similarity(a, b) >= 0.94 for a, b in zip(ending["lines"], trusted[-tail_count:]))):
                    score, _, coverage, purity = _trusted_block_score(middle, following["lines"])
                    if score >= 0.52 and coverage >= 0.50 and purity >= 0.55:
                        block["lines"] = list(trusted)
                        following["lines"] = []
                        ending["lines"] = []
                        diagnostics.append("[CHORUS BOUNDARY] Joined two trusted Chorus fragments and their verified rough middle into one complete Chorus.")
                        break
            word_limit = int(len(" ".join(trusted[max(0, size - 2):]).split()) * 1.6) + 10
            words, locations = [], []
            for line_index, line in enumerate(following["lines"]):
                for match in re.finditer(r"\S+", line):
                    words.append(match.group())
                    locations.append((line_index, match.end()))
                if len(words) >= word_limit:
                    break
            best = None
            first_text = norm(following["lines"][0]) if following["lines"] else ""
            first_source = next((s for s in (xingyu_lines or [])
                                 if len(first_text.split()) >= 5 and first_text in norm(s["text"])), None)
            independent_cache = {}
            for offset in range(max(0, size - 2), size + 1):
                candidate = trusted[offset:]
                for count in range(max(6, int(len(" ".join(candidate).split()) * 0.55)), min(len(words), word_limit) + 1):
                    heard = " ".join(words[:count])
                    score, _, coverage, purity = _trusted_block_score(candidate, [heard])
                    independent = False
                    if first_source and 0.50 <= score < 0.54 and coverage >= 0.60 and purity >= 0.60:
                        last_line = locations[count - 1][0]
                        key = (offset, last_line)
                        if key not in independent_cache:
                            last_text = norm(following["lines"][last_line])
                            last_source = next((s for s in (xingyu_lines or [])
                                                if s["start"] >= first_source["start"] and last_text in norm(s["text"])), None)
                            evidence = [s for s in (trans_lines or []) if last_source
                                        and s["start"] >= first_source["start"] - 0.5
                                        and s["end"] <= last_source["end"] + 0.5]
                            other_score, _, other_coverage, other_purity = _trusted_block_score(candidate, [s["text"] for s in evidence])
                            independent_cache[key] = (other_score >= 0.58 and other_coverage >= 0.60 and other_purity >= 0.60
                                                      and _local_phrase_evidence(candidate[-1], evidence) >= 0.50)
                        independent = independent_cache[key]
                    if coverage >= 0.60 and purity >= 0.60 and (score >= 0.54 or independent):
                        if best is None or score > best[0]:
                            best = (score, count, candidate, independent)
            if best is None:
                continue
            _, count, remaining, independent = best
            if independent:
                diagnostics.append("[CHORUS BOUNDARY] Confirmed uncertain Xingyu continuation using independent Whisper evidence from the same audio interval.")
            # End at the last multiword anchor; isolated words such as "I"
            # must not absorb the start of the following Verse.
            token_ref = norm(" ".join(remaining)).split()
            token_heard, token_locations = [], []
            for wi, word in enumerate(words[:count]):
                tokens = norm(word).split()
                token_heard.extend(tokens)
                token_locations.extend([wi] * len(tokens))
            anchors = [a for a in difflib.SequenceMatcher(None, token_ref, token_heard, autojunk=False).get_matching_blocks()
                       if a.size >= 2 and a.a + a.size >= len(token_ref) - 4]
            if not anchors:
                continue
            count = token_locations[anchors[-1].b + anchors[-1].size - 1] + 1
            line_index, char_end = locations[count - 1]
            mixed_line = following["lines"][line_index]
            tail = mixed_line[char_end:].strip()
            # When Xingyu merged across the boundary, use Whisper's independent
            # timing to retain words it heard before the common Verse suffix.
            source = next((s for s in (xingyu_lines or []) if norm(s["text"]) == norm(mixed_line)), None)
            if source and tail:
                nearby = [s for s in (trans_lines or []) if s["start"] >= source["start"] - 2 and s["start"] < source["end"]]
                endings = [s for s in nearby if similarity(s["text"], remaining[-1]) >= 0.50]
                if endings:
                    ending = max(endings, key=lambda s: similarity(s["text"], remaining[-1]))
                    after = " ".join(s["text"] for s in nearby if s["start"] >= ending["end"] - 0.05)
                    heard_words = after.split()
                    agreement = [a for a in difflib.SequenceMatcher(None, [norm(w) for w in tail.split()], [norm(w) for w in heard_words], autojunk=False).get_matching_blocks() if a.size >= 2]
                    if agreement:
                        a = agreement[-1]
                        tail = " ".join(heard_words[:a.b + a.size])
                        diagnostics.append("[CHORUS BOUNDARY] Used independent Whisper timing for the first Verse words in a merged Xingyu segment.")
            following["lines"] = ([tail] if tail else []) + following["lines"][line_index + 1:]
            block["lines"] = list(trusted)
            diagnostics.append(f"[CHORUS BOUNDARY] Recovered {len(remaining)} continuation lines across a Verse header; preserved the following ASR words.")
            break
    result = list(output[:first])
    for block in blocks:
        if block["lines"]:
            _append_section(result, block["header"])
            result.extend(block["lines"])
    return result


def _repair_locally_supported_verses(output, ref_items, diagnostics):
    """Recheck Verse wording without requiring the reference's verse order.

    Only replace supported lines. Unheard reference lines are never inserted,
    and extra audio lines keep their positions within the produced Verse.
    """
    references = [block for block in _reference_section_blocks(ref_items)
                  if block["kind"] == "verse" and len(block["lines"]) >= 4]
    result = list(output)
    headers = [i for i, line in enumerate(result) if SECTION_RE.match(line)] + [len(result)]
    for start, end in zip(headers, headers[1:]):
        if _section_name(result[start]) != "verse":
            continue
        positions = [i for i in range(start + 1, end) if result[i].strip()]
        heard = [result[i] for i in positions]
        candidates = []
        for reference in references:
            trusted = reference["lines"]
            n, m = len(heard), len(trusted)
            dp = [[(0.0, []) for _ in range(m + 1)] for _ in range(n + 1)]
            for i in range(1, n + 1):
                for j in range(1, m + 1):
                    best = max(dp[i - 1][j], dp[i][j - 1], key=lambda item: item[0])
                    score = similarity(heard[i - 1], trusted[j - 1])
                    if score >= 0.55:
                        total, pairs = dp[i - 1][j - 1]
                        option = (total + score, pairs + [(i - 1, j - 1, score)])
                        if option[0] > best[0]:
                            best = option
                    dp[i][j] = best
            total, pairs = dp[n][m]
            if (len(pairs) >= 4 and len(pairs) >= 0.60 * n
                    and len(pairs) >= 0.55 * m and total / len(pairs) >= 0.65):
                candidates.append((total / max(n, 1), pairs, reference))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            continue
        score, pairs, reference = candidates[0]
        if (len(candidates) > 1 and score - candidates[1][0] < 0.06
                and reference["lines"] != candidates[1][2]["lines"]):
            continue
        changed = 0
        for i, j, _ in pairs:
            text = reference["lines"][j]
            if result[positions[i]] != text:
                result[positions[i]] = text
                changed += 1
        if changed:
            diagnostics.append(f"[LOCAL VERSE REFERENCE] {result[start]} matched {reference['header']}: {len(pairs)} ordered line anchors; corrected {changed} lines and preserved unmatched audio.")
    return result


def _complete_fragmented_chorus_patterns(output, ref_items, diagnostics):
    """Recover a complete ordered repeat split across Verse/Chorus labels.

    Every trusted line must have local phrase evidence and every consumed
    output line must belong to the same pattern; unknown Verse text stays put.
    """
    templates = [b for b in _reference_section_blocks(ref_items) if b["kind"] == "chorus" and len(b["lines"]) >= 4]
    first = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first:])))
    for i, block in enumerate(blocks):
        if block["kind"] != "chorus" or not i or blocks[i - 1]["kind"] != "verse":
            continue
        previous = blocks[i - 1]["lines"]
        best = None
        for template in templates:
            trusted = template["lines"]
            if block["lines"] == trusted:
                continue
            vocabulary = set(norm(" ".join(trusted)).split())
            for take in range(1, min(4, len(previous)) + 1):
                heard = previous[-take:] + block["lines"]
                retained = ""
                if similarity(heard[0], trusted[0]) < 0.65:
                    words = heard[0].split()
                    splits = [(similarity(" ".join(words[cut:]), trusted[0]), cut)
                              for cut in range(3, len(words) - 2)]
                    if not splits or max(splits)[0] < 0.75:
                        continue
                    _, cut = max(splits)
                    retained = " ".join(words[:cut]).rstrip(" ,;")
                    heard[0] = " ".join(words[cut:])
                score, _, coverage, purity = _trusted_block_score(trusted, heard)
                if score < 0.72 or coverage < 0.70 or purity < 0.75:
                    continue
                def supported(j, line):
                    if max(similarity(line, text) for text in trusted) >= 0.50:
                        return True
                    tokens = norm(line).split()
                    if 0 < len(tokens) <= 2 and set(tokens) <= vocabulary:
                        return True
                    # A short ASR phrase may be the start of the next trusted
                    # line, rather than a separate lyric (e.g. a split ad-lib).
                    short_prefix = any(difflib.SequenceMatcher(None, norm(line), " ".join(norm(text).split()[:len(tokens)]), autojunk=False).ratio() >= 0.50 for text in trusted)
                    if 0 < len(tokens) <= 3 and (set(tokens) & vocabulary or short_prefix):
                        neighbors = []
                        if j + 1 < len(heard):
                            neighbors.append(line + " " + heard[j + 1])
                        if j:
                            neighbors.append(heard[j - 1] + " " + line)
                        return any(similarity(pair, text) >= 0.60 for pair in neighbors for text in trusted)
                    return False
                if not all(supported(j, line) for j, line in enumerate(heard)):
                    continue
                evidence = [{"text": " ".join(heard)}]
                if not all(_local_phrase_evidence(text, evidence) >= 0.60 for text in trusted):
                    continue
                if best is None or score > best[0]:
                    best = (score, take, trusted, retained)
        if best:
            _, take, trusted, retained = best
            del previous[-take:]
            if retained:
                previous.append(retained)
            block["lines"] = list(trusted)
            diagnostics.append(f"[COMPLETE CHORUS PATTERN] Restored {len(trusted)} reference lines from locally confirmed phrases; moved {take} opening phrase(s) out of the preceding Verse.")
    result = list(output[:first])
    for block in blocks:
        if block["lines"]:
            _append_section(result, block["header"])
            result.extend(block["lines"])
    return result


def _restore_evidenced_chorus_tail(output, ref_items, sources, diagnostics):
    """Use the next Verse's local audio boundary to verify missing Chorus lines."""
    first = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first:])))
    templates = [b["lines"] for b in _reference_section_blocks(ref_items) if b["kind"] == "chorus"]
    for block, following in zip(blocks, blocks[1:]):
        if block["kind"] != "chorus" or following["kind"] != "verse" or not following["lines"]:
            continue
        for trusted in templates:
            size = len(block["lines"])
            if not 2 <= len(trusted) - size <= 3 or size < len(trusted) / 2:
                continue
            if block["lines"] != trusted[:size]:
                continue
            opening = " ".join(following["lines"][0].split()[:8])
            anchors = [s for s in sources if _local_phrase_evidence(opening, [s]) >= 0.78]
            if len({round(s["start"] / 8) for s in anchors}) > 2 or not anchors:
                continue
            boundary = min(s["start"] for s in anchors)
            nearby = [s for s in sources if s["start"] >= boundary - 14 and s["start"] <= boundary + 1]
            evidence = [{"text": " ".join(s["text"] for s in sorted(nearby, key=lambda s: s["start"]))}]
            # Check each engine separately too: interleaving overlapping ASR
            # segments can otherwise disrupt a line split across two chunks.
            unique = []
            for s in sorted(nearby, key=lambda s: s["start"]):
                if not unique or s["start"] >= unique[-1]["end"] - 0.1:
                    unique.append(s)
            evidence.append({"text": " ".join(s["text"] for s in unique)})
            if all(_local_phrase_evidence(line, evidence) >= 0.60 for line in trusted[size - 1:]):
                block["lines"] = list(trusted)
                diagnostics.append(f"[CHORUS TAIL EVIDENCE] Restored {len(trusted) - size} missing lines before the locally recognized next Verse.")
                break
    result = list(output[:first])
    for block in blocks:
        _append_section(result, block["header"])
        result.extend(block["lines"])
    return result


def _format_novel_verse_phrases(output, ref_items, diagnostics):
    """Split long ASR sentences at clause starts, preserving all lyric words."""
    protected = {item["text"] for item in ref_items if item.get("kind") == "lyric"}
    result, kind, changed = [], None, 0
    for line in output:
        if SECTION_RE.match(line):
            kind = _section_name(line)
        if kind != "verse" or line in protected or len(line.split()) < 15:
            result.append(line)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])|(?<=uh)\s+(?=[A-Z])|\s+(?=(?:I(?:['’](?:m|ll|ve|d)|\b)|You\b|We\b|They\b|She\b|He\b)\s)", line)
        phrases = []
        for part in parts:
            if phrases and len(part.split()) < 4:
                phrases[-1] += " " + part
            else:
                phrases.append(part)
        bounded = []
        for phrase in phrases:
            words = phrase.split()
            while len(words) > 16:
                cuts = [k for k in range(6, 15) if words[k - 1].endswith((',', ';', '.', '?', '!'))]
                cut = cuts[-1] if cuts else 12
                bounded.append(' '.join(words[:cut]))
                words = words[cut:]
            if words:
                bounded.append(' '.join(words))
        phrases = bounded
        result.extend(phrases)
        changed += len(phrases) - 1
    if changed:
        diagnostics.append(f"[VERSE PHRASES] Added {changed} clause boundaries without changing lyric words.")
    return result


def _remove_confirmed_boundary_echo(output, ref_items, sources, diagnostics):
    """Remove a duplicated Chorus suffix only when a raw merged segment proves it."""
    first = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first:])))
    trusted = [b["lines"] for b in _reference_section_blocks(ref_items) if b["kind"] in ("chorus", "post-chorus", "hook", "refrain")]
    for previous, current in zip(blocks, blocks[1:]):
        if current["kind"] != "verse" or not current["lines"] or previous["lines"] not in trusted:
            continue
        ending = norm(previous["lines"][-1]).split()
        while ending and ending[-1] in {"yeah", "uh", "oh", "ooh"}:
            ending.pop()
        words = current["lines"][0].split()
        for take in range(min(3, len(words) - 4, len(ending)), 0, -1):
            prefix = norm(" ".join(words[:take])).split()
            if not prefix or prefix != ending[-len(prefix):]:
                continue
            remainder = norm(" ".join(words[take:take + 4]))
            confirmed = False
            for source in sources:
                raw = norm(source["text"])
                where = raw.find(remainder)
                if where < 0:
                    continue
                before = raw[:where].strip()
                if before.split()[-len(prefix):] == prefix and similarity(before, " ".join(ending)) >= 0.70:
                    confirmed = True
                    break
            if confirmed:
                current["lines"][0] = " ".join(words[take:])
                diagnostics.append(f"[BOUNDARY ECHO] Removed {take} duplicated Chorus word(s) from Verse opening, confirmed by the raw merged ASR segment.")
                break
    result = list(output[:first])
    for block in blocks:
        _append_section(result, block["header"])
        result.extend(block["lines"])
    return result if blocks else output


def _audit_reference_choruses(output, ref_items, diagnostics):
    """Report recognizable incomplete repeats instead of silently accepting them."""
    references = [b for b in _reference_section_blocks(ref_items) if b["kind"] in ("chorus", "hook", "refrain")]
    index = 0
    for block in _reference_section_blocks(parse_reference("\n".join(output))):
        if block["kind"] not in ("chorus", "hook", "refrain"):
            continue
        index += 1
        candidates = [b for b in references if b["kind"] == block["kind"]]
        lines = [norm(line) for line in block["lines"]]
        if any(lines == [norm(line) for line in b["lines"]] for b in candidates):
            continue
        for reference in candidates:
            trusted = [norm(line) for line in reference["lines"]]
            if 2 <= len(lines) < len(trusted) and (lines == trusted[:len(lines)] or lines == trusted[-len(lines):]):
                diagnostics.append(f"[REVIEW NEEDED] Chorus {index} contains only {len(lines)}/{len(trusted)} reference lines. Audio evidence was insufficient to safely complete it; check this boundary.")
                break


def _preserve_early_sessions(output, ref_items, ref_units, matches, transcript, xingyu, diagnostics):
    """Keep a substantial recorded lead-in before the reference's first Verse."""
    sections = _reference_section_blocks(ref_items)
    main = next((b for b in sections if b['kind'] != 'intro'), None)
    if not main or main['kind'] != 'verse' or len(main['lines']) < 3:
        return output
    indices = [i for i, u in enumerate(ref_units) if u['text'] == main['lines'][0]]
    anchor = next((m for m in matches if m['ref_start'] in indices), None)
    if not anchor or not xingyu:
        return output
    time = float(transcript[anchor['trans_start']]['start'])
    candidates = [r for r in xingyu if abs(float(r['start']) - time) <= 5
                  and _local_phrase_evidence(main['lines'][0], [r]) >= 0.55]
    if not candidates:
        return output
    onset = min(float(r['start']) for r in candidates)
    early = [r['text'] for r in xingyu if float(r['end']) <= onset + 0.05 and norm(r['text'])]
    if onset < 30 or len(early) < 5:
        return output
    position = next((i for i, line in enumerate(output) if _section_name(line) == 'verse'), None)
    if position is None:
        return output
    first_lines = [line for line in output[position + 1:] if line.strip()][:2]
    if not first_lines or similarity(first_lines[0], main['lines'][0]) < 0.80:
        # An earlier pass may already have retained the Sessions opening.
        return output
    diagnostics.append(f'[EARLY SESSIONS VERSE] Preserved {len(early)} Xingyu phrases before the reference Verse at {onset:.2f}s.')
    return output[:position] + ['[Verse]', *early, ''] + output[position:]


def _preserve_local_rap_inflections(output, ref_units, matches, transcript, xingyu, diagnostics):
    """Prefer locally heard -in' verb forms over the reference's bare stem."""
    changes = {}
    for match in matches:
        rows = transcript[match['trans_start']:match['trans_start'] + match['trans_count']]
        if not rows:
            continue
        local = [r for r in xingyu or [] if float(r['start']) < float(rows[-1]['end'])
                 and float(r['end']) > float(rows[0]['start'])]
        for unit in ref_units[match['ref_start']:match['ref_start'] + match['ref_count']]:
            text = unit['text']
            for row in local:
                if _local_phrase_evidence(text, [row]) < 0.60:
                    continue
                for word in re.findall(r"\b[A-Za-z]+in['’]", row['text']):
                    stem = word[:-3]
                    if len(stem) < 3:
                        continue
                    pattern = r'\b' + re.escape(stem) + r'\b(?![\w’\'])'
                    updated = re.sub(pattern, lambda m: word.capitalize() if m.group()[0].isupper() else word.lower(), text, flags=re.I)
                    if updated != text:
                        changes[unit['text']] = updated
                        text = updated
    if changes:
        diagnostics.append(f'[LOCAL RAP WORDING] Preserved Xingyu inflections in {len(changes)} locally matched reference lines.')
    return [changes.get(line, line) for line in output]


def _copy_reference_intro(output, ref_items, diagnostics):
    """Reference mode policy: copy its opening Intro exactly, regardless of ASR."""
    reference = _reference_section_blocks(ref_items)
    if not reference or reference[0]["kind"] != "intro":
        return output
    intros = []
    for block in reference:
        if block["kind"] != "intro":
            break
        intros.extend([block["header"], *block["lines"], ""])
    first_body = next((i for i, line in enumerate(output)
                       if SECTION_RE.match(line) and _section_name(line) != "intro"), len(output))
    diagnostics.append("[REFERENCE INTRO] Copied opening Intro verbatim from the reference (user-selected policy; ASR evidence not required).")
    return intros + list(output[first_body:])


def _timed_long_chorus_repetitions(template, trans_lines, xingyu_lines):
    """Find separated, well-supported repetitions of a long trusted Chorus."""
    reference_words = len(norm(" ".join(template)).split())
    candidates = []
    for source in (trans_lines or [], xingyu_lines or []):
        for start in range(len(source)):
            for take in range(2, min(24, len(source) - start) + 1):
                chosen = source[start:start + take]
                duration = float(chosen[-1]["end"]) - float(chosen[0]["start"])
                if duration > 35.0:
                    break
                heard_words = len(norm(" ".join(x["text"] for x in chosen)).split())
                if heard_words < max(12, int(reference_words * 0.55)):
                    continue
                score, _, coverage, purity = _trusted_block_score(
                    template, [x["text"] for x in chosen]
                )
                if score >= 0.66 and coverage >= 0.72 and purity >= 0.62:
                    candidates.append({
                        "score": score,
                        "coverage": coverage,
                        "purity": purity,
                        "start": float(chosen[0]["start"]),
                        "end": float(chosen[-1]["end"]),
                    })
    if not candidates:
        return []

    # Windows describing the same performance overlap heavily. Group them first,
    # then retain the earliest equally-good onset so a mixed Verse/Chorus ASR line
    # cannot cut off the opening of the trusted Chorus.
    clusters = []
    for candidate in sorted(candidates, key=lambda x: (x["start"], x["end"])):
        if clusters and candidate["start"] <= clusters[-1]["end"] + 0.75:
            clusters[-1]["items"].append(candidate)
            clusters[-1]["end"] = max(clusters[-1]["end"], candidate["end"])
        else:
            clusters.append({
                "start": candidate["start"],
                "end": candidate["end"],
                "items": [candidate],
            })

    hits = []
    all_sources = list(trans_lines or []) + list(xingyu_lines or [])
    for cluster in clusters:
        best = max(cluster["items"], key=lambda x: x["score"])
        eligible = [
            x for x in cluster["items"]
            if x["score"] >= best["score"] - 0.04
            and x["coverage"] >= best["coverage"] - 0.04
        ]
        hit = dict(best)
        hit["start"] = min(x["start"] for x in eligible)
        # Earlier high-quality windows help recover a clipped Chorus opening.
        # Their later ends are unsafe: an otherwise strong window can include
        # the first phrase of the next Verse. Keep the best window's end and
        # extend it below only for a genuinely short trailing fragment.
        hit["end"] = best["end"]

        # A final short ASR fragment can contain only the last two or three words
        # of a Chorus. Include it in the boundary, but stop before a real Verse.
        for row in sorted(all_sources, key=lambda x: float(x["start"])):
            start_time = float(row["start"])
            end_time = float(row["end"])
            if hit["end"] - 0.05 <= start_time <= hit["end"] + 0.45:
                if len(norm(row["text"]).split()) <= 4 and end_time <= hit["end"] + 1.5:
                    hit["end"] = max(hit["end"], end_time)
        hits.append(hit)
    return hits


def _session_lines_between_choruses(start_time, end_time, trans_lines,
                                    xingyu_lines, ref_units, diagnostics, chorus=None):
    """Return the best ASR lines whose midpoints lie between two Chorus hits."""
    def inside(row):
        middle = (float(row["start"]) + float(row["end"])) / 2.0
        return start_time - 0.05 <= middle < end_time - 0.05

    def boundary_rows(source):
        rows = []
        for row in source or []:
            if not chorus or not (float(row['end']) >= start_time and float(row['start']) <= end_time):
                if inside(row):
                    rows.append(dict(row))
                continue
            text = row['text']
            words = text.split()
            # At the next Chorus, retain the novel prefix of a merged ASR row.
            if abs(float(row['start']) - end_time) <= 3 or float(row['start']) < end_time < float(row['end']):
                cuts = [(similarity(' '.join(words[k:]), chorus[0]), k)
                        for k in range(3, len(words) - 3)]
                if cuts and max(cuts)[0] >= 0.75:
                    _, cut = max(cuts)
                    rows.append(dict(row, text=' '.join(words[:cut]), end=end_time))
                    continue
            if inside(row):
                rows.append(dict(row))
        return rows

    whisper = boundary_rows(trans_lines)
    xingyu = boundary_rows(xingyu_lines)
    # A mixed first segment may still contain the previous Chorus. Use a
    # separately timed Whisper opening to locate the novel words within it.
    if chorus:
        confirmed_opening = None
        for row in xingyu[:3]:
            if float(row['start']) > start_time + 2:
                break
            words = row['text'].split()
            for other in whisper:
                if not (float(row['start']) <= float(other['start']) <= float(row['end'])):
                    continue
                prefix = norm(other['text']).split()[:3]
                if len(prefix) < 3 or _local_phrase_evidence(' '.join(prefix), [{'text': ' '.join(chorus)}]) >= 0.70:
                    continue
                cut = next((k for k in range(1, len(words) - 2)
                            if norm(' '.join(words[k:k + 3])).split() == prefix), None)
                if cut is not None:
                    row['text'] = ' '.join(words[cut:])
                    row['start'] = other['start']
                    confirmed_opening = float(other['start'])
                    diagnostics.append('[VERSE BOUNDARY] Removed preceding Chorus words using an independent timed Verse opening.')
                    break
        if confirmed_opening is not None:
            # These earlier rows belong to the independently confirmed mixed
            # Chorus prefix, including fragments Whisper split into extra rows.
            whisper = [r for r in whisper if float(r['end']) > confirmed_opening]
            xingyu = [r for r in xingyu if float(r['end']) > confirmed_opening]
    merged = _merge_session_gap_sources(whisper, xingyu, ref_units, diagnostics)
    result = []
    for row in merged:
        text = re.sub(r"\s+", " ", row.get("text", "")).strip()
        if not norm(text) or _is_thanks_hallucination(text):
            continue
        if result and similarity(result[-1], text) >= 0.94:
            continue
        result.append(text)
    return result


def _rebuild_timed_long_chorus_tail(output, ref_items, trans_lines,
                                    xingyu_lines, diagnostics):
    """Repair a fragmented Sessions tail using repeated timed Chorus evidence.

    This deliberately activates only for a mixed short/long Chorus reference,
    at least three separate audio confirmations, and a confidently preserved
    reference Verse. Those guards keep ordinary songs on the existing path.
    """
    reference = _reference_section_blocks(ref_items)
    choruses = [b for b in reference if b["kind"] in ("chorus", "hook", "refrain")]
    verses = [b for b in reference if b["kind"] == "verse" and b["lines"]]
    if len(choruses) < 2 or not verses:
        return output
    shortest = min(choruses, key=lambda b: len(b["lines"]))
    longest = max(choruses, key=lambda b: len(b["lines"]))
    if (len(longest["lines"]) < 6
            or len(longest["lines"]) < len(shortest["lines"]) + 3):
        return output

    hits = _timed_long_chorus_repetitions(
        longest["lines"], trans_lines, xingyu_lines
    )
    if len(hits) < 3:
        return output

    first = next((i for i, line in enumerate(output) if SECTION_RE.match(line)), len(output))
    preamble = list(output[:first])
    blocks = _reference_section_blocks(parse_reference("\n".join(output[first:])))
    trusted_verse = verses[-1]["lines"]
    matches = []
    for index, block in enumerate(blocks):
        if block["kind"] != "verse":
            continue
        score, _, coverage, purity = _trusted_block_score(
            trusted_verse, block["lines"]
        )
        if score >= 0.80 and coverage >= 0.75 and purity >= 0.75:
            matches.append((score, index))
    if not matches:
        return output
    _, prefix_end = max(matches)

    rebuilt = list(preamble)
    for block in blocks[:prefix_end + 1]:
        if block["lines"]:
            _append_section(rebuilt, block["header"])
            rebuilt.extend(block["lines"])

    ref_units, _ = build_reference_units(ref_items)
    for index, hit in enumerate(hits):
        _append_section(rebuilt, longest["header"])
        rebuilt.extend(longest["lines"])
        next_start = hits[index + 1]["start"] if index + 1 < len(hits) else float("inf")
        verse_lines = _session_lines_between_choruses(
            hit["end"], next_start, trans_lines, xingyu_lines,
            ref_units, diagnostics, longest['lines']
        )
        if verse_lines:
            _append_section(rebuilt, "[Verse]")
            rebuilt.extend(verse_lines)

    diagnostics.append(
        f"[TIMED CHORUS CYCLES] Rebuilt {len(hits)} long Chorus occurrence(s) "
        "from separate timestamped ASR confirmations; preserved only the audio "
        "between them as Sessions Verses."
    )
    return rebuilt


def _restore_supported_intro(output, ref_items, trans_lines, xingyu_lines, diagnostics):
    """Complete a partial Intro only with distinctive evidence before the main section."""
    reference = _reference_section_blocks(ref_items)
    if not reference or reference[0]["kind"] != "intro":
        return output
    intro = reference[0]
    main = next((b for b in reference[1:] if b["kind"] in ("chorus", "verse", "hook")), None)
    if not main:
        return output
    sources = [source for source in (xingyu_lines, trans_lines) if source]
    hits = [_find_timed_trusted_block(source, main["lines"], 0.0, 70.0) for source in sources]
    hits = [hit for hit in hits if hit and hit["score"] >= 0.56]
    if not hits:
        return output
    boundary = min(hit["start"] for hit in hits)
    early = [line for source in sources for line in source
             if float(line["end"]) <= boundary + 0.1]
    # Shared Chorus lines and short ad-libs cannot prove that Intro was sung.
    distinctive = [line for line in intro["lines"] if len(norm(line).split()) >= 5
                   and max((similarity(line, other) for other in main["lines"]), default=0) < 0.75]
    if not any(_local_phrase_evidence(line, early) >= 0.60 for line in distinctive):
        return output
    main_index = next((i for i, line in enumerate(output)
                       if _section_name(line) in ("chorus", "verse", "hook")), None)
    if main_index is None:
        return output
    prefix = [line for line in output[:main_index] if line.strip() and not SECTION_RE.match(line)]
    if all(line in prefix for line in intro["lines"]):
        return output
    extras = [line for line in prefix if line not in intro["lines"]]
    diagnostics.append("[SUPPORTED INTRO] Restored reference Intro from distinctive opening ASR evidence; kept extra opening text inside Intro.")
    return [intro["header"], *extras, *intro["lines"], "", *output[main_index:]]


def _retry_missing_chorus_opening(audio, ref_items, output, trans_lines, xingyu_lines, tx, language, log):
    """Retry one short excerpt when a confirmed first Chorus lost its opening."""
    references = _reference_section_blocks(ref_items)
    trusted = next((b for b in references if b["kind"] == "chorus"), None)
    produced = next((b for b in _reference_section_blocks(parse_reference("\n".join(output))) if b["kind"] == "chorus"), None)
    if not trusted or not produced:
        return trans_lines
    missing = len(trusted["lines"]) - len(produced["lines"])
    if not 2 <= missing <= len(trusted["lines"]) / 2:
        return trans_lines
    if not all(similarity(a, b) >= 0.94 for a, b in zip(produced["lines"], trusted["lines"][missing:])):
        return trans_lines
    sources = [s for s in (xingyu_lines, trans_lines) if s]
    hits = [_find_timed_trusted_block(s, trusted["lines"][missing:missing + 3], 0, 60) for s in sources]
    hits = [h for h in hits if h and h["score"] >= 0.55]
    if not hits:
        return trans_lines
    stop = min(h["start"] for h in hits)
    start = max(0.0, stop - min(30.0, 4.0 * missing))
    intros = [line for b in references if b["kind"] == "intro" for line in b["lines"]]
    intro_ends = [s["end"] for s in trans_lines if s["end"] < stop - 3
                  and any(similarity(s["text"], line) >= 0.65 for line in intros)]
    if intro_ends:
        start = max(start, max(intro_ends))
    # Include a crossing old Whisper chunk in full so its failed reading is
    # replaced as well, instead of surviving next to the recovered opening.
    crossing = [s["end"] for s in trans_lines if s["start"] < stop < s["end"] <= stop + 3.0]
    if crossing:
        stop = max(crossing)
    if stop - start < 3:
        return trans_lines
    log(f"Chorus opening recovery: checking {start:.2f}s–{stop:.2f}s once, without a reference prompt…")
    try:
        with _tempfile.TemporaryDirectory(prefix="triplenine_chorus_") as folder:
            excerpt = Path(folder) / "opening.wav"
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error",
                            "-ss", str(start), "-i", str(audio), "-t", str(stop - start),
                            "-vn", "-ac", "1", "-ar", "16000", str(excerpt)],
                           check=True, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            words = tx.transcribe(excerpt, language=language or None, initial_prompt=None, separate_vocals=False)
        recovered = _remove_trailing_whisper_hallucinations(group_whisper_words(words))
        evidence = [{"text": " ".join(s["text"] for s in recovered)}]
        if not all(_local_phrase_evidence(line, evidence) >= 0.50 for line in trusted["lines"][:missing]):
            log("Chorus opening recovery: insufficient evidence; kept the existing transcript.")
            return trans_lines
        recovered = [dict(s, start=s["start"] + start, end=s["end"] + start) for s in recovered]
        # Keep whole outside chunks; replacing partial chunks could lose words.
        kept = [s for s in trans_lines if not (s["start"] >= start - 0.05 and s["end"] <= stop + 0.05)]
        log(f"Chorus opening recovery: verified {missing} missing reference lines in the short audio excerpt.")
        return sorted(kept + recovered, key=lambda s: s["start"])
    except Exception as exc:
        log(f"Chorus opening recovery failed; continuing with existing transcript: {exc}")
        return trans_lines


def _retry_missing_intro_audio(audio, ref_items, trans_lines, xingyu_lines, tx, language, log):
    """Retry an untranscribed opening once, using a temporary read-only excerpt."""
    blocks = _reference_section_blocks(ref_items)
    sources = list(trans_lines or []) + list(xingyu_lines or [])
    if not blocks or blocks[0]["kind"] != "intro" or not sources:
        return trans_lines
    first_start = min(float(line["start"]) for line in sources)
    if not 5.0 <= first_start <= 45.0:
        return trans_lines
    log(f"Intro recovery: both recognizers skipped the first {first_start:.2f}s; checking that excerpt once…")
    try:
        with _tempfile.TemporaryDirectory(prefix="triplenine_intro_") as folder:
            excerpt = Path(folder) / "intro.wav"
            subprocess.run(
                [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-i", str(audio),
                 "-t", str(first_start), "-vn", "-ac", "1", "-ar", "16000", str(excerpt)],
                check=True, capture_output=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Do not prompt with reference lyrics: evidence must come from audio.
            words = tx.transcribe(excerpt, language=language or None,
                                  initial_prompt=None, separate_vocals=False)
        recovered = _remove_trailing_whisper_hallucinations(group_whisper_words(words))
        recovered = [line for line in recovered
                     if 0 <= float(line["start"]) < float(line["end"]) <= first_start + 0.1]
        log(f"Intro recovery: recovered {len(recovered)} opening transcript lines.")
        return sorted(recovered + list(trans_lines), key=lambda line: float(line["start"]))
    except Exception as exc:
        log(f"Intro recovery could not read the excerpt; continuing with existing transcript: {exc}")
        return trans_lines


def _local_phrase_evidence(reference, segments):
    target_size = len(norm(reference).split())
    best = 0.0
    for segment in segments:
        words = segment.get("text", "").split()
        for start in range(len(words)):
            for size in range(max(3, target_size - 4), target_size + 5):
                heard = " ".join(words[start:start + size])
                best = max(best, similarity(reference, heard))
    return best


def _keep_post_chorus_tail_local(matches, ref_items, ref_units, trans_lines, xingyu_lines, diagnostics):
    """Rebind a distant tail only when local ASR confirms every missing line."""
    sections, _ = _reference_section_map(ref_items)
    repaired = []
    for match in matches:
        current = dict(match)
        if repaired:
            previous = repaired[-1]
            header = sections.get(current["ref_start"])
            if (_section_name(header) in ("post-chorus", "post chorus")
                    and current["ref_start"] == previous["ref_start"] + previous["ref_count"]
                    and current["trans_start"] == previous["trans_start"] + previous["trans_count"]
                    and all(sections.get(i) == header for i in range(previous["ref_start"], current["ref_start"] + current["ref_count"]))):
                previous["ref_count"] += current["ref_count"]
                previous["trans_count"] += current["trans_count"]
                previous["similarity"] = min(previous["similarity"], current["similarity"])
                continue
        repaired.append(current)
    for previous, current in zip(repaired, repaired[1:]):
        begin = previous["ref_start"] + previous["ref_count"]
        finish = current["ref_start"] + current["ref_count"]
        header = sections.get(begin)
        if (current["ref_start"] != begin or finish - begin > 3
                or _section_name(header) not in ("post-chorus", "post chorus")
                or sections.get(begin - 1) != header
                or any(sections.get(i) != header for i in range(begin, finish))):
            continue
        previous_end = float(trans_lines[previous["trans_start"] + previous["trans_count"] - 1]["end"])
        distant_start = float(trans_lines[current["trans_start"]]["start"])
        if distant_start - previous_end < 15.0:
            continue
        # Small local window; never search across the intervening new verse.
        local = [x for x in xingyu_lines or []
                 if float(x["end"]) > previous_end - 1.0
                 and float(x["start"]) < previous_end + 8.0]
        if not local:
            continue
        evidence = [_local_phrase_evidence(ref_units[i]["text"], local) for i in range(begin, finish)]
        if min(evidence) < 0.50 or evidence[-1] < 0.65:
            continue
        last = max(local, key=lambda x: similarity(ref_units[finish - 1]["text"], x["text"]))
        indices = [i for i in range(previous["trans_start"] + previous["trans_count"], current["trans_start"])
                   if float(trans_lines[i]["start"]) >= previous_end - 0.05
                   and float(trans_lines[i]["end"]) <= float(last["end"]) + 0.05]
        if not indices:
            continue
        current.update(trans_start=indices[0], trans_count=indices[-1] - indices[0] + 1,
                       similarity=min(evidence))
        diagnostics.append(f"[LOCAL POST-CHORUS TAIL] Rebound {finish-begin} trusted lines from {distant_start:.2f}s to {previous_end:.2f}–{float(last['end']):.2f}s; local evidence={min(evidence):.1%}.")
    return sorted(repaired, key=lambda m: m["trans_start"])


def _repair_split_post_after_chorus(output, ref_items, diagnostics, trans_lines=None, xingyu_lines=None):
    """Join a rough prefix and a trusted tail after an already restored Chorus."""
    blocks = _reference_section_blocks(parse_reference("\n".join(output)))
    variants = _reference_post_chorus_variants(ref_items)
    if not variants:
        return output
    changed = False
    i = 1
    while i + 1 < len(blocks):
        before, rough, tail = blocks[i-1:i+2]
        if (before["kind"] not in ("chorus", "hook", "refrain")
                or rough["kind"] != "verse"
                or tail["kind"] not in ("post-chorus", "post chorus")):
            i += 1
            continue
        hit = None
        for header, trusted in variants:
            count = len(tail["lines"])
            if not 1 <= count <= 3 or count >= len(trusted):
                continue
            if similarity(" ".join(tail["lines"]), " ".join(trusted[-count:])) < 0.90:
                continue
            score, _, coverage, purity = _trusted_block_score(trusted, rough["lines"] + tail["lines"])
            start = _known_section_start_score(rough["lines"], 0, trusted)
            if score >= 0.48 and coverage >= 0.60 and purity >= 0.60 and start >= 0.42:
                if hit is None or score > hit[0]:
                    hit = score, header, trusted
        if hit:
            changed = True
            blocks[i:i+2] = [dict(header=hit[1], kind="post-chorus", lines=list(hit[2]))]
            diagnostics.append(f"[POST-CHORUS JOIN] Restored {len(hit[2])} lines after Chorus from rough prefix + verified suffix ({hit[0]:.1%}).")
        i += 1
    # If re-binding the earlier tail removed the late trusted suffix, the final
    # repeat may now consist entirely of rough Xingyu. Use the independent
    # Whisper evidence at exactly that interval, never a distant repetition.
    for index in range(1, len(blocks)):
        before, rough = blocks[index-1:index+1]
        if (before["kind"] not in ("chorus", "hook", "refrain")
                or rough["kind"] != "verse" or len(rough["lines"]) < 2):
            continue
        starts = [n for n, x in enumerate(xingyu_lines or [])
                  if x["text"].strip() == rough["lines"][0].strip()]
        for start in starts:
            window = (xingyu_lines or [])[start:start + len(rough["lines"])]
            if len(window) != len(rough["lines"]) or any(
                    x["text"].strip() != line.strip() for x, line in zip(window, rough["lines"])):
                continue
            begin, end = float(window[0]["start"]), float(window[-1]["end"])
            for header, trusted in variants:
                heard = [t for t in trans_lines or []
                         if float(t["start"]) >= begin - 0.5 and float(t["start"]) < end + 0.5]
                if not heard:
                    continue
                score, _, coverage, purity = _trusted_block_score(trusted, [t["text"] for t in heard])
                hit = dict(score=score, coverage=coverage, purity=purity, lines=heard,
                           start=float(heard[0]["start"]), end=float(heard[-1]["end"]))
                if (hit["score"] < 0.52 or hit["coverage"] < 0.60 or hit["purity"] < 0.60
                        or abs(hit["start"] - begin) > 2.0 or abs(hit["end"] - end) > 2.0):
                    continue
                if (_local_phrase_evidence(trusted[-1], hit["lines"]) < 0.65
                        or _local_phrase_evidence(trusted[0], hit["lines"]) < 0.45):
                    continue
                rough.update(header=header, kind="post-chorus", lines=list(trusted))
                changed = True
                diagnostics.append(f"[TIMED POST-CHORUS RECOVERY] Restored {len(trusted)} lines at {begin:.2f}–{end:.2f}s using independent Whisper evidence ({hit['score']:.1%}).")
                break
            if rough["kind"] == "post-chorus":
                break
    if not changed:
        return output
    result = []
    for block in blocks:
        _append_section(result, block["header"])
        result.extend(block["lines"])
    return result


def _check_session_wording(row, other_source, ref_units, diagnostics):
    """Correct small anchored ASR errors, never replace or reorder sections."""
    words = row['text'].split()
    tokens = [norm(w).replace("'", '') for w in words]
    references = [u['text'] for u in ref_units]
    vocabulary = ' ' + ' '.join(norm(t).replace("'", '') for t in references) + ' '
    local = [r for r in other_source if r['start'] < row['end'] + 1 and r['end'] > row['start'] - 1]
    targets = [('reference', t) for t in references]
    if local:
        targets.append(('other engine', ' '.join(r['text'] for r in local)))
    edits = {}
    for origin, target in targets:
        tw = target.split()
        tt = [norm(w).replace("'", '') for w in tw]
        ops = difflib.SequenceMatcher(None, tokens, tt, autojunk=False).get_opcodes()
        for j, (tag, a, z, b, y) in enumerate(ops):
            if tag != 'replace' or not (1 <= z-a <= 2 and y-b == z-a):
                continue
            left = ops[j-1] if j else None
            right = ops[j+1] if j+1 < len(ops) else None
            lc = left[2]-left[1] if left and left[0] == 'equal' else 0
            rc = right[2]-right[1] if right and right[0] == 'equal' else 0
            if rc < 2 or lc+rc < 5 or (lc < 1 and origin == 'reference'):
                continue
            # Preserve rap inflections; do not normalize workin' to work.
            if any(tokens[k].endswith('in') and tokens[k][:-2] == tt[b+k-a] for k in range(a,z)):
                continue
            if origin == 'reference':
                if z-a != 1 or difflib.SequenceMatcher(None, tokens[a], tt[b]).ratio() < 0.50:
                    continue
            elif ' ' + ' '.join(tt[b:y]) + ' ' not in vocabulary:
                continue
            edits[(a,z)] = tw[b:y]
    if not edits:
        # A noisy lead-in may prevent token alignment of a two-word phrase.
        # Require the following five words in the other engine, plus a similar
        # two-word replacement already present in the trusted reference.
        for a in range(len(words) - 6):
            suffix = tokens[a+2:a+7]
            ow = ' '.join(r['text'] for r in local).split()
            ot = [norm(w).replace("'", '') for w in ow]
            for b in range(2, len(ot)-4):
                candidate = ot[b-2:b]
                if ot[b:b+5] != suffix or candidate == tokens[a:a+2]:
                    continue
                if (' ' + ' '.join(candidate) + ' ' in vocabulary
                        and difflib.SequenceMatcher(None, ' '.join(candidate), ' '.join(tokens[a:a+2])).ratio() >= 0.50):
                    edits[(a,a+2)] = ow[b-2:b]
        if not edits:
            return row
    for (a,z), replacement in sorted(edits.items(), reverse=True):
        words[a:z] = replacement
    diagnostics.append(f"[LOCAL WORD CHECK] Corrected anchored words at {row['start']:.2f}s using reference/other-engine agreement.")
    return dict(row, text=' '.join(words))


def _merge_session_gap_sources(whisper, xingyu, ref_units, diagnostics):
    """Prefer Xingyu locally, filling its uncovered intervals with Whisper.

    Never treat a single Xingyu candidate as coverage of the entire Sessions
    passage. Clip only a boundary phrase supported by overlapping source text.
    """
    merged = [dict(s, engine="Xingyu") for s in xingyu]
    for row in whisper:
        start, end = float(row["start"]), float(row["end"])
        duration = end - start
        if duration <= 0 or not row.get("text", "").strip():
            continue
        overlapping = [s for s in xingyu if s["start"] < end and s["end"] > start]
        intervals = sorted((max(start, s["start"]), min(end, s["end"])) for s in overlapping)
        covered, cursor = 0.0, start
        for left, right in intervals:
            covered += max(0.0, right - max(left, cursor))
            cursor = max(cursor, right)
        if duration - covered < 0.75 or covered / duration > 0.40:
            continue
        words = row["text"].split()
        clipped_start, clipped_end = start, end
        for side in ("left", "right"):
            neighbors = [s for s in overlapping if
                         (s["start"] <= start < s["end"] if side == "left" else s["start"] < end <= s["end"])]
            if not neighbors or len(words) < 4:
                continue
            context = [s["text"] for s in neighbors]
            # A trusted phrase may be clearer than the overlapping rough ASR,
            # but use it only if that ASR independently resembles the phrase.
            references = [u["text"] for u in ref_units if len(norm(u["text"]).split()) >= 4
                          and max(similarity(u["text"], text) for text in context) >= 0.40]
            candidates = context + references
            best = None
            for take in range(3, min(len(words) - 1, 20) + 1):
                phrase = " ".join(words[:take] if side == "left" else words[-take:])
                for candidate in candidates:
                    tokens = norm(candidate).split()
                    for size in range(max(3, take - 2), min(len(tokens), take + 2) + 1):
                        target = " ".join(tokens[-size:] if side == "left" else tokens[:size])
                        score = similarity(phrase, target, autojunk=False)
                        if score >= 0.72 and (best is None or score > best[0]):
                            best = (score, take)
            if best:
                take = best[1]
                if side == "left":
                    words = words[take:]
                    clipped_start = max(s["end"] for s in neighbors)
                else:
                    words = words[:-take]
                    clipped_end = min(s["start"] for s in neighbors)
        text = " ".join(words).strip()
        if text:
            merged.append(dict(text=text, start=clipped_start, end=clipped_end, engine="Whisper gap"))
            diagnostics.append(f"[XINGYU GAP FILLED] {clipped_start:.2f}s–{clipped_end:.2f}s | {text}")
    return sorted(merged, key=lambda s: (s["start"], s["end"]))


def _fill_whisper_time_gaps(trans_lines, xingyu_lines):
    """Preserve second-engine evidence where Whisper emitted no usable phrase.

    Keep original segmentation where Whisper has coverage. Permit a small
    boundary overlap, but never add a segment mostly covered by Whisper.
    """
    combined = list(trans_lines)
    added = []
    for segment in xingyu_lines or []:
        start, end = float(segment["start"]), float(segment["end"])
        duration = end - start
        if duration < 0.5 or not segment.get("text", "").strip():
            continue
        intervals = sorted((max(start, float(t["start"])), min(end, float(t["end"])))
                           for t in trans_lines
                           if float(t["end"]) > start and float(t["start"]) < end)
        covered, cursor = 0.0, start
        for left, right in intervals:
            covered += max(0.0, right - max(left, cursor))
            cursor = max(cursor, right)
        if covered / duration <= 0.20:
            combined.append(dict(segment))
            added.append(segment)
    combined.sort(key=lambda line: (float(line["start"]), float(line["end"])))
    return combined, added


def _release_uninformative_whisper_spans(trans_lines, xingyu_lines, ref_items, diagnostics):
    """Do not let a long repetitive Whisper span hide informative secondary ASR."""
    trusted = [item["text"] for item in ref_items if item.get("kind") == "lyric" and len(norm(item["text"]).split()) >= 5]
    result = []
    for line in trans_lines:
        duration = line["end"] - line["start"]
        words = norm(line["text"]).split()
        if duration < 12 or not words or len(set(words)) / len(words) > 0.45:
            result.append(line)
            continue
        secondary = [s for s in (xingyu_lines or []) if s["start"] >= line["start"] - 1
                     and s["end"] <= line["end"] + 0.5]
        covered = sum(s["end"] - s["start"] for s in secondary)
        if len(secondary) < 2 or covered < 0.65 * duration:
            result.append(line)
            continue
        evidence = [{"text": " ".join(s["text"] for s in secondary)}]
        improved = sum(_local_phrase_evidence(text, evidence) >= 0.60
                       and _local_phrase_evidence(text, [line]) < 0.40 for text in trusted)
        if improved < 3:
            result.append(line)
            continue
        # Removing this failed coverage lets normal gap recovery insert the
        # original timed Xingyu segments, without replacing neighboring audio.
        diagnostics.append(f"[ASR COVERAGE REPAIR] Released repetitive Whisper span {line['start']:.2f}–{line['end']:.2f}s; Xingyu independently supports {improved} reference lines.")
    return result


def hybridize(ref_items, trans_lines, threshold=0.46, xingyu_lines=None):
    """Merge trusted reference + infer structure for Sessions-only passages."""
    original_trans_lines = trans_lines
    original_xingyu_lines = xingyu_lines
    cleanup_messages = []
    protected = {norm(item.get("text", "")) for item in ref_items if item.get("kind") == "lyric"}
    cleaned = []
    for name, source in (("Whisper", trans_lines), ("Xingyu", xingyu_lines)):
        rows = list(source or [])
        removed = 0
        while rows and _is_thanks_hallucination(rows[-1]["text"]) and norm(rows[-1]["text"]) not in protected:
            rows.pop()
            removed += 1
        if removed:
            cleanup_messages.append(f"[ASR TAIL FILTER] Removed {removed} trailing hallucination segment(s) from {name} before gap recovery.")
        cleaned.append(rows)
    trans_lines, xingyu_lines = cleaned
    trans_lines = _release_uninformative_whisper_spans(trans_lines, xingyu_lines, ref_items, cleanup_messages)
    trans_lines, recovered_audio = _fill_whisper_time_gaps(trans_lines, xingyu_lines)
    ref_units, trailing_headers = build_reference_units(ref_items)
    matches = align_reference_to_transcript(ref_units, trans_lines, threshold)
    section_for_ref, first_reference_header = _reference_section_map(ref_items)

    output = []
    diagnostics = list(cleanup_messages)
    for segment in recovered_audio:
        diagnostics.append(f"[ASR GAP RECOVERY] {segment['start']:.2f}s–{segment['end']:.2f}s | {segment['text']}")
    matches = _keep_post_chorus_tail_local(matches, ref_items, ref_units, trans_lines, xingyu_lines, diagnostics)

    # V52: Full raw-engine trace is available in shorter-reference mode.
    # This is diagnostic-only and does not change either ASR result.
    _append_detailed_asr_trace(diagnostics, original_trans_lines, original_xingyu_lines)

    used_ref = set()
    matched_trans = set()
    blocks_by_start = {m["trans_start"]: m for m in matches}

    # Determine where each matched block ends so we can recognize a substantial
    # Sessions-only run between two known reference sections.
    match_starts = sorted(blocks_by_start)

    emitted_section = None
    pending_inferred_verse = False
    tj = 0

    # V49: do NOT pre-insert [Intro]. A reference Intro header is emitted only
    # when at least one trusted Intro lyric is actually matched. If none of the
    # Intro lyrics are found, the result starts directly with the first real
    # detected section (for example [Chorus]) instead of inventing Intro text.
    while tj < len(trans_lines):
        block = blocks_by_start.get(tj)

        if block is None:
            # Find the next matched block and inspect the current unmatched run.
            next_start = next((x for x in match_starts if x > tj), len(trans_lines))
            next_block = blocks_by_start.get(next_start)
            run = trans_lines[tj:next_start]

            recovered_prefix = _recover_upcoming_section_prefix(
                run, next_block, ref_units, section_for_ref,
                used_ref, emitted_section, threshold, xingyu_lines=xingyu_lines
            )
            suppress_empty_intro = False
            if recovered_prefix:
                # Keep only material before the recovered section boundary.
                # Normally that may be genuine Sessions-only audio. However,
                # when the reference begins with [Intro] and NONE of its trusted
                # Intro lyrics were found, do not invent an Intro from raw ASR.
                run = run[:recovered_prefix["run_start_index"]]
                suppress_empty_intro = (
                    _should_suppress_unmatched_intro_before_recovery(
                        run, recovered_prefix, emitted_section, ref_items
                    )
                )
                if suppress_empty_intro and run:
                    diagnostics.append(
                        '[INTRO OMITTED] Reference begins with [Intro], but its '
                        'trusted Intro lyrics were not confidently found. '
                        'Discarded unverified pre-section ASR instead of inventing '
                        'Intro text.'
                    )
                    run = []

            # A substantial new passage after Chorus/Bridge and before another
            # known section is very likely a new Sessions verse.  Require either
            # >=3 Whisper chunks or >=10 seconds so tiny ad-libs are not labeled
            # as verses.
            run_seconds = (
                float(run[-1]["end"]) - float(run[0]["start"])
                if run else 0.0
            )
            current_kind = _section_name(emitted_section)
            infer_verse = (
                bool(run)
                and current_kind in ("chorus", "hook", "bridge", "refrain",
                                     "post-chorus", "post chorus", "outro")
                and (len(run) >= 3 or run_seconds >= 10.0)
            )

            if infer_verse:
                _append_section(output, "[Verse]")
                emitted_section = "[Verse]"
                diagnostics.append(
                    f"[STRUCTURE] Inferred new Sessions verse at "
                    f"{run[0]['start']:.2f}s ({len(run)} Whisper chunks, "
                    f"{run_seconds:.1f}s)."
                )

            if run:
                run_start = float(run[0]["start"])
                run_end = float(run[-1]["end"])
                x_candidates = _xingyu_lines_for_interval(
                    xingyu_lines, run_start, run_end
                )

                _reference_gap_trace(
                    diagnostics, run, x_candidates, next_block, ref_units
                )

                suppress_preanchor = False
                preanchor_score = 0.0
                if tj == 0 and xingyu_lines is not None:
                    suppress_preanchor, preanchor_score = (
                        _preanchor_unverified_whisper(
                            run, x_candidates, next_block, ref_units
                        )
                    )

                if suppress_preanchor:
                    diagnostics.append(
                        f"[PRE-ANCHOR SUPPRESS] Removed unsupported Whisper-only "
                        f"opening phrase(s): Xingyu had no overlapping segment and "
                        f"best similarity to upcoming trusted reference was only "
                        f"{preanchor_score*100:.1f}%."
                    )
                elif x_candidates:
                    diagnostics.append(
                        f"[XINGYU SESSION   ] Using {len(x_candidates)} Xingyu "
                        f"candidate segment(s) for {run_start:.2f}s–{run_end:.2f}s."
                    )
                    session_segments = _merge_session_gap_sources(run, x_candidates, ref_units, diagnostics)
                    for x_index, x in enumerate(session_segments):
                        pretty = re.sub(r"\s+", " ", x["text"]).strip()
                        if not pretty:
                            continue

                        # Xingyu can overlap a boundary and repeat the final
                        # Chorus line as the first line of a new inferred Verse.
                        # Suppress only the first candidate when it is clearly
                        # the same lyric as the last emitted line.
                        if x_index == 0 and infer_verse:
                            is_boundary_dup, boundary_score, boundary_tail = (
                                _is_boundary_duplicate_of_output_tail(pretty, output)
                            )
                            if is_boundary_dup:
                                diagnostics.append(
                                    f'[BOUNDARY DUPLICATE] Suppressed Xingyu '
                                    f'({boundary_score*100:.1f}%): {pretty} '
                                    f'→ previous trusted tail: {boundary_tail}'
                                )
                                continue

                        # Only before the very first trusted anchor: if Xingyu's
                        # line is just a rough ASR version of an upcoming trusted
                        # reference line, keep the trusted line only.
                        if tj == 0:
                            is_dup, dup_score, trusted_text = (
                                _is_duplicate_of_upcoming_trusted_intro(
                                    pretty, next_block, ref_units
                                )
                            )
                            if is_dup:
                                diagnostics.append(
                                    f'[INTRO DUPLICATE ] Suppressed Xingyu '
                                    f'({dup_score*100:.1f}%): {pretty} '
                                    f'→ trusted: {trusted_text}'
                                )
                                continue

                        output.append(pretty)
                        diagnostics.append(
                            f'[{x["engine"].upper()} PHRASE] {pretty} '
                            f'({float(x["start"]):.2f}s–{float(x["end"]):.2f}s)'
                        )
                else:
                    for t in run:
                        pretty_lines = _split_whisper_only_line(t["text"])
                        if not pretty_lines:
                            pretty_lines = [t["text"]]
                        for pretty in pretty_lines:
                            if tj == 0:
                                is_dup, dup_score, trusted_text = (
                                    _is_duplicate_of_upcoming_trusted_intro(
                                        pretty, next_block, ref_units
                                    )
                                )
                                if is_dup:
                                    diagnostics.append(
                                        f'[INTRO DUPLICATE ] Suppressed Whisper '
                                        f'({dup_score*100:.1f}%): {pretty} '
                                        f'→ trusted: {trusted_text}'
                                    )
                                    continue
                            _append_whisper_phrase(
                                output, diagnostics, pretty,
                                float(t["start"]), float(t["end"])
                            )

            if recovered_prefix:
                wanted_section = recovered_prefix["section"]

                # If an Intro header exists but no lyric was emitted beneath it,
                # remove the empty header before moving to the recovered section.
                if wanted_section != "[Intro]" and output:
                    while output and output[-1] == "":
                        output.pop()
                    if output and output[-1] == "[Intro]":
                        output.pop()
                        emitted_section = None
                        diagnostics.append(
                            "[INTRO OMITTED] Removed empty [Intro] section."
                        )

                if wanted_section and wanted_section != emitted_section:
                    _append_section(output, wanted_section)
                    emitted_section = wanted_section

                for ref_index in recovered_prefix["indices"]:
                    unit = ref_units[ref_index]
                    output.append(unit["text"])
                    used_ref.add(ref_index)
                    diagnostics.append(
                        f'[SECTION RECOVERY {recovered_prefix["score"]*100:5.1f}% '
                        f'via {recovered_prefix.get("source", "ASR")}] '
                        f'{unit["text"]}'
                    )

                diagnostics.append(
                    f'[STRUCTURE] Recovered beginning of {wanted_section} before '
                    f'next anchor; recognized section start was replaced by the complete trusted reference prefix.'
                )

            tj = next_start
            continue

        ri = block["ref_start"]
        rb = block["ref_count"]
        tb = block["trans_count"]
        sim = block["similarity"]
        refs = ref_units[ri:ri + rb]
        trans = trans_lines[tj:tj + tb]

        for offset, unit in enumerate(refs):
            ref_index = ri + offset
            wanted_section = section_for_ref.get(ref_index)

            # V3: section belongs to EVERY lyric in that section, not only its
            # first line. Therefore [Intro] / [Verse] survives even when the
            # first reference lyric is unmatched.
            if wanted_section and wanted_section != emitted_section:
                _append_section(output, wanted_section)
                emitted_section = wanted_section

            output.append(unit["text"])
            used_ref.add(ref_index)

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

    # Exactly one blank line between structural sections.  If an inferred
    # Sessions verse appeared before a later reference [Verse 2], all verses are
    # renumbered sequentially so the extended structure remains coherent.
    # Detect Chorus repeats hidden inside long Sessions-only verses before
    # final formatting/verse numbering.
    output = _restore_repeated_choruses(output, ref_items, diagnostics)
    output = _merge_adjacent_partial_chorus_artifacts(
        output, ref_items, diagnostics
    )

    # V52 Trusted Structure Guardian.
    # IMPORTANT: these are final repair passes only. The V49.1 reference
    # alignment itself is intentionally unchanged.
    output = _repair_broken_opening_from_trusted_reference(
        output, ref_items, trans_lines, xingyu_lines, diagnostics
    )
    output = _repair_chorus_across_headers(output, ref_items, diagnostics, trans_lines, xingyu_lines)
    output = _repair_chorus_postchorus_family_inside_verses(
        output, ref_items, diagnostics
    )
    output = _repair_truncated_post_chorus_before_verse(
        output, ref_items, diagnostics
    )
    output = _repair_split_post_after_chorus(output, ref_items, diagnostics, trans_lines, xingyu_lines)
    output = _restore_supported_intro(output, ref_items, trans_lines, xingyu_lines, diagnostics)
    output = _guard_trusted_repeated_sections(output, ref_items, diagnostics)
    output = _repair_locally_supported_verses(output, ref_items, diagnostics)
    output = _complete_fragmented_chorus_patterns(output, ref_items, diagnostics)
    output = _restore_evidenced_chorus_tail(output, ref_items, list(trans_lines) + list(xingyu_lines or []), diagnostics)
    output = _copy_reference_intro(output, ref_items, diagnostics)
    output = _rebuild_timed_long_chorus_tail(
        output, ref_items, original_trans_lines, xingyu_lines, diagnostics
    )
    output = _remove_confirmed_boundary_echo(output, ref_items, list(trans_lines) + list(xingyu_lines or []), diagnostics)
    # Correct wording only after structural repair has used the raw evidence.
    # A lexical change must never alter Chorus/Verse boundary detection.
    protected_text = {u['text'] for u in ref_units}
    corrections = {}
    for source, other in ((original_xingyu_lines or [], original_trans_lines or []),
                          (original_trans_lines or [], original_xingyu_lines or [])):
        for row in source:
            if row['text'] not in output or row['text'] in protected_text:
                continue
            checked = _check_session_wording(row, other, ref_units, diagnostics)
            corrections[row['text']] = checked['text']
    output = [corrections.get(line, line) for line in output]
    output = _format_novel_verse_phrases(output, ref_items, diagnostics)

    # A reference Outro in the middle of a long Sessions continuation is not
    # the end of this recording. Keep its words as the new Verse opening.
    for i in range(len(output) - 1, -1, -1):
        if _section_name(output[i]) != 'outro':
            continue
        following = next((k for k in range(i+1, len(output)) if SECTION_RE.match(output[k])), None)
        if following is not None and _section_name(output[following]) == 'verse':
            output[i] = '[Verse]'
            del output[following]
            diagnostics.append('[SESSIONS CONTINUATION] Kept non-final Outro words inside the following Sessions Verse.')

    output = _preserve_early_sessions(output, ref_items, ref_units, matches, trans_lines, xingyu_lines, diagnostics)
    output = _preserve_local_rap_inflections(output, ref_units, matches, trans_lines, xingyu_lines, diagnostics)

    output = _format_sections_and_renumber_verses(output)
    _audit_reference_choruses(output, ref_items, diagnostics)

    return output, diagnostics, matches, unmatched, trailing_headers


def _make_reference_prompt(ref_units):
    """Use the shorter lyrics as a stronger Whisper vocabulary/context hint."""
    text = " ".join(u["text"] for u in ref_units)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 6500:
        return text
    return text[:4500] + " " + text[-2000:]


def _run_hidden(args, **kwargs):
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(args, **kwargs)


def _python_version(exe):
    try:
        p = _run_hidden([str(exe), "-c", "import sys; print('.'.join(map(str,sys.version_info[:3])))"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=15)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _has_syncalong(exe):
    try:
        p = _run_hidden(
            [str(exe), "-c",
             "import importlib.util; raise SystemExit(0 if "
             "importlib.util.find_spec('syncalong') else 1)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20
        )
        return p.returncode == 0
    except Exception:
        return False


def _find_python310_with_syncalong():
    """Find the old/stable Python 3.10 environment that owns Syncalong."""
    candidates = []

    # Current interpreter first.
    if sys.version_info[:2] == (3, 10):
        candidates.append(sys.executable)

    if os.name == "nt" and shutil.which("py"):
        try:
            p = _run_hidden(
                ["py", "-3.10", "-c", "import sys; print(sys.executable)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=15
            )
            if p.returncode == 0 and p.stdout.strip():
                candidates.append(p.stdout.strip())
        except Exception:
            pass

    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", "")
        if local:
            candidates += [
                str(Path(local) / "Programs/Python/Python310/python.exe"),
                str(Path(local) / "Python/Python310/python.exe"),
            ]
        if pf:
            candidates.append(str(Path(pf) / "Python310/python.exe"))

    for name in ("python3.10", "python310"):
        exe = shutil.which(name)
        if exe:
            candidates.append(exe)

    seen = set()
    for exe in candidates:
        exe = str(exe)
        if exe.lower() in seen or not Path(exe).exists():
            continue
        seen.add(exe.lower())
        if _python_version(exe).startswith("3.10.") and _has_syncalong(exe):
            return exe
    return None


def _ensure_hybrid_runs_on_syncalong_python():
    """If Windows opened this .py with Python 3.11, jump back to the 3.10
    interpreter where the user's existing Syncalong/Whisper installation lives.
    Python 3.11 remains reserved for Xingyu.
    """
    try:
        import syncalong  # noqa: F401
        return
    except Exception:
        pass

    py310 = _find_python310_with_syncalong()
    if not py310:
        return

    # Avoid any relaunch loop.
    if os.path.normcase(os.path.abspath(py310)) == os.path.normcase(os.path.abspath(sys.executable)):
        return

    script = os.path.abspath(__file__)
    args = [py310, script] + sys.argv[1:]
    subprocess.Popen(args, cwd=os.path.dirname(script) or None)
    raise SystemExit(0)


def _find_python311():
    """Xingyu's Python: runtime\\builder when the program has its own runtime, else a Windows Python 3.11."""
    if studio_runtime.UV.is_file():
        builder = studio_runtime.env_python(studio_runtime.BUILDER_ENV)
        return str(builder) if builder.is_file() else None
    candidates = []
    if os.name == "nt" and shutil.which("py"):
        try:
            p = _run_hidden(["py", "-3.11", "-c", "import sys; print(sys.executable)"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=15)
            if p.returncode == 0 and p.stdout.strip(): candidates.append(p.stdout.strip())
        except Exception: pass
    if os.name == "nt":
        local=os.environ.get("LOCALAPPDATA", ""); pf=os.environ.get("ProgramFiles", "")
        if local: candidates += [str(Path(local)/"Programs/Python/Python311/python.exe"), str(Path(local)/"Python/pythoncore-3.11-64/python.exe")]
        if pf: candidates.append(str(Path(pf)/"Python311/python.exe"))
    for name in ("python3.11", "python311"):
        x=shutil.which(name)
        if x: candidates.append(x)
    seen=set()
    for exe in candidates:
        if exe.lower() in seen or not Path(exe).exists(): continue
        seen.add(exe.lower())
        if _python_version(exe).startswith("3.11."): return exe
    return None


T9_WHISPER_SITE = Path(os.environ.get("APPDATA") or Path.home()) / "Adobe" / "CEP" / "extensions" / "T9Subtitles" / "whisper" / "env" / "Lib" / "site-packages"
T9_WHISPER_PTH = "t9_subtitles_whisper.pth"


def _link_t9_faster_whisper():
    """faster-whisper lives in the T9 Subtitles Premiere plugin; runtime\\builder imports it through a .pth file.

    Both environments use Python 3.11. The .pth path is appended after builder's own site-packages,
    so packages installed in builder (numpy, torch, av…) still win; only the missing faster-whisper
    and CTranslate2 come from the plugin. Without the plugin the stale .pth is removed."""
    if not studio_runtime.UV.is_file():
        return None
    site = studio_runtime.BUILDER_ENV / "Lib" / "site-packages"
    if not site.is_dir():
        return None
    pth = site / T9_WHISPER_PTH
    try:
        if (T9_WHISPER_SITE / "faster_whisper").is_dir():
            line = str(T9_WHISPER_SITE)
            if not pth.is_file() or pth.read_text(encoding="utf-8").strip() != line:
                pth.write_text(line + "\n", encoding="utf-8")
            return T9_WHISPER_SITE
        if pth.is_file():
            pth.unlink()
    except OSError:
        pass
    return None


def _xingyu_installed(py311):
    """Xingyu is usable only together with faster-whisper (its speech-recognition engine)."""
    if not py311: return False
    _link_t9_faster_whisper()
    try:
        p=_run_hidden([py311,"-c","import importlib.util as u; raise SystemExit(0 if u.find_spec('xingyu_lyrics_aligner') and u.find_spec('faster_whisper') else 1)"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        return p.returncode==0
    except Exception: return False


def _find_xingyu_command():
    py311=_find_python311()
    return [py311,"-m","xingyu_lyrics_aligner.cli"] if py311 and _xingyu_installed(py311) else None


def _install_python311(log=None):
    if os.name != "nt": raise RuntimeError("Automatyczna instalacja Python 3.11 jest przygotowana dla Windows.")
    if studio_runtime.UV.is_file():  # a second environment from the program's own Python 3.11
        if log: log("Creating the Xingyu environment (runtime\\builder)…")
        return str(studio_runtime.create_env(studio_runtime.BUILDER_ENV, log))
    existing=_find_python311()
    if existing: return existing
    if log: log("Python 3.11 not found. Installing it side-by-side…")
    winget=shutil.which("winget")
    if winget:
        p=subprocess.run([winget,"install","--id","Python.Python.3.11","-e","--accept-package-agreements","--accept-source-agreements","--silent"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace")
        if p.returncode==0:
            exe=_find_python311()
            if exe: return exe
    url="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    installer=Path(_tempfile.gettempdir())/"python-3.11.9-amd64.exe"
    if log: log("WinGet unavailable/failed; downloading official Python 3.11.9 installer…")
    urllib.request.urlretrieve(url,installer)
    p=subprocess.run([str(installer),"/quiet","InstallAllUsers=0","Include_launcher=1","Include_pip=1","PrependPath=0","Include_test=0","Shortcuts=0"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode!=0: raise RuntimeError(f"Python 3.11 installer failed (code {p.returncode}).")
    exe=_find_python311()
    if not exe: raise RuntimeError("Python 3.11 installed, but detection failed. Restart Hybrid and try again.")
    return exe


def _find_git():
    """Find Git even if a fresh install has not refreshed this process' PATH."""
    git = shutil.which("git")
    if git:
        return git
    if os.name == "nt":
        candidates = []
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name, "")
            if not base:
                continue
            candidates.extend([
                Path(base) / "Git" / "cmd" / "git.exe",
                Path(base) / "Git" / "bin" / "git.exe",
                Path(base) / "Programs" / "Git" / "cmd" / "git.exe",
            ])
        for p in candidates:
            if p.exists():
                return str(p)
    return None


def _install_git(log=None):
    """Install Git for Windows if missing, then return git.exe."""
    existing = _find_git()
    if existing:
        if log:
            log(f"Git found: {existing}")
        return existing

    if os.name != "nt":
        raise RuntimeError("Git was not found. Automatic Git installation is prepared for Windows.")

    if log:
        log("Git not found. Installing Git for Windows…")

    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError(
            "Git is missing and WinGet was not found. Install Git for Windows, "
            "then run Auto setup again."
        )

    p = subprocess.run(
        [winget, "install", "--id", "Git.Git", "-e",
         "--accept-package-agreements", "--accept-source-agreements", "--silent"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace"
    )
    if p.returncode != 0:
        raise RuntimeError(
            "Automatic Git installation failed:\n" +
            (p.stderr or p.stdout or "")[-3500:]
        )

    git = _find_git()
    if not git:
        raise RuntimeError(
            "Git installer finished, but git.exe could not be detected. "
            "Restart Hybrid and click Auto setup again."
        )

    if log:
        log(f"Git installed: {git}")
    return git


def _env_with_git(git_exe):
    env = os.environ.copy()
    git_dir = str(Path(git_exe).parent)
    git_root = Path(git_exe).parent.parent
    extra = [git_dir, str(git_root / "bin")]
    old = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(extra + [old])
    return env


def _install_xingyu_for_py311(py311,log=None):
    if studio_runtime.UV.is_file():  # program runtime: uv installs Xingyu from GitHub's zip — no Git
        if log:
            log("Installing Xingyu + candidate-lyrics dependencies (large download)…")
        studio_runtime.pip_install(py311, [f"xingyu-lyrics-aligner[candidate-lyrics] @ {XINGYU_ZIP}"], log)
        if not _xingyu_installed(py311):
            raise RuntimeError("Xingyu could not be imported after installation.")
        if log:
            log("Xingyu installed successfully.")
        return True
    # Python 3.11 may already exist from an earlier partial setup; do not reinstall it.
    if log:
        log("Preparing separate Python 3.11 environment…")
    _run_hidden(
        [py311, "-m", "ensurepip", "--upgrade"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    p = _run_hidden(
        [py311, "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace"
    )
    if p.returncode != 0:
        raise RuntimeError("pip update failed:\n" + (p.stderr or "")[-3000:])

    git = _install_git(log)
    env = _env_with_git(git)

    # Verify that pip/subprocess can really see Git in THIS already-running GUI process.
    p = _run_hidden(
        [git, "--version"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=env
    )
    if p.returncode != 0:
        raise RuntimeError("Git was found/installed but could not be executed.")
    if log:
        log("Git ready: " + p.stdout.strip())

    if log:
        log("Installing Xingyu + candidate-lyrics dependencies…")
    spec = (
        "xingyu-lyrics-aligner[candidate-lyrics] @ "
        "git+https://github.com/wangjiqing/xingyu-lyrics-aligner.git"
    )
    p = _run_hidden(
        [py311, "-m", "pip", "install", spec],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=env
    )
    if p.returncode != 0:
        raise RuntimeError(
            "Xingyu installation failed.\n" +
            (p.stderr or p.stdout or "")[-5000:]
        )
    if not _xingyu_installed(py311):
        raise RuntimeError("Xingyu could not be imported after installation.")
    if log:
        log("Xingyu installed successfully.")
    return True


def _xingyu_cuda_root():
    """Private CUDA runtime directory used only by Xingyu; nothing is copied to System32.

    With the program runtime the DLLs come from the nvidia-cublas-cu12 / nvidia-cudnn-cu12 packages
    inside runtime\\builder."""
    if studio_runtime.UV.is_file():
        return studio_runtime.BUILDER_ENV / "Lib" / "site-packages" / "nvidia"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "TRIPLENINE" / "XINGYUCUDA12"


def _find_xingyu_cuda_dirs():
    root = _xingyu_cuda_root()
    if not root.exists():
        return []

    wanted = ("cublas64_12.dll", "cudnn64_9.dll")
    dirs = []
    for dll in wanted:
        for p in root.rglob(dll):
            d = str(p.parent)
            if d not in dirs:
                dirs.append(d)
    return dirs


def _xingyu_cuda_ready():
    root = _xingyu_cuda_root()
    return (
        any(root.rglob("cublas64_12.dll")) and
        any(root.rglob("cudnn64_9.dll"))
    ) if root.exists() else False


def _has_nvidia_gpu():
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if candidate.exists():
            exe = str(candidate)
    if not exe:
        return False
    try:
        p = _run_hidden(
            [exe, "-L"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=15
        )
        return p.returncode == 0 and "GPU" in p.stdout
    except Exception:
        return False


def _env_with_xingyu_cuda(base_env=None):
    env = dict(base_env or os.environ)
    dirs = _find_xingyu_cuda_dirs()
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def _find_7zip():
    """Find 7-Zip executable even if a fresh install has not refreshed PATH."""
    for name in ("7z", "7zz"):
        exe = shutil.which(name)
        if exe:
            return exe

    if os.name == "nt":
        candidates = []
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name, "")
            if not base:
                continue
            candidates.extend([
                Path(base) / "7-Zip" / "7z.exe",
                Path(base) / "7-Zip" / "7zz.exe",
                Path(base) / "Programs" / "7-Zip" / "7z.exe",
            ])
        for p in candidates:
            if p.exists():
                return str(p)
    return None


def _install_7zip(log=None):
    existing = _find_7zip()
    if existing:
        if log:
            log(f"7-Zip found: {existing}")
        return existing

    if os.name != "nt":
        raise RuntimeError("7-Zip was not found. Automatic installation is prepared for Windows.")

    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError(
            "7-Zip is required to unpack the CUDA runtime, but WinGet was not found."
        )

    if log:
        log("7-Zip not found. Installing 7-Zip automatically…")

    p = subprocess.run(
        [winget, "install", "--id", "7zip.7zip", "-e",
         "--accept-package-agreements", "--accept-source-agreements", "--silent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if p.returncode != 0:
        raise RuntimeError(
            "Automatic 7-Zip installation failed:\n" +
            (p.stderr or p.stdout or "")[-3500:]
        )

    exe = _find_7zip()
    if not exe:
        raise RuntimeError(
            "7-Zip installation finished, but 7z.exe could not be detected. "
            "Restart Hybrid and run Install/Repair Xingyu GPU again."
        )

    if log:
        log(f"7-Zip installed: {exe}")
    return exe


def _install_xingyu_cuda_runtime(py311, log=None):
    """Download CUDA 12 cuBLAS + cuDNN 9 runtime DLLs into a private app folder.

    We intentionally do NOT modify System32 and do NOT replace the user's existing
    CUDA used by Python 3.10. The DLL directories are injected only into Xingyu's
    subprocess PATH.
    """
    if os.name != "nt":
        raise RuntimeError("Automatyczny Xingyu GPU runtime jest obecnie przygotowany dla Windows.")
    if not _has_nvidia_gpu():
        raise RuntimeError("Nie wykryto karty NVIDIA przez nvidia-smi.")

    if studio_runtime.UV.is_file() and not _xingyu_cuda_ready():
        if log:
            log("Installing CUDA 12 cuBLAS + cuDNN 9 for Xingyu (packages in runtime\\builder)…")
        studio_runtime.pip_install(py311, ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"], log)

    if _xingyu_cuda_ready():
        if log:
            log(f"Xingyu CUDA runtime already ready: {_xingyu_cuda_root()}")
        return _xingyu_cuda_root()

    root = _xingyu_cuda_root()
    root.mkdir(parents=True, exist_ok=True)

    if log:
        log("Downloading CUDA 12 cuBLAS + cuDNN 9 runtime for Xingyu…")
        log("Runtime will be kept in Triplenine's private folder; System32 is not modified.")

    api = "https://api.github.com/repos/Purfview/whisper-standalone-win/releases/tags/libs"
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": "Triplenine-Hybrid-Xingyu-Setup",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            release = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Could not query CUDA runtime release: {e}") from e

    assets = release.get("assets") or []
    # Current faster-whisper/CTranslate2 needs CUDA 12 + cuDNN 9.
    #
    # Purfview's actual Windows asset names use:
    #   cuBLAS.and.cuDNN_CUDA12_win_v1.7z  -> cuDNN 8 (WRONG for us)
    #   cuBLAS.and.cuDNN_CUDA12_win_v2.7z  -> cuDNN 9
    #   cuBLAS.and.cuDNN_CUDA12_win_v3.7z  -> cuDNN 9
    #
    # Older V16/V21 looked for "CUDA12_v3" inside the filename. That pattern
    # does not occur in the real asset name, so the generic fallback could pick
    # v1. We now match the real "_win_vN" suffix and explicitly reject v1.
    chosen = None
    cuda12_assets = []
    for a in assets:
        name = (a.get("name") or "")
        low = name.lower()
        if "cublas.and.cudnn_cuda12_win_" not in low:
            continue
        m = re.search(r"_win_v(\d+)(?:\.|$)", low)
        version = int(m.group(1)) if m else -1
        cuda12_assets.append((version, a))

    # Highest cuDNN-9 bundle first: v3, then v2. Never silently use v1.
    compatible = [(v, a) for v, a in cuda12_assets if v >= 2]
    if compatible:
        compatible.sort(key=lambda item: item[0], reverse=True)
        chosen = compatible[0][1]

    if not chosen:
        available = ", ".join(
            (a.get("name") or "?") for _, a in sorted(cuda12_assets, reverse=True)
        )
        raise RuntimeError(
            "Could not find a compatible CUDA12 + cuDNN9 Windows bundle. "
            "Need Purfview CUDA12 win v2 or newer. Found: " + (available or "none")
        )

    if not chosen:
        names = ", ".join((a.get("name") or "?") for a in assets)
        raise RuntimeError(
            "Could not find a CUDA12/cuDNN9 runtime asset in the release. "
            "Available assets: " + names
        )

    url = chosen.get("browser_download_url")
    name = chosen.get("name") or "xingyu_cuda_runtime.7z"
    if not url:
        raise RuntimeError("CUDA runtime asset has no download URL.")

    archive = root / name
    if log:
        log(f"Downloading: {name}")
    try:
        urllib.request.urlretrieve(url, archive)
    except Exception as e:
        raise RuntimeError(f"CUDA runtime download failed: {e}") from e

    extract_dir = root / "RUNTIME"
    if extract_dir.exists():
        if log:
            log("Removing previous CUDA runtime (including any old cuDNN 8 bundle)…")
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    low_name = name.lower()
    if low_name.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract_dir)
    elif low_name.endswith(".7z"):
        sevenzip = _install_7zip(log)
        if log:
            log("Extracting CUDA runtime with 7-Zip (supports BCJ2)…")

        p = _run_hidden(
            [sevenzip, "x", str(archive), f"-o{extract_dir}", "-y", "-bb0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if p.returncode != 0:
            raise RuntimeError(
                "Could not extract CUDA runtime with 7-Zip:\n" +
                (p.stderr or p.stdout or "")[-4000:]
            )
    else:
        raise RuntimeError(f"Unsupported CUDA runtime archive type: {name}")

    # Keep only the extracted payload; the archive can be large.
    try:
        archive.unlink()
    except Exception:
        pass

    if not _xingyu_cuda_ready():
        found = []
        for pattern in ("cublas*.dll", "cudnn*.dll"):
            found.extend(p.name for p in extract_dir.rglob(pattern))
        found_text = ", ".join(sorted(set(found))) or "none"
        raise RuntimeError(
            "Runtime was extracted, but the required CUDA 12 / cuDNN 9 DLLs "
            "could not be found. DLLs found: " + found_text
        )

    if log:
        log("Xingyu CUDA runtime installed successfully.")
        for d in _find_xingyu_cuda_dirs():
            log("CUDA DLL path: " + d)
    return root


def _extract_xingyu_segments(obj):
    """Tolerantly read transcript.segments.json across Xingyu versions."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            text = x.get("text")
            start = x.get("start", x.get("start_time", x.get("startTime")))
            end = x.get("end", x.get("end_time", x.get("endTime")))
            if isinstance(text, str) and text.strip() and start is not None and end is not None:
                try:
                    st = float(start)
                    en = float(end)
                    # Some schemas use milliseconds.
                    if st > 10000 or en > 10000:
                        st /= 1000.0
                        en /= 1000.0
                    found.append({"text": text.strip(), "start": st, "end": en})
                except (TypeError, ValueError):
                    pass
            for value in x.values():
                if isinstance(value, (list, dict)):
                    walk(value)
        elif isinstance(x, list):
            for value in x:
                walk(value)

    walk(obj)

    # De-duplicate recursively discovered copies.
    unique = []
    seen = set()
    for seg in sorted(found, key=lambda z: (z["start"], z["end"], z["text"])):
        key = (round(seg["start"], 3), round(seg["end"], 3), seg["text"])
        if key not in seen:
            seen.add(key)
            unique.append(seg)
    return unique


def run_xingyu_candidate(audio, language="en", preset="recommended", device="CPU", log=None):
    """Run Xingyu candidate extraction and return (timed_segments, actual_device)."""
    cmd = _find_xingyu_command()
    if not cmd:
        raise RuntimeError(
            "Xingyu nie jest gotowy w osobnym Pythonie 3.11.\n\nKliknij: Check / Setup Builder"
        )

    work = Path(_tempfile.mkdtemp(prefix="TRIPLENINE_XINGYU_"))
    out_dir = work / "CANDIDATE"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Demucs names its output directory after the audio stem. On Windows a
    # filename such as "Arctic Tundra (WOD) .mp3" therefore produces a folder
    # ending with a space and fails while saving vocals.wav. Keep the user's
    # file unchanged and give Xingyu a safe private copy for this run.
    source_audio = Path(audio).resolve()
    xingyu_audio = source_audio
    if source_audio.stem != source_audio.stem.rstrip(" ."):
        suffix = source_audio.suffix.lower() or ".audio"
        xingyu_audio = work / ("audio" + suffix)
        shutil.copy2(source_audio, xingyu_audio)
        if log:
            log(
                "Audio filename ends with a space/dot before its extension. "
                "Using a safe temporary copy for Xingyu/Demucs."
            )

    args = cmd + [
        "candidate", "extract",
        "--audio", str(xingyu_audio),
        "--output-dir", str(out_dir),
        "--preset", preset,
    ]

    requested_device = (device or "CPU").upper()
    actual_device = requested_device

    if requested_device == "AUTO":
        actual_device = "CUDA" if (_has_nvidia_gpu() and _xingyu_cuda_ready()) else "CPU"

    if actual_device == "CPU":
        args += ["--device", "cpu"]
    elif actual_device == "CUDA":
        args += ["--device", "cuda"]

    if language:
        args += ["--language", language]

    if log:
        log("Running Xingyu candidate extraction…")
        log(
            f"Xingyu preset: {preset} | Requested device: {requested_device} "
            f"| Actual device: {actual_device}"
        )
        if actual_device == "CUDA":
            if _xingyu_cuda_ready():
                log("Using Triplenine private CUDA 12/cuDNN 9 runtime.")
            else:
                log("Private CUDA runtime not installed; trying system CUDA libraries.")
        log(
            "Builder is working — this may take time depending on your GPU "
            "and selected model. The program is not frozen."
        )

    xenv = _env_with_xingyu_cuda()
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=xenv,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")
        cuda_problem = any(
            token in err.lower()
            for token in ("cublas64_12.dll", "cudnn", "cuda", "cannot be loaded")
        )

        if actual_device != "CPU" and cuda_problem:
            if log:
                log("Xingyu GPU/CUDA failed. Retrying automatically on CPU…")
                log(
                    "Builder is working — the CPU retry may take longer. "
                    "The program is not frozen."
                )
            actual_device = "CPU"

            cpu_args = []
            i = 0
            while i < len(args):
                if args[i] == "--device" and i + 1 < len(args):
                    i += 2
                    continue
                cpu_args.append(args[i])
                i += 1
            cpu_args += ["--device", "cpu"]

            proc = subprocess.run(
                cpu_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=xenv,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-5000:]
            raise RuntimeError("Xingyu candidate extraction failed:\n" + tail)

    seg_path = out_dir / "transcript.segments.json"
    if not seg_path.exists():
        # Be tolerant if a newer release nests outputs.
        matches = list(work.rglob("transcript.segments.json"))
        if matches:
            seg_path = matches[0]

    if not seg_path.exists():
        raise RuntimeError(
            "Xingyu finished, but transcript.segments.json was not found."
        )

    data = json.loads(seg_path.read_text(encoding="utf-8"))
    segments = _extract_xingyu_segments(data)
    if not segments:
        raise RuntimeError(
            "Xingyu transcript.segments.json did not contain readable timed segments."
        )

    if log:
        log(f"Xingyu returned {len(segments)} timed candidate segments.")

    return segments, actual_device


def _xingyu_lines_for_interval(xingyu_lines, start, end):
    """Return Xingyu candidate lines overlapping a Sessions-only interval."""
    if not xingyu_lines:
        return []
    picked = []
    for x in xingyu_lines:
        xs, xe = float(x["start"]), float(x["end"])
        overlap = max(0.0, min(end, xe) - max(start, xs))
        duration = max(0.01, xe - xs)
        if overlap >= 0.10 and overlap / duration >= 0.20:
            picked.append(x)
    return picked


def _settings_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    folder = Path(base) / "TRIPLENINE"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "HybridSessionsLyricsBuilder_settings.json"


def _short_elapsed_filename(seconds):
    seconds = max(0, int(round(seconds or 0)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}min{secs:02d}sec"
    if minutes:
        return f"{minutes}min{secs:02d}sec"
    return f"{secs}sec"


def _is_whisper_end_hallucination(text):
    """Known tiny Whisper outro hallucinations that should not become lyrics."""
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return normalized in {
        "thank you",
        "thanks for watching",
        "thank you for watching",
    }


def _remove_trailing_whisper_hallucinations(lines):
    """Remove only known hallucinations at the very end, never in the middle."""
    lines = list(lines or [])
    while lines and _is_whisper_end_hallucination(lines[-1].get("text", "")):
        lines.pop()
    return lines


def _format_elapsed(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class _TranscriptionEvents:
    def __init__(self, worker, events):
        self.worker, self.events = worker, events

    def put(self, event):
        if event[0] in ("done", "error"):
            fields = ("last_engine_mode", "last_xingyu_device", "last_xingyu_preset", "last_elapsed_seconds")
            self.events.put(("metadata", {name: getattr(self.worker, name, None) for name in fields}))
        self.events.put(event)


def _transcribe_in_process(events, args):
    # Reuse the same transcription pipeline, without constructing any GUI.
    worker = App.__new__(App)
    worker.q = _TranscriptionEvents(worker, events)
    worker.worker(*args)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Triple 9 Studio - Lyric Builder")
        try:
            if ICON_FILE:
                self.root.iconbitmap(ICON_FILE)
                self.root.iconbitmap(default=ICON_FILE)  # every later window too
                self.root._t9_icon_path = ICON_FILE
        except tk.TclError:
            pass
        try:  # message boxes with the triplenine icon (Windows' own ones have none)
            import studio_dialogs
            studio_dialogs.install(ICON_FILE or None)
        except ImportError:
            pass
        self.root.geometry("1120x740")
        studio_windows.configure(Path(__file__).resolve().with_name("lyrics_sync_state") / "window_positions.json")
        studio_windows.remember(self.root, key="Lyric Builder")  # same place and size as last time
        self.root.minsize(1000, 640)
        self.q = queue.Queue()
        self.running = False
        self.transcription = None
        self._worker_exit_seen = None
        self.last_result = ""
        self.active_marked_entry = None
        self._marked_tracks_geometry = None

        self.audio_var = tk.StringVar()
        self.model_var = tk.StringVar(value="turbo")
        self.language_var = tk.StringVar(value="en")
        self.threshold_var = tk.DoubleVar(value=0.46)
        self.engine_var = tk.StringVar(value="TripleNine Engine")
        self.xingyu_preset_var = tk.StringVar(value="full-recognition")
        self.xingyu_device_var = tk.StringVar(value="CPU")
        self.dark_theme_var = tk.BooleanVar(value=True)
        self.last_xingyu_device = None
        self.last_xingyu_preset = None
        self.last_engine_mode = None
        self.last_elapsed_seconds = None
        self.status_var = tk.StringVar(value="Ready")

        self.load_settings()
        self.build_ui()
        self.apply_theme()
        try:  # every button explains itself with a small tooltip
            import studio_hints
            studio_hints.Hints(self.root, None, lambda: {"bg": "#2a2a2a", "panel": "#2a2a2a", "fg": "#eeeeee"}).install()
        except ImportError:
            pass
        for done_path in reference_queue.reconcile_completed():
            self.q.put(("log", "Existing finished lyrics detected; marked Ready for Lyric Sync: " + done_path))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_queue)
        self.root.after(300, self.check_open_request)

    def check_open_request(self):
        """Downloader → View/Change Lyrics → Open in Lyric Builder leaves a small request file."""
        try:
            data = json.loads(OPEN_REQUEST_FILE.read_text(encoding="utf-8"))
            OPEN_REQUEST_FILE.unlink()
        except FileNotFoundError:
            data = None
        except (OSError, ValueError):
            data = None
            try:
                OPEN_REQUEST_FILE.unlink()
            except OSError:
                pass
        try:
            if data and time.time() - float(data.get("time", 0)) < 300:  # ignore leftovers from long ago
                self.load_song_request(data)
        finally:
            self.root.after(1000, self.check_open_request)

    def load_song_request(self, data):
        """Load the song's audio and paste its current TXT as the shorter reference."""
        audio, reference = str(data.get("audio", "")), str(data.get("reference", ""))
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:
            pass
        if self.running:
            messagebox.showinfo(APP_NAME, "Lyric Builder is busy. Stop or finish the current run, then click "
                                "Open in Lyric Builder again.", parent=self.root)
            return
        if not os.path.isfile(audio):
            messagebox.showerror(APP_NAME, "The song was not found:\n" + audio, parent=self.root)
            return
        try:
            text = reference_queue.reference_text(reference) if reference and os.path.isfile(reference) else ""
        except (OSError, UnicodeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not read the lyrics TXT:\n{exc}", parent=self.root)
            return
        if (self.reference.get("1.0", "end").strip() or self.result.get("1.0", "end").strip()) and \
                not messagebox.askokcancel(APP_NAME, f"Load {os.path.basename(audio)}?\n\nThe current shorter "
                                           "reference and result will be replaced; save the result first if needed.",
                                           parent=self.root):
            return
        self.audio_var.set(audio)
        self.active_marked_entry = None
        self.reference.delete("1.0", "end")
        self.reference.insert("1.0", text)
        self.clear_result()
        self.q.put(("log", "Opened from the Downloader: " + audio
                    + ("\nShorter reference pasted from: " + reference if text else "\n(no TXT lyrics to paste)")))

    def load_settings(self):
        try:
            path = _settings_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if "model" in data:
                self.model_var.set(data["model"])
            if "language" in data:
                self.language_var.set(data["language"])
            if "threshold" in data:
                self.threshold_var.set(float(data["threshold"]))
            self.engine_var.set("TripleNine Engine")
            saved_preset = data.get("xingyu_preset", "full-recognition")
            if saved_preset == "recommended":
                saved_preset = "normal"
            if saved_preset not in ("normal", "full-recognition"):
                saved_preset = "full-recognition"
            self.xingyu_preset_var.set(saved_preset)
            if "xingyu_device" in data:
                self.xingyu_device_var.set(data["xingyu_device"])
            if "dark_theme" in data:
                self.dark_theme_var.set(bool(data["dark_theme"]))
            self._saved_geometry = data.get("geometry")
            self._marked_tracks_geometry = data.get("marked_tracks_geometry")
        except Exception:
            self._saved_geometry = None

    def save_settings(self):
        data = {
            "model": self.model_var.get().strip(),
            "language": self.language_var.get().strip(),
            "threshold": float(self.threshold_var.get()),
            "engine": "TripleNine Engine",
            "xingyu_preset": self.xingyu_preset_var.get().strip(),
            "xingyu_device": self.xingyu_device_var.get().strip(),
            "dark_theme": bool(self.dark_theme_var.get()),
            "geometry": self.root.geometry(),
            "marked_tracks_geometry": self._marked_tracks_geometry,
        }
        _settings_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def on_close(self):
        self.stop()
        try:
            self.save_settings()
        except Exception:
            pass
        self.root.destroy()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        heading = ttk.Label(
            outer,
            text="LYRIC BUILDER",
            font=("Segoe UI", 20, "bold"),
            foreground="#858b95",
        )
        try:  # the triplenine logo next to the name
            import studio_dialogs
            studio_dialogs.add_heading_logo(heading, ICON_FILE or None)
        except ImportError:
            pass
        heading.pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Builds lyrics from the song (Xingyu + Whisper): paste a shorter reference, or leave it empty for a "
                "full transcription.  Beta — results are not always perfect.  New here? Downloader → Help > How To Use."
            ),
            wraplength=1100,
        ).pack(anchor="w", pady=(2, 8))
        # (the device note is next to Advanced → Xingyu device now)
        self.help_hint = ttk.Label(
            outer,
            text="Need help or not sure what to do? In the Downloader window, open Help > How To Use.",
            font=("Segoe UI", 10, "bold"),
        )
        # (the help hint is part of the subtitle now)
        self.builder_notice_top = ttk.Label(
            outer,
            text="I'm still working on Builder and trying to improve it. It won't always be 100% perfect.",
            font=("Segoe UI", 10, "italic"),
        )
        # ("Beta" is in the subtitle now)

        audio = ttk.LabelFrame(outer, text="1. Song audio", padding=8)
        audio.pack(fill="x")
        ttk.Entry(audio, textvariable=self.audio_var).pack(side="left", fill="x", expand=True)
        ttk.Button(audio, text="Browse…", command=self.browse_audio).pack(side="left", padx=(8, 0))
        ttk.Button(audio, text="Play Song", command=self.play_song).pack(side="left", padx=(8, 0))
        ttk.Button(audio, text="Marked tracks…", command=self.open_marked_tracks).pack(side="left", padx=(8, 0))

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
        threshold_controls = ttk.Frame(settings)
        threshold_controls.pack(side="left")
        ttk.Label(threshold_controls, text="Reference match threshold:").pack(side="left")
        ttk.Spinbox(
            threshold_controls, from_=0.35, to=0.85, increment=0.01,
            textvariable=self.threshold_var, width=6
        ).pack(side="left", padx=(5, 15))
        # The colours follow the Downloader's Settings → Theme (see apply_theme).

        engines = ttk.Frame(outer)
        engines.pack(fill="x", pady=(0, 8))
        advanced = ttk.Frame(outer)  # Xingyu preset / device, shown by "Advanced ▸"
        ttk.Label(advanced, text="Xingyu preset:").pack(side="left")
        ttk.Combobox(
            advanced,
            textvariable=self.xingyu_preset_var,
            width=16,
            state="readonly",
            values=("normal", "full-recognition"),
        ).pack(side="left", padx=(5, 10))

        ttk.Label(advanced, text="Xingyu device:").pack(side="left")
        ttk.Combobox(
            advanced,
            textvariable=self.xingyu_device_var,
            width=8,
            state="readonly",
            values=("CPU", "AUTO", "CUDA"),
        ).pack(side="left", padx=(5, 10))

        ttk.Label(advanced, text="(CUDA = NVIDIA card; CPU is slower)").pack(side="left", padx=(4, 0))
        ttk.Button(
            engines,
            text="Check / Setup Builder",
            command=self.smart_setup_builder,
        ).pack(side="left")
        advanced_open = {"value": False}

        def toggle_advanced():
            advanced_open["value"] = not advanced_open["value"]
            if advanced_open["value"]:
                advanced.pack(fill="x", pady=(0, 8), after=engines)
            else:
                advanced.pack_forget()
            advanced_btn.configure(text="Advanced  ▾" if advanced_open["value"] else "Advanced  ▸")

        advanced_btn = ttk.Button(engines, text="Advanced  ▸", command=toggle_advanced)
        advanced_btn.pack(side="left", padx=(8, 0))

        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(0, 8))

        left = ttk.LabelFrame(panes, text="2. Reference lyrics (optional)", padding=7)
        right = ttk.LabelFrame(panes, text="3. Hybrid result", padding=7)
        panes.add(left, weight=1)
        panes.add(right, weight=1)

        reference_box = ttk.Frame(left)
        reference_box.pack(fill="both", expand=True)
        reference_scroll = ttk.Scrollbar(reference_box, orient="vertical")
        reference_scroll.pack(side="right", fill="y")
        self.reference = tk.Text(
            reference_box, height=8, width=35, wrap="word", undo=True, font=("Consolas", 10),
            yscrollcommand=reference_scroll.set
        )
        self.reference.pack(side="left", fill="both", expand=True)
        reference_scroll.config(command=self.reference.yview)

        result_box = ttk.Frame(right)
        result_box.pack(fill="both", expand=True)
        result_scroll = ttk.Scrollbar(result_box, orient="vertical")
        result_scroll.pack(side="right", fill="y")
        self.result = tk.Text(
            result_box, height=8, width=35, wrap="word", undo=True, font=("Consolas", 10),
            yscrollcommand=result_scroll.set
        )
        self.result.pack(side="left", fill="both", expand=True)
        result_scroll.config(command=self.result.yview)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 8))
        self.start_btn = ttk.Button(actions, text="▶ BUILD LYRICS", command=self.start, style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 6))
        ttk.Button(actions, text="Clear result", command=self.clear_result).pack(side="left")
        ttk.Button(
            actions,
            text="Save Finished TXT for Sync",
            command=self.save_result_as_txt,
            style="Accent.TButton",
        ).pack(side="left", padx=(6, 0))
        testing_controls = ttk.Frame(actions)
        testing_controls.pack(side="right")
        ttk.Button(
            testing_controls,
            text="Export Result + Log for Testing",
            command=self.export_result_and_log,
        ).pack(side="left", padx=(6, 0))
        self.status_label = ttk.Label(
            actions,
            textvariable=self.status_var,
            font=("Segoe UI", 11, "bold"),
        )
        self.status_label.pack(side="left", padx=(14, 0))

        diag_frame = ttk.LabelFrame(
            outer,
            text="Diagnostic log — shows Reference corrections vs Whisper-only Sessions lines",
            padding=7,
        )
        # Reserve the footer before the growing editors can consume its space.
        diag_frame.pack(side="bottom", fill="x", before=outer.winfo_children()[0])
        actions.pack(side="bottom", fill="x", pady=(6, 6), before=outer.winfo_children()[0])
        log_box = ttk.Frame(diag_frame)
        log_box.pack(fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_box, orient="vertical")
        log_scroll.pack(side="right", fill="y")
        self.log = tk.Text(
            log_box, height=5, wrap="word", font=("Consolas", 9),
            yscrollcommand=log_scroll.set
        )
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.config(command=self.log.yview)

        self.builder_notice_bottom = ttk.Label(
            outer,
            text="I'm still working on Builder and trying to improve it. It won't always be 100% perfect.",
            font=("Segoe UI", 10, "italic"),
        )

        studio_preferences.watch_visibility(self.root, [threshold_controls, testing_controls])

        if getattr(self, "_saved_geometry", None):
            try:
                # Keep the remembered position, but cap old tall/maximized dimensions.
                match = re.match(r"(\d+)x(\d+)(.*)", self._saved_geometry)
                if match:
                    width = max(1000, min(int(match[1]), 1120))
                    height = max(640, min(int(match[2]), 740))
                    self.root.geometry(f"{width}x{height}{match[3]}")
            except tk.TclError:
                pass

    def apply_theme(self):
        """Switch the whole Hybrid GUI between dark and light themes.

        Dark mode is enabled by default on every program launch.
        """
        try:  # same theme as the Downloader (Settings → Theme there)
            main_config = json.loads((Path(__file__).resolve().parent / "lyrics_gui_config.json").read_text(encoding="utf-8"))
            self.dark_theme_var.set(main_config.get("theme", "dark") != "light")
        except (OSError, ValueError):
            pass
        dark = bool(self.dark_theme_var.get())
        style = ttk.Style(self.root)

        # "clam" lets us control ttk colours consistently across Windows.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        if dark:
            bg = "#141414"
            panel = "#1c1c1c"
            field = "#1c1c1c"
            fg = "#eaeaea"
            muted = "#9ca3af"
            border = "#303030"
            select_bg = "#3b82f6"
            select_fg = "#ffffff"
            button_bg = "#2a2a2a"
            active_bg = "#303030"
        else:
            bg = "#f0f0f0"
            panel = "#f0f0f0"
            field = "#ffffff"
            fg = "#111111"
            muted = "#444444"
            border = "#b8b8b8"
            select_bg = "#0078d7"
            select_fg = "#ffffff"
            button_bg = "#e8e8e8"
            active_bg = "#d9d9d9"

        self.root.configure(bg=bg)

        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure(
            "TLabelframe",
            background=bg,
            foreground=fg,
            bordercolor=border,
        )
        style.configure(
            "TLabelframe.Label",
            background=bg,
            foreground=fg,
        )
        style.configure(
            "TButton",
            background=button_bg,
            foreground=fg,
            bordercolor=border,
            focusthickness=1,
            focuscolor=border,
            padding=5,
        )
        style.map(
            "TButton",
            background=[("active", active_bg), ("pressed", active_bg)],
            foreground=[("disabled", muted), ("!disabled", fg)],
        )
        # the main steps (BUILD, Save for Sync) in the accent colour
        style.configure("Accent.TButton", background=select_bg, foreground=select_fg, bordercolor=select_bg,
                        padding=5, font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("disabled", button_bg), ("active", select_bg), ("pressed", select_bg)],
                  foreground=[("disabled", muted), ("!disabled", select_fg)])
        style.configure(
            "TCheckbutton",
            background=bg,
            foreground=fg,
        )
        style.map(
            "TCheckbutton",
            background=[("active", bg)],
            foreground=[("disabled", muted), ("!disabled", fg)],
        )
        style.configure(
            "TEntry",
            fieldbackground=field,
            foreground=fg,
            insertcolor=fg,
            bordercolor=border,
        )
        style.configure(
            "TCombobox",
            fieldbackground=field,
            background=button_bg,
            foreground=fg,
            arrowcolor=fg,
            bordercolor=border,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", field)],
            foreground=[("readonly", fg)],
            selectbackground=[("readonly", field)],
            selectforeground=[("readonly", fg)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=field,
            background=button_bg,
            foreground=fg,
            arrowcolor=fg,
            bordercolor=border,
        )
        style.configure(
            "TPanedwindow",
            background=bg,
        )

        # tk.Text is not themed by ttk, so apply colours explicitly.
        for widget_name in ("reference", "result", "log"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(
                    bg=field,
                    fg=fg,
                    insertbackground=fg,
                    selectbackground=select_bg,
                    selectforeground=select_fg,
                    highlightbackground=border,
                    highlightcolor=border,
                    relief="flat" if dark else "sunken",
                )

        # Tk option database also affects Combobox pop-down list colours.
        try:
            self.root.option_add("*TCombobox*Listbox.background", field)
            self.root.option_add("*TCombobox*Listbox.foreground", fg)
            self.root.option_add("*TCombobox*Listbox.selectBackground", select_bg)
            self.root.option_add("*TCombobox*Listbox.selectForeground", select_fg)
        except tk.TclError:
            pass
        _set_titlebar_theme(self.root, dark)

    def smart_setup_builder(self):
        """Check first, then offer only the setup/repair that is actually needed."""
        def check_worker():
            try:
                self.q.put(("log", "\n=== CHECK / SETUP REFERENCE LYRICS BUILDER ==="))
                missing = []

                py310 = sys.executable if importlib.util.find_spec("syncalong") else _find_python310_with_syncalong()
                if py310:
                    self.q.put(("log", f"OK  Lyrics Sync Python + Syncalong: {py310}"))
                else:
                    missing.append("Python 3.10 + Syncalong")
                    self.q.put(("log", "FAIL  Python 3.10 with Syncalong was not found."))

                py311 = _find_python311()
                if py311:
                    self.q.put(("log", f"OK  Xingyu Python 3.11: {py311}"))
                else:
                    missing.append("Python 3.11")
                    self.q.put(("log", "FAIL  The Xingyu Python 3.11 environment was not found."))

                if not studio_runtime.UV.is_file():  # Git is only needed by the older setup
                    git = _find_git()
                    if git:
                        self.q.put(("log", f"OK  Git: {git}"))
                    else:
                        missing.append("Git")
                        self.q.put(("log", "FAIL  Git was not found."))

                if py311 and _xingyu_installed(py311):
                    self.q.put(("log", "OK  Xingyu candidate-lyrics is installed."))
                    linked = _link_t9_faster_whisper()
                    self.q.put(("log", f"OK  faster-whisper: {'T9 Subtitles plugin – ' + str(linked) if linked else 'runtime builder'}"))
                else:
                    missing.append("Xingyu")
                    self.q.put(("log", "FAIL  Xingyu candidate-lyrics is not installed."))

                nvidia = _has_nvidia_gpu()
                if nvidia:
                    self.q.put(("log", "OK  NVIDIA GPU detected."))
                    if _xingyu_cuda_ready():
                        self.q.put(("log", "OK  Xingyu CUDA 12 cuBLAS + cuDNN 9 runtime is ready."))
                    else:
                        missing.append("Xingyu CUDA runtime")
                        self.q.put(("log", "WARN  NVIDIA detected, but Xingyu CUDA runtime needs setup/repair."))
                else:
                    self.q.put(("log", "INFO  No NVIDIA GPU detected — CPU mode is supported."))

                # Python 3.10 + Syncalong is the pre-existing side of the builder.
                # The smart installer manages only the Xingyu components introduced here.
                installable = [x for x in missing if x in {
                    "Python 3.11", "Git", "Xingyu", "Xingyu CUDA runtime"
                }]

                if not missing:
                    self.q.put(("log", "READY  Everything is installed and ready."))
                    self.root.after(0, lambda: messagebox.showinfo(
                        APP_NAME,
                        "Everything is ready.\n\nLyric Builder can run now."
                    ))
                    return

                details = "\n".join("• " + x for x in missing)

                if not installable:
                    self.root.after(0, lambda d=details: messagebox.showwarning(
                        APP_NAME,
                        "The check found a missing component:\n\n" + d +
                        "\n\nThe existing Python 3.10 + Syncalong environment is not "
                        "automatically replaced by this setup."
                    ))
                    return

                def offer():
                    if messagebox.askyesno(
                        APP_NAME,
                        "The builder is not fully ready.\n\nMissing / needs repair:\n" +
                        details +
                        "\n\nInstall or repair the supported missing components now?"
                    ):
                        threading.Thread(
                            target=self._smart_setup_install_worker,
                            args=(set(missing),),
                            daemon=True,
                        ).start()

                self.root.after(0, offer)

            except Exception as exc:
                self.q.put(("log", f"CHECK ERROR: {type(exc).__name__}: {exc}"))
                self.root.after(0, lambda e=str(exc): messagebox.showerror(APP_NAME, e))

        threading.Thread(target=check_worker, daemon=True).start()

    def _smart_setup_install_worker(self, missing):
        try:
            def log(msg):
                self.q.put(("log", str(msg)))

            log("")
            log("=== SMART SETUP ===")

            py311 = _find_python311()
            if "Python 3.11" in missing:
                py311 = _install_python311(log)
            if not py311:
                raise RuntimeError("Python 3.11 is unavailable after setup.")

            if "Git" in missing:
                _install_git(log)

            if "Xingyu" in missing:
                _install_xingyu_for_py311(py311, log)

            if "Xingyu CUDA runtime" in missing and _has_nvidia_gpu():
                _install_xingyu_cuda_runtime(py311, log)

            log("")
            log("Setup finished — verifying…")

            problems = []
            if not _find_python311():
                problems.append("Python 3.11")
            py311 = _find_python311()
            if not studio_runtime.UV.is_file() and not _find_git():
                problems.append("Git")
            if not (py311 and _xingyu_installed(py311)):
                problems.append("Xingyu")
            if _has_nvidia_gpu() and not _xingyu_cuda_ready():
                problems.append("Xingyu CUDA runtime")

            if problems:
                text = "Setup finished, but these items still need attention:\n\n" + \
                       "\n".join("• " + x for x in problems)
                log(text)
                self.root.after(0, lambda t=text: messagebox.showwarning(APP_NAME, t))
            else:
                log("READY  Installation/repair completed successfully.")
                self.root.after(0, lambda: messagebox.showinfo(
                    APP_NAME,
                    "Setup completed successfully.\n\n"
                    "The Xingyu side of Lyric Builder is ready."
                ))
        except Exception as exc:
            self.q.put(("log", f"SETUP ERROR: {type(exc).__name__}: {exc}"))
            self.root.after(0, lambda e=str(exc): messagebox.showerror(
                APP_NAME, "Setup failed:\n\n" + e
            ))


    def _close_marked_tracks(self, win):
        try:
            self._marked_tracks_geometry = win.geometry()
            self.save_settings()
        except (OSError, ValueError, tk.TclError):
            pass
        win.destroy()

    def open_marked_tracks(self):
        if self.running:
            messagebox.showinfo(APP_NAME, "Wait for the current transcription to finish.")
            return
        win = tk.Toplevel(self.root)
        win.after_idle(lambda: studio_windows.remember(win))  # its title is set right after this line
        try:
            if ICON_FILE:
                win.iconbitmap(ICON_FILE)
                win._t9_icon_path = ICON_FILE
        except tk.TclError:
            pass
        win.title("Marked tracks — shorter references")
        win.transient(self.root)
        win.minsize(650, 360)
        if self._marked_tracks_geometry:
            try:
                win.geometry(self._marked_tracks_geometry)
            except tk.TclError:
                self._marked_tracks_geometry = None
        if not self._marked_tracks_geometry:
            self.root.update_idletasks()
            width, height = 900, 520
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
            win.geometry(f"{width}x{height}+{x}+{y}")
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_marked_tracks(win))
        dark = bool(self.dark_theme_var.get())
        window_bg = "#141414" if dark else "#f0f0f0"
        field_bg = "#1c1c1c" if dark else "#ffffff"
        field_fg = "#eaeaea" if dark else "#111111"
        border = "#303030" if dark else "#b8b8b8"
        win.configure(bg=window_bg)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Choose a marked track. Its reference is loaded when available; missing entries load audio only.").pack(anchor="w")
        listing = tk.Listbox(
            frame,
            exportselection=False,
            bg=field_bg,
            fg=field_fg,
            selectbackground="#3b82f6" if dark else "#0078d7",
            selectforeground="#ffffff",
            highlightbackground=border,
            highlightcolor=border,
            relief="flat" if dark else "sunken",
        )
        listing.pack(fill="both", expand=True, pady=8)
        details = tk.StringVar()
        ttk.Label(frame, textvariable=details, wraplength=850).pack(anchor="w", pady=6)
        items = []

        def refresh():
            for done_path in reference_queue.reconcile_completed():
                self.q.put(("log", "Existing finished lyrics detected; marked Ready for Lyric Sync: " + done_path))
            items[:] = sorted(
                (entry for entry in reference_queue.entries() if not reference_queue.is_ready_for_sync(entry)),
                key=lambda entry: (
                    0 if reference_queue.ready(entry) else 1,
                    Path(entry["audio"]).name.casefold(),
                ),
            )
            listing.delete(0, "end")
            for entry in items:
                status = "Reference ready" if reference_queue.ready(entry) else "Reference missing"
                listing.insert("end", f"[{status}] {Path(entry['audio']).name}")
            details.set("No unfinished marked tracks. Completed tracks are hidden." if not items else "Select a track to see its reference source.")

        def selected():
            rows = listing.curselection()
            return items[rows[0]] if rows else None

        def show_details(event=None):
            entry = selected()
            if entry:
                details.set("Audio: " + entry["audio"] + "\nReference: " + entry.get("reference", "Missing")
                            + "\nSource: " + entry.get("source", "Not selected"))

        def use_entry(local=False):
            entry = selected()
            if not entry:
                return
            try:
                if not Path(entry["audio"]).is_file():
                    raise ValueError("Audio no longer exists. Refresh the list.")
                path = entry.get("reference", "")
                text = ""
                if local:
                    path = filedialog.askopenfilename(parent=win, title="Select shorter reference TXT",
                                                      filetypes=[("LYRICS", "*.txt")])
                    if not path:
                        return
                if path and (local or reference_queue.ready(entry)):
                    text = reference_queue.reference_text(path)
                    if not text:
                        raise ValueError("The reference TXT is empty.")
                if (self.reference.get("1.0", "end").strip() or self.result.get("1.0", "end").strip()):
                    if not messagebox.askokcancel(APP_NAME, "Load this track? The current reference and result will be cleared; save the result first if needed.", parent=win):
                        return
                if local and path != entry.get("reference"):
                    path = str(reference_queue.assign_reference(
                        entry, path, "User-selected TXT: " + path))
                self.audio_var.set(entry["audio"])
                self.active_marked_entry = dict(entry)
                self.reference.delete("1.0", "end")
                self.reference.insert("1.0", text)
                self.clear_result()
                if text:
                    self.q.put(("log", "Marked track loaded with shorter reference: " + path))
                else:
                    self.q.put(("log", "Marked track audio loaded without a shorter reference: " + entry["audio"]))
                self._close_marked_tracks(win)
            except (OSError, ValueError, UnicodeError) as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=win)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Load Marked Track", command=use_entry).pack(side="left", padx=8)
        ttk.Button(buttons, text="Choose reference TXT…", command=lambda: use_entry(True)).pack(side="left")
        listing.bind("<<ListboxSelect>>", show_details)
        listing.bind("<Double-Button-1>", lambda event: use_entry())
        refresh()

    def play_song(self):
        path = self.audio_var.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showinfo(APP_NAME, "Select an audio file or load a marked track first.")
            return
        try:
            os.startfile(str(Path(path).resolve()))
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Could not open the default audio player:\n" + str(exc))

    def browse_audio(self):
        path = filedialog.askopenfilename(
            title="Select song audio",
            filetypes=[
                ("Audio", "*.mp3 *.flac *.wav *.m4a *.ogg *.opus"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.audio_var.set(path)
            self.active_marked_entry = None

    def export_result_and_log(self):
        """Export Result + Log into the folder containing this .py file."""
        audio_path = self.audio_var.get().strip()
        if not audio_path:
            messagebox.showwarning(APP_NAME, "Select the song audio first.")
            return

        result_text = self.result.get("1.0", "end-1c").rstrip()
        log_text = self.log.get("1.0", "end-1c").rstrip()

        if not result_text and not log_text:
            messagebox.showwarning(
                APP_NAME,
                "There is no Result or Log to export yet."
            )
            return

        mp3_path = Path(audio_path)
        script_dir = Path(__file__).resolve().parent / "TESTING_EXPORTS"
        script_dir.mkdir(parents=True, exist_ok=True)

        engine = (self.last_engine_mode or self.engine_var.get().strip() or "Hybrid").strip()
        device = (
            self.last_xingyu_device
            or self.xingyu_device_var.get().strip()
            or "UNKNOWN"
        ).upper()
        if device == "CUDA":
            device = "GPU"

        preset = (
            self.last_xingyu_preset
            or self.xingyu_preset_var.get().strip()
            or "unknown"
        )

        mode_tag = f"Xingyu {device} - {preset}"

        if self.last_elapsed_seconds is not None:
            time_tag = _short_elapsed_filename(self.last_elapsed_seconds)
        else:
            time_tag = "time-unknown"

        safe_mode = re.sub(r'[<>:"/\\|?*]+', "-", mode_tag).strip()
        filename = (
            f"{mp3_path.stem} - Result + Log "
            f"[{safe_mode}] ({time_tag}).txt"
        )
        out_path = script_dir / filename

        combined = (
            "==================== RESULT ====================\n\n"
            + (result_text or "[EMPTY RESULT]")
            + "\n\n\n"
            + "===================== LOG ======================\n\n"
            + (log_text or "[EMPTY LOG]")
            + "\n"
        )

        try:
            out_path.write_text(combined, encoding="utf-8-sig")
            self.q.put((
                "log",
                f"Exported Result + Log testing file to program folder: {out_path}"
            ))
            messagebox.showinfo(
                APP_NAME,
                "Result + Log exported next to the running .py file:\n\n"
                + str(out_path)
            )
        except Exception as exc:
            messagebox.showerror(
                APP_NAME,
                "Could not export Result + Log:\n\n" + str(exc)
            )

    def save_result_as_txt(self):
        """Save a sync-ready Result in the audio's Lyrics folder."""
        audio_path = self.audio_var.get().strip()
        if not audio_path:
            messagebox.showwarning(
                APP_NAME,
                "Select the song audio first."
            )
            return

        result_text = self.result.get("1.0", "end-1c")
        if not result_text.strip():
            messagebox.showwarning(
                APP_NAME,
                "There is no result to save yet."
            )
            return

        mp3_path = Path(audio_path)
        lyrics_dir = mp3_path.parent / "LYRICS"
        txt_path = lyrics_dir / (mp3_path.stem + ".txt")

        try:
            lyrics_dir.mkdir(parents=True, exist_ok=True)
            # UTF-8 with BOM is friendly to Windows editors while remaining
            # standard UTF-8 for downstream lyrics/sync tools.
            txt_path.write_text(result_text.rstrip() + "\n", encoding="utf-8-sig")
            marker_message = ""
            marked = self.active_marked_entry
            if marked and os.path.normcase(os.path.abspath(marked.get("audio", ""))) == os.path.normcase(os.path.abspath(audio_path)):
                try:
                    done_path = reference_queue.mark_ready_for_sync(marked)
                    marker_message = "\n\nMarked track status changed to Ready for Lyric Sync:\n" + str(done_path)
                    self.q.put(("log", f"Marked track completed: {done_path}"))
                except (OSError, ValueError) as exc:
                    marker_message = "\n\nThe lyrics were saved, but the marked-track status could not be changed:\n" + str(exc)
            self.q.put((
                "log",
                f"Saved sync-ready TXT: {txt_path}"
            ))
            messagebox.showinfo(
                APP_NAME,
                "Result saved for lyrics sync:\n\n" + str(txt_path) + marker_message
            )
        except Exception as exc:
            messagebox.showerror(
                APP_NAME,
                "Could not save TXT:\n\n" + str(exc)
            )

    def clear_result(self):
        self.result.delete("1.0", "end")
        self.log.delete("1.0", "end")
        self.last_result = ""

    def clear_log(self):
        self.log.delete("1.0", "end")

    def save(self):
        text = self.result.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo(APP_NAME, "There is no hybrid result to save.")
            return
        audio = self.audio_var.get().strip()

        device_tag = (self.last_xingyu_device or self.xingyu_device_var.get().strip() or "UNKNOWN").upper()
        if device_tag == "CUDA":
            device_tag = "GPU"
        preset_tag = self.last_xingyu_preset or self.xingyu_preset_var.get().strip() or "unknown"
        suffix = f" - Hybrid [Xingyu {device_tag} - {preset_tag}]"

        if self.last_elapsed_seconds is not None:
            suffix += f" ({_short_elapsed_filename(self.last_elapsed_seconds)})"

        initial = (Path(audio).stem + suffix + ".txt") if audio else ("Hybrid Lyrics" + suffix + ".txt")
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
        self.run_started_at = time.perf_counter()
        self.clear_result()
        self.last_xingyu_device = None
        self.last_elapsed_seconds = None
        self.last_xingyu_preset = None
        self.last_engine_mode = "TripleNine Engine"
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status_var.set("Working…")

        args = (
            audio, reference, self.model_var.get().strip(),
            self.language_var.get().strip(),
            float(self.threshold_var.get()),
            "TripleNine Engine",
            self.xingyu_preset_var.get().strip(),
            self.xingyu_device_var.get().strip(),
            self.run_started_at,
        )
        try:
            self.transcription = TranscriptionProcess(_transcribe_in_process, (args,))
            self._worker_exit_seen = None
            self.stop_btn.configure(state="normal")
        except Exception as exc:
            self.running = False
            self.start_btn.configure(state="normal")
            self.status_var.set("Error")
            messagebox.showerror(APP_NAME, f"Could not start an isolated transcription: {exc}")

    def _release_transcription(self):
        task = self.transcription
        self.transcription = None
        if task:
            task.close()
        self._worker_exit_seen = None
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def stop(self):
        if self.transcription is None:
            return
        self.status_var.set("Stopping…")
        self._release_transcription()
        self.status_var.set("Stopped")
        self.putlog("Transcription stopped. Its Whisper/Xingyu processes have been terminated.")

    def worker(
        self, audio, reference, model, language, threshold,
        engine_mode, xingyu_preset, xingyu_device, run_started_at
    ):
        try:
            engine_mode = "TripleNine Engine"
            self.putlog(f"Audio: {audio}")
            self.putlog(f"Model: {model} | Language: {language or 'auto'}")
            self.putlog(f"TripleNine Engine: {engine_mode}")
            self.putlog("Loading Syncalong / Whisper…")

            try:
                import syncalong
            except Exception as exc:
                py310 = _find_python310_with_syncalong()
                hint = (
                    f"\n\nCurrent Hybrid interpreter: {sys.executable}"
                    f"\nPython version: {sys.version.split()[0]}"
                )
                if py310:
                    hint += f"\n\nSyncalong was found in Python 3.10:\n{py310}\nRestart the program."
                else:
                    hint += (
                        '\n\nNie instaluj Syncalong do Python 3.11. '
                        'Hybrid powinien działać na Twoim dotychczasowym Python 3.10.'
                    )
                raise RuntimeError(
                    "Syncalong is not available in the Python currently running Hybrid." + hint
                ) from exc

            no_reference_mode = not reference.strip()
            if no_reference_mode:
                ref_items = []
                ref_units = []
                initial_prompt = None
                self.putlog(
                    "Reference box is empty -> AUTO FULL TRANSCRIPTION MODE."
                )
                self.putlog(
                    "Xingyu/Whisper will transcribe the full song and repeated "
                    "lyric blocks will be used to infer Chorus/Verse structure."
                )
            else:
                ref_items = parse_reference(reference)
                ref_units, _ = build_reference_units(ref_items)
                if not ref_units:
                    raise RuntimeError("The pasted reference contains no lyric lines.")
                initial_prompt = _make_reference_prompt(ref_units)
                self.putlog(
                    f"Reference: {len(ref_units)} lyric lines. "
                    "Using it as Whisper initial_prompt."
                )

            xingyu_lines = None
            self.last_engine_mode = engine_mode
            self.putlog("")
            if no_reference_mode:
                self.putlog(
                    "No reference: Xingyu full-recognition is used as the "
                    "primary full-song transcription/segmentation source."
                )
            else:
                self.putlog(
                    "Xingyu will NOT rewrite trusted reference lines. "
                    "It is used only as a second ASR/segmentation source for "
                    "Sessions-only passages."
                )
            xingyu_lines, actual_xingyu_device = run_xingyu_candidate(
                audio,
                language=language or "en",
                preset=("recommended" if xingyu_preset == "normal" else "full-recognition"),
                device=xingyu_device or "CPU",
                log=self.putlog,
            )
            self.last_xingyu_device = actual_xingyu_device
            self.last_xingyu_preset = xingyu_preset
            self.putlog(
                f"Xingyu result metadata: device={actual_xingyu_device}, "
                f"preset={self.last_xingyu_preset}"
            )
            self.putlog("")

            self.putlog("Transcribing the FULL audio with Syncalong…")
            self.putlog(
                "Builder is working — this may take time depending on your GPU "
                "and selected model. The program is not frozen."
            )

            tx = syncalong.Transcriber(model_name=model)
            words = tx.transcribe(
                Path(audio),
                language=language or None,
                initial_prompt=initial_prompt or None,
                separate_vocals=False,
            )
            self.putlog(f"Whisper returned {len(words)} timestamped words.")

            trans_lines = group_whisper_words(words)
            before_cleanup = len(trans_lines)
            trans_lines = _remove_trailing_whisper_hallucinations(trans_lines)
            removed_end = before_cleanup - len(trans_lines)
            if removed_end:
                self.putlog(
                    f"Removed {removed_end} known trailing Whisper hallucination "
                    f"(for example: 'Thank you.')."
                )
            self.putlog(f"Grouped into {len(trans_lines)} transcript lines.")
            if no_reference_mode:
                self.putlog(
                    "Building full transcription and inferring song structure…"
                )
                output, diagnostics = _full_transcription_structure(
                    trans_lines, xingyu_lines=xingyu_lines
                )
                mapping, unmatched, trailing = [], [], []
                total = matched = 0
                pct = 0.0
            else:
                self.putlog("Aligning shorter reference inside full Sessions transcript…")
                output, diagnostics, mapping, unmatched, trailing = hybridize(
                    ref_items, trans_lines, threshold, xingyu_lines=xingyu_lines
                )
                recovered = _retry_missing_chorus_opening(
                    audio, ref_items, output, trans_lines, xingyu_lines, tx, language, self.putlog
                )
                if recovered is not trans_lines:
                    trans_lines = recovered
                    output, diagnostics, mapping, unmatched, trailing = hybridize(
                        ref_items, trans_lines, threshold, xingyu_lines=xingyu_lines
                    )
                total = len(ref_units)
                matched = total - len(unmatched)
                pct = (100.0 * matched / total) if total else 0.0

            result = "\n".join(output).strip()

            self.q.put(("result", result))
            self.putlog("")
            if no_reference_mode:
                self.putlog(
                    "Full transcription completed without reference lyrics."
                )
            else:
                self.putlog(
                    f"Reference lines used: {matched}/{total} ({pct:.1f}%). "
                    f"Matched blocks: {len(mapping)}. "
                    "Additional audio passages use Xingyu segmentation with Whisper alignment."
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

            elapsed = time.perf_counter() - run_started_at
            self.last_elapsed_seconds = elapsed
            elapsed_text = _format_elapsed(elapsed)

            self.putlog("")
            self.putlog("=" * 64)
            self.putlog(f"TOTAL HYBRID TIME: {elapsed_text} ({elapsed:.1f} seconds)")
            dev = (self.last_xingyu_device or xingyu_device or "unknown").upper()
            if dev == "CUDA":
                dev = "GPU"
            self.putlog(
                f"Result mode: Xingyu {dev} | preset: "
                f"{self.last_xingyu_preset or xingyu_preset or 'unknown'}"
            )
            self.putlog("=" * 64)

            self.q.put((
                "done",
                (
                    f"Text generated — review before saving — Full transcription mode | Total time: {elapsed_text}"
                    if no_reference_mode else
                    f"Text generated — review before saving — {matched}/{total} reference lines anchored | Total time: {elapsed_text}"
                )
            ))
        except Exception as exc:
            elapsed = time.perf_counter() - run_started_at
            self.putlog("")
            self.putlog(f"Stopped after: {_format_elapsed(elapsed)} ({elapsed:.1f} seconds)")
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    def poll_queue(self):
        task = self.transcription
        terminal = False
        if task:
            try:
                while True:
                    event = task.events.get_nowait()
                    terminal = terminal or event[0] in ("done", "error")
                    self.q.put(event)
            except queue.Empty:
                pass
            if not terminal and not task.process.is_alive():
                # Allow the IPC reader one more tick to receive the final event.
                if self._worker_exit_seen is None:
                    self._worker_exit_seen = time.perf_counter()
                elif time.perf_counter() - self._worker_exit_seen > 0.5:
                    self.q.put(("error", f"Transcription process exited unexpectedly (code {task.process.exitcode})."))
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "metadata":
                    for name, data in value.items():
                        setattr(self, name, data)
                elif kind == "log":
                    self.log.insert("end", value + "\n")
                    self.log.see("end")
                elif kind == "result":
                    self.result.delete("1.0", "end")
                    self.result.insert("1.0", value)
                    self.last_result = value
                elif kind == "done":
                    self._release_transcription()
                    self.status_var.set(value)
                elif kind == "error":
                    self._release_transcription()
                    self.status_var.set("Error")
                    self.log.insert("end", "\nERROR: " + value + "\n")
                    self.log.see("end")
                    messagebox.showerror(APP_NAME, value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)


def main():
    _ensure_hybrid_runs_on_syncalong_python()
    root = tk.Tk()
    studio_scrollbars.install(root)  # light grey scroll bars instead of the white ones
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
