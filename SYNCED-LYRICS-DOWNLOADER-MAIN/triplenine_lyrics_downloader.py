import os
import re
import sys
import json
import threading
import subprocess
import shutil
import importlib.util
import warnings
import time
import random
import email.utils
from datetime import datetime, timezone
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import webbrowser
import queue
import unicodedata
import difflib
import urllib.parse
import urllib.request
import urllib.error
import html as html_lib
import hashlib
from html.parser import HTMLParser


_WINDOWLESS_STANDARD_STREAMS = []


def _ensure_windowless_standard_streams():
    """Give console-oriented dependencies writable streams under pythonw.exe."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
        setattr(sys, name, stream)
        _WINDOWLESS_STANDARD_STREAMS.append(stream)

# Shared files only: never import Builder or its audio dependencies here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import studio_runtime
studio_runtime.prepare_environment()  # the program's own ffmpeg (runtime\ffmpeg) on PATH
import triplenine_reference_queue as reference_queue
import triplenine_lyrics_library as lyrics_library_transfer
import studio_resync
import studio_failures
import studio_warnings
import studio_dialogs
import studio_windows
import studio_hints
import studio_metadata
import studio_tracker
import studio_sheet
import studio_tracktable
import studio_scrollbars
import studio_shared
import studio_version
import studio_update
import studio_swap
from types import SimpleNamespace
import studio_backups
import studio_restore
import studio_preferences
import studio_audio_renames
import studio_lyrics_repair
import studio_lrc_editor
from studio_action_panel import ActionPanel
from studio_help import PAGES, NAVIGATION, render_page

# Every backup goes here: <folder that contains the app folder>\BACKUPS.
BACKUP_ROOT = Path(__file__).resolve().parent.parent / "BACKUPS"
# "Edit and Fix history" (newest first in the Edit and Fix Synced Lyrics window).
FIX_HISTORY_PATH = Path(__file__).resolve().parent / "lyrics_sync_state" / "fix_history.json"
STATE_DIR = Path(__file__).resolve().parent / "lyrics_sync_state"
studio_windows.configure(STATE_DIR / "window_positions.json")  # every window reopens where it was
# ⚠ / ❕ next to a song in Tracks: sync errors found by Edit and Fix (from its scan cache, so no rescan).
WARNING_INDEX = studio_warnings.WarningIndex(
    STATE_DIR / "scan_cache.json", STATE_DIR / "ignored_warnings.json",
    lambda text: _find_lrc_duplicate_timestamp_runs(text), studio_resync,
    lambda path, text: _find_lrc_runs_for_file(path, text))
# ❓: at the last sync most of the lyrics TXT did not match what Whisper heard.
LYRICS_MATCH = studio_warnings.LyricsMatchStore(STATE_DIR / "lyrics_match.json")
# The tag columns of Tracks (File name, Title, Artist …): read in the background, remembered on disk.
TAG_CACHE = studio_tracktable.TagCache(STATE_DIR / "tag_cache.json")


def _apply_tag_batch(batch):
    """Put freshly read tags into the rows that are still on screen (called in the window thread)."""
    try:
        row_of = {path: row for row, path in TRACK_ROW_PATHS.items()}
        for path, values in batch:
            row = row_of.get(path)
            if row is not None and row < track_list.size():
                track_list.set_tags(row, values)
    except Exception:
        pass


# Sorting by a tag column (click its heading: ascending → descending → off) and which columns are shown.
COLUMN_SORT = {"key": "", "reverse": False}  # filled from the saved settings once they are loaded


def _tags_of(path):
    return (TAG_CACHE.data.get(path) or [0, 0, []])[2]


def _column_sorted(rows):
    """``rows`` ((file, path, …) tuples) ordered by the tag column chosen in the table; songs without that tag go last."""
    key = COLUMN_SORT["key"]
    if not key:
        return rows
    position = studio_tracktable.TAG_KEYS.index(key)

    def value(row):
        values = _tags_of(row[1])
        text = (values[position] if position < len(values) else "").strip().casefold()
        if key == "tracknumber":
            digits = re.match(r"\d+", text)
            return (0, int(digits.group(0)), text) if digits else (1, 0, text)
        return (0, 0, text)

    filled = [row for row in rows if value(row)[2] != ""]
    empty = [row for row in rows if value(row)[2] == ""]
    filled.sort(key=value, reverse=COLUMN_SORT["reverse"])
    return filled + empty


def _tags_finished():
    """All tags are read: a list sorted by a tag column is put in order again."""
    if COLUMN_SORT["key"]:
        ui_call(refresh_track_badges)


TAG_READER = studio_tracktable.TagReader(
    TAG_CACHE, lambda path: studio_swap.read(path), lambda batch: ui_call(_apply_tag_batch, batch), _tags_finished)
TRACK_ROW_TIPS = {}  # Tracks row -> tooltip text for its ⚠ / ❕ / ❓


def _write_restore_map(folder, pairs):
    """Record where each backed-up file came from, so Options → Restore can put it back exactly."""
    try:
        (Path(folder) / "restore.json").write_text(
            json.dumps({str(original): str(copy) for original, copy in pairs}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass  # restore falls back to finding the file by name


def _remember_fix(action, path, detail=""):
    """Record one change in Edit and Fix history; history problems never block a save."""
    try:
        studio_resync.record_history(FIX_HISTORY_PATH, action, path, detail, BACKUP_ROOT)
    except Exception as exc:
        log(f"(Edit and Fix history not updated: {exc})")


def _restart_downloader_with_python310():
    """Run in the program's own runtime\\app Python (Install Triple 9 Studio.cmd); without it, in the
    older Windows Python 3.10. Hybrid/Xingyu manages its own process."""
    if getattr(sys, "frozen", False) or studio_runtime.running_in_runtime():
        return False
    prefer_windowless = Path(sys.executable).name.lower() in ("pythonw.exe", "pyw.exe")
    names = ("pythonw.exe", "python.exe") if prefer_windowless else ("python.exe", "pythonw.exe")
    if studio_runtime.active():
        python_dir = studio_runtime.APP_ENV / "Scripts"
    elif sys.version_info[:2] == (3, 10):
        return False
    else:
        python_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python310"
    target = next((python_dir / name for name in names if (python_dir / name).is_file()), None)
    if target is None:
        return False
    script = Path(__file__).resolve()
    subprocess.Popen([str(target), str(script), *sys.argv[1:]], cwd=str(script.parent))
    return True


if _restart_downloader_with_python310():
    raise SystemExit(0)

# ------------------ App / Config ------------------

APP_NAME = "Downloader"
STORAGE_NAME = "Triplenine Lyrics Downloader"

def _find_app_icon() -> str:
    """Find triplenine.ico next to the script/exe, including PyInstaller builds."""
    candidates = []
    try:
        candidates.append(Path(__file__).resolve().with_name("triplenine.ico"))
    except Exception:
        pass
    try:
        candidates.append(Path(sys.executable).resolve().with_name("triplenine.ico"))
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "triplenine.ico")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except Exception:
            pass
    # Keep a predictable fallback path for diagnostics.
    try:
        return str(Path(__file__).resolve().with_name("triplenine.ico"))
    except Exception:
        return "triplenine.ico"

ICON_FILE = _find_app_icon()


def apply_window_icon(window):
    """Give every visible Studio window the same application icon."""
    try:
        if ICON_FILE:
            window.iconbitmap(ICON_FILE)
            window._t9_icon_path = ICON_FILE
    except tk.TclError:
        pass


def create_toplevel(parent):
    window = tk.Toplevel(parent)
    apply_window_icon(window)
    studio_windows.remember(window)  # its place and size are saved and restored (keyed by its title)
    return window
CONFIG_FILE = str(Path(__file__).with_name("lyrics_gui_config.json"))

# The Genius token is intentionally NOT stored next to this script and is never
# embedded in the source code.  Each Windows user gets a private credentials
# file under %APPDATA%\Triplenine Lyrics Downloader.  This means you can send
# the .py/.exe (and even the normal lyrics_gui_config.json) to somebody else
# without accidentally sending your own Genius token with it.
def _credentials_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / STORAGE_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "triplenine-lyrics-downloader"

GENIUS_TOKEN_FILE = _credentials_dir() / "genius_api_credentials.json"

def get_genius_api_token() -> str:
    try:
        if not GENIUS_TOKEN_FILE.exists():
            return ""
        with GENIUS_TOKEN_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("access_token") or "").strip()
    except Exception:
        return ""

def save_genius_api_token(token: str) -> None:
    token = (token or "").strip()
    if not token:
        delete_genius_api_token()
        return
    GENIUS_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GENIUS_TOKEN_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"access_token": token}, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, GENIUS_TOKEN_FILE)

def delete_genius_api_token() -> None:
    try:
        GENIUS_TOKEN_FILE.unlink(missing_ok=True)
    except TypeError:
        if GENIUS_TOKEN_FILE.exists():
            GENIUS_TOKEN_FILE.unlink()
    except Exception:
        pass

# Persistent Genius match cache.  This is intentionally separate from both the
# executable/source and the API credentials.  It stores only already-verified
# song metadata/URLs, never the Genius access token.
def _cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / STORAGE_NAME / "CACHE"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "triplenine-lyrics-downloader"

GENIUS_MATCH_CACHE_FILE = _cache_dir() / "genius_match_cache.json"
# The lyrics themselves are cached separately from the match metadata.  Once a
# Genius page has been downloaded successfully, future restores can be 100%
# local: no Genius Search API request and no Genius lyrics-page request.
GENIUS_LYRICS_CACHE_DIR = _cache_dir() / "LYRICS"
TRACK_STATUS_CACHE_FILE = _cache_dir() / "track_status_cache.json"
_TRACK_STATUS_CACHE_LOCK = threading.Lock()
_TRACK_STATUS_CACHE_LOADED = False
_PERSISTENT_FAILED_TRACKS = set()

def _track_status_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or ""))

def _load_track_status_cache():
    global _TRACK_STATUS_CACHE_LOADED, _PERSISTENT_FAILED_TRACKS
    with _TRACK_STATUS_CACHE_LOCK:
        if _TRACK_STATUS_CACHE_LOADED:
            return _PERSISTENT_FAILED_TRACKS
        _TRACK_STATUS_CACHE_LOADED = True
        try:
            data = json.loads(TRACK_STATUS_CACHE_FILE.read_text(encoding="utf-8")) if TRACK_STATUS_CACHE_FILE.is_file() else {}
            _PERSISTENT_FAILED_TRACKS = {
                _track_status_key(str(x)) for x in data.get("failed_tracks", []) if str(x).strip()
            }
        except Exception:
            _PERSISTENT_FAILED_TRACKS = set()
        return _PERSISTENT_FAILED_TRACKS

def _save_track_status_cache():
    with _TRACK_STATUS_CACHE_LOCK:
        try:
            TRACK_STATUS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = TRACK_STATUS_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": 1, "failed_tracks": sorted(_PERSISTENT_FAILED_TRACKS)},
                ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, TRACK_STATUS_CACHE_FILE)
        except Exception:
            pass

def _set_persistent_track_failed(path: str, failed: bool):
    _load_track_status_cache()
    key = _track_status_key(path)
    changed = False
    with _TRACK_STATUS_CACHE_LOCK:
        if failed and key not in _PERSISTENT_FAILED_TRACKS:
            _PERSISTENT_FAILED_TRACKS.add(key); changed = True
        elif not failed and key in _PERSISTENT_FAILED_TRACKS:
            _PERSISTENT_FAILED_TRACKS.discard(key); changed = True
    if changed:
        _save_track_status_cache()

def _is_persistent_track_failed(path: str) -> bool:
    return _track_status_key(path) in _load_track_status_cache()

_GENIUS_MATCH_CACHE_LOCK = threading.Lock()
_GENIUS_MATCH_CACHE_LOADED = False
_GENIUS_MATCH_CACHE = {}

def _load_genius_match_cache() -> dict:
    global _GENIUS_MATCH_CACHE_LOADED, _GENIUS_MATCH_CACHE
    with _GENIUS_MATCH_CACHE_LOCK:
        if _GENIUS_MATCH_CACHE_LOADED:
            return _GENIUS_MATCH_CACHE
        _GENIUS_MATCH_CACHE_LOADED = True
        try:
            if GENIUS_MATCH_CACHE_FILE.exists():
                with GENIUS_MATCH_CACHE_FILE.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                entries = data.get("entries", data) if isinstance(data, dict) else {}
                if isinstance(entries, dict):
                    _GENIUS_MATCH_CACHE = entries
        except Exception:
            _GENIUS_MATCH_CACHE = {}
        return _GENIUS_MATCH_CACHE

def _save_genius_match_cache() -> None:
    with _GENIUS_MATCH_CACHE_LOCK:
        try:
            GENIUS_MATCH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = GENIUS_MATCH_CACHE_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": _GENIUS_MATCH_CACHE}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, GENIUS_MATCH_CACHE_FILE)
        except Exception as exc:
            # Cache failure should never break lyrics downloading.
            try:
                log(f"      Genius cache warning: could not save cache ({exc})")
            except Exception:
                pass

def _genius_match_cache_key(expected_title: str, artist_candidates: list[str]) -> str:
    # Use the strongest artist clue plus the original local title. This keeps
    # different local versions separate while still allowing the cached Genius
    # match to be reused on future program launches.
    artist = artist_candidates[0] if artist_candidates else ""
    return f"{_norm_match_text(artist)}\n{_norm_match_text(expected_title)}"

def _get_cached_genius_match(expected_title: str, artist_candidates: list[str]):
    cache = _load_genius_match_cache()
    key = _genius_match_cache_key(expected_title, artist_candidates)
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    result = entry.get("result")
    if not isinstance(result, dict) or not result.get("url"):
        return None
    try:
        score_percent = int(entry.get("score_percent", result.get("_match_score_percent", 0)))
    except Exception:
        score_percent = 0
    # Only reuse entries that met the current acceptance threshold.
    if score_percent < 51:
        return None
    restored = dict(result)
    restored["_match_score_percent"] = score_percent
    restored["_match_score"] = float(entry.get("score", score_percent / 100.0))
    restored["_match_reason"] = str(entry.get("reason") or restored.get("_match_reason") or "persistent cache")
    restored["_from_persistent_cache"] = True
    return restored

def _store_cached_genius_match(expected_title: str, artist_candidates: list[str], verified: dict) -> None:
    if not isinstance(verified, dict) or not verified.get("url"):
        return
    try:
        score_percent = int(verified.get("_match_score_percent", 0))
    except Exception:
        return
    if score_percent < 51:
        return
    # Keep only JSON-safe Genius result fields plus our match metadata.
    safe_result = {}
    for key, value in verified.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_result[key] = value
        elif isinstance(value, dict):
            safe_result[key] = value
        elif isinstance(value, list):
            safe_result[key] = value
    entry = {
        "result": safe_result,
        "score_percent": score_percent,
        "score": float(verified.get("_match_score", score_percent / 100.0)),
        "reason": str(verified.get("_match_reason") or ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _load_genius_match_cache()
    with _GENIUS_MATCH_CACHE_LOCK:
        _GENIUS_MATCH_CACHE[_genius_match_cache_key(expected_title, artist_candidates)] = entry
    _save_genius_match_cache()

def _genius_lyrics_cache_path(url: str) -> Path:
    # URL is the stable identity for the verified Genius page. Hashing keeps
    # filenames short/safe and avoids leaking song names into filesystem paths.
    digest = hashlib.sha256((url or "").strip().encode("utf-8", errors="ignore")).hexdigest()
    return GENIUS_LYRICS_CACHE_DIR / f"{digest}.txt"


def _get_cached_genius_lyrics(url: str) -> str:
    if not url:
        return ""
    try:
        path = _genius_lyrics_cache_path(url)
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
        # A tiny/corrupted file should not suppress a real Genius download.
        return text if len(text.strip()) >= 50 else ""
    except Exception:
        return ""


def _store_cached_genius_lyrics(url: str, lyrics: str) -> None:
    if not url or len((lyrics or "").strip()) < 50:
        return
    try:
        path = _genius_lyrics_cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(lyrics.rstrip() + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except Exception as exc:
        # Lyrics caching is an optimization only; never fail a download because
        # the local cache could not be written.
        try:
            log(f"      Genius lyrics-cache warning: could not save lyrics ({exc})")
        except Exception:
            pass


def _genius_cache_stats() -> tuple[int, int]:
    """Return (bytes used by Genius cache, number of cached lyrics pages)."""
    cache_root = _cache_dir()
    total = 0
    lyrics_count = 0
    try:
        if cache_root.exists():
            for path in cache_root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                        if path.parent == GENIUS_LYRICS_CACHE_DIR and path.suffix.lower() == ".txt":
                            lyrics_count += 1
                except OSError:
                    pass
    except Exception:
        pass
    return total, lyrics_count


def _format_cache_size(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            if value < 10:
                return f"{value:.2f} {unit}"
            if value < 100:
                return f"{value:.1f} {unit}"
            return f"{value:.0f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


def clear_genius_match_cache() -> None:
    global _GENIUS_MATCH_CACHE_LOADED, _GENIUS_MATCH_CACHE
    with _GENIUS_MATCH_CACHE_LOCK:
        _GENIUS_MATCH_CACHE = {}
        _GENIUS_MATCH_CACHE_LOADED = True
        try:
            GENIUS_MATCH_CACHE_FILE.unlink(missing_ok=True)
        except TypeError:
            if GENIUS_MATCH_CACHE_FILE.exists():
                GENIUS_MATCH_CACHE_FILE.unlink()
        except Exception:
            pass
        try:
            if GENIUS_LYRICS_CACHE_DIR.exists():
                shutil.rmtree(GENIUS_LYRICS_CACHE_DIR)
        except Exception:
            pass
    global _TRACK_STATUS_CACHE_LOADED, _PERSISTENT_FAILED_TRACKS
    with _TRACK_STATUS_CACHE_LOCK:
        _PERSISTENT_FAILED_TRACKS = set()
        _TRACK_STATUS_CACHE_LOADED = True
        try:
            TRACK_STATUS_CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

DEFAULT_GITHUB_URL = studio_version.GITHUB_URL
SYNCEDLYRICS_URL = "https://pypi.org/project/syncedlyrics/"

ALL_PROVIDERS = ["Lrclib", "Musixmatch", "Megalobiz", "NetEase", "Genius"]

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
TS_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
ICON_STRIP_RE = re.compile(r"^[\U00000080-\U0010ffff\ufe0f]+\s*")


def strip_icon(s: str) -> str:
    return ICON_STRIP_RE.sub("", s).strip()


# ------------------ Thread-safe UI queue ------------------

ui_q = queue.Queue()


def ui_call(fn, *args):
    ui_q.put((fn, args))


def pump_ui_queue():
    deadline = time.monotonic() + 0.015
    try:
        while time.monotonic() < deadline:
            fn, args = ui_q.get_nowait()
            try:
                fn(*args)
            except:
                pass
    except queue.Empty:
        pass
    if "update_action_context" in globals() and "undo_lyrics_btn" in globals():
        update_action_context()
    if "log_header" in globals():
        _sync_operation_row()
    root.after(50, pump_ui_queue)


# ------------------ Popup positioning ------------------

def popup_over_root(win: tk.Toplevel, width=560, height=180):
    apply_window_icon(win)
    win.update_idletasks()
    try:
        rx = root.winfo_rootx()
        ry = root.winfo_rooty()
        rw = root.winfo_width()
        rh = root.winfo_height()
        x = rx + (rw // 2) - (width // 2)
        y = ry + (rh // 2) - (height // 2)
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")
    except:
        win.geometry(f"{width}x{height}")

    win.transient(root)
    win.lift()
    win.focus_force()
    try:
        win.attributes("-topmost", True)
        win.after(150, lambda: win.attributes("-topmost", False))
    except:
        pass


# ------------------ Config ------------------

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass


config = load_config()

# Defaults
config.setdefault("music_dir", "")
config.setdefault("theme", "dark")
config.setdefault("lyrics_transfer_dir", "")

# Provider startup defaults: every application launch starts in the safest
# Genius-only mode. Users can enable other providers in Settings > Options for
# the current run, but the next application launch resets to Genius-only again.
config["providers_order"] = ["Genius", "Lrclib", "Musixmatch", "Megalobiz", "NetEase"]
config["providers_enabled"] = {p: (p == "Genius") for p in ALL_PROVIDERS}

config.setdefault("lang", "en")
# Genius supplies plain lyrics in this app, so plain fallback must be enabled
# when Genius is the default provider.
config["allow_plain_fallback"] = True
config.setdefault("upgrade_plain_to_synced", True)  # Ask to upgrade plain lyrics
config.setdefault("auto_upgrade_plain", False)  # Auto-upgrade without prompting
config.setdefault("strip_cjk", True)
config.setdefault("reject_non_ascii", True)
config.setdefault("reject_non_ascii_ratio", 0.15)
config.setdefault("lyrics_subfolder", False)
# Genius downloads start as plain .txt on every launch because Lyrics Sync
# uses the plain text as its alignment source. Users can still switch formats
# for the current run in Options; the next launch returns to .txt.
config["genius_output_format"] = "txt"  # txt or lrc

# Lyrics Sync preferences are persistent, but file paths are intentionally not.
# This keeps choices such as model / embedding / Demucs between openings while
# Audio, Lyrics and Output continue to start fresh (or use the selected track).
config.setdefault("sync_model", "turbo")
config.setdefault("sync_language", "en")
config.setdefault("sync_threshold", 55)
config.setdefault("sync_use_lyrics_prompt", True)
if not config.get("sync_prompt_default_on"):  # one-time: the prompt is the recommended default
    config["sync_use_lyrics_prompt"] = True
    config["sync_prompt_default_on"] = True
config.setdefault("sync_separate_vocals", False)
config.setdefault("sync_embed_mp3", False)

save_config(config)

MUSIC_DIR = config.get("music_dir", "")

# ------------------ Themes ------------------

THEMES = {
    "light": {
        "bg": "#f5f5f5",
        "panel": "#ffffff",
        "fg": "#111111",
        "btn_bg": "#e8e8e8",
        "btn_fg": "#111111",
        "sel_bg": "#2b6cff",
        "sel_fg": "#ffffff",
        "log_bg": "#ffffff",
        "log_fg": "#111111",
        "border": "#d0d0d0",
        "status_bg": "#eeeeee",
        "status_fg": "#111111",
        "link_fg": "#0b57d0",
        "legend_bg": "#f0f0f0",
        "legend_border": "#d0d0d0",
        "ok": "#167d2a",
        "warn": "#b26a00",
        "none": "#666666",
        "plain": "#2563eb",
        "incomp": "#c2410c",
    },
    "dark": {
        "bg": "#141414",
        "panel": "#1c1c1c",
        "fg": "#eaeaea",
        "btn_bg": "#2a2a2a",
        "btn_fg": "#eaeaea",
        "sel_bg": "#3b82f6",
        "sel_fg": "#ffffff",
        "log_bg": "#101010",
        "log_fg": "#eaeaea",
        "border": "#303030",
        "status_bg": "#1a1a1a",
        "status_fg": "#eaeaea",
        "link_fg": "#4ea1ff",
        "legend_bg": "#181818",
        "legend_border": "#303030",
        "ok": "#4ade80",
        "warn": "#fbbf24",
        "none": "#9ca3af",
        "plain": "#60a5fa",
        "incomp": "#fb923c",
    },
}

# ------------------ Windows Titlebar Theme ------------------

def set_titlebar_theme(is_dark: bool):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1 if is_dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except:
        pass


# ------------------ Helpers ------------------

def open_github():
    webbrowser.open(DEFAULT_GITHUB_URL)


_update_state = {"busy": False}


def check_for_updates(manual=False):
    """Look on GitHub for a newer version (a newer vX.Y.Z tag). At startup it is silent unless there is one."""
    if _update_state["busy"]:
        return
    if studio_update.is_development_copy():
        if manual:
            messagebox.showinfo("Updates", "This is a development copy (a git checkout), so it is not updated "
                                "automatically. Use git pull.", parent=root)
        return
    _update_state["busy"] = True

    def worker():
        try:
            info = studio_update.latest()
            ui_call(offer_update, info, manual, None)
        except Exception as exc:
            ui_call(offer_update, None, manual, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, daemon=True).start()


def offer_update(info, manual, error):
    _update_state["busy"] = False
    if error or not info:
        if manual:
            messagebox.showerror("Updates", "Could not check for updates:\n" + (error or "no version was found"),
                                 parent=root)
        return
    if not studio_update.is_newer(info["version"]):
        if manual:
            messagebox.showinfo("Updates", f"You have the latest version ({studio_version.VERSION}).", parent=root)
        return
    if not manual and config.get("update_skipped") == info["tag"]:
        return
    if downloading:
        if manual:
            messagebox.showinfo("Updates", "Wait for the current operation to finish, then check again.", parent=root)
        return
    notes = info["notes"][:700] + ("…" if len(info["notes"]) > 700 else "")
    if messagebox.askyesno(
            "Update available",
            f"Triple 9 Studio {info['version']} is available (you have {studio_version.VERSION}).\n\n"
            + (notes + "\n\n" if notes else "")
            + "Update now? Your settings, music and lyrics are not touched; the program restarts when it is done.",
            parent=root):
        run_update(info)
    elif not manual:
        config["update_skipped"] = info["tag"]  # do not ask again for this version at startup
        save_config(config)


def run_update(info):
    _update_state["busy"] = True
    set_status(f"Downloading Triple 9 Studio {info['version']}…")

    def worker():
        import tempfile
        try:
            archive = Path(tempfile.mkdtemp(prefix="t9update_")) / "update.zip"
            studio_update.download(info["zip"], archive, lambda done, total: ui_call(
                set_status, f"Downloading Triple 9 Studio {info['version']}… {done * 100 // total}%"))
            result = studio_update.apply(archive, backup_dir=BACKUP_ROOT / "PROGRAM-UPDATES")
            if result["install"]:  # the packages the program needs may have changed
                ui_call(set_status, "Updating the program's packages (a window opens)…")
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "studio_install.py")],
                               creationflags=flags, cwd=str(studio_update.program_root()))
            ui_call(update_done, info, result, "")
        except Exception as exc:
            ui_call(update_done, info, None, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, daemon=True).start()


def update_done(info, result, error):
    _update_state["busy"] = False
    if error:
        set_status("Update failed")
        messagebox.showerror("Update failed", "The update could not be installed:\n" + error +
                             "\n\nNothing was lost; you can try again from Help > Check for updates.", parent=root)
        return
    log(f"Updated to {info['version']}: {len(result['changed'])} file(s) replaced, {len(result['new'])} added")
    messagebox.showinfo("Updated", f"Triple 9 Studio {info['version']} is installed. The program restarts now.", parent=root)
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    launcher = str(pythonw if pythonw.is_file() else interpreter)
    script = studio_update.program_root() / "Triplenine_Lyrics_Studio.py"
    subprocess.Popen([launcher, str(script)], cwd=str(script.parent))
    root.destroy()
    sys.exit(0)


def get_lang_code():
    return config.get("lang", "en")


def allow_plain_fallback():
    return bool(config.get("allow_plain_fallback", False))


def get_enabled_providers_in_order():
    enabled = config.get("providers_enabled", {})
    order = config.get("providers_order", [])
    out = [p for p in order if enabled.get(p, False)]
    return out if out else ["Lrclib"]


def get_synced_provider_order():
    return [p for p in get_enabled_providers_in_order() if p != "Genius"]


def get_plain_provider_order():
    return get_enabled_providers_in_order()


def get_lyrics_path(song_path: str, extension: str = ".lrc", ensure_dir: bool = False) -> str:
    """Return the output path for a lyrics file according to Options.

    When `lyrics_subfolder` is enabled, files are stored in a Lyrics folder
    beside the audio track. Otherwise they sit next to the track itself.
    """
    if not extension.startswith("."):
        extension = "." + extension
    base_dir = os.path.dirname(song_path)
    if config.get("lyrics_subfolder", False):
        base_dir = os.path.join(base_dir, "LYRICS")
        if ensure_dir:
            os.makedirs(base_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(song_path))[0]
    return os.path.join(base_dir, stem + extension)


def get_genius_lyrics_path(song_path: str, ensure_dir: bool = False) -> str:
    ext = ".txt" if str(config.get("genius_output_format", "txt")).lower() == "txt" else ".lrc"
    return get_lyrics_path(song_path, ext, ensure_dir=ensure_dir)

def get_provider_output_path(song_path: str, provider: str, want_synced: bool, ensure_dir: bool = False) -> str:
    """Choose the correct extension for a provider.

    Genius is plain lyrics and obeys the .lrc/.txt option. Synced providers
    always use .lrc so timestamped lyrics keep the expected extension.
    """
    if provider == "Genius" and not want_synced:
        return get_genius_lyrics_path(song_path, ensure_dir=ensure_dir)
    return get_lyrics_path(song_path, ".lrc", ensure_dir=ensure_dir)


def find_existing_lyrics(song_path: str) -> str:
    """Find existing .lrc/.txt lyrics in either supported location.

    This deliberately checks both old and new layouts so changing the option
    does not make already-downloaded lyrics appear missing.
    """
    stem = os.path.splitext(os.path.basename(song_path))[0]
    track_dir = os.path.dirname(song_path)
    candidates = [
        get_genius_lyrics_path(song_path),
        get_lyrics_path(song_path, ".lrc"),
        get_lyrics_path(song_path, ".txt"),
        os.path.join(track_dir, stem + ".lrc"),
        os.path.join(track_dir, stem + ".txt"),
        os.path.join(track_dir, "LYRICS", stem + ".lrc"),
        os.path.join(track_dir, "LYRICS", stem + ".txt"),
        os.path.join(track_dir, "SYNCED LYRICS", stem + ".lrc"),
    ]
    seen = set()
    for candidate in candidates:
        norm = os.path.normcase(os.path.normpath(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isfile(candidate):
            return candidate
    return ""


_SYNCED_LYRICS_CACHE = {}
_LRC_TIMESTAMP_RE = re.compile(r"(?m)^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")


def _synced_lyrics_sources(song_path: str, force=False):
    """Return (embedded_timed_lyrics, timestamped_lrc) from a cached disk check."""
    audio = Path(song_path)
    try:
        audio_signature = (audio.stat().st_mtime_ns, audio.stat().st_size)
    except OSError:
        return False, False

    candidates = studio_resync.lrc_locations(audio)
    lrc_signature = []
    for candidate in candidates:
        try:
            stat = candidate.stat()
            lrc_signature.append((str(candidate), stat.st_mtime_ns, stat.st_size))
        except OSError:
            lrc_signature.append((str(candidate), 0, 0))
    signature = (audio_signature, tuple(lrc_signature))
    key = os.path.normcase(str(audio.resolve()))
    cached = _SYNCED_LYRICS_CACHE.get(key)
    # Windows file times tick about every 15 ms, so two quick tag writes can
    # keep the same mtime and size. Never trust the cache for a file changed
    # in the last two seconds.
    newest_ns = max([audio_signature[0]] + [entry[1] for entry in lrc_signature])
    recently_changed = time.time_ns() - newest_ns < 2_000_000_000
    if not force and cached and cached[0] == signature and not recently_changed:
        return cached[1]

    sidecar = False
    for candidate in candidates:
        try:
            if candidate.is_file() and _LRC_TIMESTAMP_RE.search(
                    candidate.read_text(encoding="utf-8-sig", errors="replace")):
                sidecar = True
                break
        except OSError:
            pass

    embedded = False
    if audio.suffix.lower() == ".mp3":
        try:
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tags = ID3(str(audio))
            except ID3NoHeaderError:
                tags = None
            if tags:
                for frame in tags.values():
                    frame_id = getattr(frame, "FrameID", "")
                    if frame_id == "SYLT" and getattr(frame, "text", None):
                        embedded = True
                        break
                    if frame_id == "USLT" and _LRC_TIMESTAMP_RE.search(str(getattr(frame, "text", ""))):
                        embedded = True
                        break
                    if (frame_id == "TXXX" and "lyric" in str(getattr(frame, "desc", "")).lower()
                            and any(_LRC_TIMESTAMP_RE.search(str(value)) for value in getattr(frame, "text", []))):
                        embedded = True
                        break
        except (ImportError, OSError, ValueError):
            pass

    sources = embedded, sidecar
    _SYNCED_LYRICS_CACHE[key] = (signature, sources)
    return sources


def _has_synced_lyrics(song_path: str, force=False) -> bool:
    return any(_synced_lyrics_sources(song_path, force=force))


def infer_artist_from_path(song_path: str) -> str:
    try:
        rel = os.path.relpath(song_path, MUSIC_DIR)
        return rel.split(os.sep, 1)[0] if os.sep in rel else os.path.basename(os.path.normpath(MUSIC_DIR))
    except:
        return ""


def get_track_metadata(song_path: str) -> tuple[str, str]:
    """Return (title, artist) from audio tags.

    On Windows, the Explorer field "Contributing artists" for MP3 files normally
    maps to the ID3 TPE1/artist tag. We prefer that tag over folder names.
    Folder and filename values are used only as fallbacks.
    """
    title = ""
    artist = ""

    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(song_path, easy=True)
        if audio is not None:
            title_values = audio.get("title", [])
            artist_values = audio.get("artist", [])
            if title_values:
                title = str(title_values[0]).strip()
            if artist_values:
                # Some formats can expose multiple artist values. Keep all of them,
                # because Genius validation can match any contributing artist.
                artist = "; ".join(str(v).strip() for v in artist_values if str(v).strip())
    except Exception:
        # Mutagen is optional at startup; the app still works with folder/file fallbacks.
        pass

    if not title:
        title = normalize_title(os.path.splitext(os.path.basename(song_path))[0])
    if not artist:
        artist = infer_artist_from_path(song_path)

    return title, artist


def _extract_artist_from_title(title: str) -> str:
    """Extract a likely leading artist from titles like 'Juice WRLD - Drive Me Crazy'.

    This is only an additional clue. Metadata artist stays first priority and the
    artist folder is also kept as a fallback/search candidate.
    """
    value = (title or "").strip()
    # Require spaces around the separator so ordinary hyphenated song titles are
    # not accidentally interpreted as ARTIST - TITLE.
    parts = re.split(r"\s+[\-–—]\s+", value, maxsplit=1)
    if len(parts) != 2:
        return ""
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right or len(left) > 80:
        return ""
    # Reject values that look much more like a title/version label than an artist.
    if re.fullmatch(r"(?i)(track|song|demo|remix|edit|version|v\s*\d+)\s*\d*", left):
        return ""
    return left


def get_artist_candidates(song_path: str, title: str, metadata_artist: str) -> list[str]:
    """Return artist clues in priority order: tags -> title prefix -> artist folder."""
    candidates = []

    def add(value: str):
        value = " ".join((value or "").split()).strip()
        if not value:
            return
        norm = _norm_match_text(value) if "_norm_match_text" in globals() else value.lower()
        if norm and all((_norm_match_text(x) if "_norm_match_text" in globals() else x.lower()) != norm for x in candidates):
            candidates.append(value)

    add(metadata_artist)
    add(_extract_artist_from_title(title))
    add(infer_artist_from_path(song_path))
    return candidates


def _strip_artist_prefix_from_title(title: str, artist_candidates: list[str]) -> str:
    """Remove a leading 'ARTIST - ' only when it matches one of our artist clues."""
    value = (title or "").strip()
    parts = re.split(r"\s+[\-–—]\s+", value, maxsplit=1)
    if len(parts) != 2:
        return value
    left, right = parts[0].strip(), parts[1].strip()
    left_norm = _norm_match_text(left) if "_norm_match_text" in globals() else left.lower()
    for artist in artist_candidates:
        artist_norm = _norm_match_text(artist) if "_norm_match_text" in globals() else artist.lower()
        if left_norm and artist_norm and (left_norm == artist_norm or difflib.SequenceMatcher(None, left_norm, artist_norm).ratio() >= 0.93):
            return right
    return value


def normalize_title(title: str) -> str:
    if " " in title:
        first, rest = title.split(" ", 1)
        if first.isdigit():
            title = rest
    title = title.replace("_", " ").replace("'", "'")
    for ch in ["!", "?", ":", ";"]:
        title = title.replace(ch, "")
    return " ".join(title.split())


# ------------------ Status scanning / caching ------------------

lyrics_cache = {}
missing_targets = []
scanned_artists = set()  # Track which artists have been scanned for artist-level completeness display
# Audio paths that were actually attempted during this app session.  This is
# intentionally per-track: downloading four selected tracks must not paint every
# untouched track in the artist folder with a red X.
attempted_track_paths = set()


def completeness_icon(have: int, total: int) -> str:
    if total <= 0 or have == 0:
        return "⬜"
    if have >= total:
        return "✅"
    return "🟨"


def analyze_lrc(lrc_path: str) -> str:
    if not os.path.exists(lrc_path):
        return "none"
    try:
        with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
    except:
        return "none"

    lyric_lines = []
    for ln in lines:
        if not ln:
            continue
        if ln.startswith(("[ar:", "[ti:", "[al:", "[by:", "[offset:", "[re:", "[ve:")):
            continue
        lyric_lines.append(ln)

    if len(lyric_lines) < 6:
        return "incomplete"

    ts_lines = sum(1 for ln in lyric_lines if TS_RE.match(ln))
    return "synced" if ts_lines >= 3 else "plain"


# ------------------ UI helpers ------------------

def _set_status(text: str, color: str = "normal"):
    status_var.set(text)
    t = THEMES[config.get("theme", "dark")]
    
    # Set color based on type
    if color == "success":
        status_label.configure(fg=t["ok"])
    elif color == "error":
        status_label.configure(fg=t["incomp"])
    elif color == "working":
        status_label.configure(fg=t["plain"])
    else:
        status_label.configure(fg=t["status_fg"])


def set_status(text: str, color: str = "normal"):
    if threading.current_thread() is threading.main_thread():
        _set_status(text, color)
    else:
        ui_call(_set_status, text, color)


def _log(msg: str):
    log_box.insert(tk.END, msg + "\n")
    log_box.see(tk.END)


def log(msg: str):
    if threading.current_thread() is threading.main_thread():
        _log(msg)
    else:
        ui_call(_log, msg)


# ------------------ Artist filtering ------------------

all_artists = []
ROOT_ARTIST_LABEL = None


def _artist_folder(name):
    return MUSIC_DIR if name == ROOT_ARTIST_LABEL else os.path.join(MUSIC_DIR, name or "")

def rebuild_artist_list_filtered(keep_selection_name=None):
    artist_list.delete(0, tk.END)
    q = ""  # Artist list is no longer filtered; top search filters songs only.

    def add_artist(name: str):
        ap = _artist_folder(name)
        # Check if we have a cached result for this artist
        cached = next((v for k, v in lyrics_cache.items()
                       if isinstance(k, tuple) and k[0] == ap and k[2] == "artist"), None)
        if cached is not None:
            # Show icon from cache - no scanning needed
            icon = completeness_icon(cached["have"], cached["total"])
            artist_list.insert(tk.END, f"{icon} {name}")
        else:
            # Never scanned - no icon
            artist_list.insert(tk.END, name)

    if not q:
        for a in all_artists:
            add_artist(a)
    else:
        for a in all_artists:
            if q in a.lower():
                add_artist(a)

    if keep_selection_name:
        for i in range(artist_list.size()):
            if strip_icon(artist_list.get(i)) == keep_selection_name:
                artist_list.selection_set(i)
                artist_list.activate(i)
                artist_list.see(i)  # Scroll to keep selected artist in view
                break


# ------------------ Tooltips ------------------

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        widget._t9_tooltip_obj = self  # studio_hints reuses this text for the hint line
        widget.bind("<Enter>", self.show_tooltip)
        widget.bind("<Leave>", self.hide_tooltip)
    
    def show_tooltip(self, event=None):
        if self.tooltip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        
        t = THEMES[config.get("theme", "dark")]
        label = tk.Label(tw, text=self.text, justify='left',
                        background=t["panel"], foreground=t["fg"],
                        relief='solid', borderwidth=1,
                        font=("Segoe UI", 9), padx=8, pady=4)
        label.pack()
    
    def hide_tooltip(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None


def create_tooltip(widget, text):
    """Helper to create tooltips easily"""
    return ToolTip(widget, text)


# ------------------ Keyboard Shortcuts ------------------

def setup_keyboard_shortcuts():
    """Setup global keyboard shortcuts"""
    root.bind('<F5>', lambda e: load_artists() if MUSIC_DIR else None)
    root.bind('<Escape>', lambda e: request_cancel() if downloading else None)
    root.bind('<Control-d>', lambda e: start_download_tracks() if not downloading else None)
    root.bind('<Control-D>', lambda e: start_download_tracks() if not downloading else None)
    root.bind('<Control-Shift-D>', lambda e: start_download_artist() if not downloading else None)


# ------------------ Refresh helpers ------------------

def get_selected_artist_name():
    if not artist_list.curselection():
        return None
    if len(artist_list.curselection()) != 1:
        return None
    return strip_icon(artist_list.get(artist_list.curselection()[0]))


def refresh_artist_list(keep_selection=True):
    global all_artists, ROOT_ARTIST_LABEL
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return

    sel_artist = get_selected_artist_name() if keep_selection else None

    try:
        artists = [a for a in sorted(os.listdir(MUSIC_DIR))
                   if os.path.isdir(os.path.join(MUSIC_DIR, a))
                   and next(_audio_files_under_artist(os.path.join(MUSIC_DIR, a)), None) is not None]
    except:
        artists = []

    ROOT_ARTIST_LABEL = None
    if any(p.is_file() and p.suffix.lower() in (".mp3", ".flac") for p in Path(MUSIC_DIR).iterdir()):
        ROOT_ARTIST_LABEL = Path(MUSIC_DIR).name
        if ROOT_ARTIST_LABEL in artists:
            ROOT_ARTIST_LABEL += " (main folder)"
        artists.insert(0, ROOT_ARTIST_LABEL)
    all_artists = artists[:]
    rebuild_artist_list_filtered(keep_selection_name=sel_artist)
    

def refresh_current_view():
    _invalidate_view_status()
    on_artist_select(None)


# ------------------ Library Scan ------------------

def choose_folder():
    global MUSIC_DIR, missing_targets
    folder = filedialog.askdirectory(title="Select Music Folder")
    if not folder:
        return
    MUSIC_DIR = folder
    missing_targets = []
    song_search_var.set("")
    config["music_dir"] = MUSIC_DIR
    save_config(config)
    load_artists()


def load_artists():
    _invalidate_view_status()
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return

    if "album_list" in globals():
        album_list.delete(0, tk.END)
        ALBUM_ROW_DIRS.clear()
    track_list.delete(0, tk.END)
    TRACK_ROW_PATHS.clear()
    log_box.delete("1.0", tk.END)

    set_status(f"Loaded: {MUSIC_DIR}")
    log(f"Loaded artists from: {MUSIC_DIR}")
    log(f"Config file: {CONFIG_FILE}")

    lyrics_cache.clear()
    refresh_artist_list(keep_selection=False)
    on_artist_select()


TRACK_ROW_PATHS = {}
ALBUM_ROW_DIRS = {}
ALBUM_ROOT_LABEL = "(Main folder)"


def _track_matches_song_search(display_name: str, query: str) -> bool:
    """Return True when the TRACK NAME matches Song Search.

    Artist names and album/folder names are intentionally ignored. This keeps
    the Albums column purely organizational and prevents folder names from
    affecting song-search results.
    """
    query = (query or "").strip()
    if not query:
        return True
    filename_only = os.path.basename(strip_icon(display_name))
    return query.lower() in filename_only.lower()


def _audio_files_under_artist(artist_path: str):
    """Yield (absolute_path, album_rel_dir, filename) for supported audio."""
    for root_dir, _, files in os.walk(artist_path):
        rel_dir = os.path.relpath(root_dir, artist_path)
        if rel_dir == ".":
            rel_dir = ""
        for file in files:
            if file.lower().endswith((".mp3", ".flac")):
                yield os.path.join(root_dir, file), rel_dir, file


def _track_sort_key(row, mode):
    filename, _path, prefix, status, album = row
    priorities = {
        "Failed first": {"failed": 0, "transcription": 1, "synced": 2, "lrc": 3, "downloaded": 4, "untouched": 5},
        "Marked first": {"transcription": 0, "failed": 1, "synced": 2, "lrc": 3, "downloaded": 4, "untouched": 5},
    }
    if mode == "Downloaded first":
        # This view is specifically for finished plain lyrics that still need
        # synchronization. Synced audio and timed LRC have their own sort mode.
        return (0 if status == "downloaded" else 1), filename.casefold(), album.casefold()
    if mode == "New":  # no lyrics downloaded and nothing synced: songs without any status
        return (0 if status == "untouched" else 1), filename.casefold(), album.casefold()
    if mode == "Lyrics Synced First":
        return (0 if status == "synced" else 1), filename.casefold(), album.casefold()
    if mode == "Marked first" and status == "transcription":
        # Within the marked group, ready shorter references always come first.
        reference_order = 0 if prefix.startswith("📝✅") else 1
        return 0, reference_order, filename.casefold(), album.casefold()
    if mode in priorities:
        priority = priorities[mode].get(status, 4)
        if mode == "Marked first":
            return priority, 0, filename.casefold(), album.casefold()
        return priority, filename.casefold(), album.casefold()
    return filename.casefold(), album.casefold()


def _selected_album_dirs():
    """Return selected album relative directories. Empty means 'show all'."""
    if "album_list" not in globals():
        return []
    selected = []
    for row in album_list.curselection():
        rel_dir = ALBUM_ROW_DIRS.get(row)
        if rel_dir is not None:
            selected.append(rel_dir)
    return selected


def _populate_albums_for_artist(artist: str):
    """Populate Albums from actual folders that directly contain audio files.

    Directly stored Artist/track.mp3 files are grouped under '(Artist Root)'.
    Nested folders are shown by their relative path, e.g. Album/CD1.
    No album is selected automatically: no selection means show all tracks.
    """
    album_list.delete(0, tk.END)
    ALBUM_ROW_DIRS.clear()

    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return

    artist_path = _artist_folder(artist) if artist else MUSIC_DIR
    album_dirs = set()

    try:
        for _path, rel_dir, _file in _audio_files_under_artist(artist_path):
            album_dirs.add(rel_dir)
    except Exception as exc:
        log(f"Could not read album folders: {exc}")
        return

    def album_sort_key(rel_dir):
        # Artist Root first, then alphabetical folder paths.
        return (0 if rel_dir == "" else 1, rel_dir.casefold())

    def folder_name(rel_dir):
        return rel_dir.replace("\\", "/").rstrip("/").split("/")[-1]

    names = [folder_name(d) for d in album_dirs if d]
    for rel_dir in sorted(album_dirs, key=album_sort_key):
        if rel_dir == "":
            label = ALBUM_ROOT_LABEL
        else:
            label = folder_name(rel_dir)
            if names.count(label) > 1:  # same folder name in two places: keep the parent to tell them apart
                label = rel_dir.replace("\\", "/")
        row = album_list.size()
        album_list.insert(tk.END, label)
        ALBUM_ROW_DIRS[row] = rel_dir

    if "album_select_btn" in globals():
        album_select_btn.configure(text="Select All")
    if "album_card" in globals():
        if any(album_dirs):
            album_card.pack(side="left", fill="both", padx=(0, 6), pady=4, before=track_card)
        else:
            album_card.pack_forget()


_VIEW_STATUS_CACHE = {}
_VIEW_STATUS_PENDING = set()
_VIEW_STATUS_GENERATION = 0


def _invalidate_view_status():
    global _VIEW_STATUS_GENERATION
    _VIEW_STATUS_GENERATION += 1
    _VIEW_STATUS_CACHE.clear()
    _VIEW_STATUS_PENDING.clear()


def _scan_view_status(paths):
    generation = _VIEW_STATUS_GENERATION
    _VIEW_STATUS_PENDING.update(paths)
    def worker():
        results = {}
        for path in paths:
            if generation != _VIEW_STATUS_GENERATION:
                return
            try:
                existing = find_existing_lyrics(path)
                failed = _is_persistent_track_failed(path)
                key = _track_status_key(path)
                if failed and (existing or _has_synced_lyrics(path)):
                    _set_persistent_track_failed(path, False)
                    failed = False
                results[path] = _track_display_status(path, existing, failed, key in attempted_track_paths)
            except Exception:
                results[path] = ("", "untouched")
        def finish():
            if generation != _VIEW_STATUS_GENERATION:
                return
            _VIEW_STATUS_CACHE.update(results)
            _VIEW_STATUS_PENDING.difference_update(paths)
            selected = {TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS}
            refresh_tracks_for_current_artist()
            for i, path in TRACK_ROW_PATHS.items():
                if path in selected:
                    track_list.selection_set(i)
            update_track_selection_counter()
        ui_call(finish)
    threading.Thread(target=worker, daemon=True).start()


def _track_badges(song, status, match_data):
    """('⚠❓', tooltip) for songs that have an LRC: sync errors and/or lyrics that look wrong."""
    if status not in ("synced", "lrc"):
        return "", ""
    badge, tips = "", []
    try:
        info = WARNING_INDEX.for_audio(song)
        if info:
            badge += "⚠" if info["level"] == "error" else "❕"
            tips.append(studio_warnings.sync_tooltip(info))
        entry = match_data.get(LYRICS_MATCH._key(song))
        if LYRICS_MATCH.is_suspect(entry) and LYRICS_MATCH.suspect_entry(
                song, _find_plain_txt_for_audio(song), data=match_data):
            badge += "❓"
            tips.append(studio_warnings.lyrics_tooltip(entry))
    except Exception:
        return "", ""  # a badge must never break the list
    return badge, "\n\n".join(tips)


def refresh_track_badges():
    """Redraw Tracks (keeping the selection and scroll position) after a scan, fix or sync."""
    selected = {TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS}
    top = track_list.yview()[0]
    refresh_tracks_for_current_artist()
    for i, path in TRACK_ROW_PATHS.items():
        if path in selected:
            track_list.selection_set(i)
    track_list.yview_moveto(top)
    update_track_selection_counter()


def refresh_tracks_for_current_artist():
    """Populate Tracks using optional Album selection + Song Search."""
    track_list.delete(0, tk.END)
    TRACK_ROW_PATHS.clear()
    if "track_selected_count_label" in globals():
        update_track_selection_counter()

    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return

    if len(artist_list.curselection()) > 1:
        set_status(f"{len(artist_list.curselection())} artists selected")
        return

    artist = get_selected_artist_name()
    artist_path = _artist_folder(artist) if artist else MUSIC_DIR
    artist = artist or os.path.basename(os.path.normpath(MUSIC_DIR))
    selected_album_dirs = set(_selected_album_dirs())
    query = song_search_var.get().strip() if "song_search_var" in globals() else ""

    total_tracks = 0
    album_filtered_tracks = 0
    visible_tracks = 0

    try:
        rows = []
        pending_status = []

        for path, rel_dir, file in _audio_files_under_artist(artist_path):
            total_tracks += 1

            # Albums are an OPTIONAL organizational filter.
            # No album selection = all folders.
            if selected_album_dirs and rel_dir not in selected_album_dirs:
                continue

            album_filtered_tracks += 1

            # Search sees ONLY the actual track filename, never Artist/Album path.
            if not _track_matches_song_search(file, query):
                continue

            prefix, status = _VIEW_STATUS_CACHE.get(path, ("… ", "checking"))
            if path not in _VIEW_STATUS_CACHE and path not in _VIEW_STATUS_PENDING:
                pending_status.append(path)

            # Album/folder is already visible in the middle column. Tracks stays
            # clean and shows only the filename. Exact path is stored separately.
            rows.append((file, path, prefix, status, rel_dir))

        mode = track_sort_var.get() if "track_sort_var" in globals() else "A-Z"
        rows.sort(key=lambda row: _track_sort_key(row, mode))
        rows = _column_sorted(rows)

        TRACK_ROW_TIPS.clear()
        need_tags = []
        try:
            WARNING_INDEX.begin()
            match_data = LYRICS_MATCH.load()
        except Exception:
            match_data = {}
        for filename, path, prefix, status, _rel_dir in rows:
            row = track_list.size()
            badge, tip = _track_badges(path, status, match_data)
            tags = TAG_CACHE.lookup(path)
            if tags is None:  # new or changed: show what was known and read the file in the background
                need_tags.append(path)
                tags = (TAG_CACHE.data.get(path) or [0, 0, []])[2]
            track_list.insert(tk.END, (prefix.rstrip() + badge + " " if badge else prefix) + filename, tags)
            TRACK_ROW_PATHS[row] = path
            if tip:
                TRACK_ROW_TIPS[row] = tip
            visible_tracks += 1
        if pending_status:
            _scan_view_status(pending_status)
        if need_tags:
            TAG_READER.request(need_tags)

    except Exception as e:
        log(f"Could not read artist folder: {e}")
        return

    if selected_album_dirs:
        album_count = len(selected_album_dirs)
        album_text = f"{album_count} album folder{'s' if album_count != 1 else ''}"
        if query:
            set_status(
                f"{artist} — {visible_tracks}/{album_filtered_tracks} tracks match "
                f"'{query}' in {album_text}"
            )
        else:
            set_status(f"{artist} — {album_filtered_tracks} tracks in {album_text}")
    else:
        if query:
            set_status(f"{artist} — {visible_tracks}/{total_tracks} tracks match '{query}'")
        else:
            set_status(f"{artist} — {total_tracks} tracks")


def on_artist_select(event=None):
    """Populate Albums + Tracks for exactly one selected artist.

    Albums are organizational only. Selecting one or more album folders filters
    Tracks; leaving Albums unselected shows every track under the artist.
    Song Search never filters Artists or Albums and never matches folder names.
    """
    has_missing_targets = bool(missing_targets)

    if not has_missing_targets:
        missing_scan_btn.configure(text="Find & Download Missing Lyrics TXT [0]")


    if artist_list.curselection():
        if len(artist_list.curselection()) == artist_list.size() and artist_list.size() > 0:
            artist_select_btn.configure(text="Clear All")
        else:
            artist_select_btn.configure(text="Select All")
    else:
        artist_select_btn.configure(text="Select All")

    if "album_list" in globals():
        album_list.delete(0, tk.END)
        ALBUM_ROW_DIRS.clear()
    track_list.delete(0, tk.END)
    TRACK_ROW_PATHS.clear()

    if "album_select_btn" in globals():
        album_select_btn.configure(text="Select All")
    if "track_selected_count_label" in globals():
        update_track_selection_counter()

    if len(artist_list.curselection()) > 1:
        set_status(f"{len(artist_list.curselection())} artists selected")
        return

    artist = get_selected_artist_name()
    _populate_albums_for_artist(artist)
    refresh_tracks_for_current_artist()


def on_album_select(event=None):
    """Filter Tracks by selected album folder(s); Albums never affect search."""
    if "album_list" in globals():
        count = len(album_list.curselection())
        if count and count == album_list.size() and album_list.size() > 0:
            album_select_btn.configure(text="Clear All")
        else:
            album_select_btn.configure(text="Select All")
    refresh_tracks_for_current_artist()


# ------------------ Lyrics helpers ------------------

def _norm_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _split_artist_names(value: str) -> list[str]:
    if not value:
        return []
    # Handles common multi-artist tag styles without splitting names such as AC/DC.
    parts = re.split(r"\s*(?:;|\||,|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*", value, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _artist_matches(expected: str, genius_artist_names: str) -> bool:
    expected_parts = _split_artist_names(expected) or [expected]
    genius_parts = _split_artist_names(genius_artist_names) or [genius_artist_names]
    expected_norm = [_norm_match_text(x) for x in expected_parts if _norm_match_text(x)]
    genius_norm = [_norm_match_text(x) for x in genius_parts if _norm_match_text(x)]

    for e in expected_norm:
        for g in genius_norm:
            if e == g:
                return True
            # Accept a very close formatting variant, but not a different artist.
            if len(e) >= 5 and len(g) >= 5 and difflib.SequenceMatcher(None, e, g).ratio() >= 0.93:
                return True
            # Genius sometimes combines collaborators into a single artist string.
            if len(e) >= 4 and re.search(r"(?:^| )" + re.escape(e) + r"(?:$| )", g):
                return True
    return False


def _extract_featured_artists_from_title(title: str) -> list[str]:
    """Treat with/ft/feat/featuring annotations as collaborator artist evidence."""
    found = []
    for m in re.finditer(
        r"(?i)[\(\[\{]\s*(?:with|ft\.?|feat\.?|featuring)\s+([^\)\]\}]+)",
        title or "",
    ):
        chunk = m.group(1).strip(" -–—,;")
        for part in re.split(r"(?i)\s*(?:,|&|\band\b)\s*", chunk):
            part = " ".join(part.split()).strip(" -–—,;")
            if part and _norm_match_text(part) and part not in found:
                found.append(part)
    return found


def _artist_evidence_coverage(artist_clue: str, title_clue: str, genius_artists: str) -> tuple[int, int, float]:
    """Count metadata artist + title collaborators present in Genius credits."""
    expected = []
    for value in (_split_artist_names(artist_clue) or [artist_clue]):
        if value and _norm_match_text(value) and value not in expected:
            expected.append(value)
    for value in _extract_featured_artists_from_title(title_clue):
        if value not in expected:
            expected.append(value)

    actual_parts = _split_artist_names(genius_artists) or [genius_artists]
    actual_norms = [_norm_match_text(x) for x in actual_parts if _norm_match_text(x)]
    actual_all = _norm_match_text(genius_artists)
    matched = 0
    for value in expected:
        e = _norm_match_text(value)
        if re.search(r"(?:^| )" + re.escape(e) + r"(?:$| )", actual_all):
            matched += 1
        elif any(
            len(e) >= 5 and difflib.SequenceMatcher(None, e, a).ratio() >= 0.93
            for a in actual_norms
        ):
            matched += 1
    total = len(expected)
    return matched, total, (matched / total if total else 0.0)


def _near_exact_main_title_typo(expected: str, actual: str) -> tuple[float, str]:
    """Strict typo matcher for main titles such as Bitty <-> Bity."""
    e = _norm_match_text(_strip_bracketed_parts(expected))
    a = _norm_match_text(_strip_bracketed_parts(actual))
    if not e or not a:
        return 0.0, ""

    ratio = difflib.SequenceMatcher(None, e, a).ratio()

    prev = list(range(len(a) + 1))
    for i, ce in enumerate(e, 1):
        cur = [i]
        for j, ca in enumerate(a, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ce != ca)))
        prev = cur
    distance = prev[-1]

    # One edit for short names, at most two for longer names.
    if min(len(e), len(a)) <= 7:
        ok = distance <= 1 and ratio >= 0.80
    else:
        ok = distance <= 2 and ratio >= 0.90

    if not ok:
        return 0.0, ""
    score = min(0.97, max(0.90, ratio))
    return score, (
        f'near-exact main-title typo "{e}" ↔ "{a}" '
        f'(edit distance={distance}, similarity={ratio:.0%})'
    )


def _artist_match_coverage(expected: str, genius_artist_names: str) -> tuple[int, int, float]:
    """Return how many expected artist components are present in Genius artists.

    This is stricter/more informative than the legacy boolean matcher and is
    useful for borderline title cases. Example:
        expected: Juice WRLD, Ski Mask The Slump God
        Genius:   Ski Mask The Slump God & Juice WRLD
    gives 2/2 = 100%.
    """
    expected_parts = _split_artist_names(expected) or [expected]
    expected_norm = [_norm_match_text(x) for x in expected_parts if _norm_match_text(x)]
    actual_norm = _norm_match_text(genius_artist_names)

    matched = 0
    for e in expected_norm:
        if not e:
            continue
        if re.search(r"(?:^| )" + re.escape(e) + r"(?:$| )", actual_norm):
            matched += 1
            continue

        # Very close formatting/spelling variant of a complete artist component.
        actual_parts = _split_artist_names(genius_artist_names) or [genius_artist_names]
        if any(
            len(e) >= 5
            and difflib.SequenceMatcher(None, e, _norm_match_text(g)).ratio() >= 0.93
            for g in actual_parts
            if _norm_match_text(g)
        ):
            matched += 1

    total = len(expected_norm)
    ratio = (matched / total) if total else 0.0
    return matched, total, ratio


def _strip_semantic_version_markers_for_match(title: str) -> str:
    """Remove standalone V2/V3/... only for a guarded fallback comparison."""
    value = title or ""
    value = re.sub(r"(?i)\b(?:v|version\s*)\s*\d+\b(?!\.\d)", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -–—._")


def _strip_local_revision_markers(title: str) -> str:
    """Remove file/catalog revision labels such as v1.0 or v1.1.

    Decimal revisions are commonly used to identify a local render/export and
    are not Genius song-version markers.  Standalone V2/V3/etc. remain protected.
    """
    value = title or ""
    value = re.sub(r"(?i)\b(?:v|version\s*)\s*\d+\.\d+\b", " ", value)
    return " ".join(value.split()).strip(" -–—")


def _strip_version_labels_for_fallback(title: str) -> str:
    """Last-chance search form: remove V/version labels but only for search.

    Normal matching/search still runs first with the original title and version
    markers. This fallback is used only when that normal pass finds nothing.
    """
    value = title or ""
    value = re.sub(r"\b(?:v\s*\d+(?:\.\d+)?|version\s*\d+(?:\.\d+)?)\b", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -–—")


def _extract_version_markers(title: str) -> set[str]:
    """Extract semantic standalone version markers such as V2/V3/V10.

    Decimal local revision labels (v1.0, v1.1, version 2.3) are deliberately
    ignored: they usually describe a local file/render revision, not a distinct
    Genius song version.
    """
    raw = unicodedata.normalize("NFKD", title or "")
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    markers = set(re.findall(r"\bv\s*(\d+)\b(?!\.\d)", raw, flags=re.I))
    markers.update(re.findall(r"\bversion\s*(\d+)\b(?!\.\d)", raw, flags=re.I))
    return markers


def _extract_required_edition_markers(title: str) -> set[str]:
    """Extract edition markers that are semantically important when present locally.

    Session/Sessions/Session Edit identify a genuinely different lyrics version in
    many unreleased catalogs, so a local Session track must not silently match the
    ordinary song.  Other labels (e.g. WOD album shorthand) remain non-binding.
    """
    norm = _norm_match_text(title)
    markers = set()
    if re.search(r"\bsessions?\b", norm):
        markers.add("session")
    # Keep the more specific marker too, so logs can explain what was detected.
    if re.search(r"\bsessions?\s+edit\b|\bsession\s+edit\b", norm):
        markers.add("session_edit")
    return markers


def _edition_markers_compatible(expected: str, actual: str) -> tuple[bool, str]:
    """Require a Session-family marker while treating its labels as equivalent."""
    expected_markers = _extract_required_edition_markers(expected)
    if not expected_markers:
        return True, ""

    actual_markers = _extract_required_edition_markers(actual)
    if "session" in expected_markers and "session" not in actual_markers:
        return False, "missing required Sessions/Session Edit marker"
    # Genius and local libraries use Sessions and Session Edit interchangeably
    # for the same recording. Requiring the more specific spelling made the
    # comparison asymmetric and incorrectly returned 0% in one direction.
    return True, ""


def _catalog_noise_key(value: str) -> str:
    """Compare catalog labels ignoring case, spacing and '&': 'GB&GR' == 'gb and gr' == 'GBGR'."""
    return re.sub(r"\band\b|\s+", "", _norm_match_text(value))


def _strip_search_noise_brackets(title: str) -> str:
    """Remove low-value catalog/album bracket labels while preserving title aliases.

    WOD is commonly used as an album/project shorthand and should not decide which
    Genius song version is selected. Session markers are deliberately preserved.
    """
    noise = {_catalog_noise_key(label) for label in ("wod", "drfl", "jw3", "gb&gr", "jute")}

    def repl(match):
        inner = next((g for g in match.groups() if g is not None), "").strip()
        return " " if _catalog_noise_key(inner) in noise else match.group(0)

    value = re.sub(r"\(([^)]*)\)|\[([^]]*)\]|\{([^}]*)\}", repl, title or "")
    return " ".join(value.split())


def _strip_bracketed_parts(title: str) -> str:
    """Remove (...) / [...] / {...} while keeping the main title intact."""
    value = re.sub(r"\([^)]*\)", " ", title or "")
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"\{[^}]*\}", " ", value)
    return " ".join(value.split()).strip(" -–—")


def _extract_bracket_aliases(title: str) -> list[str]:
    """Return useful text found inside brackets as optional search aliases."""
    aliases = []
    for part in re.findall(r"\(([^)]*)\)|\[([^]]*)\]|\{([^}]*)\}", title or ""):
        value = next((x for x in part if x), "").strip()
        norm = _norm_match_text(value)
        # Very common edition labels are not useful aliases by themselves.
        if not norm or norm in {
            "stem", "edit", "remix", "remaster", "remastered", "version",
            "original", "og", "unreleased", "leak", "demo", "session", "sessions",
            "session edit", "sessions edit", "cdq", "wod", "drfl"
        } or _catalog_noise_key(value) in {"gbgr", "jw3", "jute"}:
            continue
        if len(norm) >= 3:
            aliases.append(value)
    return aliases


def _strip_feature_suffix_for_title_match(value: str) -> str:
    """Remove trailing collaborator text from a title-only clue.

    Genius frequently moves feature information out of the local main title and
    into a separate parenthetical suffix, e.g.:

        local:  Conjuring ft. NLE CHOPA
        Genius: Pull Up In The Range (Conjuring) (Ft. NLE Choppa)

    Featured artists are verified separately through the Genius result/artist
    metadata, so they must not prevent the alternate-title alias from matching.
    """
    value = " ".join((value or "").split()).strip()
    value = re.sub(
        r"(?i)\s+(?:feat\.?|ft\.?|featuring|with)\s+.+$",
        "",
        value,
    )
    return " ".join(value.split()).strip(" -–—._")


def _token_similarity(left: str, right: str) -> float:
    a = set(_norm_match_text(left).split())
    b = set(_norm_match_text(right).split())
    if not a or not b:
        return 0.0
    return (2.0 * len(a & b)) / (len(a) + len(b))


def _title_match_details(expected: str, actual: str) -> tuple[float, str]:
    """Return a conservative similarity score and a human-readable reason.

    Main-title similarity matters most. Parenthetical aliases/edition labels are
    secondary. Explicit V-number markers are protected and cannot be discarded.
    """
    e_full = _norm_match_text(expected)
    a_full = _norm_match_text(actual)
    if not e_full or not a_full:
        return 0.0, "empty title"

    expected_versions = _extract_version_markers(expected)
    actual_versions = _extract_version_markers(actual)

    # If the library explicitly says V2/V3/etc., require the same version marker.
    # This prevents e.g. "Autograph V3" from matching the different "Autograph".
    if expected_versions:
        if not actual_versions:
            return 0.0, f"missing version marker V{','.join(sorted(expected_versions))}"
        if expected_versions != actual_versions:
            return 0.0, (
                "version mismatch "
                f"expected={','.join('V'+x for x in sorted(expected_versions))} "
                f"actual={','.join('V'+x for x in sorted(actual_versions))}"
            )

    # Genius occasionally labels an unreleased/session recording as "(Extended)"
    # even when the local library calls the same recording "(Sessions)" or
    # "(Session Edit)".  Handle that as a LOCAL verification equivalence:
    # no extra Genius query is sent.  We only allow it when the non-bracketed
    # main title is essentially identical, so "Busch (Sessions)" may match
    # "Busch (Extended)", but an unrelated "... (Extended)" result cannot pass.
    e_core_raw = _strip_bracketed_parts(expected)
    a_core_raw = _strip_bracketed_parts(actual)
    e_core_norm_for_edition = _norm_match_text(e_core_raw)
    a_core_norm_for_edition = _norm_match_text(a_core_raw)
    expected_editions = _extract_required_edition_markers(expected)
    actual_norm_for_edition = _norm_match_text(actual)
    actual_is_extended = bool(re.search(r"\bextended\b", actual_norm_for_edition))
    session_expected = bool({"session", "session_edit"} & expected_editions)

    extended_session_equivalent = False
    if session_expected and actual_is_extended and e_core_norm_for_edition and a_core_norm_for_edition:
        e_compact = e_core_norm_for_edition.replace(" ", "")
        a_compact = a_core_norm_for_edition.replace(" ", "")
        core_ratio = difflib.SequenceMatcher(None, e_core_norm_for_edition, a_core_norm_for_edition).ratio()
        extended_session_equivalent = (
            e_core_norm_for_edition == a_core_norm_for_edition
            or e_compact == a_compact
            or core_ratio >= 0.94
        )

    edition_ok, edition_reason = _edition_markers_compatible(expected, actual)
    if not edition_ok and not extended_session_equivalent:
        return 0.0, edition_reason

    if extended_session_equivalent:
        return 0.97, "local Sessions/Session Edit accepted as Genius Extended equivalent; same main title"

    if e_full == a_full:
        return 1.0, "exact title"

    # Genius often stores an alternate/older title in parentheses after a
    # different main title, e.g.
    #   Heart Racing (Drive Me Crazy)
    #   Where I Stay (My Side Remix)
    # Treat bracket text on EITHER side as a real title alias.  Version-marker
    # validation above still applies, so V3 cannot accidentally match V2/no-V.
    expected_alias_forms = []
    actual_alias_forms = []

    def add_alias_form(target, value):
        norm = _norm_match_text(value)
        if norm and norm not in target:
            target.append(norm)

    add_alias_form(expected_alias_forms, expected)
    add_alias_form(expected_alias_forms, e_core_raw)
    add_alias_form(expected_alias_forms, _strip_feature_suffix_for_title_match(e_core_raw))
    for alias in _extract_bracket_aliases(expected):
        if re.match(r"(?i)^\s*(?:with|ft\.?|feat\.?|featuring)\s+", alias or ""):
            continue
        add_alias_form(expected_alias_forms, alias)
        add_alias_form(expected_alias_forms, _strip_feature_suffix_for_title_match(alias))

    add_alias_form(actual_alias_forms, actual)
    add_alias_form(actual_alias_forms, a_core_raw)
    add_alias_form(actual_alias_forms, _strip_feature_suffix_for_title_match(a_core_raw))
    actual_bracket_aliases = []
    for alias in _extract_bracket_aliases(actual):
        if re.match(r"(?i)^\s*(?:with|ft\.?|feat\.?|featuring)\s+", alias or ""):
            continue
        add_alias_form(actual_alias_forms, alias)
        add_alias_form(actual_alias_forms, _strip_feature_suffix_for_title_match(alias))
        alias_norm = _norm_match_text(_strip_feature_suffix_for_title_match(alias))
        if alias_norm:
            actual_bracket_aliases.append(alias_norm)

    # Parenthetical text on Genius is often a genuine SECOND TITLE, not merely
    # an edition note:
    #
    #   Overdose (911) (Sessions)
    #   Motor Boat (Anacondas)
    #   Sacrifices (Cake)
    #   Pull Up In The Range (Conjuring) (Ft. NLE Choppa)
    #
    # If our local title/core is EXACTLY one of those bracket aliases, accept it
    # very strongly even when it is short, numeric, or one word. Artist and
    # required edition checks above still have to pass, which keeps this safe.
    expected_primary_forms = []
    for value in (
        expected,
        e_core_raw,
        _strip_feature_suffix_for_title_match(e_core_raw),
    ):
        norm = _norm_match_text(value)
        if norm and norm not in expected_primary_forms:
            expected_primary_forms.append(norm)

    exact_second_titles = set(expected_primary_forms) & set(actual_bracket_aliases)
    exact_second_titles = {
        x for x in exact_second_titles
        if len(x) >= 3 and x not in {
            "mix", "edit", "stem", "demo", "remix", "version",
            "session", "sessions", "extended", "original", "unreleased",
        }
    }
    if exact_second_titles:
        best_alias = max(exact_second_titles, key=len)
        return 0.99, f'exact Genius parenthetical second title: "{best_alias}"'

    # Some Genius second titles are written as one compound word even though
    # the local audio title uses spaces, for example:
    #
    #   local:  Lava Girl
    #   Genius: Stick Talk (Lavagirl)
    #
    # Treat only an exact spacing-insensitive parenthetical alias as equivalent.
    # This remains conservative: artist and edition/version validation above
    # still apply, and a partial/subsequence match is not enough.
    compact_second_titles = []
    for exp in expected_primary_forms:
        exp_compact = exp.replace(" ", "")
        if len(exp_compact) < 5:
            continue
        for alias in actual_bracket_aliases:
            if exp != alias and exp_compact == alias.replace(" ", ""):
                compact_second_titles.append(alias)

    if compact_second_titles:
        best_alias = max(compact_second_titles, key=len)
        return 0.98, (
            "exact Genius parenthetical second title after ignoring spaces: "
            f'"{best_alias}"'
        )


    # A one-character typo must not destroy an otherwise obvious alternate
    # title. This is intentionally limited to the Genius parenthetical
    # second-title field, where the semantic signal is strong:
    #
    #   local:  Cokkie Crisp
    #   Genius: Send Away (Cookie Crisp)
    #
    # Require very high similarity and comparable token counts. This does NOT
    # turn the whole-title matcher into a loose fuzzy search.
    expected_typo_forms = []
    for value in (
        e_core_raw,
        _strip_feature_suffix_for_title_match(e_core_raw),
    ):
        norm = _norm_match_text(value)
        if norm and norm not in expected_typo_forms:
            expected_typo_forms.append(norm)

    best_fuzzy_alias = None
    for exp in expected_typo_forms:
        for alias in actual_bracket_aliases:
            if len(exp) < 5 or len(alias) < 5:
                continue
            if len(exp.split()) != len(alias.split()):
                continue
            ratio = difflib.SequenceMatcher(None, exp, alias).ratio()
            if ratio >= 0.90:
                if best_fuzzy_alias is None or ratio > best_fuzzy_alias[0]:
                    best_fuzzy_alias = (ratio, exp, alias)

    if best_fuzzy_alias:
        ratio, exp, alias = best_fuzzy_alias
        return min(0.96, 0.90 + (ratio - 0.90) * 0.60), (
            f'near-exact Genius parenthetical second title: "{alias}" '
            f'(local "{exp}", similarity={ratio:.0%})'
        )

    # Also support the reverse direction and multi-title local filenames such
    # as "5 Bitches (500 Carats) (Sessions)" vs Genius
    # "500 Carats (Sessions)".
    alias_matches = set(expected_alias_forms) & set(actual_alias_forms)
    meaningful_aliases = {
        x for x in alias_matches
        if len(x) >= 3 and x not in {
            "mix", "edit", "stem", "demo", "remix", "version",
            "session", "sessions", "extended", "original", "unreleased",
        }
    }
    if meaningful_aliases:
        best_alias = max(meaningful_aliases, key=len)
        # Short one-word aliases are accepted because they are exact titles,
        # not fuzzy token overlaps, and artist/edition validation has already
        # succeeded.
        return 0.98, f'exact alternate title alias: "{best_alias}"'

    e_core_raw = _strip_bracketed_parts(expected)
    a_core_raw = _strip_bracketed_parts(actual)
    e_core = _norm_match_text(e_core_raw)
    a_core = _norm_match_text(a_core_raw)

    full_seq = difflib.SequenceMatcher(None, e_full, a_full).ratio()
    core_seq = difflib.SequenceMatcher(None, e_core, a_core).ratio() if e_core and a_core else 0.0
    core_tokens = _token_similarity(e_core_raw, a_core_raw)
    full_tokens = _token_similarity(expected, actual)

    # Same main title with different parenthetical subtitle/edition is a very
    # strong match: "Autograph V3 (On My Line)" vs "Autograph V3 (Stem)".
    if e_core and e_core == a_core:
        return max(0.96, full_seq), "same main title; bracket text differs"

    # A tiny spelling difference in the main title is also strong evidence.
    # This fixes cases such as local "Bitty" vs Genius "Bity".
    typo_score, typo_reason = _near_exact_main_title_typo(e_core_raw, a_core_raw)
    if typo_score >= 0.90:
        return typo_score, typo_reason

    # Weighted score deliberately favors the non-parenthetical core.
    score = (0.52 * core_seq) + (0.23 * core_tokens) + (0.15 * full_seq) + (0.10 * full_tokens)

    # Small bonus when one normalized core contains the other as complete words.
    if e_core and a_core and (f" {e_core} " in f" {a_core} " or f" {a_core} " in f" {e_core} "):
        score = min(0.95, score + 0.08)

    return score, f"core={core_seq:.0%}, tokens={core_tokens:.0%}, full={full_seq:.0%}"


def _title_similarity(expected: str, actual: str) -> float:
    return _title_match_details(expected, actual)[0]


# ------------------ Genius HTTP rate-limit protection ------------------
# Genius does not publish a stable numeric quota for the unauthenticated
# /api/search/multi endpoint used here.  Be deliberately conservative,
# serialize requests, respect Retry-After when it is present, and use
# exponential backoff + jitter when Genius returns HTTP 429.
_GENIUS_HTTP_LOCK = threading.Lock()
_GENIUS_LAST_REQUEST_AT = 0.0
_GENIUS_CONSECUTIVE_429 = 0
# Official API searches are lighter than full Genius lyrics pages. Keep both
# paced, but allow the API to move faster while still avoiding request bursts.
_GENIUS_API_MIN_REQUEST_GAP = 1.25
_GENIUS_PAGE_MIN_REQUEST_GAP = 2.20
_GENIUS_API_JITTER = (0.15, 0.45)
_GENIUS_PAGE_JITTER = (0.20, 0.70)
# Add a short breathing pause during long batches. This is deliberately small
# enough not to make normal runs painful, but prevents hundreds of searches
# from being emitted as one uninterrupted stream.
_GENIUS_API_REQUESTS_PER_BREAK = 10
_GENIUS_API_BREAK_RANGE = (8.0, 12.0)
_GENIUS_API_REQUESTS_SINCE_BREAK = 0
_GENIUS_429_BASE_WAIT = 60          # fallback if Genius sends no Retry-After
_GENIUS_429_MAX_WAIT = 600          # cap individual cooldowns at 10 minutes
_GENIUS_429_MAX_RETRIES = 6
_GENIUS_SEARCH_CACHE = {}           # in-memory cache for this program run


class GeniusRateLimitError(RuntimeError):
    pass


def _cancelable_sleep(seconds: float, reason: str = "") -> bool:
    """Sleep in short chunks so the GUI Cancel button remains useful."""
    seconds = max(0.0, float(seconds))
    if reason:
        log(reason)
    deadline = time.monotonic() + seconds
    while True:
        if globals().get("cancel_requested", False):
            return False
        if globals().get("pause_requested", False):
            if not wait_if_paused():
                return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.5, remaining))


def _retry_after_seconds(headers) -> float | None:
    """Parse HTTP Retry-After as either seconds or an HTTP-date."""
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _http_get(url: str, timeout: int = 15, extra_headers: dict | None = None) -> bytes:
    """GET a Genius URL with conservative pacing and HTTP-429 handling.

    Official API searches use a shorter steady gap than full lyrics pages. In
    long runs, every 10 successful API requests are followed by an 8-12 second
    breathing pause. Retry-After is always respected when Genius returns 429.
    """
    global _GENIUS_LAST_REQUEST_AT, _GENIUS_CONSECUTIVE_429
    global _GENIUS_API_REQUESTS_SINCE_BREAK

    is_official_api = url.startswith("https://api.genius.com/")
    min_gap = _GENIUS_API_MIN_REQUEST_GAP if is_official_api else _GENIUS_PAGE_MIN_REQUEST_GAP
    jitter_range = _GENIUS_API_JITTER if is_official_api else _GENIUS_PAGE_JITTER

    with _GENIUS_HTTP_LOCK:
        # Periodic batch cooldown applies only to official API searches.
        if is_official_api and _GENIUS_API_REQUESTS_SINCE_BREAK >= _GENIUS_API_REQUESTS_PER_BREAK:
            pause = random.uniform(*_GENIUS_API_BREAK_RANGE)
            if not _cancelable_sleep(
                pause,
                f"      Genius API pacing: {int(round(pause))}s break after "
                f"{_GENIUS_API_REQUESTS_PER_BREAK} API requests...",
            ):
                raise RuntimeError("Cancelled")
            _GENIUS_API_REQUESTS_SINCE_BREAK = 0

        for attempt in range(_GENIUS_429_MAX_RETRIES + 1):
            now = time.monotonic()
            target_gap = min_gap + random.uniform(*jitter_range)
            remaining_gap = (_GENIUS_LAST_REQUEST_AT + target_gap) - now
            if remaining_gap > 0 and not _cancelable_sleep(remaining_gap):
                raise RuntimeError("Cancelled")

            request_headers = {
                "User-Agent": "Triplenine Lyrics Downloader/1.0 (+Genius API client)",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            }
            if extra_headers:
                request_headers.update(extra_headers)
            req = urllib.request.Request(url, headers=request_headers)

            try:
                _GENIUS_LAST_REQUEST_AT = time.monotonic()
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                if is_official_api:
                    _GENIUS_API_REQUESTS_SINCE_BREAK += 1
                if _GENIUS_CONSECUTIVE_429:
                    log("      Genius rate limit cleared; continuing.")
                _GENIUS_CONSECUTIVE_429 = 0
                return data

            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise

                _GENIUS_CONSECUTIVE_429 += 1
                retry_after = _retry_after_seconds(exc.headers)
                fallback = min(_GENIUS_429_BASE_WAIT * (2 ** attempt), _GENIUS_429_MAX_WAIT)
                wait_s = max(retry_after or 0.0, float(fallback))
                wait_s += random.uniform(1.0, 5.0)

                if attempt >= _GENIUS_429_MAX_RETRIES:
                    raise GeniusRateLimitError(
                        f"Genius still rate-limited after {_GENIUS_429_MAX_RETRIES + 1} attempts"
                    ) from exc

                source = f"Retry-After={retry_after:.0f}s" if retry_after is not None else "no Retry-After header"
                log(f"      ⚠ Genius HTTP 429 Too Many Requests ({source}).")
                log(f"      Rate-limit protection: waiting {int(round(wait_s))}s before retry {attempt + 2}/{_GENIUS_429_MAX_RETRIES + 1}...")
                if not _cancelable_sleep(wait_s):
                    raise RuntimeError("Cancelled")

        raise GeniusRateLimitError("Genius rate limit retry loop exhausted")

def _genius_search_request(query: str, api_token: str = "") -> tuple[dict, bool]:
    """Return (payload, used_official_api), caching duplicate searches in RAM.

    The cache lasts only until the program is closed and prevents the same
    artist/title query from consuming another Genius request during one run.
    """
    normalized_query = " ".join((query or "").split()).strip()
    cache_key = (bool(api_token), normalized_query.casefold())
    cached = _GENIUS_SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if api_token:
        url = "https://api.genius.com/search?" + urllib.parse.urlencode({"q": normalized_query})
        data = _http_get(
            url,
            extra_headers={"Authorization": f"Bearer {api_token}"},
        )
        value = (json.loads(data.decode("utf-8", errors="replace")), True)
    else:
        url = "https://genius.com/api/search/multi?" + urllib.parse.urlencode({"q": normalized_query})
        data = _http_get(url)
        value = (json.loads(data.decode("utf-8", errors="replace")), False)

    _GENIUS_SEARCH_CACHE[cache_key] = value
    return value

def _genius_hits_from_payload(payload: dict, official_api: bool) -> list[dict]:
    if official_api:
        hits = payload.get("response", {}).get("hits", [])
        return [h for h in hits if isinstance(h, dict)]

    hits = []
    sections = payload.get("response", {}).get("sections", [])
    for section in sections:
        for hit in section.get("hits", []):
            if isinstance(hit, dict):
                hits.append(hit)
    return hits


def _test_genius_token_once(token: str) -> tuple[bool, str]:
    """Small one-shot token check used by the Options window."""
    token = (token or "").strip()
    if not token:
        return False, "Enter a Genius Client Access Token first."
    url = "https://api.genius.com/search?" + urllib.parse.urlencode({"q": "Triplenine token test"})
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Triplenine Lyrics Downloader/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            response.read(64)
        return True, "Genius API token is valid."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"Genius rejected this token (HTTP {exc.code})."
        if exc.code == 429:
            return False, "Genius API is rate-limiting requests right now (HTTP 429). Try again later."
        return False, f"Genius API returned HTTP {exc.code}."
    except Exception as exc:
        return False, f"Could not reach Genius API: {exc}"


def _build_clean_genius_verification_title(expected_title: str, artist_candidates: list[str]) -> str:
    """Remove local filename noise without dropping semantic song markers.

    Unlike the search query optimizer, this keeps meaningful parentheses such
    as aliases and (Sessions), and keeps true integer V2/V3 markers.
    """
    value = _strip_artist_prefix_from_title(expected_title, artist_candidates)
    value = _strip_search_noise_brackets(value or expected_title)
    value = _strip_local_revision_markers(value)
    value = re.sub(r"(?i)^\s*\([a-z]\)\s*[._\-–—]*\s*", "", value)
    value = re.sub(r"^\s*[._]+\s*", "", value)
    return " ".join(value.split()).strip(" -–—._")


def _classify_genius_title_parts(title: str, artist_candidates: list[str]) -> dict:
    """Classify local filename/title decorations before any Genius request.

    This intentionally spends local CPU work to make the *first* API query as
    useful as possible.  Parenthetical text is split into four groups:
      - editions: Session(s), Session Edit, Stem/Stem Edit (kept in query #1)
      - catalog noise: DRFL/WOD/JW3/GB&GR/JUTE etc. (never sent)
      - features: feat./ft./with artist information (usually omitted from search)
      - aliases: likely alternate song titles (reserved for query #2 only)

    True standalone V2/V3/etc. markers are kept in query #1. Decimal v1.0/v1.1
    markers are treated as local revision metadata: they remain available to the
    verifier/cache key but are omitted from the first search because the 20-song
    sample showed they can materially hurt Genius ranking.
    """
    raw = _strip_artist_prefix_from_title(title, artist_candidates) or title or ""
    raw = " ".join(raw.split()).strip()

    # Remove archive prefixes such as "(b).Back In Chicago" before parsing the
    # meaningful title.  They are local organizational markers, not song names.
    raw = re.sub(r"(?i)^\s*\([a-z]\)\s*[._\-–—]*\s*", "", raw)
    raw = re.sub(r"^\s*[._]+\s*", "", raw).strip()

    catalog_noise = {
        "wod", "drfl", "jw3", "gb&gr", "jute", "tpne", "outsiders",
        "fd", "lnd", "gbgr", "post", "death race for love", "wrld on drugs",
    }

    editions = []
    aliases = []
    features = []
    noise = []

    def add_unique(bucket, value):
        value = " ".join((value or "").split()).strip(" -–—._")
        n = _norm_match_text(value)
        if value and n and n not in {_norm_match_text(x) for x in bucket}:
            bucket.append(value)

    noise_keys = {_catalog_noise_key(label) for label in catalog_noise}

    def classify_inner(inner: str):
        inner = " ".join((inner or "").split()).strip()
        norm = _norm_match_text(inner)
        if not norm:
            return

        if _catalog_noise_key(inner) in noise_keys:
            add_unique(noise, inner)
            return

        # Standalone V2/V3/... in brackets is a semantic version marker, not an
        # alternate title. _extract_version_markers() adds it to query #1 later.
        if re.fullmatch(r"(?i)(?:v|version\s*)\s*\d+", inner):
            return

        # Preserve only meaningful edition words in the first query.  Normalize
        # singular/plural wording so Genius gets a simple, high-signal phrase.
        if re.fullmatch(r"(?i)(?:studio\s+)?sessions?", inner):
            add_unique(editions, "Sessions")
            return
        if re.fullmatch(r"(?i)(?:studio\s+)?sessions?\s+edit", inner):
            add_unique(editions, "Session Edit")
            return
        if re.fullmatch(r"(?i)stem", inner):
            add_unique(editions, "Stem")
            return
        if re.fullmatch(r"(?i)stem\s+edit", inner):
            add_unique(editions, "Stem Edit")
            return

        # Feature/collaborator information can make API ranking worse when the
        # primary artist and song title are already known. Keep it for local
        # verification but do not spend query #1 on it.
        if re.match(r"(?i)^(?:feat\.?|ft\.?|featuring|with)\b", inner):
            add_unique(features, inner)
            return

        add_unique(aliases, inner)

    # Parse bracket contents locally, then remove them from the main title.
    for groups in re.findall(r"\(([^)]*)\)|\[([^]]*)\]|\{([^}]*)\}", raw):
        inner = next((x for x in groups if x), "")
        classify_inner(inner)

    core = _strip_bracketed_parts(raw)
    # Remove decimal local revisions from the API query only. They still remain
    # in expected_title for cache identity / diagnostics.
    core = _strip_local_revision_markers(core)

    # True semantic integer versions (V2/V3/...) are strong distinguishing data.
    versions = sorted(_extract_version_markers(title), key=lambda x: int(x))
    for v in versions:
        marker = f"V{v}"
        if not re.search(rf"(?i)\bV\s*{re.escape(v)}\b", core):
            core = f"{core} {marker}".strip()

    core = " ".join(core.split()).strip(" -–—._")
    return {
        "core": core,
        "editions": editions,
        "aliases": aliases,
        "features": features,
        "noise": noise,
        "versions": versions,
        "raw": raw,
    }


def _build_smart_genius_first_query_title(expected_title: str, artist_candidates: list[str]) -> str:
    """Build one high-yield first Genius query using local title classification.

    Goal: do more work locally and send fewer Genius API requests. Alternate
    title aliases are intentionally held back for the second/final query so the
    first query stays concise. Important edition terms are kept immediately.
    """
    parts = _classify_genius_title_parts(expected_title, artist_candidates)
    core = parts["core"]
    editions = parts["editions"]

    # Avoid duplicating an edition word already present in the core.
    query_parts = [core] if core else []
    core_norm = _norm_match_text(core)
    for edition in editions:
        edition_norm = _norm_match_text(edition)
        if edition_norm and edition_norm not in core_norm:
            query_parts.append(edition)

    return " ".join(" ".join(query_parts).split()).strip(" -–—._")


def _build_smart_genius_second_query_title(expected_title: str, artist_candidates: list[str], first_title: str) -> str:
    """Build the one allowed fallback query, prioritizing alternate-title text.

    Query #2 is only used when query #1 produced no >=51% verified result.
    Prefer a likely alias from brackets, optionally paired with the main title.
    If there is no alias, fall back to a compact core without edition/version
    labels.  This keeps the total search budget at two requests per uncached song.
    """
    parts = _classify_genius_title_parts(expected_title, artist_candidates)
    core = parts["core"]
    aliases = parts["aliases"]
    editions = parts["editions"]

    def clean(v: str) -> str:
        v = _strip_version_labels_for_fallback(v or "")
        return " ".join(v.split()).strip(" -–—._")

    core_relaxed = clean(core)

    # Most useful alias first. Prefer a substantive multi-character phrase and
    # avoid aliases that merely duplicate the core.
    useful_aliases = []
    for alias in aliases:
        a = clean(alias)
        if not a:
            continue
        if _norm_match_text(a) == _norm_match_text(core_relaxed):
            continue
        useful_aliases.append(a)

    if useful_aliases:
        alias = useful_aliases[0]

        # The fallback exists specifically to test the alternate title. If the
        # local file has a required edition, pair the alias with that edition:
        #
        #   5 Bitches (500 Carats) (Sessions)
        #       request #1: 5 Bitches Sessions
        #       request #2: 500 Carats Sessions
        #
        # This is much more useful than repeating both titles in one query.
        required = _extract_required_edition_markers(expected_title)
        if "session_edit" in required:
            candidate = f"{alias} Session Edit".strip()
        elif "session" in required:
            candidate = f"{alias} Sessions".strip()
        else:
            alias_norm = _norm_match_text(alias)
            alias_tokens = alias_norm.split()
            if core_relaxed and len(alias_tokens) == 1 and len(alias_norm) <= 5:
                # A very short one-word alias is too broad by itself. Preserve
                # the main title in the same, already-budgeted fallback request:
                #
                #   Like Shit (Pump) -> "Like Shit Pump"
                #
                # This targets Genius pages whose title contains both parts and
                # avoids an unhelpful artist + "Pump" search.
                candidate = f"{core_relaxed} {alias}".strip()
            else:
                # Longer aliases are distinctive enough on their own. Genius
                # search already includes the trusted artist, and repeating the
                # old core can hurt alternate-title catalog ranking.
                candidate = alias
    else:
        candidate = core_relaxed

    # If the candidate accidentally equals query #1, first remove edition
    # words. If it is STILL identical, spend request #2 on a distinctive core
    # token instead of sending the exact same Genius query twice.
    if _norm_match_text(candidate) == _norm_match_text(first_title):
        for edition in editions:
            candidate = re.sub(rf"(?i)\b{re.escape(edition)}\b", " ", candidate)
        candidate = " ".join(candidate.split()).strip(" -–—._")

    if _norm_match_text(candidate) == _norm_match_text(first_title):
        tokens = [
            t for t in re.findall(r"[A-Za-z0-9']+", core_relaxed)
            if len(t) >= 4
        ]
        if len(tokens) >= 2:
            compact = "".join(tokens)
            if len(tokens) == 2 and 6 <= len(compact) <= 10:
                # Genius sometimes publishes a short two-word local title as
                # one compound parenthetical alias: Lava Girl -> Lavagirl.
                # Reuse the existing final request for that spelling instead
                # of spending it on the generic last word ("Girl").
                candidate = compact
            else:
                # Prefer the final substantial token. For typoed dual-word
                # aliases such as "Cokkie Crisp", "Crisp" often gives Genius a
                # better ranking than repeating the misspelled full title.
                candidate = tokens[-1]

    return candidate


def _genius_public_slug(value: str) -> str:
    """Build a likely Genius public-page slug locally; this performs no request."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("&", " and ")
    value = re.sub(r"['’]", "", value)
    value = re.sub(r"[^A-Za-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-").lower()


def _try_direct_extended_genius_page(expected_title: str, artist_candidates: list[str]):
    """
    Very narrow fallback for a Genius catalog quirk:
    local "(Sessions)/(Session Edit)" may be published as "(Extended)".

    IMPORTANT:
    - performs ZERO additional Search API requests;
    - constructs one deterministic lyrics-page URL locally;
    - only runs after the normal two-search budget found nothing;
    - makes at most ONE normal Genius page GET;
    - requires real lyrics content on that page before accepting it.
    """
    required = _extract_required_edition_markers(expected_title)
    if not ({"session", "session_edit"} & required):
        return None

    core = _strip_bracketed_parts(_strip_local_revision_markers(expected_title)).strip()
    if not core:
        return None

    artist = next((str(a).strip() for a in (artist_candidates or []) if str(a).strip()), "")
    if not artist:
        return None

    artist_slug = _genius_public_slug(artist)
    title_slug = _genius_public_slug(f"{core} Extended")
    if not artist_slug or not title_slug:
        return None

    url = f"https://genius.com/{artist_slug}-{title_slug}-lyrics"
    log("      Genius rare fallback: checking local Sessions → Extended URL guess "
        "(0 extra Search API requests).")
    log(f"      Genius direct Extended candidate: {url}")

    if should_cancel() or not wait_if_paused():
        return None

    try:
        page = _http_get(url).decode("utf-8", errors="replace")
        parser = _GeniusLyricsParser()
        parser.feed(page)
        lyrics = parser.get_text()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log("      Genius direct Extended candidate does not exist (404).")
        else:
            log(f"      Genius direct Extended page error: HTTP {exc.code}")
        return None
    except GeniusRateLimitError:
        raise
    except Exception as exc:
        log(f"      Genius direct Extended page unavailable: {exc}")
        return None

    if len((lyrics or "").strip()) < 50:
        log("      Genius direct Extended candidate had no usable lyrics; rejected.")
        return None

    # This fallback is deliberately strict: artist and core title are not fuzzy
    # guessed from search results; they are the trusted local metadata used to
    # construct exactly one expected "(Extended)" page.
    verified = {
        "url": url,
        "title": f"{core} (Extended)",
        "artist_names": artist,
        "_match_score": 0.97,
        "_match_score_percent": 97,
        "_match_reason": "same core title; local Sessions/Session Edit mapped to Genius Extended",
        "_direct_extended_fallback": True,
    }

    # We already downloaded the page to verify that it exists, so cache its
    # lyrics immediately. _run_genius_strict will then use a 0-page-request HIT
    # instead of downloading the same page a second time.
    _store_cached_genius_lyrics(url, lyrics)
    _store_cached_genius_match(expected_title, artist_candidates, verified)
    log("      Genius direct Extended fallback VERIFIED (97%): same artist + same core title.")
    log("      Genius cache: Extended match and lyrics saved locally.")
    return verified


def _genius_find_verified_song(expected_title: str, artist_candidates: list[str]):
    """Very low-request Genius search with a persistent verified-match cache.

    Normal flow is at most TWO network search requests for a new song:
      1) strongest artist clue + version-aware/local title;
      2) only if the first request has no >=51% candidate, one compact fallback.

    If a >=51% verified match was previously found, its Genius URL/metadata are
    loaded directly from %LOCALAPPDATA% and ZERO Genius search requests are sent.
    Hard artist/version compatibility rules still apply. Session labels are strict
    except for the local, zero-request equivalence Sessions/Session Edit ↔ Extended
    when the main title itself matches very strongly.
    """
    artist_candidates = [x for x in artist_candidates if x]
    if not artist_candidates:
        return None

    log(f"      Artist clues: {' | '.join(artist_candidates)}")

    # Persistent cache is checked before even reading the API token or building
    # a request. The final lyrics page may still be fetched if the local .txt is
    # missing, but no Genius SEARCH request is needed.
    cached_match = _get_cached_genius_match(expected_title, artist_candidates)
    if cached_match is not None:
        score_percent = int(cached_match.get("_match_score_percent", 0))
        log(f"      Genius cache HIT: reusing verified match ({score_percent}% accuracy).")
        log(f"      Genius cached source: {cached_match.get('url', '')}")
        return cached_match

    expected_titles = []
    def add_expected(value: str):
        value = " ".join((value or "").split()).strip()
        norm = _norm_match_text(value)
        if value and norm and norm not in {_norm_match_text(x) for x in expected_titles}:
            expected_titles.append(value)

    add_expected(expected_title)
    stripped_artist_title = _strip_artist_prefix_from_title(expected_title, artist_candidates)
    add_expected(stripped_artist_title)
    add_expected(_strip_local_revision_markers(expected_title))
    add_expected(_strip_local_revision_markers(stripped_artist_title))
    add_expected(_build_clean_genius_verification_title(expected_title, artist_candidates))
    add_expected(_build_clean_genius_verification_title(stripped_artist_title, artist_candidates))

    api_token = get_genius_api_token()
    log("      Genius search mode: Official API (personal token)" if api_token
        else "      Genius search mode: Public fallback (no API token configured)")

    primary_artist = artist_candidates[0]

    # Collaborators written in the title are real search evidence:
    # "(with Trippie Redd)", "(ft. Trippie Redd)", "(feat. Trippie Redd)".
    # Append them to the Genius SEARCH query when metadata only names the lead.
    featured_query_artists = _extract_featured_artists_from_title(expected_title)
    primary_artist_norm = _norm_match_text(primary_artist)
    featured_query_artists = [
        a for a in featured_query_artists
        if _norm_match_text(a) and _norm_match_text(a) not in primary_artist_norm
    ]
    if featured_query_artists:
        primary_artist = f"{primary_artist} {' '.join(featured_query_artists)}".strip()
        log(
            "      Genius artist evidence from title: "
            + " | ".join(featured_query_artists)
            + " (with/ft./feat. treated as collaborators)"
        )

    by_key = {}

    def score_hits(payload: dict, official_api: bool, used_search_title: str, relaxed: bool = False):
        for hit in _genius_hits_from_payload(payload, official_api):
            result = hit.get("result", {})
            if not isinstance(result, dict) or not result.get("url"):
                continue

            actual_title = str(result.get("title_with_featured") or result.get("title") or "")
            actual_artist = str(result.get("artist_names") or "")
            if not actual_artist:
                pa = result.get("primary_artist") or {}
                actual_artist = str(pa.get("name") or "")

            # Fresh searches remain strict on artist. Use all supplied clues for
            # verification, even though only the strongest clue is normally sent.
            matching_artist_clues = [
                a for a in artist_candidates if _artist_matches(a, actual_artist)
            ]
            if not matching_artist_clues:
                continue

            # Prefer the clue with the most matched artist components. This makes
            # "Juice WRLD, Ski Mask The Slump God" stronger than the fallback
            # single-artist clue "Juice WRLD".
            matched_artist = max(
                matching_artist_clues,
                key=lambda a: (
                    _artist_match_coverage(a, actual_artist)[0],
                    _artist_match_coverage(a, actual_artist)[2],
                    len(_split_artist_names(a)),
                ),
            )

            scored = []
            for title_clue in expected_titles:
                clue = _strip_version_labels_for_fallback(title_clue) if relaxed else title_clue
                if not clue:
                    continue
                score, reason = _title_match_details(clue, actual_title)

                # Guarded exception for local V2/V3 labels when Genius publishes
                # the same song without that suffix. We only relax the version
                # requirement when ALL expected artist components are present
                # and the version-stripped title is a very strong match.
                #
                # Example:
                #   local artists: Juice WRLD, Ski Mask The Slump God
                #   local title:   Compromise (V3)
                #   Genius:        Ski Mask The Slump God & Juice WRLD - Compromised
                #
                # The one-letter title difference plus 2/2 artist agreement is
                # much stronger evidence than an unrelated same-name song.
                if score <= 0 and reason.startswith("missing version marker"):
                    stripped_clue = _strip_semantic_version_markers_for_match(clue)
                    stripped_clue = re.sub(
                        r"\((?:\\s*)\)|\[(?:\\s*)\]|\{(?:\\s*)\}",
                        " ",
                        stripped_clue,
                    )
                    stripped_clue = " ".join(stripped_clue.split()).strip(" -–—._")
                    stripped_score, stripped_reason = _title_match_details(
                        stripped_clue, actual_title
                    )

                    matched_count, artist_total, artist_ratio = _artist_match_coverage(
                        matched_artist, actual_artist
                    )
                    all_artists_match = artist_total >= 2 and matched_count == artist_total

                    # Also allow exact/near-exact main-title typo comparison.
                    local_core = _norm_match_text(_strip_bracketed_parts(stripped_clue))
                    genius_core = _norm_match_text(_strip_bracketed_parts(actual_title))
                    near_title = (
                        local_core
                        and genius_core
                        and difflib.SequenceMatcher(None, local_core, genius_core).ratio() >= 0.90
                    )

                    if all_artists_match and (stripped_score >= 0.88 or near_title):
                        title_ratio = difflib.SequenceMatcher(
                            None, local_core, genius_core
                        ).ratio() if local_core and genius_core else stripped_score
                        score = max(0.92, min(0.97, title_ratio))
                        reason = (
                            "local version marker relaxed: all credited artists match "
                            f"({matched_count}/{artist_total}) + near-exact title "
                            f"({title_ratio:.0%}); Genius omits local V-marker"
                        )

                # "with/ft./feat." in the local title is collaborator evidence.
                # When Genius credits BOTH the metadata artist and collaborator,
                # strengthen an already-positive title match.
                ev_matched, ev_total, ev_ratio = _artist_evidence_coverage(
                    matched_artist, clue, actual_artist
                )
                if score > 0 and ev_total >= 2 and ev_matched == ev_total:
                    score = min(0.99, score + 0.04)
                    reason = (
                        f"{reason}; all credited/featured artists match "
                        f"({ev_matched}/{ev_total})"
                    )

                if relaxed:
                    # Tolerate spacing/casing such as darkside <-> Dark Side.
                    e_compact = _norm_match_text(clue).replace(" ", "")
                    a_compact = _norm_match_text(_strip_bracketed_parts(actual_title)).replace(" ", "")
                    if score > 0 and e_compact and a_compact and (
                        e_compact == a_compact or e_compact in a_compact or a_compact in e_compact
                    ):
                        score = max(score, 0.96)
                        reason = "spacing-insensitive relaxed title match"
                scored.append(((score, reason), clue))

            if not scored:
                continue
            ((score, reason), matched_expected_title) = max(scored, key=lambda item: item[0][0])
            key = str(result.get("id") or result.get("url"))
            item = (score, result, actual_title, actual_artist, reason,
                    used_search_title, matched_artist, matched_expected_title)
            previous = by_key.get(key)
            if previous is None or score > previous[0]:
                by_key[key] = item

    def best_acceptable():
        candidates = [x for x in by_key.values() if round(x[0] * 100) >= 51]
        return max(candidates, key=lambda x: x[0]) if candidates else None

    def send_one(search_artist: str, title: str, relaxed: bool = False) -> bool:
        if not wait_if_paused() or should_cancel():
            return False
        title = " ".join((title or "").split()).strip(" -–—")
        if not title:
            return False
        query = f"{search_artist} {title}".strip()
        label = "Genius fallback search" if relaxed else "Genius search"
        log(f"      {label}: {query}")
        try:
            payload, official_api = _genius_search_request(query, api_token)
            if should_cancel() or not wait_if_paused():
                return False
        except GeniusRateLimitError:
            raise
        except urllib.error.HTTPError as exc:
            if api_token and exc.code in (401, 403):
                log("      Genius API token was rejected. Open Settings > Options and update it.")
                return False
            log(f"      Genius search error: {exc}")
            return False
        except Exception as exc:
            log(f"      Genius search error: {exc}")
            return False
        score_hits(payload, official_api, title, relaxed=relaxed)
        return best_acceptable() is not None

    # QUERY 1: keep local version/session information because it can distinguish
    # V2/V3/session variants when Genius actually has them.
    classified = _classify_genius_title_parts(
        stripped_artist_title or expected_title, artist_candidates
    )
    first_title = _build_smart_genius_first_query_title(
        stripped_artist_title or expected_title, artist_candidates
    )
    if classified.get("editions"):
        log(f"      Genius local title analysis: edition kept for request #1: {', '.join(classified['editions'])}")
    if classified.get("aliases"):
        log(f"      Genius local title analysis: alternate title reserved for fallback: {classified['aliases'][0]}")
    if _norm_match_text(first_title) != _norm_match_text(stripped_artist_title or expected_title):
        log(f"      Genius optimized first query title: {first_title}")
    if send_one(primary_artist, first_title, relaxed=False):
        log("      Genius adaptive search: >=51% match found on first request; stopping search.")
    elif not should_cancel():
        # QUERY 2 (and final query): if the first high-signal query failed, use
        # a likely alternate title found in brackets.  All of this classification
        # was done locally, so we still spend at most one extra API request.
        second_title = _build_smart_genius_second_query_title(
            stripped_artist_title or expected_title, artist_candidates, first_title
        )
        second_artist = primary_artist
        if not second_title or _norm_match_text(second_title) == _norm_match_text(first_title):
            # No distinct title fallback exists; only then use the secondary
            # artist clue, if one is available.
            second_title = first_title
            if len(artist_candidates) > 1:
                second_artist = artist_candidates[1]

        log("      Genius: first request had no >=51% match; trying one final alias-aware fallback...")
        send_one(second_artist, second_title, relaxed=True)

    best = best_acceptable()
    if best is None:
        if by_key:
            rejected = max(by_key.values(), key=lambda x: x[0])
            score, _, actual_title, actual_artist, reason, _, matched_artist, matched_expected = rejected
            # Keep the closest rejected search hit for explicit user review only.
            candidate = dict(rejected[1])
            candidate.update(full_title=f"{actual_artist} - {actual_title}",
                             _match_score_percent=round(score * 100),
                             _match_score=score, _match_reason=reason, _review_only=True)
            _genius_review_state.closest_rejected = candidate
            log(f"      Genius rejected best candidate: {actual_artist} - {actual_title} "
                f"({round(score * 100)}%; {reason}; artist clue={matched_artist}; title clue={matched_expected})")

        # The Search API cannot be locally matched against a result it never
        # returned. For the rare Sessions→Extended catalog case, try exactly one
        # deterministic public-page URL instead of spending a third search query.
        direct_extended = _try_direct_extended_genius_page(expected_title, artist_candidates)
        if direct_extended:
            return direct_extended

        log(f"      Genius: no verified >=51% match after maximum 2 searches for "
            f"{' | '.join(artist_candidates)} - {expected_title}")
        return None

    score, result, actual_title, actual_artist, reason, used_search_title, matched_artist, matched_expected = best
    score_percent = round(score * 100)
    if score_percent < 90:
        log(f"      Genius verified with LOW CONFIDENCE: {actual_artist} - {actual_title} "
            f"({score_percent}% accuracy; {reason})")
    else:
        log(f"      Genius verified: {actual_artist} - {actual_title} ({score_percent}% accuracy; {reason})")
    log(f"      Genius matched artist clue: {matched_artist}")
    if _norm_match_text(matched_expected) != _norm_match_text(expected_title):
        log(f"      Genius matched title clue: {matched_expected}")
    if _norm_match_text(used_search_title) != _norm_match_text(expected_title):
        log(f"      Genius fallback title used: {used_search_title}")

    verified = dict(result)
    verified["_match_score"] = score
    verified["_match_score_percent"] = score_percent
    verified["_match_reason"] = reason
    _store_cached_genius_match(expected_title, artist_candidates, verified)
    log("      Genius cache: verified match saved for future runs.")
    return verified


class _GeniusLyricsParser(HTMLParser):
    """Extract every Genius lyrics container while ignoring its metadata header."""
    VOID_TAGS = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "embed", "param", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.in_lyrics = False
        self.exclude_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "div" and "data-lyrics-container" in attrs_dict:
            self.in_lyrics = True
            self.depth = 1
            self.exclude_depth = 0
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            return

        if not self.in_lyrics:
            return

        # Genius puts contributor/translation UI inside the first lyrics
        # container and marks it as excluded from text selection.  Do not save it.
        if self.exclude_depth:
            if tag not in self.VOID_TAGS:
                self.exclude_depth += 1
            return
        if "data-exclude-from-selection" in attrs_dict:
            if tag not in self.VOID_TAGS:
                self.exclude_depth = 1
            return

        if tag == "br":
            self.parts.append("\n")
            return
        if tag in self.VOID_TAGS:
            return
        self.depth += 1

    def handle_startendtag(self, tag, attrs):
        # Crucial for Genius: <br/> must add a newline but MUST NOT reduce
        # container depth.  HTMLParser's default start+end behavior would close
        # the lyrics container at the first self-closing <br/>.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.in_lyrics:
            return
        if tag in self.VOID_TAGS:
            return
        if self.exclude_depth:
            self.exclude_depth -= 1
            return

        self.depth -= 1
        if self.depth <= 0:
            self.in_lyrics = False
            self.depth = 0
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.in_lyrics and not self.exclude_depth:
            self.parts.append(data)

    def get_text(self) -> str:
        text = html_lib.unescape("".join(self.parts))
        lines = []
        for line in text.splitlines():
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line or (lines and lines[-1] != ""):
                lines.append(line)
        return "\n".join(lines).strip()


# Matches below this threshold require an explicit decision, even on cache hits.
GENIUS_REVIEW_THRESHOLD = 85
_genius_review_state = threading.local()


def _pending_genius_reviews():
    if not hasattr(_genius_review_state, "pending"):
        _genius_review_state.pending = {}
    return _genius_review_state.pending


def _has_pending_genius_review(song):
    return os.path.normcase(os.path.abspath(song)) in _pending_genius_reviews()


def _validated_genius_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if (parsed.scheme not in ("http", "https")
            or parsed.netloc.lower() not in ("genius.com", "www.genius.com")
            or not parsed.path.lower().endswith("-lyrics")
            or len(parsed.path.strip("/")) <= len("-lyrics")):
        raise ValueError("Paste a Genius song link, e.g. https://genius.com/Artist-song-lyrics")
    return urllib.parse.urlunsplit(("https", "genius.com", parsed.path, "", ""))


def _fetch_genius_lyrics(url):
    """Return lyrics text for a Genius song page, using the local page cache."""
    url = _validated_genius_url(url)
    lyrics = _get_cached_genius_lyrics(url)
    if not lyrics:
        page = _http_get(url).decode("utf-8", errors="replace")
        parser = _GeniusLyricsParser()
        parser.feed(page)
        lyrics = parser.get_text()
    return lyrics


def _lyrics_body_similarity(left, right):
    """Word-sequence similarity of two lyric texts, ignoring headers and source footers."""
    def words(text):
        lines = [line for line in (text or "").splitlines()
                 if not re.match(r"^\s*(\[.*\]|https?://\S+|Match accuracy:.*|Source .* by user)\s*$", line)]
        return re.findall(r"\w+(?:['’]\w+)*", " ".join(lines).casefold())
    a, b = words(left), words(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _genius_alternative_candidates(song, exclude_url="", limit=5):
    """Search Genius for other versions of one song, best candidates first.

    Unlike the strict downloader, different version labels (remix, leak, V2…)
    are kept because a wrong TXT is often a different version of the song.
    At most three search requests are sent. Returns (candidates, notes).
    """
    title, metadata_artist = get_track_metadata(song)
    artists = get_artist_candidates(song, title, metadata_artist)
    clean_title = _strip_artist_prefix_from_title(title, artists) or title
    primary_artist = artists[0] if artists else ""
    # Requests #1 and #2 are the downloader's own smart queries. Request #3 is
    # the bare main title: e.g. "Already (V2) ((GB&GR)" searched as
    # "Already V2" only returns "Already Dead (V2)", while "Already" also
    # returns the unversioned "Already" page. Unbalanced brackets are cut off.
    first_title = _build_smart_genius_first_query_title(clean_title, artists)
    second_title = _build_smart_genius_second_query_title(clean_title, artists, first_title)
    second_artist = primary_artist
    if not second_title or _norm_match_text(second_title) == _norm_match_text(first_title):
        second_title = first_title
        if len(artists) > 1:
            second_artist = artists[1]
    main_title = re.split(r"[\(\[\{]", _strip_bracketed_parts(clean_title))[0]
    main_title = _strip_version_labels_for_fallback(main_title) or first_title
    candidate_queries = [f"{primary_artist} {first_title}",
                         f"{second_artist} {second_title}",
                         f"{primary_artist} {main_title}"]
    queries = []
    for query in candidate_queries:
        query = " ".join(query.split())
        if query and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)

    api_token = get_genius_api_token()
    exclude = ""
    if exclude_url:
        try:
            exclude = _validated_genius_url(exclude_url)
        except ValueError:
            pass
    expected_core = _norm_match_text(_strip_bracketed_parts(clean_title))
    found, notes = {}, []
    for query in queries:
        try:
            payload, official_api = _genius_search_request(query, api_token)
        except GeniusRateLimitError:
            raise
        except Exception as exc:
            notes.append(f"Search '{query}' failed: {exc}")
            continue
        for hit in _genius_hits_from_payload(payload, official_api):
            if hit.get("type", "song") != "song":
                continue
            result = hit.get("result") or {}
            try:
                url = _validated_genius_url(str(result.get("url") or ""))
            except ValueError:
                continue
            if url == exclude or url in found:
                continue
            actual_title = str(result.get("title_with_featured") or result.get("title") or "")
            actual_artist = str(result.get("artist_names")
                                or (result.get("primary_artist") or {}).get("name") or "")
            strict_score, reason = _title_match_details(clean_title, actual_title)
            actual_core = _norm_match_text(_strip_bracketed_parts(actual_title))
            core_ratio = (difflib.SequenceMatcher(None, expected_core, actual_core).ratio()
                          if expected_core and actual_core else 0.0)
            title_score = max(strict_score, core_ratio * 0.9)
            artist_ok = any(_artist_matches(a, actual_artist) for a in artists)
            score = title_score * 0.75 + (0.25 if artist_ok else 0.0)
            if title_score < 0.5:
                continue
            found[url] = {
                "url": url, "title": actual_title, "artist": actual_artist,
                "score_percent": round(score * 100), "artist_match": artist_ok,
                "reason": reason if strict_score >= core_ratio * 0.9 else
                          f"main title {core_ratio:.0%} similar; version label differs",
            }
    ranked = sorted(found.values(), key=lambda item: -item["score_percent"])
    return ranked[:limit], notes


def _save_genius_result(result, out_path, confirmed=False, corrected=False):
    """Fetch only after approval and replace the destination only after validation."""
    global LAST_GENIUS_MATCH_PERCENT
    import tempfile
    temporary_path = None
    try:
        url = _validated_genius_url(result["url"])
        lyrics = _fetch_genius_lyrics(url)
        if len(lyrics.strip()) < 50:
            raise ValueError("Lyrics could not be extracted from this page. Check the link or retry.")
        score = int(result.get("_match_score_percent", round(float(result.get("_match_score", 1.0)) * 100)))
        footer = []
        if corrected:
            footer.append("Source selected manually by user")
        else:
            if score < 90:
                footer.append(f"Match accuracy: {score}%")
            if confirmed:
                footer.append("Source confirmed by user")
        footer.append(url)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=os.path.dirname(os.path.abspath(out_path)),
                                         suffix=".tmp", delete=False) as stream:
            temporary_path = stream.name
            stream.write(lyrics.rstrip() + "\n\n" + "\n".join(footer) + "\n")
        if config.get("strip_cjk", True):
            strip_cjk_lines_in_lrc(temporary_path)
        if reject_if_mostly_non_ascii(temporary_path):
            raise ValueError("Lyrics rejected by the current language filter. Check the source/settings.")
        if should_cancel():
            return False
        os.replace(temporary_path, out_path)
        temporary_path = None
        _store_cached_genius_lyrics(url, lyrics)
        LAST_GENIUS_MATCH_PERCENT = None if corrected else score
        log(f"      Genius source: {url}")
        log(f"      Saved: {out_path}")
        return True
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def _ask_genius_review(item, index, total, error=""):
    """Called by a worker; all Tk operations stay in the UI queue."""
    done = threading.Event()
    answer = {"action": "stop"}

    def show():
        dialog = None
        try:
            if should_cancel():
                done.set()
                return
            dialog = create_toplevel(root)
            dialog.title(f"Verify Genius match — {index}/{total}")
            dialog.transient(root)
            t = THEMES[config.get("theme", "dark")]
            dialog.configure(bg=t["bg"])

            def label(text, bold=False):
                tk.Label(dialog, text=text, bg=t["bg"], fg=t["fg"],
                         justify="left", wraplength=680,
                         font=("Segoe UI", 10, "bold" if bold else "normal")).pack(
                             anchor="w", padx=16, pady=5)

            label(f"Audio file: {os.path.basename(item['song'])}", True)
            if item.get("reference_title"):
                label("SHORTER REFERENCE for Builder — base title: " + item["reference_title"], True)
            label("No lyrics file has been saved by this search yet.")
            label(item["song"])
            tk.Button(dialog, text="Play Song", command=lambda: play_song(item["song"], dialog)).pack(
                anchor="w", padx=16, pady=5)
            result = item["result"]
            has_candidate = bool(result.get("url"))
            if has_candidate:
                label("Closest Genius match: " + str(result.get("full_title") or result.get("title") or result["url"]))
                label(f"Title match: {'Low confidence' if item['score'] < GENIUS_REVIEW_THRESHOLD else 'Needs review'} ({item['score']}%)", True)
                label("This is a title similarity score, not a guarantee that the lyrics match your recording.")
            else:
                label("No accepted Genius match found", True)
                label("Paste the correct Genius lyrics link below, or skip this track. Rejected search results have not been downloaded.")
            link = tk.Entry(dialog, font=("Segoe UI", 10))
            link.insert(0, result.get("url", ""))
            link.configure(state="readonly")
            link.pack(fill="x", padx=16, pady=5)
            links = tk.Frame(dialog, bg=t["bg"])
            links.pack(fill="x", padx=16)

            def copy_link():
                dialog.clipboard_clear()
                dialog.clipboard_append(result["url"])

            tk.Button(links, text="Copy link", command=copy_link,
                      state="normal" if has_candidate else "disabled").pack(side="left", padx=(0, 8))
            tk.Button(links, text="Open Genius", command=lambda: webbrowser.open(
                _validated_genius_url(result["url"])),
                state="normal" if has_candidate else "disabled").pack(side="left")
            if error:
                label(error)

            def finish(action, url=None):
                answer.update(action=action, url=url)
                done.set()
                dialog.destroy()

            correction = tk.Frame(dialog, bg=t["bg"])
            tk.Label(correction, text="Correct Genius link:", bg=t["bg"], fg=t["fg"]).pack(anchor="w")
            corrected_link = tk.Entry(correction, font=("Segoe UI", 10))
            corrected_link.pack(fill="x", pady=5)

            def submit():
                try:
                    url = _validated_genius_url(corrected_link.get())
                except ValueError as exc:
                    messagebox.showerror("Invalid Genius link", str(exc), parent=dialog)
                    return
                finish("correct", url)

            tk.Button(correction, text="Download from this link", command=submit).pack(anchor="w")
            buttons = tk.Frame(dialog, bg=t["bg"])
            buttons.pack(fill="x", padx=16, pady=16)
            confirm_btn = tk.Button(buttons, text="Confirm", command=lambda: finish("confirm"))
            confirm_btn.pack(side="left", padx=(0, 8))

            def reject():
                confirm_btn.configure(state="disabled")
                correction.pack(fill="x", padx=16, pady=(0, 16))
                corrected_link.focus_set()

            tk.Button(buttons, text="Reject", command=reject).pack(side="left", padx=4)
            tk.Button(buttons, text="Skip", command=lambda: finish("skip")).pack(side="left", padx=4)
            if item.get("reference_title"):
                def load_reference_txt():
                    path = filedialog.askopenfilename(parent=dialog, title="Select shorter reference",
                                                      filetypes=[("Lyrics text", "*.txt")])
                    if path:
                        try:
                            if not reference_queue.reference_text(path):
                                raise ValueError("The selected TXT is empty.")
                        except (OSError, UnicodeError, ValueError) as exc:
                            messagebox.showerror("Reference", str(exc), parent=dialog)
                            return
                        finish("local", path)
                tk.Button(buttons, text="Load reference TXT…", command=load_reference_txt).pack(side="left", padx=4)
            if not has_candidate:
                reject()
            dialog.protocol("WM_DELETE_WINDOW", lambda: finish("stop"))
            popup_over_root(dialog, 740, 640)
            dialog.grab_set()

            def poll_cancel():
                if done.is_set():
                    return
                if should_cancel():
                    finish("stop")
                else:
                    dialog.after(150, poll_cancel)

            poll_cancel()
        except Exception as exc:
            log(f"Review dialog error: {exc}")
            if dialog is not None:
                dialog.destroy()
            done.set()

    ui_call(show)
    while not done.wait(0.1):
        if should_cancel():
            return {"action": "stop"}
    return answer


def _review_pending_genius():
    pending = _pending_genius_reviews()
    items = list(pending.values())
    saved = 0
    ui_call(pause_btn.configure, {"state": "disabled", "text": "Pause"})
    for index, item in enumerate(items, 1):
        if should_cancel():
            break
        error = ""
        while not should_cancel():
            set_status(f"Verify Genius match {index}/{len(items)}: {os.path.basename(item['song'])}", "working")
            decision = _ask_genius_review(item, index, len(items), error)
            action = decision["action"]
            if action == "stop":
                log(f"Review: saved {saved}; skipped/unresolved {len(items) - saved}.")
                pending.clear()
                return saved, len(items) - saved
            if action == "skip":
                log(f"Review skipped: {os.path.basename(item['song'])}")
                break
            result = dict(item["result"])
            if action == "correct":
                result["url"] = decision["url"]
            elif not result.get("url"):
                error = "No candidate to confirm. Paste the correct Genius link or Skip."
                continue
            try:
                if _save_genius_result(result, item["out"], confirmed=True, corrected=action == "correct"):
                    saved += 1
                    attempted_track_paths.add(_track_status_key(item["song"]))
                    _set_persistent_track_failed(item["song"], False)
                    log(f"Review confirmed: {os.path.basename(item['song'])}")
                    break
            except Exception as exc:
                error = f"Download failed: {exc}\nRetry Confirm, paste a correct link, or Skip."
                log(error)
    pending.clear()
    log(f"Review: saved {saved}; skipped/unresolved {len(items) - saved}.")
    return saved, len(items) - saved


def _run_genius_strict(expected_title: str, expected_artist: str, out_path: str, song_path: str = "") -> bool:
    global LAST_GENIUS_MATCH_PERCENT
    LAST_GENIUS_MATCH_PERCENT = None
    if not expected_title:
        log("      Genius strict: missing title; refusing unverified download")
        return False

    artist_candidates = get_artist_candidates(song_path, expected_title, expected_artist) if song_path else [expected_artist]
    artist_candidates = [x for x in artist_candidates if x]
    if not artist_candidates:
        log("      Genius strict: no artist clue in metadata, title or folder; refusing unverified download")
        return False

    _genius_review_state.closest_rejected = None
    result = _genius_find_verified_song(expected_title, artist_candidates)
    if not result:
        result = getattr(_genius_review_state, "closest_rejected", None)
    if not result:
        if not should_cancel():
            key = os.path.normcase(os.path.abspath(song_path or out_path))
            _pending_genius_reviews()[key] = {
                "song": song_path or out_path, "result": {}, "score": None,
                "out": missing_txt_path(song_path) if song_path else out_path,
            }
            log("      Genius: no accepted match; queued for manual Genius link after the batch.")
        return False

    score = int(result.get("_match_score_percent", round(float(result.get("_match_score", 1.0)) * 100)))
    LAST_GENIUS_MATCH_PERCENT = score
    if score < GENIUS_REVIEW_THRESHOLD or result.get("_review_only"):
        key = os.path.normcase(os.path.abspath(song_path or out_path))
        _pending_genius_reviews()[key] = {
            "song": song_path or out_path, "result": dict(result), "score": score,
            "out": missing_txt_path(song_path) if song_path else out_path,
        }
        log(f"      Genius deferred for manual review: {score}% — {result['url']}")
        return False
    try:
        return _save_genius_result(result, out_path)
    except Exception as exc:
        log(f"      Genius strict download error: {exc}")
        return False


def run_provider(query: str, provider: str, out_path: str, lang_code: str, want_synced: bool,
                 expected_title: str = "", expected_artist: str = "", song_path: str = "") -> bool:
    global LAST_GENIUS_MATCH_PERCENT
    LAST_GENIUS_MATCH_PERCENT = None
    # Genius is handled separately so we can verify the returned artist before saving.
    # Genius is a plain-lyrics source in this app, never a synced source.
    if provider == "Genius" and not want_synced:
        return _run_genius_strict(expected_title, expected_artist, out_path, song_path)

    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except:
            pass

    import sysconfig
    provider_exe = Path(sysconfig.get_path("scripts")) / ("syncedlyrics.exe" if os.name == "nt" else "syncedlyrics")
    cmd = [str(provider_exe) if provider_exe.is_file() else "syncedlyrics",
           query, "-p", provider, "-o", out_path]
    cmd.append("--synced-only" if want_synced else "--plain-only")
    if lang_code:
        cmd.extend(["--lang", lang_code])

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 50


def strip_cjk_lines_in_lrc(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except:
        return False

    kept = []
    changed = False
    for line in lines:
        if line.startswith(("[ar:", "[ti:", "[al:", "[by:", "[offset:", "[re:", "[ve:")):
            kept.append(line)
            continue
        if "]" in line:
            text = line.rsplit("]", 1)[-1].strip()
            if text and CJK_RE.search(text):
                changed = True
                continue
        kept.append(line)

    try:
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write("\n".join(kept).strip() + "\n")
    except:
        return False

    return changed


def reject_if_mostly_non_ascii(path: str) -> bool:
    if not config.get("reject_non_ascii", True):
        return False

    ratio_limit = float(config.get("reject_non_ascii_ratio", 0.15))
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except:
        return False

    non_ascii = sum(1 for c in content if ord(c) > 127)
    ratio = non_ascii / max(len(content), 1)

    if ratio > ratio_limit:
        try:
            os.remove(path)
        except:
            pass
        return True
    return False


# ------------------ Download (thread + cancel) ------------------

downloading = False
cancel_requested = False
pause_requested = False
upgrade_all_session = False  # Session-level "Yes to All" for upgrades
# Set by the strict Genius downloader after a successful verified match. This
# lets the batch downloader count matches below 90% in its final summary.
LAST_GENIUS_MATCH_PERCENT = None


def require_genius_token():
    if get_genius_api_token():
        return True
    win = create_toplevel(root)
    win.title("Genius API token required")
    theme = THEMES[config.get("theme", "dark")]
    win.configure(bg=theme["bg"])
    accepted = {"value": False}
    tk.Label(win, text="Add your Genius API token", font=("Segoe UI", 14, "bold"),
             bg=theme["bg"], fg=theme["fg"]).pack(anchor="w", padx=18, pady=(18, 10))
    tk.Label(win, text="A personal token is required to search Genius for matching lyrics.\n"
             "Sign in to Genius, create an API client and generate a free Client Access Token.\n"
             "Paste the access token below (not the Client ID or Client Secret).\n"
             "It will be saved in the same place as Options > Genius API token.",
             wraplength=540, justify="left", bg=theme["bg"], fg=theme["fg"]).pack(anchor="w", padx=18)
    link = tk.Label(win, text="https://genius.com/api-clients", fg="#589bff", bg=theme["bg"], cursor="hand2")
    link.pack(anchor="w", padx=18, pady=10)
    link.bind("<Button-1>", lambda event: webbrowser.open("https://genius.com/api-clients"))
    entry = tk.Entry(win, show="*", bg=theme["panel"], fg=theme["fg"], insertbackground=theme["fg"])
    entry.pack(fill="x", padx=18, pady=8)
    def save():
        token = entry.get().strip()
        if not token:
            messagebox.showwarning("Genius API token", "Paste your Client Access Token first.", parent=win)
            return
        try:
            save_genius_api_token(token)
        except OSError:
            messagebox.showerror("Genius API token", "Could not save the token. Check access to the credentials folder.", parent=win)
            return
        accepted["value"] = True
        entry.delete(0, "end")
        win.destroy()
    tk.Button(win, text="Save and Continue", command=save, bg=theme["btn_bg"], fg=theme["btn_fg"]).pack(pady=(8, 18))
    popup_over_root(win, 590, 330)
    win.transient(root)
    win.grab_set()
    entry.focus_set()
    win.bind("<Return>", lambda event: save())
    root.wait_window(win)
    return accepted["value"]


def _start_download_mode(mode: str):
    global downloading, cancel_requested, pause_requested, upgrade_all_session
    if downloading:
        return
    if not require_genius_token():
        return
    cancel_requested = False
    pause_requested = False
    downloading = True
    upgrade_all_session = False  # Reset "Yes to All" for new download session
    cancel_btn.configure(state="normal")
    pause_btn.configure(state="normal", text="Pause")
    threading.Thread(target=lambda: download_selected(mode), daemon=True).start()


def start_download_artist():
    """Download every track belonging to the selected artist(s).

    Track selection is deliberately ignored in this mode.
    """
    _start_download_mode("artist")


def start_download_tracks():
    """Download only the explicitly selected track(s) in the Tracks column."""
    _start_download_mode("tracks")


# Backward-compatible alias for older callbacks.


def request_cancel():
    global cancel_requested, pause_requested
    if not downloading:
        return
    cancel_requested = True
    pause_requested = False
    ui_call(pause_btn.configure, {"state": "disabled", "text": "Pause"})
    set_status("Cancelling…")
    log("🛑 Cancel requested — no further tracks or Genius searches will be started.")


def request_pause_resume():
    global pause_requested
    if not downloading or cancel_requested:
        return
    pause_requested = not pause_requested
    if pause_requested:
        ui_call(pause_btn.configure, {"text": "Resume"})
        set_status("Paused")
        log("⏸ Paused — already saved lyrics and Genius cache are kept.")
    else:
        ui_call(pause_btn.configure, {"text": "Pause"})
        set_status("Resuming…", "working")
        log("▶ Resumed from the current queue position.")


def should_cancel():
    return cancel_requested


def wait_if_paused() -> bool:
    while globals().get("pause_requested", False):
        if globals().get("cancel_requested", False):
            return False
        time.sleep(0.10)
    return not globals().get("cancel_requested", False)


def resolve_track_display_to_path(artist: str, display: str) -> str:
    """Fallback resolver for a Tracks row.

    Normal code uses TRACK_ROW_PATHS, which is exact even when duplicate
    filenames exist in different Albums. This fallback also respects one
    selected album folder when possible.
    """
    display = os.path.basename(strip_icon(display))
    try:
        album_dirs = _selected_album_dirs()
        if len(album_dirs) == 1:
            return os.path.join(_artist_folder(artist), album_dirs[0], display)
    except Exception:
        pass
    return os.path.join(_artist_folder(artist), display)


def _abort_download_start_state():
    global downloading, pause_requested
    downloading = False
    pause_requested = False
    ui_call(cancel_btn.configure, {"state": "disabled"})
    ui_call(pause_btn.configure, {"state": "disabled", "text": "Pause"})


def download_selected(mode="tracks"):
    global downloading, missing_targets, upgrade_all_session, pause_requested
    _pending_genius_reviews().clear()
    downloading = True
    pause_requested = False

    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        ui_call(messagebox.showwarning, "Music folder", "Pick a valid music folder first.")
        _abort_download_start_state()
        return

    selected_artists_disp = [artist_list.get(i) for i in artist_list.curselection()]
    selected_artists = [strip_icon(a) for a in selected_artists_disp]
    selected_track_indices = list(track_list.curselection())
    targets = []

    if mode == "artist":
        if not selected_artists:
            ui_call(messagebox.showwarning, "Select artist", "Select an artist first. This button downloads all tracks for the selected artist(s).")
            _abort_download_start_state()
            return

        # Artist mode always downloads all tracks for the selected artist(s),
        # regardless of any highlighted rows in the Tracks list or Song Search.
        for artist in selected_artists:
            base_path = _artist_folder(artist)
            for root_dir, _, files in os.walk(base_path):
                for f in files:
                    if f.lower().endswith((".mp3", ".flac")):
                        targets.append(os.path.join(root_dir, f))
    else:
        # Track mode is intentionally strict: only explicitly selected tracks
        # are downloaded. Selecting an artist alone is not enough.
        if not selected_track_indices:
            ui_call(messagebox.showwarning, "Select tracks", "Select one or more tracks in the Tracks column first.")
            _abort_download_start_state()
            return

        artist = selected_artists[0] if len(selected_artists) == 1 else ""
        unresolved = []
        for i in selected_track_indices:
            # Prefer the exact absolute path captured when the Tracks row was
            # populated.  Fall back to the older display-text resolver only
            # for compatibility with any rows created by older code paths.
            path = TRACK_ROW_PATHS.get(i)
            if not path:
                path = resolve_track_display_to_path(artist, track_list.get(i))
            if path and os.path.isfile(path) and path.lower().endswith((".mp3", ".flac")):
                targets.append(path)
            else:
                unresolved.append((i, track_list.get(i), path))

        if unresolved:
            log("⚠ Could not resolve these selected track rows to audio files:")
            for _, display_name, attempted_path in unresolved:
                log(f"   {display_name} -> {attempted_path}")

    seen = set()
    deduped = []
    for p in targets:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    targets = deduped

    providers_synced = get_synced_provider_order()
    providers_plain = get_plain_provider_order()
    lang_code = get_lang_code()
    plain_ok = allow_plain_fallback()

    total = len(targets)
    if total == 0:
        ui_call(messagebox.showwarning, "No tracks resolved",
                "None of the selected rows could be resolved to an MP3/FLAC file. "
                "Refresh the artist/Tracks list and try again.")
        ui_call(cancel_btn.configure, {"state": "disabled"})
        set_status("No tracks to download")
        log("\n⚠ No valid selected audio files were resolved; download was not started.\n")
        return

    set_status(f"Downloading… ({total} tracks)", "working")
    log(f"\nProcessing {total} tracks...\n")
    
    success_count = 0
    failed_count = 0
    low_confidence_count = 0
    from studio_eta import OperationETA
    eta = OperationETA(total)
    processed_items = 0

    actually_attempted_this_run = set()
    for idx, song in enumerate(targets, 1):
        if not wait_if_paused() or should_cancel():
            log("🛑 Cancelled by user.")
            break

        if processed_items:
            eta.advance()
        processed_items = idx

        actually_attempted_this_run.add(_track_status_key(song))
        existing_path = find_existing_lyrics(song)
        lrc = get_lyrics_path(song, ".lrc")
        
        song_name = os.path.basename(song)
        title, metadata_artist = get_track_metadata(song)

        set_status(f"[{idx}/{total}] {song_name} • {eta.label()}", "working")
        log(f"[{idx}/{total}] Searching: {metadata_artist} - {title}")
        log(f"   Metadata artist (Contributing artists): {metadata_artist or '[missing]'}")
        log(f"   Metadata title: {title}")
        log(f"   Artist clues: {' | '.join(get_artist_candidates(song, title, metadata_artist)) or '[missing]'}")

        # Check if we already have lyrics
        existing_lrc_state = None
        if existing_path:
            lrc = existing_path
            existing_lrc_state = analyze_lrc(lrc)

            # If plain lyrics exist and upgrade is enabled, ask user
            if existing_lrc_state == "plain" and config.get("upgrade_plain_to_synced", True):
                log("   ℹ Found plain lyrics (.lrc file)")

                # Check if auto-upgrade is enabled or session-level "yes to all" is set
                should_upgrade = config.get("auto_upgrade_plain", False) or upgrade_all_session

                if not should_upgrade:
                    # Show dialog with Yes/No/Yes to All options
                    ui_result = {"done": False, "upgrade": False, "upgrade_all": False}

                    def ask_on_main():
                        # Custom dialog with 3 buttons
                        dialog = create_toplevel(root)
                        dialog.title("Upgrade Plain Lyrics?")
                        dialog.transient(root)
                        dialog.grab_set()
                        
                        # Center on parent
                        root.update_idletasks()
                        dialog_w = 400
                        dialog_h = 150
                        x = root.winfo_x() + (root.winfo_width() - dialog_w) // 2
                        y = root.winfo_y() + (root.winfo_height() - dialog_h) // 2
                        dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
                        
                        msg = f"Found plain lyrics for:\n{song_name}\n\nSearch for a synced version?"
                        tk.Label(dialog, text=msg, padx=20, pady=20, wraplength=360).pack()
                        
                        btn_frame = tk.Frame(dialog)
                        btn_frame.pack(pady=10)
                        
                        def on_yes():
                            ui_result["upgrade"] = True
                            ui_result["done"] = True
                            dialog.destroy()
                        
                        def on_yes_all():
                            ui_result["upgrade"] = True
                            ui_result["upgrade_all"] = True
                            ui_result["done"] = True
                            dialog.destroy()
                        
                        def on_no():
                            ui_result["upgrade"] = False
                            ui_result["done"] = True
                            dialog.destroy()
                        
                        tk.Button(btn_frame, text="Yes", command=on_yes, width=10).pack(side="left", padx=5)
                        tk.Button(btn_frame, text="Yes to All", command=on_yes_all, width=10).pack(side="left", padx=5)
                        tk.Button(btn_frame, text="No", command=on_no, width=10).pack(side="left", padx=5)
                        
                        dialog.protocol("WM_DELETE_WINDOW", on_no)

                    ui_call(ask_on_main)
                    while not ui_result["done"]:
                        if should_cancel() or _has_pending_genius_review(song):
                            break
                        import time
                        time.sleep(0.1)

                    if should_cancel() or _has_pending_genius_review(song):
                        break
                    
                    # Set session-level flag if "Yes to All" was clicked
                    if ui_result["upgrade_all"]:
                        upgrade_all_session = True
                    
                    should_upgrade = ui_result["upgrade"]
                else:
                    log("   🔄 Auto-upgrading (setting enabled or 'Yes to All' selected)")

                if should_upgrade:
                    log("   🔄 Searching for synced version...")
                    inferred_artist = metadata_artist or infer_artist_from_path(song) or (selected_artists[0] if selected_artists else "")
                    query = f"{inferred_artist} {title}".strip()

                    found_synced = False
                    for p in providers_synced:
                        if should_cancel() or _has_pending_genius_review(song):
                            break
                        log(f"      trying synced: {p}")
                        temp_lrc = get_lyrics_path(song, ".lrc", ensure_dir=True) + ".temp"
                        if run_provider(query, p, temp_lrc, lang_code, want_synced=True, expected_title=title, expected_artist=inferred_artist, song_path=song):
                            if analyze_lrc(temp_lrc) == "synced":
                                try:
                                    os.remove(lrc)
                                    os.rename(temp_lrc, lrc)
                                    log(f"      ⬆ Upgraded plain → synced [{p}]")
                                    success_count += 1
                                    found_synced = True
                                    break
                                except:
                                    pass
                            else:
                                try:
                                    os.remove(temp_lrc)
                                except:
                                    pass

                    if not found_synced:
                        log("      ↪ No synced version found, keeping plain .lrc")
                else:
                    log("   ↪ Skipped upgrade, keeping plain .lrc")

                continue  # Move to next song

            # Skip if .lrc already exists (synced, plain, or incomplete)
            if existing_lrc_state in ("synced", "incomplete"):
                log(f"   ↪ Skip (already has .lrc)")
                continue

        inferred_artist = metadata_artist or infer_artist_from_path(song) or (selected_artists[0] if selected_artists else "")
        query = f"{inferred_artist} {title}".strip()

        used = None
        mode = None

        for p in providers_synced:
            if should_cancel() or _has_pending_genius_review(song):
                break
            log(f"   trying synced: {p}")
            provider_out = get_provider_output_path(song, p, want_synced=True, ensure_dir=True)
            if run_provider(query, p, provider_out, lang_code, want_synced=True, expected_title=title, expected_artist=inferred_artist, song_path=song):
                lrc = provider_out
                used, mode = p, "synced"
                break

        if (not find_existing_lyrics(song)) and plain_ok and (not should_cancel()):
            for p in providers_plain:
                if should_cancel() or _has_pending_genius_review(song):
                    break
                log(f"   trying plain: {p}")
                provider_out = get_provider_output_path(song, p, want_synced=False, ensure_dir=True)
                if run_provider(query, p, provider_out, lang_code, want_synced=False, expected_title=title, expected_artist=inferred_artist, song_path=song):
                    lrc = provider_out
                    used, mode = p, "plain"
                    break

        if _has_pending_genius_review(song):
            actually_attempted_this_run.discard(_track_status_key(song))
            continue

        if os.path.exists(lrc):
            if config.get("strip_cjk", True):
                if strip_cjk_lines_in_lrc(lrc):
                    log("   🧹 Stripped CJK lines")

            if reject_if_mostly_non_ascii(lrc):
                log(f"   ⚠ Rejected (mostly non-ASCII) [{used}/{mode}]")
                failed_count += 1
                continue

            log(f"   ✔ Saved [{used}/{mode}]")
            success_count += 1
            if used == "Genius" and LAST_GENIUS_MATCH_PERCENT is not None and LAST_GENIUS_MATCH_PERCENT < 90:
                low_confidence_count += 1
        else:
            log("   ✖ Not found")
            failed_count += 1

    if processed_items:
        eta.advance()
    review_saved, review_skipped = _review_pending_genius()
    success_count += review_saved
    log(f"   Review confirmed: {review_saved}; skipped/unresolved: {review_skipped}")
    log("\nSummary:")
    log(f"   Lyrics found: {success_count}")
    log(f"   Auto-saved matches (85-89%): {low_confidence_count}")
    log(f"   Not found: {failed_count}")
    log("Done.\n")
    
    # Show detailed results
    if cancel_requested:
        set_status(f"Cancelled. Downloaded: {success_count}/{total} ({failed_count} failed; {review_skipped} review skipped)")
    else:
        set_status(f"Done. Downloaded: {success_count}/{total} ({failed_count} failed; {review_skipped} review skipped)", color="success")

    # Keep X across restarts, but only for songs this worker actually reached.
    # A later successful download automatically clears the remembered failure.
    for song in targets:
        key = _track_status_key(song)
        if key not in actually_attempted_this_run:
            continue
        attempted_track_paths.add(key)
        _set_persistent_track_failed(song, not bool(find_existing_lyrics(song)))

    # Store which items were downloaded and mark as scanned
    global scanned_artists
    temp_scanned = set()
    for song in targets:
        artist = infer_artist_from_path(song)
        if artist:
            temp_scanned.add(artist)
            scanned_artists.add(artist)  # Mark for icon display

    missing_targets = []
    ui_call(missing_scan_btn.configure, {"text": "Find & Download Missing Lyrics TXT [0]"})
    
    # Clear cache for downloaded artists to force rescan with updated icons
    for artist in temp_scanned:
        ap = _artist_folder(artist)
        keys_to_remove = [k for k in lyrics_cache.keys() if isinstance(k, tuple) and k[0].startswith(ap)]
        for k in keys_to_remove:
            lyrics_cache.pop(k, None)
    
    ui_call(refresh_artist_list, True)
    ui_call(refresh_current_view)

    downloading = False
    pause_requested = False
    ui_call(pause_btn.configure, {"state": "disabled", "text": "Pause"})
    ui_call(artist_dl_btn.configure, {"state": "normal"})
    ui_call(track_dl_btn.configure, {"state": "normal"})
    ui_call(open_btn.configure, {"state": "normal"})
    ui_call(edit_lyrics_btn.configure, {"state": "normal"})
    ui_call(missing_scan_btn.configure, {"state": "normal"})
    ui_call(cancel_btn.configure, {"state": "disabled"})


# ------------------ Missing (Selection-only) ------------------

def missing_txt_path(song):
    """SMART_MISSING_TXT: only the exact TXT beside this audio counts."""
    return os.path.join(os.path.dirname(song), "LYRICS",
                        os.path.splitext(os.path.basename(song))[0] + ".txt")


def collect_missing_txt(roots, songs=()):
    candidates = list(songs)
    for root_dir in roots:
        for directory, _, files in os.walk(root_dir):
            candidates.extend(os.path.join(directory, name) for name in files
                              if name.lower().endswith((".mp3", ".flac")))
    seen = set()
    missing = []
    for song in candidates:
        key = os.path.normcase(os.path.abspath(song))
        if key in seen or not os.path.isfile(song):
            continue
        seen.add(key)
        if not os.path.isfile(missing_txt_path(song)):
            missing.append(song)
    return missing


def build_missing_for_selection():
    global missing_targets, downloading, cancel_requested, pause_requested
    if downloading:
        return
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        set_status("Choose a music folder first.")
        return
    artists = [strip_icon(artist_list.get(i)) for i in artist_list.curselection()]
    rows = list(track_list.curselection())
    songs = []
    if rows:
        for i in rows:
            song = TRACK_ROW_PATHS.get(i)
            if not song and len(artists) == 1:
                song = resolve_track_display_to_path(artists[0], track_list.get(i))
            if song and song.lower().endswith((".mp3", ".flac")):
                songs.append(song)
        roots = []
    else:
        roots = [_artist_folder(a) for a in artists] if artists else [MUSIC_DIR]
    missing_targets = collect_missing_txt(roots, songs)
    config["lyrics_subfolder"] = True
    config["genius_output_format"] = "txt"
    lyrics_cache.clear()
    log(f"[SMART_MISSING_TXT] Exact Lyrics/<audio name>.txt missing: {len(missing_targets)}")
    missing_scan_btn.configure(text=f"Find & Download Missing Lyrics TXT [{len(missing_targets)}]")
    if not missing_targets:
        set_status("All tracks in this scope already have TXT files.")
        return
    cancel_requested = False
    pause_requested = False
    downloading = True
    cancel_btn.configure(state="normal")
    pause_btn.configure(state="normal", text="Pause")
    threading.Thread(target=download_missing_queue, daemon=True).start()


def download_missing_queue():
    global downloading, missing_targets, pause_requested

    if not missing_targets:
        return
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return

    _pending_genius_reviews().clear()
    downloading = True
    pause_requested = False
    ui_call(artist_dl_btn.configure, {"state": "disabled"})
    ui_call(track_dl_btn.configure, {"state": "disabled"})
    ui_call(open_btn.configure, {"state": "disabled"})
    ui_call(edit_lyrics_btn.configure, {"state": "disabled"})
    ui_call(missing_scan_btn.configure, {"state": "disabled"})

    targets = list(missing_targets)
    providers_plain = get_plain_provider_order()
    lang_code = get_lang_code()

    total = len(targets)
    set_status(f"Downloading missing… ({total} tracks)", "working")
    log(f"\nProcessing {total} missing tracks...\n")
    
    success_count = 0
    failed_count = 0
    low_confidence_count = 0
    from studio_eta import OperationETA
    eta = OperationETA(total)
    processed_items = 0

    actually_attempted_this_run = set()
    for idx, song in enumerate(targets, 1):
        if not wait_if_paused() or should_cancel():
            log("🛑 Cancelled by user.")
            break

        if processed_items:
            eta.advance()
        processed_items = idx

        actually_attempted_this_run.add(_track_status_key(song))
        existing_path = missing_txt_path(song)
        if os.path.isfile(existing_path):
            log(f"   Skipped existing TXT: {os.path.basename(song)}")
            continue
        lrc = missing_txt_path(song)
        
        song_name = os.path.basename(song)
        title, metadata_artist = get_track_metadata(song)

        set_status(f"[{idx}/{total}] {song_name} • {eta.label()}", "working")
        log(f"[{idx}/{total}] Searching: {metadata_artist} - {title}")
        log(f"   Metadata artist (Contributing artists): {metadata_artist or '[missing]'}")
        log(f"   Metadata title: {title}")
        log(f"   Artist clues: {' | '.join(get_artist_candidates(song, title, metadata_artist)) or '[missing]'}")

        inferred_artist = metadata_artist or infer_artist_from_path(song)
        query = f"{inferred_artist} {title}".strip()

        used = None
        mode = None

        for p in providers_plain:
            if should_cancel() or _has_pending_genius_review(song):
                break
            log(f"   trying plain TXT: {p}")
            os.makedirs(os.path.dirname(lrc), exist_ok=True)
            if run_provider(query, p, lrc, lang_code, want_synced=False,
                            expected_title=title, expected_artist=inferred_artist, song_path=song):
                used, mode = p, "plain"
                break

        if _has_pending_genius_review(song):
            actually_attempted_this_run.discard(_track_status_key(song))
            continue

        if os.path.exists(lrc):
            if config.get("strip_cjk", True):
                if strip_cjk_lines_in_lrc(lrc):
                    log("   🧹 Stripped CJK lines")

            if reject_if_mostly_non_ascii(lrc):
                log(f"   ⚠ Rejected (mostly non-ASCII) [{used}/{mode}]")
                failed_count += 1
                continue

            log(f"   ✔ Saved [{used}/{mode}]")
            success_count += 1
            if used == "Genius" and LAST_GENIUS_MATCH_PERCENT is not None and LAST_GENIUS_MATCH_PERCENT < 90:
                low_confidence_count += 1
        else:
            log("   ✖ Not found")
            failed_count += 1

    if processed_items:
        eta.advance()
    review_saved, review_skipped = _review_pending_genius()
    success_count += review_saved
    log(f"   Review confirmed: {review_saved}; skipped/unresolved: {review_skipped}")
    log("\nSummary:")
    log(f"   Lyrics found: {success_count}")
    log(f"   Auto-saved matches (85-89%): {low_confidence_count}")
    log(f"   Not found: {failed_count}")
    log("Done.\n")
    
    # Show detailed results with color
    if cancel_requested:
        set_status(f"Cancelled. Downloaded: {success_count}/{total} ({failed_count} failed; {review_skipped} review skipped)", "error")
    else:
        set_status(f"Done. Downloaded: {success_count}/{total} ({failed_count} failed; {review_skipped} review skipped)", "success")

    # Keep X across restarts, but only for songs this worker actually reached.
    # A later successful download automatically clears the remembered failure.
    for song in targets:
        key = _track_status_key(song)
        if key not in actually_attempted_this_run:
            continue
        attempted_track_paths.add(key)
        _set_persistent_track_failed(song, not os.path.isfile(missing_txt_path(song)))

    # Mark artists as scanned and clear their cache
    global scanned_artists
    temp_scanned = set()
    for song in targets:
        artist = infer_artist_from_path(song)
        if artist:
            temp_scanned.add(artist)
            scanned_artists.add(artist)  # Mark for icon display

    missing_targets = []
    ui_call(missing_scan_btn.configure, {"text": "Find & Download Missing Lyrics TXT [0]"})
    
    # Clear cache for scanned artists to force rescan with updated icons
    for artist in temp_scanned:
        ap = _artist_folder(artist)
        keys_to_remove = [k for k in lyrics_cache.keys() if isinstance(k, tuple) and k[0].startswith(ap)]
        for k in keys_to_remove:
            lyrics_cache.pop(k, None)
    
    ui_call(refresh_artist_list, True)
    ui_call(refresh_current_view)

    downloading = False
    pause_requested = False
    ui_call(pause_btn.configure, {"state": "disabled", "text": "Pause"})
    ui_call(artist_dl_btn.configure, {"state": "normal"})
    ui_call(track_dl_btn.configure, {"state": "normal"})
    ui_call(open_btn.configure, {"state": "normal"})
    ui_call(edit_lyrics_btn.configure, {"state": "normal"})
    ui_call(missing_scan_btn.configure, {"state": "normal"})
    ui_call(cancel_btn.configure, {"state": "disabled"})


# ------------------ Manual lyrics replacement ------------------

def refresh_tracks_from_disk(selected_paths=None):
    global missing_targets
    if downloading:
        set_status("Wait for the current operation to finish before refreshing.")
        return
    _invalidate_view_status()
    if selected_paths is None:
        selected_paths = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
    selected_keys = {_track_status_key(p) for p in selected_paths}
    artists = {strip_icon(artist_list.get(i)) for i in artist_list.curselection()}
    albums = set(_selected_album_dirs())
    lyrics_cache.clear()
    refresh_artist_list(keep_selection=False)
    artist_list.selection_clear(0, tk.END)
    for i in range(artist_list.size()):
        if strip_icon(artist_list.get(i)) in artists:
            artist_list.selection_set(i)
    artist = get_selected_artist_name()
    _populate_albums_for_artist(artist)
    for row, directory in ALBUM_ROW_DIRS.items():
        if directory in albums:
            album_list.selection_set(row)
    refresh_tracks_for_current_artist()
    for row, path in TRACK_ROW_PATHS.items():
        if _track_status_key(path) in selected_keys:
            track_list.selection_set(row)
            track_list.see(row)
    on_track_select()
    missing_targets = []
    missing_scan_btn.configure(text="Find & Download Missing Lyrics TXT [0]")


def build_backup_panel(parent, t):
    """Backups section: total size, sizes per file type with checkboxes, optional age, delete."""
    roots = [BACKUP_ROOT, Path(__file__).resolve().parent / "BACKUPS"]  # the second is the pre-merge location
    box = tk.LabelFrame(parent, text="Backups \u2014 free disk space", bg=t["bg"], fg=t["fg"],
                        font=("Segoe UI", 10, "bold"), padx=10, pady=8)
    tk.Label(box, text=("Copies the program makes before it changes your songs: lyrics text, synced lyrics "
                        "(LRC) and MP3 files. Tick what you no longer need and delete it to free space. "
                        "Program code backups are never shown or deleted."),
             bg=t["bg"], fg=t["fg"], justify="left", wraplength=900).grid(row=0, column=0, columnspan=4, sticky="w")
    total_var = tk.StringVar(value="Counting backups\u2026")
    tk.Label(box, textvariable=total_var, bg=t["bg"], fg=t["fg"], font=("Segoe UI", 10, "bold"),
             justify="left").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 4))
    type_vars, size_vars = {}, {}
    for row, (key, label) in enumerate(studio_backups.FILE_TYPES, start=2):
        type_vars[key] = tk.BooleanVar(value=False)
        tk.Checkbutton(box, text=label, variable=type_vars[key], bg=t["bg"], fg=t["fg"],
                       selectcolor=t["panel"], activebackground=t["bg"]).grid(row=row, column=0, sticky="w")
        size_vars[key] = tk.StringVar(value="\u2026")
        tk.Label(box, textvariable=size_vars[key], bg=t["bg"], fg=t["fg"]).grid(row=row, column=1, sticky="w", padx=12)
    age_row = tk.Frame(box, bg=t["bg"])
    age_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 2))
    tk.Label(age_row, text="Only backups older than", bg=t["bg"], fg=t["fg"]).pack(side="left")
    days_var = tk.StringVar(value="0")
    days_box = tk.Spinbox(age_row, from_=0, to=3650, width=6, textvariable=days_var)
    days_box.pack(side="left", padx=6)
    tk.Label(age_row, text="days (0 = all backups, whatever their age)", bg=t["bg"], fg=t["fg"]).pack(side="left")
    buttons = tk.Frame(box, bg=t["bg"])
    buttons.grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))
    state = {"busy": False, "usage": None}

    def days():
        try:
            return max(0, int(float(days_var.get() or 0)))
        except ValueError:
            return 0

    def recount(*_):
        if state["busy"]:
            return
        total_var.set("Counting backups\u2026")
        value = days()

        def worker():
            everything = studio_backups.usage_by_type(roots)
            selected = studio_backups.usage_by_type(roots, value) if value else everything
            ui_call(show, value, everything, selected)

        threading.Thread(target=worker, daemon=True).start()

    def show(value, everything, selected):
        if not box.winfo_exists() or value != days():
            return
        state["usage"] = selected
        total = sum(size for _count, size in everything.values())
        files = sum(count for count, _size in everything.values())
        total_var.set(f"All song backups: {studio_backups.format_size(total)} in {files} files"
                      + (f"   \u2022   older than {value} days: "
                         f"{studio_backups.format_size(sum(size for _c, size in selected.values()))}" if value else ""))
        for key, (count, size) in selected.items():
            size_vars[key].set(f"{count} files \u2022 {studio_backups.format_size(size)}")

    def delete():
        kinds = [key for key, var in type_vars.items() if var.get()]
        window = box.winfo_toplevel()
        if not kinds:
            messagebox.showinfo("Backups", "Tick at least one type of backup to delete.", parent=window)
            return
        usage = state["usage"] or {}
        size = sum(usage.get(kind, (0, 0))[1] for kind in kinds)
        count = sum(usage.get(kind, (0, 0))[0] for kind in kinds)
        labels = ", ".join(label for key, label in studio_backups.FILE_TYPES if key in kinds)
        value = days()
        if not count:
            messagebox.showinfo("Backups", "There is nothing of the ticked types to delete.", parent=window)
            return
        if not messagebox.askyesno(
                "Delete backups",
                f"Permanently delete {count} backup file(s) ({studio_backups.format_size(size)})?\n\n"
                f"Types: {labels}\n" + (f"Only backups older than {value} days.\n" if value else "All ages.\n")
                + "\nThis cannot be undone. Your current songs and lyrics are not affected.", parent=window):
            return
        state["busy"] = True
        delete_btn.configure(state="disabled")
        total_var.set("Deleting\u2026")

        def worker():
            deleted, freed, errors = studio_backups.delete_types(roots, kinds, value)

            def finish():
                state["busy"] = False
                log(f"Backups deleted: {deleted} file(s), {studio_backups.format_size(freed)} freed ({labels})."
                    + ("\n" + "\n".join(errors[:10]) if errors else ""))
                if box.winfo_exists():
                    delete_btn.configure(state="normal")
                    messagebox.showinfo("Backups", f"Deleted {deleted} file(s), freed {studio_backups.format_size(freed)}."
                                        + (f"\n{len(errors)} could not be deleted (see the log)." if errors else ""),
                                        parent=window)
                    recount()
            ui_call(finish)

        threading.Thread(target=worker, daemon=True).start()

    delete_btn = tk.Button(buttons, text="Delete ticked backups", command=delete)
    delete_btn.pack(side="left")
    tk.Button(buttons, text="Recount", command=recount).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="♻ Restore from backups…", font=("Segoe UI", 10, "bold"),
              command=lambda: open_restore_window(box.winfo_toplevel())).pack(side="left", padx=(16, 0))
    days_box.configure(command=recount)
    days_box.bind("<Return>", recount)
    box.after(100, recount)
    return box


def open_restore_window(parent=None):
    """Options → Restore from backups: put song files from the app's backups back into the library."""
    parent = parent or root
    if downloading:
        messagebox.showinfo("Restore from backups", "Wait for the current download to finish first.", parent=parent)
        return
    t = THEMES[config.get("theme", "dark")]
    roots = [BACKUP_ROOT, Path(__file__).resolve().parent / "BACKUPS"]  # the second is the pre-merge location
    previous_grab = parent.grab_current()
    win = create_toplevel(parent)
    win.title("Restore from backups")
    win.configure(bg=t["bg"])
    win.minsize(760, 480)
    tk.Label(win, text=("Every copy the program made before it changed or deleted a song file, newest first. "
                        "Select one or more and click Restore: the files go back to where they came from. "
                        "The files they replace are saved first (as “Before a restore”), so a restore can be undone too."),
             bg=t["bg"], fg=t["fg"], justify="left", anchor="w", wraplength=1000).pack(fill="x", padx=14, pady=(12, 6))
    filters = tk.Frame(win, bg=t["bg"])
    filters.pack(fill="x", padx=14, pady=(0, 6))
    tk.Label(filters, text="Type:", bg=t["bg"], fg=t["fg"]).pack(side="left")
    kind_var = tk.StringVar(value="All types")
    kind_box = ttk.Combobox(filters, textvariable=kind_var, state="readonly", width=38,
                            values=["All types"] + [label for _key, label in studio_restore.KINDS])
    kind_box.pack(side="left", padx=(6, 16))
    search_var = tk.StringVar(value="")
    tk.Label(filters, text="🔎 Search song:", bg=t["bg"], fg=t["fg"]).pack(side="left")
    tk.Entry(filters, textvariable=search_var, width=36, bg=t["panel"], fg=t["fg"],
             insertbackground=t["fg"]).pack(side="left", padx=(6, 0))
    count_var = tk.StringVar(value="Loading backups…")
    tk.Label(filters, textvariable=count_var, bg=t["bg"], fg=t["fg"]).pack(side="right")
    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
    embed_var = tk.BooleanVar(value=True)
    tk.Checkbutton(win, text="After restoring an LRC, also update the MP3's embedded lyrics (when the MP3 has lyrics)",
                   variable=embed_var, bg=t["bg"], fg=t["fg"], selectcolor=t["panel"],
                   activebackground=t["bg"]).pack(side="bottom", anchor="w", padx=14)
    panes = tk.PanedWindow(win, orient="vertical", sashwidth=8, sashrelief="raised", bg=t.get("muted", "#555555"),
                           bd=0, opaqueresize=True)
    panes.pack(fill="both", expand=True, padx=14)
    list_frame = tk.Frame(panes, bg=t["bg"])
    backups_box = tk.Listbox(list_frame, selectmode="extended", exportselection=False, bg=t["panel"], fg=t["fg"],
                             font=("Consolas", 10), selectbackground=t.get("accent", "#355a7a"))
    scroll = tk.Scrollbar(list_frame, command=backups_box.yview)
    backups_box.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    backups_box.pack(fill="both", expand=True)
    details = tk.Text(panes, height=8, wrap="none", bg=t["panel"], fg=t["fg"], font=("Consolas", 10))
    panes.add(list_frame, minsize=140, stretch="always")
    panes.add(details, minsize=70, stretch="always")
    state = {"items": [], "view": [], "busy": False}

    def load():
        count_var.set("Loading backups…")

        def worker():
            index = studio_restore.library_index(MUSIC_DIR)
            items = studio_restore.list_backups(roots, index)
            ui_call(loaded, items)

        threading.Thread(target=worker, daemon=True).start()

    def loaded(items):
        if not win.winfo_exists():
            return
        state["items"] = items
        render()

    def render(*_):
        kind = kind_var.get()
        needle = search_var.get().strip().casefold()
        state["view"] = [item for item in state["items"]
                         if (kind == "All types" or item["kind"] == kind)
                         and (not needle or any(needle in Path(str(target or copy)).name.casefold()
                                                for copy, target in item["files"]))]
        backups_box.delete(0, "end")
        for item in state["view"]:
            unknown = any(target is None for _copy, target in item["files"])
            backups_box.insert("end", studio_restore.describe(item) + ("   ⚠ place unknown" if unknown else ""))
        count_var.set(f"{len(state['view'])} of {len(state['items'])} backups")
        details.delete("1.0", "end")
        if state["view"]:
            backups_box.selection_set(0)
            show_details()

    def show_details(_event=None):
        selection = backups_box.curselection()
        details.delete("1.0", "end")
        if not selection:
            return
        item = state["view"][selection[0]]
        lines = [f"{item['kind']} — {item['path']}", ""]
        for copy, target in item["files"]:
            if target is None:
                lines.append(f"  {copy.name}  →  place in the library unknown (skipped)")
            else:
                note = "replaces the current file" if target.exists() else "the file is missing now"
                lines.append(f"  {copy.name}  →  {target}   ({note})")
        details.insert("1.0", "\n".join(lines))

    def restore_selected():
        items = [state["view"][i] for i in backups_box.curselection()]
        if not items or state["busy"]:
            if not items:
                messagebox.showinfo("Restore from backups", "Select one or more backups first.", parent=win)
            return
        files = sum(1 for item in items for _copy, target in item["files"] if target is not None)
        if not messagebox.askyesno(
                "Restore from backups",
                f"Restore {files} file(s) from {len(items)} backup(s)?\n\n"
                "The current files they replace are saved first, so you can undo this from this window "
                "(type “Before a restore”).", parent=win):
            return
        state["busy"] = True
        restore_btn.configure(state="disabled")
        embed = bool(embed_var.get())

        def worker():
            restored, skipped, safety = studio_restore.restore(items, BACKUP_ROOT)
            embedded = 0
            restored_mp3 = {path.with_suffix("").name.casefold() for path in restored if path.suffix.lower() == ".mp3"}
            for target in restored:
                if target.suffix.lower() in (".lrc", ".txt"):
                    _remember_fix("Restored from backup", target, next(i["kind"] for i in items
                                                                     if any(t == target for _c, t in i["files"])))
                if not embed or target.suffix.lower() != ".lrc" or target.stem.casefold() in restored_mp3:
                    continue
                try:
                    audio = studio_resync.audio_for_lrc(target)
                    if _has_embedded_lyrics(str(audio)):
                        _embed_synced_lrc_as_mp3tag_unsyncedlyrics(
                            str(audio), target.read_text(encoding="utf-8-sig"), config.get("sync_language", "en"))
                        embedded += 1
                except Exception as exc:
                    skipped.append(f"{target.name}: restored, but the MP3 was not updated ({exc})")
            ui_call(finish, restored, skipped, safety, embedded)

        threading.Thread(target=worker, daemon=True).start()

    def finish(restored, skipped, safety, embedded):
        state["busy"] = False
        _SYNCED_LYRICS_CACHE.clear()
        lyrics_cache.clear()
        refresh_tracks_from_disk()
        log(f"Restored from backups: {len(restored)} file(s), {embedded} MP3 embedded lyrics updated"
            + (f"; the replaced files are in {safety}" if safety else "")
            + ("\n" + "\n".join(skipped[:20]) if skipped else ""))
        if not win.winfo_exists():
            return
        restore_btn.configure(state="normal")
        messagebox.showinfo(
            "Restore from backups",
            f"Restored {len(restored)} file(s)." + (f" Updated the embedded lyrics in {embedded} MP3(s)." if embedded else "")
            + (f"\n\n{len(skipped)} skipped:\n" + "\n".join(skipped[:12]) if skipped else ""), parent=win)
        load()

    def close():
        win.destroy()
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()

    restore_btn = tk.Button(buttons, text="♻ Restore selected", command=restore_selected, font=("Segoe UI", 10, "bold"))
    restore_btn.pack(side="left")
    tk.Button(buttons, text="Refresh", command=load).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=close).pack(side="right")
    backups_box.bind("<<ListboxSelect>>", show_details)
    kind_box.bind("<<ComboboxSelected>>", render)
    search_var.trace_add("write", render)
    win.protocol("WM_DELETE_WINDOW", close)
    popup_over_root(win, 1080, 720)
    win.grab_set()
    load()


def open_backup_cleanup():
    """Library button: the same backups panel as in Options, in its own window."""
    if downloading:
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Clean Old Backups")
    win.configure(bg=t["bg"])
    win.transient(root)
    build_backup_panel(win, t).pack(fill="both", expand=True, padx=12, pady=12)
    tk.Button(win, text="Close", command=win.destroy).pack(anchor="e", padx=12, pady=(0, 12))
    popup_over_root(win, 960, 340)


def open_missing_lyrics_repair():
    if downloading:
        set_status("Wait for the current operation to finish before checking lyrics.")
        return
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        messagebox.showinfo("Check missing TXT / LRC", "Open a music folder first.", parent=root)
        return
    studio_lyrics_repair.open_review(root, MUSIC_DIR, refresh_tracks_from_disk, log,
                                    palette=THEMES[config.get('theme', 'dark')],
                                    create_window=create_toplevel)


def _validate_song_name(name):
    # Spaces inside a title are valid. Trim only accidental whitespace around
    # the value so names such as "Song (SSSS) (XDXD)" work as expected.
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a song name.")
    if name.endswith("."):
        raise ValueError("The song name cannot end with a dot.")
    if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name):
        raise ValueError('The name cannot contain Windows filename characters: < > : " / \\ | ? *')
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if name.split(".")[0].upper() in reserved:
        raise ValueError("This name is reserved by Windows.")
    if name.lower().endswith((".mp3", ".flac")):
        raise ValueError("Enter the song name without .mp3 or .flac; the extension is kept automatically.")
    return name


def _write_audio_title(path, title, extension):
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2
        from mutagen.flac import FLAC
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The Mutagen library is missing in this Python environment. "
            "Close the app and start triplenine_lyrics_downloader.py normally; "
            "its launcher is configured for Python 3.10."
        ) from exc
    if extension.lower() == ".mp3":
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        version = 3 if tags.version[1] == 3 else 4
        tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
        tags.save(str(path), v2_version=version)
    elif extension.lower() == ".flac":
        tags = FLAC(str(path))
        tags["title"] = [title]
        tags.save()
    else:
        raise ValueError("Only MP3 and FLAC files are supported.")


def rename_song_and_title(song, name):
    """Stage tag changes, reject collisions, then rename audio and exact sidecars."""
    import tempfile
    import uuid
    name = _validate_song_name(name)
    source = Path(song).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Audio file not found: {source}")
    destination = source.with_name(name + source.suffix)
    same_path = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    pairs = [(source, destination)]
    for folder in (source.parent, source.parent / "LYRICS", TRANSCRIPTION_TODO_DIR):
        if folder == TRANSCRIPTION_TODO_DIR:
            marker_suffixes = (reference_queue.FOUND_SUFFIX,
                               reference_queue.MISSING_SUFFIX,
                               reference_queue.READY_FOR_SYNC_SUFFIX, "")
            for marker_suffix in marker_suffixes:
                old = folder / (source.stem + marker_suffix + ".txt")
                if old.is_file():
                    pairs.append((old, folder / (name + marker_suffix + ".txt")))
        else:
            for ext in (".txt", ".lrc"):
                old = folder / (source.stem + ext)
                if old.is_file():
                    pairs.append((old, folder / (name + ext)))
    for old, new in pairs:
        if new.exists() and not same_path(old, new):
            raise FileExistsError(f"Name already exists; nothing changed:\n{new}")
    # Kept after success too: metadata can always be restored byte-for-byte.
    backup_dir = BACKUP_ROOT / "SONG-RENAMES" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    backup_dir.mkdir(parents=True)
    backup = backup_dir / source.name
    shutil.copy2(source, backup)
    fd, staged = tempfile.mkstemp(dir=source.parent, suffix=source.suffix)
    os.close(fd)
    moved = []
    audio_committed = False
    try:
        shutil.copy2(source, staged)
        _write_audio_title(staged, name, source.suffix)
        for old, new in pairs:
            if str(old) != str(new):
                os.rename(old, new)
                moved.append((old, new))
        os.replace(staged, destination)
        audio_committed = True
    except Exception as exc:
        rollback_errors = []
        for old, new in reversed(moved):
            try:
                os.rename(new, old)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RuntimeError(f"Rename failed: {exc}. Restore backup: {backup}. Rollback errors: {rollback_errors}") from exc
        raise
    finally:
        if os.path.exists(staged):
            os.remove(staged)
    if audio_committed:
        return str(destination), str(backup)


def remove_embedded_lyrics(song_path):
    """Remove only MP3 lyrics frames; leave audio, other tags and sidecars intact."""
    from mutagen.id3 import ID3, ID3NoHeaderError
    if Path(song_path).suffix.lower() != ".mp3":
        return False
    try:
        tags = ID3(song_path)
    except ID3NoHeaderError:
        return False
    keys = [key for key, frame in tags.items()
            if frame.FrameID in {"USLT", "SYLT"}
            or (frame.FrameID == "TXXX" and "lyric" in frame.desc.lower())]
    if not keys:
        return False
    version = 3 if tags.version[1] == 3 else 4
    for key in keys:
        del tags[key]
    tags.save(song_path, v2_version=version)
    _SYNCED_LYRICS_CACHE.pop(os.path.normcase(str(Path(song_path).resolve())), None)
    return True


def remove_selected_embedded_lyrics():
    global downloading
    if downloading:
        return
    songs = [TRACK_ROW_PATHS[row] for row in track_list.curselection()
             if row in TRACK_ROW_PATHS]
    if not songs:
        messagebox.showinfo("Remove embedded lyrics", "Select tracks in the Tracks list first.")
        return
    downloading = True
    _invalidate_view_status()
    set_status("Removing embedded lyrics and checking files…", "info")

    def worker():
        from studio_eta import OperationETA
        removed = skipped = remaining = 0
        errors = []
        eta = OperationETA(len(songs))
        for index, song in enumerate(songs, 1):
            try:
                if remove_embedded_lyrics(song):
                    removed += 1
                else:
                    skipped += 1
                remaining += int(_has_synced_lyrics(song, force=True))
            except Exception as exc:
                errors.append(f"{os.path.basename(song)}: {exc}")
            eta.advance()
            ui_call(set_status, f"Removing embedded lyrics [{index}/{len(songs)}] • {eta.label()}", "working")

        def finish():
            global downloading
            downloading = False
            refresh_tracks_from_disk(selected_paths=songs)
            summary = f"Embedded lyrics removed: {removed}. Unchanged: {skipped}. Failed: {len(errors)}."
            if remaining:
                summary += f" Synced LRC files still available: {remaining}."
            log(summary)
            set_status(summary, "warning" if errors else "success")
            if errors:
                messagebox.showerror("Remove embedded lyrics", summary + "\n\n" + "\n".join(errors))
        ui_call(finish)

    threading.Thread(target=worker, daemon=True).start()


def embed_existing_lrc(song):
    """Embed an exact-name sidecar using a staged MP3 and a recoverable backup."""
    import tempfile
    import uuid
    audio = Path(song).resolve()
    if audio.suffix.lower() != '.mp3':
        raise ValueError('Only MP3 files support this action.')
    candidates = []
    for path in studio_resync.lrc_locations(audio):
        if path.is_file():
            text = path.read_text(encoding='utf-8-sig')
            if _LRC_TIMESTAMP_RE.search(text):
                candidates.append((path, text.rstrip()))
    if not candidates:
        raise ValueError('No matching LRC with timestamps found next to the song, in LYRICS or SYNCED LYRICS.')
    if len({text for _, text in candidates}) != 1:
        raise ValueError('Several different LRC versions exist. Keep the intended version in one of the lyrics folders before embedding.')
    source, text = candidates[0]
    audio_stat = audio.stat()
    stamp = (audio_stat.st_ino, audio_stat.st_size, audio_stat.st_mtime_ns)
    backup_dir = BACKUP_ROOT / 'LRC-EMBED' / (
        datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    backup_dir.mkdir(parents=True)
    backup = backup_dir / audio.name
    shutil.copy2(audio, backup)
    _write_restore_map(backup_dir, [(audio, backup)])
    fd, staged_name = tempfile.mkstemp(dir=audio.parent, prefix='.t9-embed-', suffix='.tmp')
    os.close(fd)
    staged = Path(staged_name)
    try:
        shutil.copy2(backup, staged)
        # Keep the existing Mp3tag-compatible writer and its language handling.
        staged_mp3 = staged.with_suffix('.mp3')
        staged.rename(staged_mp3)
        staged = staged_mp3
        _embed_synced_lrc_as_mp3tag_unsyncedlyrics(str(staged), text, config.get('sync_language', 'en'))
        current = audio.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != stamp:
            raise RuntimeError('Audio changed during embedding. Original left untouched; try again.')
        if source.read_text(encoding='utf-8-sig').rstrip() != text:
            raise RuntimeError('LRC changed during embedding. Original left untouched; try again.')
        os.replace(staged, audio)
    finally:
        if staged.exists():
            staged.unlink()
    _SYNCED_LYRICS_CACHE.pop(os.path.normcase(str(audio)), None)
    return source, backup


def embed_selected_lrc():
    global downloading
    if downloading:
        return
    songs = [TRACK_ROW_PATHS[row] for row in track_list.curselection() if row in TRACK_ROW_PATHS]
    if not songs:
        messagebox.showinfo('Embed LRC into MP3', 'Select one or more MP3 tracks first.', parent=root)
        return
    downloading = True
    set_status('Embedding edited LRC files into MP3…', 'working')

    def worker():
        completed, errors = [], []
        for index, song in enumerate(songs, 1):
            try:
                source, backup = embed_existing_lrc(song)
                completed.append(song)
                ui_call(log, f'Embedded {source} into {song}. Backup: {backup}')
            except Exception as exc:
                errors.append(f'{Path(song).name}: {exc}')
            ui_call(set_status, f'Embedding LRC [{index}/{len(songs)}]', 'working')

        def finish():
            global downloading
            downloading = False
            refresh_tracks_from_disk(selected_paths=songs)
            summary = f'LRC embedded: {len(completed)}. Skipped / failed: {len(errors)}.'
            log(summary)
            set_status(summary, 'warning' if errors else 'success')
            if errors:
                messagebox.showwarning('Embed LRC into MP3', summary + '\n\n' + '\n'.join(errors), parent=root)
        ui_call(finish)
    threading.Thread(target=worker, daemon=True).start()


def change_lrc_manually():
    if downloading:
        return
    songs = [TRACK_ROW_PATHS[row] for row in track_list.curselection() if row in TRACK_ROW_PATHS]
    if len(songs) != 1 or Path(songs[0]).suffix.lower() != '.mp3':
        messagebox.showinfo('Change LRC Manually', 'Select exactly one MP3 track.', parent=root)
        return
    audio = Path(songs[0])
    candidates = [folder / (audio.stem + '.lrc') for folder in
                  (audio.parent / 'SYNCED LYRICS', audio.parent / 'LYRICS', audio.parent)]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        messagebox.showinfo('Change LRC Manually', 'No matching LRC was found for this song.', parent=root)
        return
    selected = candidates[0]
    if len(candidates) > 1:
        filename = filedialog.askopenfilename(parent=root, title='Choose the LRC version to edit',
            initialdir=str(selected.parent), initialfile=selected.name, filetypes=[('LRC lyrics', '*.lrc')])
        if not filename:
            return
        selected = Path(filename)
        if selected.resolve() not in [p.resolve() for p in candidates]:
            messagebox.showerror('Change LRC Manually', 'Choose a matching LRC belonging to this song.', parent=root)
            return
    language = config.get('sync_language', 'en')
    def saved(song, lrc, backup):
        _SYNCED_LYRICS_CACHE.clear()
        refresh_tracks_from_disk([song])
        log(f'Manual LRC saved and embedded: {lrc}. Backup: {backup}')
    try:
        studio_lrc_editor.open_editor(root, audio, selected, THEMES[config.get('theme', 'dark')],
            create_toplevel, lambda song, text: _embed_synced_lrc_as_mp3tag_unsyncedlyrics(song, text, language),
            BACKUP_ROOT / 'LRC-EDITS-AND-EMBED', saved)
    except (OSError, UnicodeError) as exc:
        messagebox.showerror('Change LRC Manually', str(exc), parent=root)


def open_change_song_name():
    if downloading:
        return
    rows = list(track_list.curselection())
    if len(rows) != 1:
        messagebox.showinfo("Change Song Name + Title", "Select exactly one track first.")
        return
    song = TRACK_ROW_PATHS.get(rows[0])
    if not song or not os.path.isfile(song):
        messagebox.showerror("Change Song Name + Title", "Audio file not found. Click Refresh.")
        return
    win = create_toplevel(root)
    win.title("Change Song Name + Title")
    win.transient(root)
    t = THEMES[config.get("theme", "dark")]
    win.configure(bg=t["bg"])
    title, _artist = get_track_metadata(song)
    for text in ("Current file: " + os.path.basename(song), "Current Title: " + title,
                 "New name (without extension) — also used as the Title tag:"):
        tk.Label(win, text=text, bg=t["bg"], fg=t["fg"], wraplength=650,
                 justify="left").pack(anchor="w", padx=16, pady=6)
    entry = tk.Entry(win, font=("Segoe UI", 11))
    entry.insert(0, Path(song).stem)
    entry.pack(fill="x", padx=16, pady=6)
    tk.Label(win, text="Matching TXT/LRC files and the transcription reminder are renamed too.\nAn audio backup is saved in backups/song-renames.",
             bg=t["bg"], fg=t["fg"], wraplength=650, justify="left").pack(anchor="w", padx=16, pady=6)
    status = tk.Label(win, text="", bg=t["bg"], fg=t["fg"], wraplength=650)
    status.pack(fill="x", padx=16)
    busy = {"value": False}

    def close():
        if not busy["value"]:
            win.destroy()

    def submit():
        global downloading
        if busy["value"] or downloading:
            return
        try:
            name = _validate_song_name(entry.get())
        except ValueError as exc:
            status.configure(text=str(exc))
            return
        downloading = True
        busy["value"] = True
        save_btn.configure(state="disabled")
        entry.configure(state="disabled")
        status.configure(text="Saving name and Title…")

        def finish(new_path, backup, error):
            global downloading
            downloading = False
            busy["value"] = False
            if error:
                status.configure(text=error)
                save_btn.configure(state="normal")
                entry.configure(state="normal")
                return
            failed = _is_persistent_track_failed(song)
            _set_persistent_track_failed(song, False)
            _set_persistent_track_failed(new_path, failed)
            old_key = _track_status_key(song)
            if old_key in attempted_track_paths:
                attempted_track_paths.discard(old_key)
                attempted_track_paths.add(_track_status_key(new_path))
            win.destroy()
            if not _track_matches_song_search(os.path.basename(new_path), song_search_var.get()):
                song_search_var.set("")
            refresh_tracks_from_disk([new_path])
            set_status("Renamed file and Title: " + os.path.basename(new_path), "success")
            log(f"Renamed: {song} -> {new_path}\nAudio backup: {backup}")

        def worker():
            try:
                new_path, backup = rename_song_and_title(song, name)
            except Exception as exc:
                ui_call(finish, "", "", str(exc))
            else:
                ui_call(finish, new_path, backup, "")

        threading.Thread(target=worker, daemon=True).start()

    save_btn = tk.Button(win, text="Save Name + Title", command=submit)
    save_btn.pack(side="left", padx=16, pady=12)
    tk.Button(win, text="Close", command=close).pack(side="right", padx=16, pady=12)
    win.protocol("WM_DELETE_WINDOW", close)
    popup_over_root(win, 700, 350)
    win.grab_set()
    entry.focus_set()
    entry.selection_range(0, tk.END)


def _find_transcription_todo_dir():
    """Use an existing project-level folder; otherwise default beside this script."""
    script_dir = Path(__file__).resolve().parent
    project_folder = script_dir.parent / "MARKED FOR TRANSCRIPTION"
    if project_folder.is_dir():
        return project_folder
    return script_dir / "MARKED FOR TRANSCRIPTION"


TRANSCRIPTION_TODO_DIR = _find_transcription_todo_dir()


def transcription_marker_path(song, destination=None):
    destination = Path(destination) if destination is not None else TRANSCRIPTION_TODO_DIR
    return (reference_queue.existing_marker(song, destination)
            or reference_queue.marker_paths(song, destination)[1])


def _is_marked_for_transcription(song):
    # The marker files themselves are the persistent cache. This intentionally
    # checks disk so markers survive restarts and F5 sees external changes.
    return reference_queue.existing_marker(song, TRANSCRIPTION_TODO_DIR) is not None


def _track_display_status(song, existing_lyrics, failed_before, attempted=False):
    embedded, sidecar = _synced_lyrics_sources(song)
    if embedded:
        return "🎶 ", "synced"
    if sidecar:
        return "📄 ", "lrc"
    marker = reference_queue.existing_marker(song, TRANSCRIPTION_TODO_DIR)
    if existing_lyrics and marker and marker.stem.casefold().endswith(reference_queue.READY_FOR_SYNC_SUFFIX.casefold()):
        return "⏱ Ready for Lyric Sync — ", "downloaded"
    if existing_lyrics:
        return "✅ ", "downloaded"
    marker = reference_queue.existing_marker(song, TRANSCRIPTION_TODO_DIR)
    if marker is not None:
        # Read the filename on every refresh, including the first load after
        # restart. A shorter reference is not a completed Sessions transcript.
        found = marker.stem.casefold().endswith(reference_queue.FOUND_SUFFIX.casefold())
        return ("📝✅ " if found else "📝❌ "), "transcription"
    if failed_before or attempted:
        return "❌ ", "failed"
    return "", "untouched"


def create_transcription_markers(songs, destination=TRANSCRIPTION_TODO_DIR):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    created, existing, errors = [], [], []
    for song in dict.fromkeys(songs):
        audio = Path(song)
        if not audio.is_file() or audio.suffix.lower() not in (".mp3", ".flac"):
            errors.append(f"Audio file not found or unsupported: {song}")
            continue
        marker = reference_queue.existing_marker(audio, destination)
        if marker:
            existing.append(str(marker))
            continue
        try:
            marker = reference_queue.ensure_marker(audio, destination)
            created.append(str(marker))
        except OSError as exc:
            errors.append(f"{audio.name}: {exc}")
    return created, existing, errors


def _lyrics_files_for_song(song):
    audio = Path(song)
    candidates = []
    for folder in (audio.parent, audio.parent / "LYRICS", audio.parent / "SYNCED LYRICS"):
        candidates.extend((folder / (audio.stem + ".txt"), folder / (audio.stem + ".lrc")))
    result, seen = [], set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and path.is_file():
            seen.add(key)
            result.append(path)
    return result


def mark_songs_for_transcription(songs, destination=TRANSCRIPTION_TODO_DIR):
    """Create persistent markers and safely remove any exact TXT/LRC lyrics."""
    import uuid
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    created, existing, removed, backup_dirs, errors = [], [], [], [], []
    for song in dict.fromkeys(songs):
        audio = Path(song)
        if not audio.is_file() or audio.suffix.lower() not in (".mp3", ".flac"):
            errors.append(f"Audio file not found or unsupported: {song}")
            continue
        marker = reference_queue.existing_marker(audio, destination)
        marker_was_created = False
        try:
            if marker is None:
                marker = reference_queue.ensure_marker(audio, destination)
                marker_was_created = True
        except OSError as exc:
            errors.append(f"{audio.name}: could not create marker: {exc}")
            continue

        lyrics_files = _lyrics_files_for_song(audio)
        backup_map = []
        backup_dir = None
        try:
            if lyrics_files:
                backup_dir = BACKUP_ROOT / "MARKED-FOR-TRANSCRIPTION" / (
                    datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
                )
                backup_dir.mkdir(parents=True)
                for index, lyrics_path in enumerate(lyrics_files, 1):
                    backup_path = backup_dir / f"{index:02d}_{lyrics_path.parent.name}_{lyrics_path.name}"
                    shutil.copy2(lyrics_path, backup_path)
                    if backup_path.read_bytes() != lyrics_path.read_bytes():
                        raise OSError(f"Backup verification failed for {lyrics_path}")
                    backup_map.append((lyrics_path, backup_path))
                deleted = []
                try:
                    for lyrics_path, _backup_path in backup_map:
                        os.remove(lyrics_path)
                        deleted.append(lyrics_path)
                except OSError:
                    for original, backup_path in backup_map:
                        if not original.exists():
                            shutil.copy2(backup_path, original)
                    raise
                removed.extend(str(path) for path in deleted)
                backup_dirs.append(str(backup_dir))
        except Exception as exc:
            if marker_was_created:
                try:
                    marker.unlink()
                except OSError:
                    pass
            errors.append(f"{audio.name}: lyrics were kept because marking failed: {exc}")
            continue

        (created if marker_was_created else existing).append(str(marker))
    return created, existing, removed, backup_dirs, errors


def mark_selected_for_transcription():
    if downloading:
        return
    rows = list(track_list.curselection())
    if not rows:
        messagebox.showinfo("Mark for Transcription", "Select one or more tracks in Tracks first.")
        return
    songs, unresolved = [], []
    artist = get_selected_artist_name()
    for row in rows:
        song = TRACK_ROW_PATHS.get(row)
        if not song and artist:
            song = resolve_track_display_to_path(artist, track_list.get(row))
        if song:
            songs.append(song)
        else:
            unresolved.append(str(track_list.get(row)))
    try:
        created, existing, removed, backup_dirs, errors = mark_songs_for_transcription(songs)
    except OSError as exc:
        messagebox.showerror("Mark for Transcription", f"Cannot create transcription folder:\n{exc}")
        return
    errors.extend(f"Could not resolve track: {name}" for name in unresolved)
    for path in created:
        log("Marked for transcription: " + path)
    for path in removed:
        log("Removed active lyrics after backup: " + path)
    for path in backup_dirs:
        log("Removed lyrics backup: " + path)
    for error in errors:
        log("Transcription marker error: " + error)
    marked_songs = created + existing
    for song in songs:
        if str(transcription_marker_path(song)) in marked_songs:
            _set_persistent_track_failed(song, False)
            attempted_track_paths.discard(_track_status_key(song))
    summary = (f"Created: {len(created)} TXT markers\nAlready marked: {len(existing)}\n"
               f"Removed lyrics files: {len(removed)} (backups kept)\n"
               f"Errors: {len(errors)}\n\nFolder:\n{TRANSCRIPTION_TODO_DIR}")
    set_status(f"Transcription markers: {len(created)} created, {len(existing)} already exist")
    refresh_tracks_from_disk(songs)
    if errors:
        messagebox.showwarning("Mark for Transcription", summary + "\n\nSee Log for details.")
    else:
        messagebox.showinfo("Mark for Transcription", summary)
    start_shorter_references([song for song in songs
                              if str(transcription_marker_path(song)) in marked_songs])


_last_deleted_lyrics = []


def undo_delete_lyrics():
    if downloading or not _last_deleted_lyrics:
        return
    restored, blocked, errors = [], [], []
    for song, original, backup in list(_last_deleted_lyrics):
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            content = backup.read_bytes()
            # Exclusive creation protects text downloaded/edited after deletion.
            with original.open("xb") as stream:
                stream.write(content)
            restored.append(song)
            _last_deleted_lyrics.remove((song, original, backup))
            log(f"Restored lyrics: {original}")
        except FileExistsError:
            blocked.append(str(original))
        except OSError as exc:
            errors.append(f"{original}: {exc}")
    refresh_tracks_from_disk(restored or None)
    set_status(f"Lyrics restored: {len(restored)}; existing files kept: {len(blocked)}; errors: {len(errors)}")
    if blocked or errors:
        messagebox.showwarning("Undo Delete Lyrics", "Existing files were not overwritten. Backups are kept.\n\n" + "\n".join(blocked + errors))


def delete_synced_lyrics_for_selected():
    delete_lyrics_for_selected(synced_only=True)


def delete_lyrics_for_selected(synced_only=False):
    if downloading:
        messagebox.showinfo("Delete Lyrics", "Wait for the current operation to finish first.")
        return
    songs = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
    if not songs:
        messagebox.showinfo("Delete Lyrics", "Select tracks first.")
        return
    description = ("Delete timed LRC files only? Plain TXT lyrics and embedded tags will be kept."
                   if synced_only else "Delete ALL matching TXT and LRC files? Embedded tags will be kept.")
    if not messagebox.askyesno("Delete Lyrics", f"{description}\n\nSelected tracks: {len(songs)}\nBackups will be saved."):
        return
    import tempfile
    removed, errors = 0, []
    deleted = []
    for song in songs:
        audio = Path(song)
        # Plain lyrics normally live in LYRICS. Timed sidecars can live beside
        # the audio or in SYNCED LYRICS. Remove all supported exact-name forms.
        files = _lyrics_files_for_song(audio)
        try:
            if synced_only:
                files = [path for path in files if path.suffix.lower() == ".lrc"
                         and _LRC_TIMESTAMP_RE.search(path.read_text(encoding="utf-8-sig", errors="replace"))]
            if files:
                backup_root = BACKUP_ROOT
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = Path(tempfile.mkdtemp(prefix="deleted_lyrics_", dir=backup_root))
                backup_map = [(path, backup / str(index) / path.name)
                              for index, path in enumerate(files)]
                for path, copy in backup_map:
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, copy)
                    if path.read_bytes() != copy.read_bytes():
                        raise OSError("Backup verification failed: " + str(path))
                _write_restore_map(backup, backup_map)
                for path, copy in backup_map:
                    path.unlink()
                    removed += 1
                    deleted.append((song, path, copy))
                log(f"Deleted lyrics: {audio.name}; backup: {backup}")
            _set_persistent_track_failed(song, False)
            attempted_track_paths.discard(_track_status_key(song))
        except OSError as exc:
            errors.append(f"{audio.name}: {exc}")
    if deleted:
        _last_deleted_lyrics[:] = deleted
    refresh_tracks_from_disk(songs)
    set_status(f"Lyrics files deleted: {removed}; errors: {len(errors)}. Backups kept.")
    if errors:
        messagebox.showerror("Delete Lyrics", "\n".join(errors))


def _format_transfer_size(byte_count):
    value = float(byte_count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _timed_lrc_for_audio(audio):
    audio = Path(audio)
    for candidate in studio_resync.lrc_locations(audio):
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
            if _LRC_TIMESTAMP_RE.search(text):
                return candidate, text.rstrip()
        except OSError:
            pass
    return None, ""


def _remove_timed_embedded_lyrics(mp3_path):
    """Remove timestamped lyric frames while preserving plain lyrics and other tags."""
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            return False
        version = getattr(tags, "version", (2, 4, 0))
        v2_version = version[1] if len(version) > 1 and version[1] in (3, 4) else 4
        remove_keys = []
        for key, frame in tags.items():
            frame_id = getattr(frame, "FrameID", "")
            timed = frame_id == "SYLT" and bool(getattr(frame, "text", None))
            if frame_id == "USLT":
                timed = bool(_LRC_TIMESTAMP_RE.search(str(getattr(frame, "text", ""))))
            elif frame_id == "TXXX" and "lyric" in str(getattr(frame, "desc", "")).lower():
                timed = any(_LRC_TIMESTAMP_RE.search(str(value))
                            for value in getattr(frame, "text", []))
            if timed:
                remove_keys.append(key)
        for key in remove_keys:
            del tags[key]
        if remove_keys:
            tags.save(str(mp3_path), v2_version=v2_version)
        return bool(remove_keys)
    except (ImportError, OSError, ValueError):
        return False


def _apply_imported_sidecars_to_mp3(result):
    embedded = removed = unchanged = skipped = 0
    errors = []
    for relative in result.get("matched_track_paths", []):
        audio = Path(MUSIC_DIR).joinpath(*Path(relative).parts)
        if audio.suffix.lower() != ".mp3" or not audio.is_file():
            skipped += 1
            continue
        _lrc_path, lrc_text = _timed_lrc_for_audio(audio)
        try:
            if lrc_text:
                current = ""
                try:
                    from mutagen.id3 import ID3, ID3NoHeaderError
                    try:
                        tags = ID3(str(audio))
                        frame = tags.get(f"USLT::{_sync_id3_language(config.get('sync_language', 'en'))}")
                        current = str(getattr(frame, "text", "")).rstrip() if frame else ""
                    except ID3NoHeaderError:
                        pass
                except ImportError:
                    pass
                if current == lrc_text:
                    unchanged += 1
                else:
                    _embed_synced_lrc_as_mp3tag_unsyncedlyrics(
                        str(audio), lrc_text, config.get("sync_language", "en"))
                    embedded += 1
            elif _remove_timed_embedded_lyrics(audio):
                removed += 1
            else:
                unchanged += 1
        except Exception as exc:
            errors.append(f"{relative}: {exc}")
    _SYNCED_LYRICS_CACHE.clear()
    return embedded, removed, unchanged, skipped, errors


def start_export_lyrics_library(destination, parent=None, button=None):
    if not MUSIC_DIR or not Path(MUSIC_DIR).is_dir():
        messagebox.showwarning("Export Lyrics Library", "Choose a valid music folder first.", parent=parent)
        return
    destination_text = str(destination or "").strip()
    if not destination_text:
        messagebox.showwarning("Export Lyrics Library", "Choose an export folder first.", parent=parent)
        return
    destination = Path(destination_text)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        messagebox.showerror("Export Lyrics Library", f"Could not create the export folder:\n{exc}", parent=parent)
        return
    package = destination / ("T9_Lyrics_Library_" + datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".zip")
    if button:
        button.configure(state="disabled")
    set_status("Exporting the complete lyrics library…", "working")
    log("Lyrics Library export started: " + str(package))

    def worker():
        try:
            result = lyrics_library_transfer.export_package(MUSIC_DIR, package)
        except Exception as exc:
            ui_call(finish_error, str(exc))
        else:
            ui_call(finish_ok, result)

    def finish_error(error):
        if button and button.winfo_exists():
            button.configure(state="normal")
        set_status("Lyrics Library export failed.", "error")
        log("Lyrics Library export failed: " + error)
        messagebox.showerror("Export Lyrics Library", error, parent=parent)

    def finish_ok(result):
        if button and button.winfo_exists():
            button.configure(state="normal")
        set_status(f"Lyrics Library exported: {result['files']} files", "success")
        log("Lyrics Library exported: " + result["package"])
        messagebox.showinfo(
            "Export Lyrics Library",
            f"Package created successfully.\n\nTracks recorded: {result['tracks']}\n"
            f"TXT/LRC files: {result['files']}\nSize: {_format_transfer_size(result['bytes'])}\n\n"
            f"Saved to:\n{result['package']}", parent=parent)

    threading.Thread(target=worker, daemon=True).start()


def start_import_lyrics_library(package, parent=None, button=None):
    if not MUSIC_DIR or not Path(MUSIC_DIR).is_dir():
        messagebox.showwarning("Import Lyrics Library", "Choose a valid music folder first.", parent=parent)
        return
    if button:
        button.configure(state="disabled")
    set_status("Checking the Lyrics Library package…", "working")

    def inspect_worker():
        try:
            preview = lyrics_library_transfer.inspect_import(MUSIC_DIR, package)
        except Exception as exc:
            ui_call(finish_error, str(exc))
        else:
            ui_call(confirm_import, preview)

    def finish_error(error):
        if button and button.winfo_exists():
            button.configure(state="normal")
        set_status("Lyrics Library import failed.", "error")
        log("Lyrics Library import failed: " + error)
        messagebox.showerror("Import Lyrics Library", error, parent=parent)

    def confirm_import(preview):
        counts = preview["counts"]
        answer = messagebox.askyesnocancel(
            "Import Lyrics Library",
            "The package was verified.\n\n"
            f"Tracks matched: {preview['matched_tracks']} of {preview['package_tracks']}\n"
            f"New files: {counts['create']}\nChanged files: {counts['update']}\n"
            f"Files removed to match source: {counts['delete']}\n"
            f"Unchanged files: {preview['unchanged']}\n"
            f"Tracks missing on this computer: {len(preview['missing_tracks'])}\n\n"
            "YES — import files and update embedded timed lyrics in local MP3 files\n"
            "NO — import TXT/LRC files only\nCANCEL — do nothing\n\n"
            "Every replaced or removed TXT/LRC file will be backed up.", parent=parent)
        if answer is None:
            if button and button.winfo_exists():
                button.configure(state="normal")
            set_status("Lyrics Library import cancelled.")
            return
        set_status("Importing the Lyrics Library…", "working")

        def import_worker():
            try:
                result = lyrics_library_transfer.import_package(
                    MUSIC_DIR, package,
                    BACKUP_ROOT / "LYRICS-LIBRARY-IMPORT")
                embedding = _apply_imported_sidecars_to_mp3(result) if answer else None
            except Exception as exc:
                ui_call(finish_error, str(exc))
            else:
                ui_call(finish_ok, result, embedding)

        threading.Thread(target=import_worker, daemon=True).start()

    def finish_ok(result, embedding):
        if button and button.winfo_exists():
            button.configure(state="normal")
        refresh_tracks_from_disk()
        counts = result["counts"]
        details = (f"New files: {counts['create']}\nChanged files: {counts['update']}\n"
                   f"Removed files: {counts['delete']}\nUnchanged files: {result['unchanged']}\n"
                   f"Missing tracks skipped: {len(result['missing_tracks'])}")
        if embedding:
            embedded, removed, unchanged, skipped, errors = embedding
            details += (f"\n\nEmbedded LRC updated: {embedded}\nOld timed embedding removed: {removed}"
                        f"\nEmbedded already current: {unchanged}\nNon-MP3 skipped: {skipped}"
                        f"\nEmbedding errors: {len(errors)}")
            for error in errors:
                log("Lyrics Library embedding error: " + error)
        if result.get("backup_dir"):
            details += "\n\nBackup:\n" + result["backup_dir"]
        set_status("Lyrics Library import finished.", "success")
        log("Lyrics Library import finished: " + str(package))
        messagebox.showinfo("Import Lyrics Library", details, parent=parent)

    threading.Thread(target=inspect_worker, daemon=True).start()


def delete_shorter_references():
    if downloading:
        messagebox.showinfo("Delete shorter reference", "Wait for the current download to finish first.")
        return
    songs = [TRACK_ROW_PATHS[row] for row in track_list.curselection() if row in TRACK_ROW_PATHS]
    if not songs:
        messagebox.showinfo("Delete shorter reference", "Select marked tracks in Tracks first.")
        return
    removed, errors = 0, []
    for song in songs:
        try:
            backup = reference_queue.delete_reference(song, TRANSCRIPTION_TODO_DIR)
            if backup:
                removed += 1
                log(f"Deleted shorter reference: {Path(song).name}; backup: {backup}")
        except (OSError, ValueError) as exc:
            errors.append(f"{Path(song).name}: {exc}")
    refresh_tracks_from_disk(songs)
    set_status(f"Shorter references deleted: {removed}; skipped: {len(songs) - removed - len(errors)}; errors: {len(errors)}")
    if errors:
        messagebox.showerror("Delete shorter reference", "\n".join(errors))
    elif not removed:
        messagebox.showinfo("Delete shorter reference", "No removable references selected. Ready for Lyric Sync tracks are kept.")


def find_shorter_references():
    """Selected marked tracks, or all marked tracks in the loaded library."""
    if downloading:
        return
    rows = list(track_list.curselection())
    if rows:
        songs = [TRACK_ROW_PATHS[row] for row in rows if row in TRACK_ROW_PATHS]
    else:
        songs = []
        if MUSIC_DIR and Path(MUSIC_DIR).is_dir():
            for directory, _, files in os.walk(MUSIC_DIR):
                songs.extend(str(Path(directory) / name) for name in files
                             if Path(name).suffix.lower() in (".mp3", ".flac"))
    songs = [song for song in songs if _is_marked_for_transcription(song)]
    if not songs:
        messagebox.showinfo("Shorter references", "Select marked tracks, or clear the selection to process all marked tracks in the loaded library.")
        return
    start_shorter_references(songs)


def _local_shorter_reference(song, title, artists):
    # Restrict to the same artist's library, never another artist's same title.
    artist_root = Path(MUSIC_DIR) / infer_artist_from_path(song)
    if not artist_root.is_dir() or not Path(song).resolve().is_relative_to(artist_root.resolve()):
        artist_root = Path(song).parent
    candidates = []
    for path in artist_root.rglob("*.txt"):
        if path.parent.name.lower() != "lyrics" or reference_queue.SESSION.search(path.stem):
            continue
        actual = _strip_artist_prefix_from_title(path.stem, artists)
        score = _title_similarity(title, actual)
        if score >= 0.85 and reference_queue.reference_text(path):
            candidates.append((score, path))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    # Ambiguous local matches are left for explicit selection in the review window.
    if candidates and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.10):
        return candidates[0][1]
    return None


def start_shorter_references(songs):
    global downloading, cancel_requested, pause_requested
    if downloading or not songs:
        return
    if not require_genius_token():
        return
    downloading, cancel_requested, pause_requested = True, False, False
    cancel_btn.configure(state="normal")
    pause_btn.configure(state="disabled")

    def job():
        from studio_eta import OperationETA
        global downloading
        pending = []
        prepared = 0
        unique_songs = list(dict.fromkeys(songs))
        eta = OperationETA(len(unique_songs))
        try:
            for index, song in enumerate(unique_songs, 1):
                if should_cancel():
                    break
                if index > 1:
                    eta.advance()
                set_status(
                    f"Finding shorter reference [{index}/{len(unique_songs)}]: "
                    f"{Path(song).name} • {eta.label()}", "working"
                )
                try:
                    entry = reference_queue.read_entry(song)
                    entry["marker"] = str(reference_queue.ensure_marker(song, TRANSCRIPTION_TODO_DIR))
                    title, artist = get_track_metadata(song)
                    title = reference_queue.base_title(title or Path(song).stem)
                    artists = get_artist_candidates(song, title, artist)
                    title = _strip_artist_prefix_from_title(title, artists)
                    entry["base_title"] = title
                    reference_queue.write_entry(entry)
                    if reference_queue.is_ready_for_sync(entry):
                        log("Shorter reference already completed: " + Path(song).name)
                        continue
                    if reference_queue.ready(entry):
                        marker = reference_queue.existing_marker(song, TRANSCRIPTION_TODO_DIR)
                        if (marker is None or not marker.stem.endswith(reference_queue.FOUND_SUFFIX)
                                or os.path.normcase(str(marker)) != os.path.normcase(entry.get("reference", ""))):
                            marker = reference_queue.assign_reference(
                                entry, entry["reference"], entry.get("source", "Cached shorter reference"))
                        log("Shorter reference cache HIT: " + entry["reference"])
                        prepared += 1
                        continue
                    set_status("Finding shorter reference: " + Path(song).name, "working")
                    local = _local_shorter_reference(song, title, artists)
                    if local:
                        reference_queue.assign_reference(entry, local, "Local Lyrics: " + str(local))
                        log("Shorter reference from local Lyrics: " + str(local))
                        prepared += 1
                        continue
                    _genius_review_state.closest_rejected = None
                    result = _genius_find_verified_song(title, artists) if title else None
                    result = result or getattr(_genius_review_state, "closest_rejected", None) or {}
                    score = int(result.get("_match_score_percent", 0))
                    out = str(reference_queue.entry_path(song).with_suffix(".txt"))
                    if (result.get("url") and score >= GENIUS_REVIEW_THRESHOLD
                            and not result.get("_review_only")
                            and not reference_queue.SESSION.search(result.get("title", ""))):
                        if _save_genius_result(result, out):
                            reference_queue.assign_reference(entry, out, result["url"])
                            try:
                                Path(out).unlink()
                            except OSError:
                                pass
                            prepared += 1
                            continue
                    pending.append((entry, {"song": song, "result": result, "score": score,
                                            "out": out, "reference_title": title}))
                except Exception as exc:
                    log(f"Shorter reference error ({song}): {exc}")
            if unique_songs and not should_cancel():
                eta.advance()
            # Review after all automatic work; never change track download status.
            for index, (entry, item) in enumerate(pending, 1):
                error = ""
                while not should_cancel():
                    decision = _ask_genius_review(item, index, len(pending), error)
                    action = decision["action"]
                    if action == "stop":
                        return
                    if action == "skip":
                        break
                    try:
                        if action == "local":
                            path = decision["url"]
                            if not reference_queue.reference_text(path):
                                raise ValueError("Empty reference TXT")
                            reference_queue.assign_reference(entry, path, "User-selected TXT: " + path)
                        else:
                            result = dict(item["result"])
                            if action == "correct":
                                result["url"] = decision["url"]
                            if not _save_genius_result(result, item["out"], confirmed=True,
                                                       corrected=action == "correct"):
                                break
                            reference_queue.assign_reference(entry, item["out"], result["url"])
                            try:
                                Path(item["out"]).unlink()
                            except OSError:
                                pass
                        prepared += 1
                        break
                    except Exception as exc:
                        error = str(exc)
        finally:
            downloading = False
            log(f"Shorter references ready/reused: {prepared}. Markers and audio preserved.")
            ui_call(cancel_btn.configure, {"state": "disabled"})
            ui_call(refresh_tracks_from_disk, songs)
            set_status("Shorter reference preparation finished")

    threading.Thread(target=job, daemon=True).start()


def play_song(song_path=None, parent=None):
    """Open the exact audio file with the Windows default player."""
    parent = parent or root
    if song_path is None:
        rows = list(track_list.curselection())
        if len(rows) != 1:
            messagebox.showinfo("Play Song", "Select exactly one track in Tracks first.", parent=parent)
            return
        song_path = TRACK_ROW_PATHS.get(rows[0])
        if not song_path:
            artist = get_selected_artist_name()
            if artist:
                song_path = resolve_track_display_to_path(artist, track_list.get(rows[0]))
    if not song_path or not os.path.isfile(song_path):
        messagebox.showerror("Play Song", "The audio file could not be found.", parent=parent)
        return
    try:
        os.startfile(os.path.abspath(song_path))
    except OSError as exc:
        messagebox.showerror("Play Song", f"Could not open the default audio player:\n{exc}", parent=parent)


def _current_lyrics_source(text):
    for line in reversed(text.splitlines()):
        try:
            return _validated_genius_url(line.strip())
        except ValueError:
            continue
    return ""


def _replace_track_lyrics(song, old_path, url):
    """Commit a valid TXT first; remove only the old file selected in the editor."""
    url = _validated_genius_url(url)
    destination = missing_txt_path(song)
    if not _save_genius_result({"url": url}, destination, corrected=True):
        return ""
    if (old_path and os.path.normcase(os.path.abspath(old_path)) !=
            os.path.normcase(os.path.abspath(destination)) and os.path.isfile(old_path)):
        try:
            os.remove(old_path)
        except OSError as exc:
            raise OSError(f"New TXT saved to {destination}, but the old file could not be removed: {old_path}. {exc}") from exc
    return destination


def _clear_old_sync_after_new_lyrics(song, txt):
    """New lyrics invalidate the old sync: back up and delete LRC + embedded lyrics."""
    app_dir = Path(__file__).resolve().parent
    try:
        LYRICS_MATCH.forget(song)  # the old "lyrics might be wrong" note belongs to the old text
    except Exception:
        pass
    backup, removed, embedded = studio_resync.clear_sync(
        song, txt, app_dir / "lyrics_sync_state" / "marked.json",
        BACKUP_ROOT, remove_embedded_lyrics)
    _SYNCED_LYRICS_CACHE.pop(os.path.normcase(str(Path(song).resolve())), None)
    if backup:
        log("Old sync removed after new lyrics: "
            + ", ".join([str(p) for p in removed] + (["embedded MP3 lyrics"] if embedded else []))
            + f"\nBackup: {backup}")
    log("Song marked for Lyrics Sync: " + os.path.basename(song))
    _remember_fix("New lyrics downloaded — old sync removed", song)


def _sung_lyric_lines(text):
    """Lines Syncalong turns into LRC lines: no blanks, [Section] headers or source footer."""
    lines = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if (not stripped or re.fullmatch(r"[\[\(].*[\]\)]", stripped)
                or re.fullmatch(r"(?i)match accuracy:\s*\d{1,3}%", stripped)
                or re.match(r"(?i)^https?://", stripped)
                or re.match(r"(?i)^source .* by user$", stripped)
                or not re.search(r"\w", stripped)):
            continue
        lines.append(re.sub(r"^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*", "", stripped))
    return lines


def _apply_text_edit_to_lrc(lrc_text, new_lyrics_text):
    """Carry an edited TXT into an existing LRC, keeping every timestamp it can.

    Lines are compared in order. A changed line keeps its time. When N lines
    are replaced by M lines, the first min(N, M) new lines take the old times,
    surplus old lines are removed and surplus new lines get a time estimated
    evenly between the surrounding lines. Returns
    (new_lrc_text, changed_count, removed_count, estimated_count).
    """
    raw_lines = (lrc_text or "").splitlines(keepends=True)
    newline = "\r\n" if raw_lines and raw_lines[0].endswith("\r\n") else "\n"
    timed = []
    for index, raw in enumerate(raw_lines):
        match = _LRC_TIMED_TEXT_RE.match(raw.rstrip("\r\n"))
        if match and match.group(5).strip():
            ms = _lrc_timestamp_milliseconds(match.group(2), match.group(3), match.group(4) or "")
            timed.append((index, match, ms / 1000))
    old = [match.group(5).strip() for _index, match, _time in timed]
    new = _sung_lyric_lines(new_lyrics_text)
    replacements, removed = {}, set()
    insert_after, insert_before = {}, {}  # raw line index -> [(seconds, text)]
    changed = estimated = 0

    def stamp(seconds):
        cs = max(0, int(round(seconds * 100)))
        return f"[{cs // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}]"

    for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        keep = min(a2 - a1, b2 - b1)
        for k in range(keep):
            replacements[timed[a1 + k][0]] = new[b1 + k]
        changed += keep
        removed.update(timed[k][0] for k in range(a1 + keep, a2))
        extra = new[b1 + keep:b2]
        if not extra:
            continue
        previous = a1 + keep - 1  # last old line still in place before the new ones
        start = timed[previous][2] if previous >= 0 else 0.0
        end = timed[a2][2] if a2 < len(timed) else start + 2.0 * (len(extra) + 1)
        step = max(0.0, end - start) / (len(extra) + 1)
        items = [(start + step * (j + 1), text) for j, text in enumerate(extra)]
        if previous >= 0:
            insert_after.setdefault(timed[previous][0], []).extend(items)
        elif timed:
            insert_before.setdefault(timed[0][0], []).extend(items)
        else:
            insert_after.setdefault(len(raw_lines) - 1, []).extend(items)
        estimated += len(extra)
    result = []
    for index, raw in enumerate(raw_lines):
        for seconds, text in insert_before.get(index, []):
            result.append(f"{stamp(seconds)} {text}{newline}")
        if index not in removed:
            if index in replacements:
                content = raw.rstrip("\r\n")
                match = _LRC_TIMED_TEXT_RE.match(content)
                raw = content[:match.end(1)] + " " + replacements[index] + raw[len(content):]
            if index in insert_after and not raw.endswith("\n"):
                raw += newline
            result.append(raw)
        for seconds, text in insert_after.get(index, []):
            result.append(f"{stamp(seconds)} {text}{newline}")
    return "".join(result), changed, len(removed), estimated


def _existing_lrc_sidecars(song):
    return [path for path in studio_resync.lrc_locations(song) if path.is_file()]


def _has_embedded_lyrics(song):
    """True when the MP3 carries any lyrics frame (USLT, SYLT or a lyrics TXXX)."""
    if Path(song).suffix.lower() != ".mp3":
        return False
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            tags = ID3(song)
        except ID3NoHeaderError:
            return False
        return any(frame.FrameID in {"USLT", "SYLT"}
                   or (frame.FrameID == "TXXX" and "lyric" in frame.desc.lower())
                   for frame in tags.values())
    except Exception:
        return False


def _save_edited_lyrics(lyrics_path, text):
    """Atomically save a manual edit and retain the previous file as backup."""
    import shutil as local_shutil
    import tempfile
    import uuid
    from datetime import datetime as local_datetime
    target = Path(lyrics_path)
    if not text.strip():
        raise ValueError("The lyrics text cannot be empty.")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if target.is_file():
        backup_dir = BACKUP_ROOT / "LYRICS-EDITS"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / (
            local_datetime.now().strftime("%Y%m%d_%H%M%S_")
            + uuid.uuid4().hex[:8] + "_" + target.name
        )
        local_shutil.copy2(target, backup_path)
        backup = str(backup_path)
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                suffix=".tmp", delete=False) as stream:
            temporary_path = stream.name
            stream.write(text.rstrip() + "\n")
        os.replace(temporary_path, target)
        temporary_path = ""
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
    return str(target), backup


def open_replace_lyrics():
    global downloading, cancel_requested, pause_requested
    if downloading:
        return
    rows = list(track_list.curselection())
    if len(rows) != 1:
        messagebox.showinfo("View/Change Lyrics", "Select exactly one track in Tracks first.")
        return
    song = TRACK_ROW_PATHS.get(rows[0])
    if not song:
        artist = get_selected_artist_name()
        if artist:
            song = resolve_track_display_to_path(artist, track_list.get(rows[0]))
    if not song or not os.path.isfile(song):
        messagebox.showerror("View/Change Lyrics", "The selected audio file could not be found. Refresh Tracks.")
        return
    open_replace_lyrics_for_song(song)


def open_replace_lyrics_for_song(song, parent=None):
    """Open the View/Change Lyrics window for one audio file; returns it (None if not opened)."""
    global downloading, cancel_requested, pause_requested
    parent = parent or root
    if downloading:
        messagebox.showinfo("View/Change Lyrics", "Finish the current download first.", parent=parent)
        return
    preferred = missing_txt_path(song)
    old_path = preferred if os.path.isfile(preferred) else find_existing_lyrics(song)
    try:
        current_text = Path(old_path).read_text(encoding="utf-8-sig", errors="replace") if old_path else ""
    except OSError as exc:
        messagebox.showerror("View/Change Lyrics", f"Cannot read the current lyrics: {exc}", parent=parent)
        return
    source = _current_lyrics_source(current_text)
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(parent)
    win.title("View/Change Lyrics — " + os.path.basename(song))
    win.configure(bg=t["bg"])
    win.transient(parent)
    busy = {"value": False}
    editing = {"enabled": False, "dirty": False}

    def label(text):
        tk.Label(win, text=text, bg=t["bg"], fg=t["fg"], wraplength=700,
                 justify="left").pack(anchor="w", padx=14, pady=4)

    label("Audio: " + os.path.basename(song))
    tk.Button(win, text="Play Song", command=lambda: play_song(song, win)).pack(anchor="w", padx=14, pady=4)
    current_file_label = tk.Label(
        win, text="Current lyrics: " + (old_path or "No lyrics file yet"),
        bg=t["bg"], fg=t["fg"], wraplength=700, justify="left"
    )
    current_file_label.pack(anchor="w", padx=14, pady=4)
    label("Current Genius source:")
    source_entry = tk.Entry(win)
    source_entry.insert(0, source or "No source link recorded in this file")
    source_entry.configure(state="readonly")
    source_entry.pack(fill="x", padx=14, pady=4)
    source_buttons = tk.Frame(win, bg=t["bg"])
    source_buttons.pack(fill="x", padx=14)

    def copy_source():
        win.clipboard_clear()
        win.clipboard_append(source)

    copy_source_btn = tk.Button(source_buttons, text="Copy current link", command=copy_source,
                                state="normal" if source else "disabled")
    copy_source_btn.pack(side="left", padx=(0, 8))
    open_source_btn = tk.Button(source_buttons, text="Open current source", command=lambda: webbrowser.open(source),
                                state="normal" if source else "disabled")
    open_source_btn.pack(side="left")

    def find_different_lyrics():
        global downloading, cancel_requested, pause_requested
        if busy["value"] or downloading:
            return
        dialog = create_toplevel(win)
        dialog.title("Find Different Lyrics — " + os.path.basename(song))
        dialog.configure(bg=t["bg"])
        dialog.transient(win)
        state = {"candidates": [], "texts": {}, "busy": False, "closed": False}
        info = tk.Label(dialog, text="Searching Genius for other versions…", bg=t["bg"], fg=t["fg"],
                        wraplength=820, justify="left", anchor="w")
        info.pack(fill="x", padx=14, pady=(12, 4))
        choices = tk.Listbox(dialog, height=6, exportselection=False, bg=t["panel"], fg=t["fg"],
                             font=("Consolas", 10))
        choices.pack(fill="x", padx=14, pady=4)
        tk.Label(dialog, text="Lyrics preview of the selected candidate:", bg=t["bg"], fg=t["fg"],
                 anchor="w").pack(fill="x", padx=14, pady=(6, 0))
        candidate_frame = tk.Frame(dialog)
        candidate_frame.pack(fill="both", expand=True, padx=14, pady=4)
        candidate_text = tk.Text(candidate_frame, height=14, wrap="word", bg=t["panel"], fg=t["fg"])
        candidate_scroll = tk.Scrollbar(candidate_frame, command=candidate_text.yview)
        candidate_text.configure(yscrollcommand=candidate_scroll.set, state="disabled")
        candidate_scroll.pack(side="right", fill="y")
        candidate_text.pack(side="left", fill="both", expand=True)
        dialog_buttons = tk.Frame(dialog, bg=t["bg"])
        dialog_buttons.pack(fill="x", padx=14, pady=12)

        def set_busy(value):
            global downloading, cancel_requested, pause_requested
            state["busy"] = value
            downloading = value
            if value:
                cancel_requested = False
                pause_requested = False
            if not state["closed"] and dialog.winfo_exists():
                use_btn.configure(state="disabled" if value or not choices.curselection() else "normal")

        def show_preview(text):
            candidate_text.configure(state="normal")
            candidate_text.delete("1.0", "end")
            candidate_text.insert("1.0", text)
            candidate_text.configure(state="disabled")

        def selected_candidate():
            selection = choices.curselection()
            return state["candidates"][selection[0]] if selection else None

        def on_select(_event=None):
            item = selected_candidate()
            if not item:
                return
            use_btn.configure(state="disabled" if state["busy"] else "normal")
            open_btn.configure(state="normal")
            if item["url"] in state["texts"]:
                show_preview(state["texts"][item["url"]])
                return
            if state["busy"]:
                return
            show_preview("Loading lyrics from Genius…")
            set_busy(True)

            def worker(url=item["url"]):
                try:
                    lyrics, error = _fetch_genius_lyrics(url), ""
                except Exception as exc:
                    lyrics, error = "", str(exc)
                ui_call(loaded, url, lyrics, error)

            threading.Thread(target=worker, daemon=True).start()

        def loaded(url, lyrics, error):
            set_busy(False)
            if state["closed"] or not dialog.winfo_exists():
                return
            if lyrics:
                similarity = _lyrics_body_similarity(current_text, lyrics)
                state["texts"][url] = (f"Similarity to your current TXT: {similarity:.0%}\n"
                                       f"{url}\n\n{lyrics}")
            current = selected_candidate()
            if current and current["url"] == url:
                show_preview(state["texts"].get(url, f"Could not load lyrics: {error}"))
            elif current:
                on_select()

        def found(candidates, notes, error):
            set_busy(False)
            if state["closed"] or not dialog.winfo_exists():
                return
            state["candidates"] = candidates
            for item in candidates:
                choices.insert("end", f"{item['score_percent']:>3}%  {'' if item['artist_match'] else '[other artist] '}"
                                      f"{item['artist']} — {item['title']}")
            if error:
                info.configure(text="Genius search failed: " + error)
            elif not candidates:
                info.configure(text="No other Genius versions were found for this song."
                                    + ("\n" + "\n".join(notes) if notes else ""))
            else:
                info.configure(text=(f"Found {len(candidates)} other Genius version(s), best title/artist match first. "
                                     "Select one to preview it and compare it with your current TXT. "
                                     "The current source is excluded."))
                choices.selection_set(0)
                on_select()

        def search_worker():
            candidates, notes, error = [], [], ""
            try:
                candidates, notes = _genius_alternative_candidates(song, exclude_url=source)
            except Exception as exc:
                error = str(exc)
            ui_call(found, candidates, notes, error)

        def use_selected():
            item = selected_candidate()
            if not item or state["busy"]:
                return
            if not messagebox.askyesno(
                    "Replace lyrics",
                    f"Download and replace the current TXT with:\n\n{item['artist']} — {item['title']}\n{item['url']}\n\n"
                    "The old LRC and embedded MP3 lyrics will be backed up and deleted, "
                    "and the song will be marked for Lyrics Sync.",
                    parent=dialog):
                return
            close_dialog()
            entry.delete(0, tk.END)
            entry.insert(0, item["url"])
            submit()

        def open_on_genius():
            item = selected_candidate()
            if item:
                webbrowser.open(item["url"])

        def close_dialog():
            # Closing mid-request is allowed; the worker callback clears the busy flag.
            state["closed"] = True
            dialog.destroy()
            try:
                win.grab_set()
            except tk.TclError:
                pass

        use_btn = tk.Button(dialog_buttons, text="Use selected — download and replace",
                            command=use_selected, state="disabled")
        use_btn.pack(side="left")
        open_btn = tk.Button(dialog_buttons, text="Open on Genius", command=open_on_genius, state="disabled")
        open_btn.pack(side="left", padx=(8, 0))
        tk.Button(dialog_buttons, text="Play Song",
                  command=lambda: play_song(song, dialog)).pack(side="left", padx=(8, 0))
        tk.Button(dialog_buttons, text="Close", command=close_dialog).pack(side="right")
        choices.bind("<<ListboxSelect>>", on_select)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        popup_over_root(dialog, 860, 640)
        dialog.grab_set()
        set_busy(True)
        threading.Thread(target=search_worker, daemon=True).start()

    tk.Button(source_buttons, text="Find different lyrics…",
              command=find_different_lyrics).pack(side="left", padx=(8, 0))
    previews = tk.Frame(win, bg=t["bg"])
    previews.pack(fill="both", expand=True, padx=14, pady=4)
    previews.grid_columnconfigure(0, weight=1, uniform="preview")
    previews.grid_columnconfigure(1, weight=1, uniform="preview")
    previews.grid_rowconfigure(1, weight=1)
    tk.Label(previews, text="Current lyrics preview (TXT):", bg=t["bg"], fg=t["fg"],
             anchor="w").grid(row=0, column=0, sticky="w")
    lrc_title = tk.Label(previews, text="LRC preview (read-only):", bg=t["bg"], fg=t["fg"], anchor="w")
    lrc_title.grid(row=0, column=1, sticky="w", padx=(10, 0))
    preview_frame = tk.Frame(previews)
    preview_frame.grid(row=1, column=0, sticky="nsew")
    preview = tk.Text(preview_frame, height=10, wrap="word", bg=t["panel"], fg=t["fg"])
    scroll = tk.Scrollbar(preview_frame, command=preview.yview)
    preview.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    preview.pack(side="left", fill="both", expand=True)
    preview.insert("1.0", current_text)
    preview.configure(state="disabled")
    preview.edit_modified(False)
    lrc_frame = tk.Frame(previews)
    lrc_frame.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
    lrc_preview = tk.Text(lrc_frame, height=10, wrap="none", bg=t["panel"], fg=t["fg"], font=("Consolas", 9))
    lrc_scroll = tk.Scrollbar(lrc_frame, command=lrc_preview.yview)
    lrc_preview.configure(yscrollcommand=lrc_scroll.set)
    lrc_scroll.pack(side="right", fill="y")
    lrc_preview.pack(side="left", fill="both", expand=True)
    lrc_preview.tag_configure("changed", background="#6b5b00", foreground="#ffffff")
    lrc_preview.tag_configure("removed", background="#6b1f1f", foreground="#ffb3b3", overstrike=True)

    def render_lrc(old_text, new_text):
        """Show ``new_text``; lines changed vs ``old_text`` are yellow, removed ones red and struck out."""
        lrc_preview.configure(state="normal")
        lrc_preview.delete("1.0", "end")
        old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
        first = None
        for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(
                None, old_lines, new_lines, autojunk=False).get_opcodes():
            if tag == "equal":
                for line in new_lines[b1:b2]:
                    lrc_preview.insert("end", line + "\n")
                continue
            first = first or lrc_preview.index("end-1c")
            for line in old_lines[a1:a2] if tag in ("delete", "replace") else []:
                if tag == "delete" or len(old_lines[a1:a2]) != len(new_lines[b1:b2]):
                    lrc_preview.insert("end", line + "\n", "removed")
            for line in new_lines[b1:b2]:
                lrc_preview.insert("end", line + "\n", "changed")
        if first:
            lrc_preview.see(first)
        lrc_preview.configure(state="disabled")

    def load_lrc_from_disk():
        sidecars = _existing_lrc_sidecars(song)
        if not sidecars:
            return None, ""
        try:
            return sidecars[0], sidecars[0].read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return sidecars[0], f"Cannot read the LRC: {exc}"

    def lrc_title_text(path, extra=""):
        if path is None:
            return "LRC preview: no LRC for this song"
        more = len(_existing_lrc_sidecars(song)) - 1
        return (f"LRC preview: {path.parent.name}\\{path.name}"
                + (f"  (+{more} more LRC)" if more > 0 else "") + (f" — {extra}" if extra else ""))

    def refresh_lrc_preview(previous_text=None):
        """Reload the LRC from disk; after a save, highlight what changed vs ``previous_text``."""
        path, text = load_lrc_from_disk()
        lrc_title.configure(text=lrc_title_text(path, "saved" if previous_text is not None else ""))
        render_lrc(previous_text if previous_text is not None else text, text)
        return text

    lrc_state = {"text": refresh_lrc_preview(), "pending": None}

    def live_lrc_preview():
        """While editing, show how the LRC would look after saving (nothing is written)."""
        lrc_state["pending"] = None
        if not win.winfo_exists():
            return
        path = (_existing_lrc_sidecars(song) or [None])[0]
        if path is None or not editing["enabled"]:
            return
        new_lrc, changed, removed, estimated = _apply_text_edit_to_lrc(
            lrc_state["text"], preview.get("1.0", "end-1c"))
        summary = f"{changed} changed, {removed} removed" + (
            f", {estimated} new with estimated time" if estimated else "")
        lrc_title.configure(text=lrc_title_text(
            path, f"live: {summary} (not saved yet)" if changed or removed or estimated
            else "live: no changes"))
        render_lrc(lrc_state["text"], new_lrc)
    edit_controls = tk.Frame(win, bg=t["bg"])
    edit_controls.pack(fill="x", padx=14, pady=(2, 8))

    def on_preview_modified(_event=None):
        if editing["enabled"] and preview.edit_modified():
            editing["dirty"] = True
            # Debounce: recompute the live LRC preview 250 ms after the last keystroke.
            if lrc_state["pending"]:
                win.after_cancel(lrc_state["pending"])
            lrc_state["pending"] = win.after(250, live_lrc_preview)
        preview.edit_modified(False)

    preview.bind("<<Modified>>", on_preview_modified)

    def enable_text_editing():
        editing["enabled"] = True
        editing["dirty"] = False
        preview.configure(state="normal")
        preview.focus_set()
        edit_text_btn.configure(state="disabled")
        save_text_btn.configure(state="normal")
        save_both_btn.configure(state="normal")
        submit_btn.configure(state="disabled")
        status.configure(text="Editing enabled. Change the text, then click Save Text.")

    def load_text_file():
        if busy["value"]:
            return
        if editing["enabled"] and editing["dirty"]:
            if not messagebox.askyesno(
                    "Discard unsaved changes?",
                    "Loading a TXT file will replace the unsaved text currently in the editor. Continue?",
                    parent=win):
                return
        path = filedialog.askopenfilename(
            parent=win,
            title="Load lyrics from TXT",
            initialdir=os.path.dirname(song),
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            try:
                imported = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    imported = raw.decode("utf-16")
                except UnicodeDecodeError:
                    imported = raw.decode("cp1252", errors="replace")
        except OSError as exc:
            messagebox.showerror("Load TXT", f"Cannot read the selected TXT file: {exc}", parent=win)
            return
        editing.update(enabled=True, dirty=True)
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", imported)
        preview.edit_modified(False)
        preview.focus_set()
        edit_text_btn.configure(state="disabled")
        save_text_btn.configure(state="normal")
        save_both_btn.configure(state="normal")
        submit_btn.configure(state="disabled")
        status.configure(
            text="Loaded from TXT: " + path + "\nReview it, then click Save Text to add/replace the song lyrics."
        )

    def update_source_display():
        state = "normal" if source else "disabled"
        source_entry.configure(state="normal")
        source_entry.delete(0, tk.END)
        source_entry.insert(0, source or "No source link recorded in this file")
        source_entry.configure(state="readonly")
        copy_source_btn.configure(state=state)
        open_source_btn.configure(state=state)

    def save_text_edit():
        nonlocal old_path, current_text, source
        if not editing["enabled"]:
            return None
        text = preview.get("1.0", "end-1c")
        target = old_path or preferred
        try:
            saved_path, backup = _save_edited_lyrics(target, text)
        except Exception as exc:
            status.configure(text=f"Could not save text: {exc}")
            return None
        old_path = saved_path
        current_text = Path(saved_path).read_text(encoding="utf-8-sig", errors="replace")
        source = _current_lyrics_source(current_text)
        editing.update(enabled=False, dirty=False)
        preview.configure(state="disabled")
        preview.edit_modified(False)
        edit_text_btn.configure(state="normal")
        save_text_btn.configure(state="disabled")
        save_both_btn.configure(state="disabled")
        submit_btn.configure(state="normal")
        current_file_label.configure(text="Current lyrics: " + saved_path)
        update_source_display()
        lyrics_cache.clear()
        attempted_track_paths.add(_track_status_key(song))
        _set_persistent_track_failed(song, False)
        refresh_tracks_from_disk([song])
        status.configure(text="Text saved." + (f" Backup: {backup}" if backup else ""))
        log(f"Lyrics text edited manually: {saved_path}" + (f"\nBackup: {backup}" if backup else ""))
        return saved_path

    has_embedded = _has_embedded_lyrics(song)

    def save_text_and_lrc():
        """Save the TXT, carry the edit into the LRC(s) keeping timings, re-embed the new LRC."""
        import tempfile
        if not editing["enabled"]:
            return
        text = preview.get("1.0", "end-1c")
        lrc_paths = _existing_lrc_sidecars(song)
        updates = []
        try:
            for lrc in lrc_paths:
                lrc_text = lrc.read_text(encoding="utf-8-sig")
                updates.append((lrc, *_apply_text_edit_to_lrc(lrc_text, text)))
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("Save TXT and LRC", f"Cannot read the LRC: {exc}", parent=win)
            return
        saved_path = save_text_edit()
        if not saved_path:
            return
        notes = []
        backup_root = BACKUP_ROOT
        try:
            for lrc, new_lrc, changed, removed, estimated in updates:
                if changed or removed or estimated:
                    studio_resync.save_fragment_repair(lrc, new_lrc, backup_root)
                notes.append(f"{lrc.name}: {changed} line(s) changed, {removed} removed, timings kept"
                             + (f", {estimated} new line(s) got an estimated time — check them with "
                                "Play Song or Fix fragment" if estimated else ""))
            if has_embedded and updates:
                backup = Path(tempfile.mkdtemp(prefix="edit_embedded_", dir=backup_root))
                shutil.copy2(song, backup / Path(song).name)
                _write_restore_map(backup, [(song, backup / Path(song).name)])
                # Drop every old lyrics frame (USLT/SYLT/TXXX), then embed the edited LRC.
                remove_embedded_lyrics(song)
                _embed_synced_lrc_as_mp3tag_unsyncedlyrics(song, updates[0][1], config.get("sync_language", "en"))
                notes.append(f"embedded MP3 lyrics updated to the new LRC (MP3 backup: {backup})")
            elif has_embedded:
                notes.append("no LRC, so the embedded MP3 lyrics were left unchanged")
        except Exception as exc:
            status.configure(text=f"TXT saved, but updating LRC/embedded lyrics failed: {exc}")
            return
        if not lrc_paths:
            notes.append("no LRC found — only the TXT was changed")
        _SYNCED_LYRICS_CACHE.clear()
        refresh_tracks_from_disk([song])
        lrc_state["text"] = refresh_lrc_preview(lrc_state["text"])
        status.configure(text="TXT saved. " + "; ".join(notes)
                         + (" Changed LRC lines are highlighted." if any(u[2] or u[4] for u in updates) else ""))
        log("Lyrics text + LRC edited manually: " + saved_path + "\n   " + "\n   ".join(notes))
        _remember_fix("Lyrics text edited (TXT + LRC)", lrc_paths[0] if lrc_paths else song, "; ".join(notes))

    edit_text_btn = tk.Button(edit_controls, text="Turn on editing", command=enable_text_editing)
    edit_text_btn.pack(side="left")
    load_text_btn = tk.Button(edit_controls, text="Load from TXT…", command=load_text_file)
    load_text_btn.pack(side="left", padx=(8, 0))
    save_text_btn = tk.Button(edit_controls, text="Save Text", command=save_text_edit, state="disabled")
    save_text_btn.pack(side="left", padx=(8, 0))
    save_both_btn = tk.Button(
        edit_controls,
        text="Save and Change Lyrics and Synced Lyrics" + (" + Update Embedded" if has_embedded else ""),
        command=save_text_and_lrc, state="disabled")
    save_both_btn.pack(side="left", padx=(8, 0))
    label("New Genius lyrics link:")
    entry = tk.Entry(win, font=("Segoe UI", 10))
    entry.pack(fill="x", padx=14, pady=4)
    label("Download and replace saves the new TXT in Lyrics. The current file is replaced only after a successful download. "
          "The old LRC and embedded MP3 lyrics are then backed up and deleted, and the song is marked for Lyrics Sync.")
    status = tk.Label(win, text="", bg=t["bg"], fg=t["fg"], wraplength=700, justify="left")
    status.pack(fill="x", padx=14, pady=4)
    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=12)

    def close():
        if busy["value"]:
            return
        if editing["enabled"] and editing["dirty"]:
            if not messagebox.askyesno("Discard changes?", "Close without saving your text changes?", parent=win):
                return
        win.destroy()

    def submit():
        global downloading, cancel_requested, pause_requested
        if busy["value"] or downloading:
            return
        try:
            url = _validated_genius_url(entry.get())
        except ValueError as exc:
            messagebox.showerror("Invalid Genius link", str(exc), parent=win)
            return
        downloading = True
        cancel_requested = False
        pause_requested = False
        busy["value"] = True
        submit_btn.configure(state="disabled")
        sync_now_btn.configure(state="disabled")
        builder_btn.configure(state="disabled")
        close_btn.configure(state="disabled")
        entry.configure(state="disabled")
        status.configure(text="Downloading replacement…")
        for button in (artist_dl_btn, track_dl_btn, open_btn, edit_lyrics_btn, missing_scan_btn):
            button.configure(state="disabled")

        def finish(destination, error):
            global downloading
            downloading = False
            busy["value"] = False
            for button in (artist_dl_btn, track_dl_btn, open_btn, edit_lyrics_btn, missing_scan_btn):
                button.configure(state="normal")
            submit_btn.configure(state="normal")
            sync_now_btn.configure(state="normal")
            builder_btn.configure(state="normal")
            close_btn.configure(state="normal")
            entry.configure(state="normal")
            lyrics_cache.clear()
            if destination:
                attempted_track_paths.add(_track_status_key(song))
                _set_persistent_track_failed(song, False)
                scanned_artists.add(infer_artist_from_path(song))
                win.destroy()
                refresh_tracks_from_disk([song])
                set_status("Lyrics replaced: " + os.path.basename(destination), "success")
                log("Lyrics replaced manually: " + destination)
            else:
                status.configure(text=error or "Download cancelled. The old lyrics were kept.")

        def worker():
            destination, error = "", ""
            try:
                destination = _replace_track_lyrics(song, old_path, url)
                if destination:
                    try:
                        _clear_old_sync_after_new_lyrics(song, destination)
                    except Exception as clear_exc:
                        log("⚠ New TXT saved, but the old LRC/embedded lyrics could not be removed: "
                            f"{clear_exc}")
            except Exception as exc:
                error = str(exc)
                log("Lyrics replacement failed: " + error)
            finally:
                ui_call(finish, destination, error)

        threading.Thread(target=worker, daemon=True).start()

    def lyric_sync_now():
        """Close this window and load this song (with its saved TXT) into Lyrics Sync."""
        if busy["value"] or downloading:
            return
        if not _find_plain_txt_for_audio(song):
            messagebox.showinfo("Lyric Sync Now", "This song has no TXT lyrics yet — download or save "
                                "the lyrics text first.", parent=win)
            return
        if editing["enabled"] and editing["dirty"] and not messagebox.askyesno(
                "Lyric Sync Now", "Your text changes are not saved. Load the saved TXT into Lyrics Sync and discard them?",
                parent=win):
            return
        win.destroy()
        root.after(50, lambda: open_syncalong_window(sync_song=song))

    submit_btn = tk.Button(buttons, text="Download and replace", command=submit)
    submit_btn.pack(side="left")
    sync_now_btn = tk.Button(buttons, text="🎤 Lyric Sync Now", command=lyric_sync_now,
                             font=("Segoe UI", 10, "bold"))
    sync_now_btn.pack(side="left", padx=(12, 0))

    def open_builder_with_song():
        """The song + its current TXT (as the shorter reference) in Lyric Builder."""
        if busy["value"] or downloading:
            return
        txt = _find_plain_txt_for_audio(song)
        if not txt:
            messagebox.showinfo("Open in Lyric Builder", "This song has no TXT lyrics yet — download or save "
                                "the lyrics text first.", parent=win)
            return
        if editing["enabled"] and editing["dirty"] and not messagebox.askyesno(
                "Open in Lyric Builder", "Your text changes are not saved. Use the saved TXT as the shorter "
                "reference anyway?", parent=win):
            return
        open_in_lyric_builder(song, txt, parent=win)

    builder_btn = tk.Button(buttons, text="🧩 Open in Lyric Builder", command=open_builder_with_song,
                            font=("Segoe UI", 10, "bold"))
    builder_btn.pack(side="left", padx=(8, 0))
    close_btn = tk.Button(buttons, text="Close", command=close)
    close_btn.pack(side="right")
    win.protocol("WM_DELETE_WINDOW", close)
    popup_over_root(win, 1100, 720)
    win.grab_set()
    entry.focus_set()
    return win



# ------------------ Metadata tab and song duplicates ------------------

def _library_scope(use_selected_tracks=False):
    """(song files, description) for the current selection: the selected tracks (when asked), else the
    selected albums of the selected artist, else the selected artist folders, else the whole library."""
    if use_selected_tracks:
        rows = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
        if rows:
            return rows, f"{len(rows)} selected track(s)"
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return [], ""
    artists = [strip_icon(artist_list.get(i)) for i in artist_list.curselection()]
    if not artists:
        files = [path for path, _rel, _file in _audio_files_under_artist(MUSIC_DIR)]
        return files, "the whole library"
    albums = set(_selected_album_dirs()) if len(artists) == 1 else set()
    files = []
    for artist in artists:
        for path, rel_dir, _file in _audio_files_under_artist(_artist_folder(artist)):
            if not albums or rel_dir in albums:
                files.append(path)
    where = (f"{len(albums)} selected album folder(s) of {artists[0]}" if albums
             else ", ".join(artists))
    return files, where


def open_song_duplicates():
    """Repair library → Search for song duplicates: songs that look like the same recording."""
    files, where = _library_scope()
    if not files:
        messagebox.showinfo("Song duplicates", "Open your music folder first.", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Song duplicates")
    win.configure(bg=t["bg"])
    win.minsize(820, 420)
    popup_over_root(win, 1180, 640)
    tk.Label(win, text=(f"Searching {len(files)} song(s) in {where}. Album marks like (JW3) (WOD) and featured "
                        "artists are ignored; version marks like (Sessions), (V2), (OG), (Stem) make a different "
                        "song, so they are never listed here. Nothing is changed or deleted."),
             bg=t["bg"], fg=t["fg"], wraplength=1120, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    summary_var = tk.StringVar(value="Searching…")
    tk.Label(win, textvariable=summary_var, bg=t["bg"], fg="#ffb000", anchor="w",
             font=("Segoe UI", 10, "bold")).pack(fill="x", padx=14)
    frame = tk.Frame(win, bg=t["bg"])
    frame.pack(fill="both", expand=True, padx=14, pady=6)
    box = tk.Listbox(frame, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], activestyle="none",
                     selectbackground=t.get("accent", "#355a7a"), exportselection=False)
    scroll_y = tk.Scrollbar(frame, command=box.yview)
    scroll_x = tk.Scrollbar(frame, orient="horizontal", command=box.xview)
    box.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    box.pack(side="left", fill="both", expand=True)
    row_paths = {}

    def fill():
        box.delete(0, "end")
        row_paths.clear()
        groups = studio_metadata.group_duplicates(files)
        same = sum(1 for kind, _ in groups if kind == "same")
        summary_var.set(f"{same} group(s) with the same song name • {len(groups) - same} possible duplicate "
                        "group(s) — only an alternative name in brackets differs, so check them by ear."
                        if groups else "No duplicates found.")
        for kind, paths in groups:
            title = ("SAME NAME" if kind == "same" else "POSSIBLE (different alternative name)")
            box.insert("end", f"■ {title}: {paths[0].name}")
            box.itemconfig("end", fg="#ff7070" if kind == "same" else "#ffcf70")
            for path in paths:
                length = studio_metadata.song_length(path)
                minutes = f"{int(length // 60)}:{int(length % 60):02d}" if length else "?:??"
                try:
                    size = f"{path.stat().st_size / 1e6:5.1f} MB"
                except OSError:
                    size = "   ? MB"
                box.insert("end", f"     {minutes}  {size}   {path}")
                row_paths[box.size() - 1] = path
            box.insert("end", "")

    def chosen():
        rows = box.curselection()
        return row_paths.get(rows[0]) if rows else None

    def open_folder():
        path = chosen()
        if path is None:
            messagebox.showinfo("Song duplicates", "Select a file line first.", parent=win)
            return
        subprocess.Popen(["explorer", "/select,", str(path)])

    def play():
        path = chosen()
        if path is None:
            messagebox.showinfo("Song duplicates", "Select a file line first.", parent=win)
            return
        play_song(str(path), win)

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(0, 12))
    for text, command in (("Open file location", open_folder), ("Play selected file", play), ("Search again", fill)):
        tk.Button(buttons, text=text, command=command, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left", padx=(0, 8))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    box.bind("<Double-Button-1>", lambda _e: play())
    win.after(50, fill)


def open_bracket_lines_check():
    """Repair library → LRC missing bracket lines: older syncs dropped TXT lines that start with "(" and end
    with ")" — e.g. "(Yeah) Get right witcha … (Rah, woo)" — as if they were section headers."""
    files, where = _library_scope(use_selected_tracks=True)
    if not files:
        messagebox.showinfo("LRC missing bracket lines", "Open your music folder first.", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("LRC missing bracket lines")
    win.configure(bg=t["bg"])
    win.minsize(820, 420)
    popup_over_root(win, 1180, 640)
    tk.Label(win, text=(f"Checking {len(files)} song(s) in {where}. Older syncs left out TXT lines that start with “(” and "
                        "end with “)” but have sung text in between — e.g. “(Yeah) Bad bitches, fuck 'em then dismiss 'em "
                        "(Bad, woo)” — because they looked like section headers. Lines made only of brackets, like "
                        "“(Cha-ching, brrt, hello?)”, stay out on purpose. Songs whose LRC lacks such lines are listed (all selected). "
                        "“Mark selected for Lyrics Sync” backs up and deletes their LRC + embedded lyrics (the TXT is "
                        "kept); sync them again from Lyrics Sync → Marked for Lyrics Sync."),
             bg=t["bg"], fg=t["fg"], wraplength=1120, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    summary_var = tk.StringVar(value="Checking the LRC files…")
    tk.Label(win, textvariable=summary_var, bg=t["bg"], fg="#ffb000", anchor="w",
             font=("Segoe UI", 10, "bold")).pack(fill="x", padx=14)
    frame = tk.Frame(win, bg=t["bg"])
    frame.pack(fill="both", expand=True, padx=14, pady=6)
    box = tk.Listbox(frame, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], selectmode="extended",
                     selectbackground=t.get("accent", "#355a7a"), exportselection=False)
    scroll_y = tk.Scrollbar(frame, command=box.yview)
    scroll_x = tk.Scrollbar(frame, orient="horizontal", command=box.xview)
    box.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    box.pack(side="left", fill="both", expand=True)
    state = {"rows": [], "busy": False}

    def check():
        state["busy"] = True
        summary_var.set("Checking the LRC files…")
        box.delete(0, "end")

        def worker():
            rows = []
            for audio in files:
                try:
                    lrc = next((p for p in studio_resync.lrc_locations(Path(audio)) if p.is_file()), None)
                    txt = _find_plain_txt_for_audio(str(audio)) if lrc else ""
                    if not txt or not Path(txt).is_file():
                        continue
                    missing = studio_resync.missing_bracket_lines(
                        Path(txt).read_text(encoding="utf-8-sig", errors="replace"),
                        lrc.read_text(encoding="utf-8-sig", errors="replace"))
                except (OSError, ValueError):
                    continue
                if missing:
                    rows.append((Path(audio), lrc, missing))
            rows.sort(key=lambda row: (-len(row[2]), row[0].name.casefold()))
            ui_call(show, rows)

        threading.Thread(target=worker, daemon=True).start()

    def show(rows):
        state["busy"], state["rows"] = False, rows
        if not win.winfo_exists():
            return
        box.delete(0, "end")
        for audio, _lrc, missing in rows:
            box.insert("end", f"{len(missing):>3} missing   {audio.name:<60}   e.g. {missing[0]}")
        box.selection_set(0, "end")
        summary_var.set(f"{len(rows)} song(s) miss lines from their TXT (all selected)." if rows else
                        f"Every LRC in {where} has all the bracket lines of its TXT.")

    def mark_selected():
        chosen = [state["rows"][i] for i in box.curselection()]
        if state["busy"] or not chosen:
            if not chosen and not state["busy"]:
                messagebox.showinfo("LRC missing bracket lines", "Select the songs first.", parent=win)
            return
        if not messagebox.askyesno(
                "LRC missing bracket lines",
                f"Mark {len(chosen)} song(s) for Lyrics Sync?\n\nTheir LRC and embedded MP3 lyrics are backed up and "
                "deleted; the plain TXT lyrics are kept.", parent=win):
            return
        state["busy"] = True
        summary_var.set("Marking for Lyrics Sync…")
        queue_path = Path(__file__).resolve().parent / "lyrics_sync_state" / "marked.json"

        def worker():
            errors = []
            for audio, lrc, _missing in chosen:
                try:
                    studio_resync.mark(lrc, queue_path, BACKUP_ROOT, _find_plain_txt_for_audio, remove_embedded_lyrics)
                except Exception as exc:
                    errors.append(f"{audio.name}: {exc}")
            ui_call(marked, len(chosen) - len(errors), errors)

        threading.Thread(target=worker, daemon=True).start()

    def marked(done, errors):
        state["busy"] = False
        _SYNCED_LYRICS_CACHE.clear()
        refresh_tracks_from_disk()
        log(f"LRC missing bracket lines: {done} song(s) marked for Lyrics Sync")
        messagebox.showinfo("LRC missing bracket lines",
                            f"Marked for Lyrics Sync: {done}. Open Lyrics Sync → Marked for Lyrics Sync to sync them."
                            + ("\n\nFailed:\n" + "\n".join(errors[:8]) if errors else ""), parent=win)
        if win.winfo_exists():
            check()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(0, 12))
    tk.Button(buttons, text="Mark selected for Lyrics Sync", command=mark_selected, bg=t["btn_bg"], fg=t["btn_fg"],
              font=("Segoe UI", 10, "bold")).pack(side="left")
    tk.Button(buttons, text="Open Lyrics Sync", command=lambda: open_syncalong_window(),
              bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Check again", command=check, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    check()


def open_year_from_comment():
    """Metadata → Set Year from comment: "(Recorded July 15, 2019)" in the comment → Year 2019."""
    files, where = _library_scope(use_selected_tracks=True)
    if not files:
        messagebox.showinfo("Year from comment", "Open your music folder first.", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Year from comment")
    win.configure(bg=t["bg"])
    win.minsize(820, 420)
    popup_over_root(win, 1180, 640)
    tk.Label(win, text=(f"Checking {len(files)} song(s) in {where}. When the MP3 comment says when the song was "
                        "recorded — e.g. “(Recorded July 15, 2019)” — the Year tag is set to that year (2019). "
                        "Leaked / released dates are never used, and “(Recorded ?)” without a year changes nothing. "
                        "Only songs whose Year would change are listed; the old years are saved, so Undo puts them back."),
             bg=t["bg"], fg=t["fg"], wraplength=1120, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    summary_var = tk.StringVar(value="Reading the comments…")
    tk.Label(win, textvariable=summary_var, bg=t["bg"], fg="#ffb000", anchor="w",
             font=("Segoe UI", 10, "bold")).pack(fill="x", padx=14)
    frame = tk.Frame(win, bg=t["bg"])
    frame.pack(fill="both", expand=True, padx=14, pady=6)
    box = tk.Listbox(frame, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], selectmode="extended",
                     selectbackground=t.get("accent", "#355a7a"), exportselection=False)
    scroll_y = tk.Scrollbar(frame, command=box.yview)
    scroll_x = tk.Scrollbar(frame, orient="horizontal", command=box.xview)
    box.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    box.pack(side="left", fill="both", expand=True)
    state = {"plan": [], "busy": False}
    undo_dir = BACKUP_ROOT / "YEAR-FROM-COMMENT"

    def load():
        state["busy"] = True
        summary_var.set("Reading the comments…")
        box.delete(0, "end")

        def worker():
            plan = studio_metadata.plan_years(files)
            ui_call(show, plan)

        threading.Thread(target=worker, daemon=True).start()

    def show(plan):
        state["busy"], state["plan"] = False, plan
        if not win.winfo_exists():
            return
        box.delete(0, "end")
        for path, comment, old, new in plan:
            box.insert("end", f"{(old or '—'):>10} → {new}   {path.name:<60}   {comment}")
        box.selection_set(0, "end")
        summary_var.set(f"{len(plan)} song(s) would get a new Year (all selected). Untick by clicking; "
                        "Ctrl/Shift+click to choose several." if plan else
                        "Nothing to change: no comment with a recorded year that differs from the Year tag.")

    def apply_years():
        rows = list(box.curselection())
        if state["busy"] or not rows:
            if not rows and not state["busy"]:
                messagebox.showinfo("Year from comment", "Select the songs to change first.", parent=win)
            return
        chosen = [state["plan"][i] for i in rows]
        if not messagebox.askyesno("Year from comment", f"Set the Year of {len(chosen)} song(s) from their comment?",
                                   parent=win):
            return
        state["busy"] = True
        summary_var.set("Writing the Year tags…")

        def worker():
            done, failed, old_years = 0, [], {}
            for path, _comment, old, new in chosen:
                try:
                    studio_metadata.write_year(path, new)
                    old_years[str(path)] = old
                    done += 1
                except Exception as exc:
                    failed.append(f"{path.name}: {exc}")
            if old_years:
                undo_dir.mkdir(parents=True, exist_ok=True)
                (undo_dir / datetime.now().strftime("years_%Y-%m-%d_%H%M%S.json")).write_text(
                    json.dumps(old_years, ensure_ascii=False, indent=1), encoding="utf-8")
            ui_call(finished, done, failed)

        threading.Thread(target=worker, daemon=True).start()

    def finished(done, failed):
        log(f"Year from comment: {done} song(s) updated" + (f", {len(failed)} failed" if failed else ""))
        messagebox.showinfo("Year from comment", f"Year set for {done} song(s)."
                            + (f"\n\n{len(failed)} could not be changed:\n" + "\n".join(failed[:8]) if failed else ""),
                            parent=win)
        load()

    def undo_last():
        saved = sorted(undo_dir.glob("years_*.json")) if undo_dir.is_dir() else []
        if not saved or state["busy"]:
            if not saved:
                messagebox.showinfo("Year from comment", "There is no change to undo.", parent=win)
            return
        old_years = json.loads(saved[-1].read_text(encoding="utf-8"))
        if not messagebox.askyesno("Year from comment",
                                   f"Put back the previous Year of {len(old_years)} song(s) (the last change)?",
                                   parent=win):
            return
        failed = []
        for path, old in old_years.items():
            try:
                studio_metadata.restore_year(path, old)
            except Exception as exc:
                failed.append(f"{Path(path).name}: {exc}")
        saved[-1].rename(saved[-1].with_suffix(".undone"))
        messagebox.showinfo("Year from comment", f"Previous Year restored for {len(old_years) - len(failed)} song(s)."
                            + (f"\n\nFailed:\n" + "\n".join(failed[:8]) if failed else ""), parent=win)
        load()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(0, 12))
    apply_btn = tk.Button(buttons, text="Set Year for selected songs", command=apply_years,
                          bg=t["btn_bg"], fg=t["btn_fg"], font=("Segoe UI", 10, "bold"))
    apply_btn.pack(side="left")
    tk.Button(buttons, text="Undo last Year change", command=undo_last, bg=t["btn_bg"],
              fg=t["btn_fg"]).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    load()


def check_row(parent, text, variable, command, t):
    """A tick box that is easy to see on the dark theme: white box with a black tick, the text as a label beside it."""
    row = tk.Frame(parent, bg=t["bg"])
    box = tk.Checkbutton(row, variable=variable, command=command, bg=t["bg"], activebackground=t["bg"],
                         selectcolor="#ffffff", fg="#000000", activeforeground="#000000")
    box.pack(side="left")
    label = tk.Label(row, text=text, bg=t["bg"], fg=t["fg"], anchor="w", cursor="hand2")
    label.pack(side="left", fill="x", expand=True)

    def toggle(_event=None):
        variable.set(not variable.get())
        if command:
            command()

    label.bind("<Button-1>", toggle)
    return row


def _save_shared_lrc(song, text, backup_dir):
    """Put a downloaded LRC where Lyrics Sync keeps it (SYNCED LYRICS, or where the song's LRC already is; other
    copies are updated too, after a backup) and embed it into an MP3. Returns "embedded" or "saved"."""
    audio = Path(song)
    existing = [path for path in studio_resync.lrc_locations(audio) if path.is_file()]
    for index, path in enumerate(existing):
        target = backup_dir / str(index)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name)
    targets = existing or [studio_resync.lrc_locations(audio)[0]]
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        WARNING_INDEX.forget(path)
    if audio.suffix.lower() == ".mp3":
        embed_existing_lrc(str(audio))
        return "embedded"
    return "saved"


def open_shared_lrc_import():
    """Download → Get synced lyrics from the shared sheet: find the songs of Tracks in the public SYNCED LYRICS
    DATABASE sheet, then (after asking) download each LRC into the song's SYNCED LYRICS folder and embed it."""
    songs = [path for _row, path in sorted(TRACK_ROW_PATHS.items())
             if Path(path).suffix.lower() in (".mp3", ".flac") and os.path.isfile(path)]
    selected = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
    if len(selected) > 1:
        songs = [path for path in songs if path in set(selected)]
    if not songs:
        messagebox.showinfo("Synced lyrics from the shared sheet", "Open a music folder first (the songs in Tracks are "
                            "looked up).", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Synced lyrics from the shared sheet")
    win.configure(bg=t["bg"])
    win.minsize(900, 560)
    popup_over_root(win, 1100, 720)
    tk.Label(win, text=(f"The {len(songs)} song(s) {'selected' if len(selected) > 1 else 'in Tracks'} are looked up in the "
                        "sheet by their file name. Ticked (selected) rows are downloaded into the song's SYNCED LYRICS "
                        "folder — the same place Lyrics Sync uses — and embedded into the MP3 (a backup is made first). "
                        "Names are compared without a track number or “Artist - ” in front. The downloaded file is named like YOUR "
                        "MP3, whatever the sheet calls the song. Songs matched only by a similar name (? confirm), "
                        "with another length, or already synced are not selected — select a row yourself to confirm it."),
             bg=t["bg"], fg=t["fg"], wraplength=1060, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    link_row = tk.Frame(win, bg=t["bg"])
    link_row.pack(fill="x", padx=14)
    tk.Label(link_row, text="Sheet:", bg=t["bg"], fg=t["fg"]).pack(side="left")
    link_var = tk.StringVar(value=config.get("shared_lrc_link") or studio_shared.DEFAULT_LINK)
    tk.Entry(link_row, textvariable=link_var, bg=t["panel"], fg=t["fg"], insertbackground=t["fg"]).pack(
        side="left", fill="x", expand=True, padx=8)
    status_var = tk.StringVar(value="Reading the sheet…")
    tk.Label(win, textvariable=status_var, bg=t["bg"], fg="#ffb000", anchor="w", justify="left", wraplength=1060,
             font=("Segoe UI", 10, "bold")).pack(fill="x", padx=14, pady=(6, 4))
    replace_var = tk.BooleanVar(value=False)
    frame = tk.Frame(win, bg=t["bg"])
    box = tk.Listbox(frame, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], selectmode="extended",
                     exportselection=False, selectbackground=t.get("accent", "#355a7a"))
    scroll_y = tk.Scrollbar(frame, command=box.yview)
    box.configure(yscrollcommand=scroll_y.set)
    scroll_y.pack(side="right", fill="y")
    box.pack(side="left", fill="both", expand=True)
    state = {"plan": [], "busy": False}

    def row_text(item):
        name = Path(item["song"]).name
        if not item["row"]:
            return f"✗ not in the sheet      {name}"
        lengths = f"yours {studio_shared.clock(item['local'])} / sheet {studio_shared.clock(item['sheet'])}"
        if not item["same_length"]:
            mark = "≠ other length?"
        elif not item["sure"]:
            mark = "? confirm"
        elif item["already"]:
            mark = "✓ already synced"
        else:
            mark = "✓ found"
        how = ("  → sheet: “" + item["row"]["song"] + "”") if item["how"] in ("similar", "fuzzy", "cleaned") else ""
        return f"{mark:22} {name}   [{lengths}]{how}"

    def default_selection():
        box.selection_clear(0, "end")
        for index, item in enumerate(state["plan"]):
            if item["row"] and item["same_length"] and item["sure"] and (replace_var.get() or not item["already"]):
                box.selection_set(index)
        count = len(box.curselection())
        apply_btn.configure(text=f"Download and embed {count} selected")

    def show(plan_rows, error=None):
        state["busy"] = False
        if not win.winfo_exists():
            return
        check_btn.configure(state="normal")
        if error:
            status_var.set("Could not read the sheet: " + error)
            return
        plan_rows.sort(key=lambda item: (not item["row"], not item["same_length"], not item["sure"], item["already"],
                                         Path(item["song"]).name.casefold()))
        state["plan"] = plan_rows
        box.delete(0, "end")
        for item in plan_rows:
            box.insert("end", row_text(item))
        found = sum(1 for item in plan_rows if item["row"])
        other = sum(1 for item in plan_rows if item["row"] and not item["same_length"])
        already = sum(1 for item in plan_rows if item["row"] and item["already"])
        unsure = sum(1 for item in plan_rows if item["row"] and item["same_length"] and not item["sure"])
        status_var.set(f"Found in the sheet: {found} of {len(plan_rows)} song(s)"
                       + (f" — {unsure} to confirm (select them if the sheet name is the same song)" if unsure else "")
                       + (f" — {other} with another length" if other else "")
                       + (f" — {already} already synced" if already else "")
                       + f". Not in the sheet: {len(plan_rows) - found}.")
        default_selection()

    def check():
        if state["busy"]:
            return
        link = link_var.get().strip()
        state["busy"] = True
        check_btn.configure(state="disabled")
        status_var.set("Reading the sheet…")

        def worker():
            try:
                rows = studio_shared.fetch_rows(link, lambda text: ui_call(status_var.set, text))
                if not rows:
                    raise ValueError("no rows with a Song column and Download links were found")
                ui_call(status_var.set, f"Comparing {len(songs)} song(s) with {len(rows)} rows of the sheet…")
                plan_rows = studio_shared.plan(songs, rows, studio_shared.local_length)
                for item in plan_rows:
                    item["already"] = bool(item["row"]) and _has_synced_lyrics(item["song"], force=True)
                config["shared_lrc_link"] = link
                ui_call(save_config, config)
                ui_call(show, plan_rows)
            except Exception as exc:
                ui_call(show, [], f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def apply_selected():
        global downloading
        chosen = [state["plan"][i] for i in box.curselection() if state["plan"][i]["row"]]
        if state["busy"] or downloading or not chosen:
            if not chosen and not state["busy"]:
                status_var.set("Select the songs to download first.")
            return
        replacing = sum(1 for item in chosen if item["already"])
        if not messagebox.askyesno(
                "Synced lyrics from the shared sheet",
                f"Download the synced lyrics of {len(chosen)} song(s) from the sheet and embed them into the MP3s?"
                + (f"\n\n{replacing} of them already have synced lyrics — those are replaced (the old LRC and the MP3 "
                   "are backed up first)." if replacing else "\n\nEach MP3 is backed up before embedding."), parent=win):
            return
        state["busy"] = True
        downloading = True
        apply_btn.configure(state="disabled")
        check_btn.configure(state="disabled")
        backup_root = BACKUP_ROOT / "SHARED-LRC" / datetime.now().strftime("%Y%m%d_%H%M%S")

        def worker():
            results = {"embedded": [], "saved": [], "failed": []}
            for number, item in enumerate(chosen, 1):
                name = Path(item["song"]).name
                ui_call(status_var.set, f"Downloading {number} of {len(chosen)}: {name}")
                try:
                    text = studio_shared.download(item["row"]["url"])
                    kind = _save_shared_lrc(item["song"], text, backup_root / f"{number:04d}")
                    results[kind].append(name)
                except Exception as exc:
                    results["failed"].append(f"{name}: {exc}")
            ui_call(finished, results)

        threading.Thread(target=worker, daemon=True).start()

    def finished(results):
        global downloading
        downloading = False
        state["busy"] = False
        _SYNCED_LYRICS_CACHE.clear()
        _invalidate_view_status()
        done = len(results["embedded"]) + len(results["saved"])
        missing = [Path(item["song"]).name for item in state["plan"] if not item["row"]]
        log(f"Shared sheet: {len(results['embedded'])} LRC downloaded and embedded, {len(results['saved'])} saved "
            f"(FLAC), {len(results['failed'])} failed, {len(missing)} not in the sheet")
        refresh_tracks_from_disk([item["song"] for item in state["plan"]
                                  if Path(item["song"]).name in set(results["embedded"] + results["saved"])])
        lines = [f"Downloaded and embedded: {len(results['embedded'])}"]
        if results["saved"]:
            lines.append(f"Downloaded (FLAC, LRC saved next to it): {len(results['saved'])}")
        if results["failed"]:
            lines.append(f"Failed: {len(results['failed'])}\n  " + "\n  ".join(results["failed"][:10]))
        lines.append(f"Not in the sheet: {len(missing)}" + ("\n  " + "\n  ".join(missing[:15]) if missing else "")
                     + ("\n  …" if len(missing) > 15 else ""))
        if win.winfo_exists():
            status_var.set(f"Done — {done} song(s) got synced lyrics from the sheet, {len(results['failed'])} failed, "
                           f"{len(missing)} not in the sheet.")
            apply_btn.configure(state="normal")
            check_btn.configure(state="normal")
        messagebox.showinfo("Synced lyrics from the shared sheet", "\n\n".join(lines),
                            parent=win if win.winfo_exists() else root)
        if win.winfo_exists():
            check()

    options = tk.Frame(win, bg=t["bg"])
    options.pack(fill="x", padx=14, pady=(0, 4))
    check_row(options, "Also select songs that are already synced (their lyrics are replaced)", replace_var,
              lambda: default_selection(), t).pack(side="left")
    frame.pack(fill="both", expand=True, padx=14, pady=4)
    box.bind("<<ListboxSelect>>", lambda _e: apply_btn.configure(
        text=f"Download and embed {len(box.curselection())} selected"))
    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(4, 12))
    apply_btn = tk.Button(buttons, text="Download and embed selected", command=apply_selected, bg=t["sel_bg"],
                          fg=t["sel_fg"], font=("Segoe UI", 10, "bold"))
    apply_btn.pack(side="left")
    create_tooltip(apply_btn, "Download the LRC of the selected rows into SYNCED LYRICS and embed it into the MP3s.")
    check_btn = tk.Button(buttons, text="Check again", command=check, bg=t["btn_bg"], fg=t["btn_fg"])
    check_btn.pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    check()


def open_batch_edit(songs):
    """Change Metadata with several songs selected: give all of them the same Artist / Album / Year / Genre …
    (like Mp3tag). Every box offers <keep>, <clear> and each value the songs already have — listed once."""
    songs = [s for s in songs if Path(s).suffix.lower() in (".mp3", ".flac") and os.path.isfile(s)]
    if not songs:
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Change Metadata — " + str(len(songs)) + " songs")
    win.configure(bg=t["bg"])
    win.transient(root)
    popup_over_root(win, 700, 520)
    tk.Label(win, text=(f"{len(songs)} songs selected. Pick a value in a box (the arrow lists what these songs already "
                        "have) or type a new one; <keep> leaves the field as it is, <clear> empties it. Only the fields you "
                        "change are written (a backup of each song is made first)."),
             bg=t["bg"], fg=t["fg"], wraplength=660, justify="left", anchor="w").pack(fill="x", padx=16, pady=(14, 8))
    fields = (("artist", "Artist"), ("albumartist", "Album artist"), ("album", "Album"), ("date", "Year"),
              ("genre", "Genre"), ("comment", "Comment"))
    position = {key: studio_tracktable.TAG_KEYS.index(key) for key, _label in fields}
    counts = {key: {} for key, _label in fields}
    for song in songs:
        tags = _tags_of(song)
        if not tags:
            try:
                read = studio_swap.read(song)
                tags = [read.get(k, "") for k in studio_tracktable.TAG_KEYS]
            except Exception:
                tags = []
        for key, _label in fields:
            value = (tags[position[key]] if position[key] < len(tags) else "").strip()
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1
    form = tk.Frame(win, bg=t["bg"])
    form.pack(fill="x", padx=16)
    form.columnconfigure(1, weight=1)
    boxes, values_of = {}, {}
    for row, (key, label) in enumerate(fields):
        tk.Label(form, text=label + ":", bg=t["bg"], fg=t["fg"], anchor="w").grid(row=row, column=0, sticky="w", pady=5)
        values = ["<keep>", "<clear>"] + sorted(counts[key], key=lambda v: (-counts[key][v], v.casefold()))
        cell = tk.Frame(form, bg=t["bg"])
        cell.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
        box = tk.Entry(cell, font=("Segoe UI", 10), bg=t["panel"], fg=t["fg"], insertbackground=t["fg"])
        box.insert(0, "<keep>")
        box.pack(side="left", fill="x", expand=True)
        menu = tk.Menu(cell, tearoff=0, bg=t["panel"], fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["sel_fg"])
        for value in values:
            menu.add_command(label=value if len(value) < 90 else value[:87] + "…",
                             command=lambda v=value, b=box: (b.delete(0, "end"), b.insert(0, v)))
        arrow = tk.Button(cell, text="▾", width=3, bg=t["btn_bg"], fg=t["btn_fg"],
                          command=lambda m=menu, c=cell: m.tk_popup(c.winfo_rootx(), c.winfo_rooty() + c.winfo_height()))
        arrow.pack(side="left", padx=(4, 0))
        create_tooltip(arrow, "List the values these songs already have (each once) and pick one.")
        boxes[key] = box
        values_of[key] = values
    status = tk.Label(win, text="", bg=t["bg"], fg="#ffb000", wraplength=660, justify="left", anchor="w")
    status.pack(fill="x", padx=16, pady=(8, 0))
    state = {"busy": False}

    def apply_all():
        global downloading
        if state["busy"] or downloading:
            return
        changes = {}
        for key, box in boxes.items():
            text = box.get().strip()
            if text in ("<keep>", ""):
                continue
            changes[key] = "" if text == "<clear>" else text
        if not changes:
            status.configure(text="Nothing to change — every box says <keep>.")
            return
        names = ", ".join(label for key, label in fields if key in changes)
        if not messagebox.askyesno("Change Metadata", f"Write {names} into {len(songs)} song(s)?\n\n"
                                   "A backup of each song is made first.", parent=win):
            return
        state["busy"] = True
        downloading = True
        apply_btn.configure(state="disabled")
        status.configure(text="Saving…")

        def worker():
            done, failed = 0, []
            for song in songs:
                try:
                    studio_swap.write_fields(song, changes, BACKUP_ROOT)
                    done += 1
                except Exception as exc:
                    failed.append(f"{Path(song).name}: {exc}")
            ui_call(finished, done, failed)

        threading.Thread(target=worker, daemon=True).start()

    def finished(done, failed):
        global downloading
        downloading = False
        state["busy"] = False
        _SYNCED_LYRICS_CACHE.clear()
        log(f"Change Metadata: {done} song(s) updated" + (f", {len(failed)} failed" if failed else ""))
        refresh_tracks_from_disk(songs)
        messagebox.showinfo("Change Metadata", f"Changed {done} song(s)."
                            + (f"\n\n{len(failed)} could not be changed:\n" + "\n".join(failed[:8]) if failed else ""),
                            parent=win)
        if win.winfo_exists():
            win.destroy()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=16, pady=14, side="bottom")
    apply_btn = tk.Button(buttons, text="Apply to selected songs", command=apply_all, bg=t["sel_bg"], fg=t["sel_fg"],
                          font=("Segoe UI", 10, "bold"))
    apply_btn.pack(side="left")
    tk.Button(buttons, text="Remove brackets from Titles…", command=lambda: open_batch_titles(songs),
              bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")


def open_batch_titles(songs):
    """Change Metadata with several songs selected: remove (…) / […] from every Title (optionally rename the files)."""
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Change Metadata — remove brackets from Titles")
    win.configure(bg=t["bg"])
    win.transient(root)
    win.minsize(760, 420)
    popup_over_root(win, 980, 600)
    tk.Label(win, text=(f"{len(songs)} songs selected. Below: every Title that contains brackets and how it will look. "
                        "Untick rows you want to leave alone. Only the Title tag changes (the file name keeps its brackets) "
                        "unless you tick the second option."),
             bg=t["bg"], fg=t["fg"], wraplength=940, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    rename_var = tk.BooleanVar(value=False)
    check_row(win, "Also rename each file to its new Title (the TXT / LRC files follow)", rename_var, None, t).pack(
        fill="x", padx=14)
    summary = tk.StringVar(value="Reading the titles…")
    tk.Label(win, textvariable=summary, bg=t["bg"], fg="#ffb000", anchor="w", font=("Segoe UI", 10, "bold")).pack(
        fill="x", padx=14, pady=(6, 0))
    frame = tk.Frame(win, bg=t["bg"])
    frame.pack(fill="both", expand=True, padx=14, pady=6)
    box = tk.Listbox(frame, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], selectmode="extended", exportselection=False,
                     selectbackground=t.get("accent", "#355a7a"))
    scroll_y = tk.Scrollbar(frame, command=box.yview)
    scroll_x = tk.Scrollbar(frame, orient="horizontal", command=box.xview)
    box.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    box.pack(side="left", fill="both", expand=True)
    state = {"rows": [], "busy": False}

    def load():
        rows = []
        for path in songs:
            if Path(path).suffix.lower() not in (".mp3", ".flac") or not os.path.isfile(path):
                continue
            try:
                title = studio_swap.read(path).get("title", "")
            except Exception:
                continue
            new = studio_swap.strip_brackets(title)
            if new != title.strip():
                rows.append((path, title, new))
        state["rows"] = rows
        box.delete(0, "end")
        for path, title, new in rows:
            box.insert("end", f"{title}  →  {new}      [{Path(path).name}]")
        box.selection_set(0, "end")
        summary.set(f"{len(rows)} of {len(songs)} song(s) have brackets in the Title (all ticked)." if rows else
                    "None of the selected songs has brackets in its Title.")

    def apply_titles():
        global downloading
        chosen = [state["rows"][i] for i in box.curselection()]
        if state["busy"] or downloading or not chosen:
            if not chosen and not state["busy"]:
                messagebox.showinfo("Change Metadata", "Select the songs to change first.", parent=win)
            return
        rename = bool(rename_var.get())
        if not messagebox.askyesno("Change Metadata", f"Remove the brackets from the Title of {len(chosen)} song(s)"
                                   + (" and rename their files" if rename else "") + "?\n\nA backup of each song is made first.",
                                   parent=win):
            return
        state["busy"] = True
        downloading = True
        apply_btn.configure(state="disabled")
        summary.set("Saving…")

        def worker():
            done, failed, paths = 0, [], []
            for path, _title, new in chosen:
                try:
                    final = path
                    if rename:
                        name = studio_swap.filename_from_title(new)
                        if name and name != Path(path).stem:
                            final, _backup = rename_song_and_title(path, name)  # also sets the Title to the file name
                    studio_swap.write_fields(final, {"title": new}, BACKUP_ROOT)
                    if final != path:
                        failed_flag = _is_persistent_track_failed(path)
                        _set_persistent_track_failed(path, False)
                        _set_persistent_track_failed(final, failed_flag)
                    done += 1
                    paths.append(final)
                except Exception as exc:
                    failed.append(f"{Path(path).name}: {exc}")
            ui_call(finished, done, failed, paths)

        threading.Thread(target=worker, daemon=True).start()

    def finished(done, failed, paths):
        global downloading
        downloading = False
        state["busy"] = False
        _SYNCED_LYRICS_CACHE.clear()
        log(f"Change Metadata: brackets removed from the Title of {done} song(s)" + (f", {len(failed)} failed" if failed else ""))
        refresh_tracks_from_disk(paths or None)
        messagebox.showinfo("Change Metadata", f"Titles changed: {done}."
                            + (f"\n\n{len(failed)} could not be changed:\n" + "\n".join(failed[:8]) if failed else ""),
                            parent=win)
        if win.winfo_exists():
            apply_btn.configure(state="normal")
            load()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(0, 12))
    apply_btn = tk.Button(buttons, text="Remove brackets from selected Titles", command=apply_titles, bg=t["sel_bg"],
                          fg=t["sel_fg"], font=("Segoe UI", 10, "bold"))
    apply_btn.pack(side="left")
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    load()


def open_edit_metadata():
    """Metadata → Change Metadata: edit the tags of one song by hand (file name, title, artist, album …)."""
    if downloading:
        return
    rows = list(track_list.curselection())
    if len(rows) > 1:  # several songs: clean the brackets out of all their Titles at once
        open_batch_edit([TRACK_ROW_PATHS[i] for i in rows if i in TRACK_ROW_PATHS])
        return
    song = TRACK_ROW_PATHS.get(rows[0]) if len(rows) == 1 else None
    if not song or not os.path.isfile(song):
        messagebox.showinfo("Change Metadata", "Select one track to edit it by hand, or several to remove the brackets "
                            "from their Titles.", parent=root)
        return
    if Path(song).suffix.lower() not in (".mp3", ".flac"):
        messagebox.showinfo("Change Metadata", "Only MP3 and FLAC songs can be edited here.", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Change Metadata")
    win.configure(bg=t["bg"])
    win.transient(root)
    popup_over_root(win, 940, 600)
    try:
        current = studio_swap.read(song)
    except Exception as exc:
        win.destroy()
        messagebox.showerror("Change Metadata", f"Could not read the tags:\n{exc}", parent=root)
        return
    tk.Label(win, text=("Edit the fields and click Save. Only fields you changed are written; the audio, lyrics and "
                        "front cover are not touched. The file name field renames the file (its TXT / LRC follow)."),
             bg=t["bg"], fg=t["fg"], wraplength=720, justify="left", anchor="w").pack(fill="x", padx=16, pady=(14, 6))
    form = tk.Frame(win, bg=t["bg"])
    form.pack(fill="x", padx=16, pady=6)
    form.columnconfigure(1, weight=1)
    entries = {}
    for row, (key, label) in enumerate(studio_swap.FIELDS):
        tk.Label(form, text=label + ":", bg=t["bg"], fg=t["fg"], anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        entry = tk.Entry(form, font=("Segoe UI", 10), bg=t["panel"], fg=t["fg"], insertbackground=t["fg"])
        entry.insert(0, current.get(key, ""))
        entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        entries[key] = entry
    extras = ", ".join(f"{label}: {current.get(key) or 'none'}" for key, label in studio_swap.EXTRAS)
    tk.Label(form, text=extras, bg=t["bg"], fg=t.get("muted", t["fg"]), anchor="w").grid(
        row=len(studio_swap.FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))
    new_cover = {"path": ""}
    thumb = tk.Label(form, text="No cover", bg=t["panel"], fg=t.get("muted", t["fg"]), width=22, height=10)
    thumb.grid(row=0, column=2, rowspan=6, sticky="n", padx=(14, 0), pady=4)

    def show_thumb(data=None, path=None):
        """A small preview of the front cover (the song's own, or the picture picked to replace it)."""
        try:
            import io
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(data) if data is not None else path)
            image.thumbnail((170, 170))
            photo = ImageTk.PhotoImage(image)
            thumb.configure(image=photo, text="", width=photo.width(), height=photo.height())
            thumb.image = photo
        except Exception:
            thumb.configure(image="", text="No cover", width=22, height=10)
            thumb.image = None

    try:
        show_thumb(studio_swap.cover_bytes(song))
    except Exception:
        show_thumb(None, None)
    cover_row = tk.Frame(form, bg=t["bg"])
    cover_row.grid(row=len(studio_swap.FIELDS) + 1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    cover_label = tk.Label(cover_row, text="Front cover stays as it is.", bg=t["bg"], fg=t["fg"], anchor="w")

    def pick_cover():
        chosen = filedialog.askopenfilename(parent=win, title="Choose the new front cover",
                                            filetypes=[("Pictures", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
        if chosen:
            new_cover["path"] = chosen
            show_thumb(path=chosen)
            cover_label.configure(text="New front cover: " + os.path.basename(chosen) + " (replaces the old one on Save)")

    def keep_cover():
        new_cover["path"] = ""
        cover_label.configure(text="Front cover stays as it is.")
        try:
            show_thumb(studio_swap.cover_bytes(state["path"]))
        except Exception:
            show_thumb(None, None)

    tk.Button(cover_row, text="Replace front cover…", command=pick_cover, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left")
    tk.Button(cover_row, text="Keep old cover", command=keep_cover, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left", padx=(8, 0))
    cover_label.pack(side="left", padx=(12, 0))
    status = tk.Label(win, text="", bg=t["bg"], fg="#ffb000", wraplength=720, justify="left", anchor="w")
    status.pack(fill="x", padx=16)
    state = {"path": song, "busy": False}

    def put(key, text):
        entries[key].delete(0, "end")
        entries[key].insert(0, text)

    def name_from_title():
        name = studio_swap.filename_from_title(entries["title"].get())
        if not name:
            status.configure(text="The Title is empty.")
            return
        put("filename", name)
        status.configure(text="File name set to the Title — click Save Metadata to apply.")

    def title_without_brackets():
        before = entries["title"].get()
        after = studio_swap.strip_brackets(before)
        put("title", after)
        status.configure(text=("Brackets removed from the Title — click Save Metadata to apply." if after != before.strip()
                               else "The Title has no brackets."))

    quick = tk.Frame(win, bg=t["bg"])
    quick.pack(fill="x", padx=16, pady=(6, 0))
    tk.Button(quick, text="Set name same as Title", command=name_from_title, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left")
    tk.Button(quick, text="Remove brackets from Title", command=title_without_brackets, bg=t["btn_bg"],
              fg=t["btn_fg"]).pack(side="left", padx=(8, 0))

    def save():
        global downloading
        if state["busy"] or downloading:
            return
        changed = {key: entry.get().strip() for key, entry in entries.items()
                   if entry.get().strip() != (current.get(key, "") or "")}
        if not changed and not new_cover["path"]:
            status.configure(text="Nothing changed.")
            return
        new_name = changed.pop("filename", None)
        if new_name is not None:
            try:
                new_name = _validate_song_name(new_name)
            except ValueError as exc:
                status.configure(text=str(exc))
                return
        state["busy"] = True
        downloading = True
        save_btn.configure(state="disabled")
        status.configure(text="Saving…")

        def worker():
            path, error = state["path"], ""
            try:
                if new_name is not None:  # renames the file and its TXT / LRC; it also sets the Title tag to the name
                    path, _backup = rename_song_and_title(path, new_name)
                    if "title" not in changed:
                        changed["title"] = current.get("title", "")  # keep the old Title instead of the new file name
                if changed:
                    studio_swap.write_fields(path, changed, BACKUP_ROOT)
                if new_cover["path"]:
                    studio_swap.set_cover(path, new_cover["path"], BACKUP_ROOT)
            except Exception as exc:
                error = str(exc)
            ui_call(finished, path, error)

        threading.Thread(target=worker, daemon=True).start()

    def finished(path, error):
        global downloading
        downloading = False
        state["busy"] = False
        if error:
            status.configure(text="Could not save: " + error)
            save_btn.configure(state="normal")
            return
        if path != song:
            failed = _is_persistent_track_failed(song)
            _set_persistent_track_failed(song, False)
            _set_persistent_track_failed(path, failed)
        _SYNCED_LYRICS_CACHE.clear()
        log(f"Change Metadata: {path}")
        refresh_tracks_from_disk([path])
        set_status("Metadata saved: " + os.path.basename(path), "success")
        win.destroy()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=16, pady=12)
    save_btn = tk.Button(buttons, text="Save Metadata", command=save, bg=t["sel_bg"], fg=t["sel_fg"],
                         font=("Segoe UI", 10, "bold"))
    save_btn.pack(side="left")
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")


def open_swap_metadata():
    """Metadata → Swap Song and Transfer Metadata: copy tags (and file name, lyrics, front cover) from the song
    being replaced onto a new file, then swap the two files' places — like Mp3tag copy / paste tag."""
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Swap Song and Transfer Metadata")
    win.configure(bg=t["bg"])
    win.minsize(900, 560)
    popup_over_root(win, 1100, 640)
    tk.Label(win, text=("1. Choose the song to replace — its metadata is copied.   2. Choose the new song that receives it.   "
                        "Tick what to transfer, then Transfer + Swap: the new song takes the old one's place in its folder "
                        "(with its file name when ticked) and the old song goes to a new subfolder "
                        "“Old Metadata swapped songs” in the new one's former folder. "
                        "Both files are backed up first."),
             bg=t["bg"], fg=t["fg"], wraplength=1060, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 8))
    paths = {"old": tk.StringVar(), "new": tk.StringVar()}
    picker = tk.Frame(win, bg=t["bg"])
    picker.pack(fill="x", padx=14)
    picker.columnconfigure(1, weight=1)

    def browse(key):
        path = filedialog.askopenfilename(parent=win, title="Choose a song",
                                          filetypes=[("Songs", "*.mp3 *.flac"), ("All files", "*.*")])
        if path:
            paths[key].set(path)

    def use_selected(key):
        rows = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
        if not rows:
            messagebox.showinfo("Swap Song", "Select a track in the main window first.", parent=win)
            return
        if key == "old" and len(rows) >= 2:  # two selected: first = song to replace, second = new song
            paths["old"].set(rows[0]); paths["new"].set(rows[1])
        else:
            paths[key].set(rows[0])

    for row, (key, label) in enumerate((("old", "1. Song to replace (metadata from):"),
                                         ("new", "2. New song (gets the metadata):"))):
        tk.Label(picker, text=label, bg=t["bg"], fg=t["fg"], anchor="w").grid(row=row, column=0, sticky="w", pady=3)
        tk.Entry(picker, textvariable=paths[key], bg=t["panel"], fg=t["fg"], insertbackground=t["fg"]).grid(
            row=row, column=1, sticky="ew", padx=8, pady=3)
        tk.Button(picker, text="Browse…", command=lambda k=key: browse(k), bg=t["btn_bg"], fg=t["btn_fg"]).grid(
            row=row, column=2, padx=(0, 6))
        tk.Button(picker, text="Use selected track", command=lambda k=key: use_selected(k), bg=t["btn_bg"],
                  fg=t["btn_fg"]).grid(row=row, column=3)

    table = tk.Frame(win, bg=t["panel"], highlightthickness=1, highlightbackground=t["border"])
    table.pack(fill="both", expand=True, padx=14, pady=10)
    table.columnconfigure(2, weight=1, uniform="v")
    table.columnconfigure(3, weight=1, uniform="v")
    for column, text in enumerate(("Transfer", "Field", "Song to replace", "New song (now)")):
        tk.Label(table, text=text, bg=t["panel"], fg=t["fg"], font=("Segoe UI", 9, "bold"), anchor="w").grid(
            row=0, column=column, sticky="w", padx=8, pady=(6, 4))
    ticks, cells = {}, {}
    for row, (key, label) in enumerate(studio_swap.FIELDS + studio_swap.EXTRAS, start=1):
        ticks[key] = tk.BooleanVar(value=True)
        tk.Checkbutton(table, variable=ticks[key], bg=t["panel"], activebackground=t["panel"],
                       selectcolor="#ffffff", fg="#000000", activeforeground="#000000").grid(row=row, column=0, padx=8)
        tk.Label(table, text=label, bg=t["panel"], fg=t["fg"], anchor="w").grid(row=row, column=1, sticky="w", padx=8)
        cells[key] = []
        for column in (2, 3):
            cell = tk.Label(table, text="", bg=t["panel"], fg=t["fg"], anchor="w", justify="left", wraplength=420)
            cell.grid(row=row, column=column, sticky="w", padx=8, pady=1)
            cells[key].append(cell)

    def refresh(*_):
        for column, key in enumerate(("old", "new")):
            path = paths[key].get().strip()
            try:
                values = studio_swap.read(path) if path and os.path.isfile(path) else {}
            except Exception as exc:
                values = {"filename": f"(cannot read: {exc})"}
            for field, labels in cells.items():
                labels[column].configure(text=values.get(field, "") or ("—" if values else ""))

    paths["old"].trace_add("write", refresh)
    paths["new"].trace_add("write", refresh)

    def set_all(value):
        for var in ticks.values():
            var.set(value)

    def transfer():
        old, new = paths["old"].get().strip(), paths["new"].get().strip()
        chosen = [key for key, var in ticks.items() if var.get()]
        if not (old and new and os.path.isfile(old) and os.path.isfile(new)):
            messagebox.showinfo("Swap Song", "Choose both songs first.", parent=win)
            return
        if not chosen:
            messagebox.showinfo("Swap Song", "Tick at least one thing to transfer.", parent=win)
            return
        names = ", ".join(label for key, label in studio_swap.FIELDS + studio_swap.EXTRAS if key in chosen)
        if not messagebox.askyesno(
                "Swap Song", f"Copy from\n{Path(old).name}\nto\n{Path(new).name}:\n\n{names}\n\n"
                f"Then the new song moves to\n{Path(old).parent}\nand the old song moves to\n{Path(new).parent / studio_swap.OLD_SONGS_FOLDER}\n\n"
                "Both files are backed up first. Continue?", parent=win):
            return
        try:
            new_final, old_final, backup = studio_swap.swap(old, new, chosen, BACKUP_ROOT)
        except Exception as exc:
            messagebox.showerror("Swap Song", f"Nothing was swapped:\n{exc}", parent=win)
            return
        log(f"Swap Song: {Path(new).name} → {new_final}; {Path(old).name} → {old_final}. Backup: {backup}")
        _remember_fix("Swap Song and Transfer Metadata", str(new_final), f"old song moved to {old_final}")
        _SYNCED_LYRICS_CACHE.clear()
        refresh_tracks_from_disk()
        messagebox.showinfo("Swap Song", f"Done.\n\nNew song now:\n{new_final}\n\nOld song now:\n{old_final}\n\n"
                            f"Backup of both: {backup}", parent=win)
        paths["old"].set(str(new_final)); paths["new"].set(str(old_final))

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(0, 12))
    tk.Button(buttons, text="All metadata", command=lambda: set_all(True), bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left")
    tk.Button(buttons, text="None", command=lambda: set_all(False), bg=t["btn_bg"], fg=t["btn_fg"]).pack(
        side="left", padx=(8, 0))
    tk.Button(buttons, text="Transfer Metadata + Swap", command=transfer, bg=t["sel_bg"], fg=t["sel_fg"],
              font=("Segoe UI", 10, "bold")).pack(side="left", padx=(16, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    use = [TRACK_ROW_PATHS[i] for i in track_list.curselection() if i in TRACK_ROW_PATHS]
    if len(use) >= 2:
        paths["old"].set(use[0]); paths["new"].set(use[1])
    elif use:
        paths["old"].set(use[0])


def open_sheet_import():
    """Metadata → Import from Spreadsheet: paste a Google Sheets / Docs link, find the selected songs in it, ask which
    row is the same song, then ask which columns (date, record date, producer, credited artist, location …) to import."""
    songs = [TRACK_ROW_PATHS[i] for i in track_list.curselection()
             if i in TRACK_ROW_PATHS and Path(TRACK_ROW_PATHS[i]).suffix.lower() in (".mp3", ".flac")]
    if not songs:
        messagebox.showinfo("Import from Spreadsheet", "Select the songs in Tracks first (MP3 or FLAC).", parent=root)
        return
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Import from Spreadsheet")
    win.configure(bg=t["bg"])
    win.minsize(980, 640)
    popup_over_root(win, 1320, 860)
    label_of = dict(studio_sheet.TARGETS)
    key_of = {label: key for key, label in studio_sheet.TARGETS}
    state = {"sheet": None, "index": -1, "found": [], "fields": [], "current": {}, "done": 0, "busy": False}

    link_row = tk.Frame(win, bg=t["bg"])
    link_row.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(link_row, text="Link:", bg=t["bg"], fg=t["fg"]).pack(side="left")
    link_var = tk.StringVar(value=config.get("sheet_link") or studio_sheet.DEFAULT_LINK)
    tk.Entry(link_row, textvariable=link_var, bg=t["panel"], fg=t["fg"], insertbackground=t["fg"]).pack(
        side="left", fill="x", expand=True, padx=8)
    status_var = tk.StringVar(value="")
    tk.Label(win, textvariable=status_var, bg=t["bg"], fg="#ffb000", anchor="w", justify="left", wraplength=1140,
             font=("Segoe UI", 10, "bold")).pack(fill="x", padx=14, pady=(2, 6))
    song_var = tk.StringVar(value="")
    tk.Label(win, textvariable=song_var, bg=t["bg"], fg=t["fg"], anchor="w", font=("Segoe UI", 12, "bold")).pack(
        fill="x", padx=14)
    search_row = tk.Frame(win, bg=t["bg"])
    search_row.pack(fill="x", padx=14, pady=(2, 4))
    tk.Label(search_row, text="Is this the same song? Pick the matching row — or search another name:",
             bg=t["bg"], fg=t["fg"]).pack(side="left")
    search_var = tk.StringVar(value="")
    search_entry = tk.Entry(search_row, textvariable=search_var, width=30, bg=t["panel"], fg=t["fg"],
                            insertbackground=t["fg"])
    search_entry.pack(side="left", padx=(8, 4))
    box = tk.Listbox(win, height=4, font=("Consolas", 10), bg=t["panel"], fg=t["fg"], exportselection=False,
                     selectbackground=t.get("accent", "#355a7a"))
    box.pack(fill="x", padx=14)
    fields = tk.Frame(win, bg=t["bg"])
    fields.pack(fill="both", expand=True, padx=14, pady=(8, 4))
    preview = tk.Text(win, height=7, wrap="word", bg=t["panel"], fg=t["fg"], font=("Segoe UI", 10), padx=8, pady=6,
                      relief="flat")
    preview.pack(fill="x", padx=14, pady=(0, 6), side="bottom", before=fields)
    preview.configure(state="disabled")

    def refresh_preview():
        """The full text of every field that Import will change: what it is now → what it becomes."""
        chosen = [(f["key"], f["text"]) for f in state["fields"] if f.get("key") and f["tick"].get() and f.get("text")]
        new = studio_sheet.apply_values(state["current"], chosen) if chosen else {}
        lines = [f"{label_of_tag.get(key, key)}:  {state['current'].get(key) or '(empty)'}   →   {value}"
                 for key, value in new.items()]
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", "\n".join(lines) if lines else "Nothing will change with the current choices.")
        preview.configure(state="disabled")

    def remember_choices():
        """The last choice of every column — what it is imported as and whether it is ticked — stays until changed."""
        if not state["fields"]:
            return
        config["sheet_targets"] = dict(config.get("sheet_targets", {}),
                                       **{f["header"]: f["key"] for f in state["fields"] if f.get("key")})
        config["sheet_ticks"] = dict(config.get("sheet_ticks", {}),
                                     **{f["header"]: bool(f["tick"].get()) for f in state["fields"] if f.get("text")})
        save_config(config)

    label_of_tag = {"date": "Year", "artist": "Artist", "albumartist": "Album artist", "album": "Album", "title": "Title",
                    "genre": "Genre", "comment": "Comment"}

    def tag_now(key):
        if key.startswith("comment"):
            return state["current"].get("comment", "")
        return state["current"].get(key, "") if key != "skip" else ""

    def summary(table, song):
        parts = {}
        for header, cell in zip(table["header"], song["cells"]):
            key = studio_sheet.default_target(header)
            value = studio_sheet.column_value(header, cell, key) if key in ("artist", "date") else ""
            if value and key not in parts:
                parts[key] = value
        return " — ".join(v for v in (parts.get("artist"), parts.get("date")) if v)

    def row_text(found):
        _strength, table, song = found
        names = " | ".join(song["names"])
        return f"{table['name'][:26]:26} row {song['row']:<5} {names[:56]:56} {summary(table, song)}"

    def refresh_row(field):
        key = key_of.get(field["target"].get(), "skip")
        value = studio_sheet.column_value(field["header"], field["cell"], key) if key != "skip" else ""
        field["value"].configure(text=value if key != "skip" else "")
        field["now"].configure(text=tag_now(key) or "")
        field["key"], field["text"] = key, value
        refresh_preview()

    def build_fields(_event=None):
        for child in fields.winfo_children():
            child.destroy()
        state["fields"] = []
        selection = box.curselection()
        if not selection or not state["found"]:
            tk.Label(fields, text="No row selected.", bg=t["bg"], fg=t["fg"]).pack(anchor="w")
            return
        _strength, table, song = state["found"][selection[0]]
        remembered = config.get("sheet_targets", {})
        ticks = config.get("sheet_ticks", {})
        heads = ("", "Column", "In the spreadsheet", "Import as", "Will write", "Song now")
        for column, text in enumerate(heads):
            tk.Label(fields, text=text, bg=t["bg"], fg=t.get("muted", t["fg"]), anchor="w",
                     font=("Segoe UI", 9, "bold")).grid(row=0, column=column, sticky="w", padx=4)
        fields.columnconfigure(2, weight=3)
        fields.columnconfigure(4, weight=3)
        fields.columnconfigure(5, weight=4)
        seen_date = False
        row = 1
        for index, (header, cell) in enumerate(zip(table["header"], song["cells"])):
            if index == table["name_col"] or not cell.strip().strip("-– "):
                continue
            key = remembered.get(header) or studio_sheet.default_target(header)
            if key not in label_of:
                key = "skip"
            field = {"header": header, "cell": cell, "tick": tk.BooleanVar(value=False),
                     "target": tk.StringVar(value=label_of[key])}
            value = studio_sheet.column_value(header, cell, key) if key != "skip" else ""
            wanted = ticks[header] if header in ticks else not (key == "date" and seen_date)  # last choice wins
            field["tick"].set(bool(value) and bool(wanted))
            seen_date = seen_date or (key == "date" and bool(value))
            tk.Checkbutton(fields, variable=field["tick"], command=lambda: (refresh_preview(), remember_choices()), bg=t["bg"],
                           activebackground=t["bg"], selectcolor="#ffffff", fg="#000000",
                           activeforeground="#000000").grid(row=row, column=0, padx=4)
            tk.Label(fields, text=header, bg=t["bg"], fg=t["fg"], anchor="w", wraplength=150, justify="left").grid(
                row=row, column=1, sticky="w", padx=4)
            tk.Label(fields, text=studio_sheet.flat(cell), bg=t["bg"], fg=t["fg"], anchor="w", wraplength=260,
                     justify="left").grid(row=row, column=2, sticky="w", padx=4)
            menu = tk.OptionMenu(fields, field["target"], *[label for _key, label in studio_sheet.TARGETS],
                                 command=lambda _v, f=field: (refresh_row(f), f["tick"].set(bool(f["text"])), refresh_preview(),
                                                          remember_choices()))
            menu.configure(bg=t["btn_bg"], fg=t["btn_fg"], font=("Segoe UI", 9), highlightthickness=0)
            menu.grid(row=row, column=3, sticky="ew", padx=4, pady=1)
            field["value"] = tk.Label(fields, text="", bg=t["bg"], fg="#7be08b", anchor="w", wraplength=260,
                                      justify="left")
            field["value"].grid(row=row, column=4, sticky="w", padx=4)
            field["now"] = tk.Label(fields, text="", bg=t["bg"], fg=t.get("muted", t["fg"]), anchor="w", wraplength=300,
                                    justify="left")
            field["now"].grid(row=row, column=5, sticky="w", padx=4)
            refresh_row(field)
            state["fields"].append(field)
            row += 1
        refresh_preview()

    def fill_candidates(found):
        state["found"] = found
        box.delete(0, "end")
        for item in found:
            box.insert("end", row_text(item))
        if found:
            box.selection_set(0)
            status_var.set(f"{len(found)} possible match(es) — {'check that it is the same song' if len(found) == 1 else 'pick the right one'}.")
        else:
            status_var.set("This song was not found. Type another name above (or Skip song).")
        build_fields()

    def locate(song, current=None):
        """Rows of the spreadsheet for a song: by its file name, and when that finds nothing by its Title tag."""
        sheet = state["sheet"]
        if not sheet:
            return [], False
        found = sheet.find(Path(song).name)
        if found:
            return found, False
        title = str((current if current is not None else studio_swap.read(song)).get("title", "") or "").strip()
        if title and title.casefold() != Path(song).stem.casefold():
            found = sheet.find(title)
        return found, bool(found)

    def show_song(index):
        state["index"] = index
        if index >= len(songs):
            finish_all()
            return
        song = songs[index]
        try:
            state["current"] = studio_swap.read(song)
        except Exception as exc:
            state["current"] = {}
            status_var.set(f"Could not read the tags: {exc}")
        song_var.set(f"Song {index + 1} of {len(songs)}:  {Path(song).name}")
        search_var.set("")
        found, by_title = locate(song, state["current"])
        fill_candidates(found)
        if by_title:
            status_var.set(f"Not found by the file name — {len(found)} match(es) found by the Title "
                           f"“{state['current'].get('title', '')}”: check that it is the same song.")

    def search_other(_event=None):
        query = search_var.get().strip()
        if state["sheet"] and query:
            fill_candidates(state["sheet"].find(query))

    def import_now():
        if state["busy"] or state["index"] < 0 or state["index"] >= len(songs):
            return
        song = songs[state["index"]]
        chosen = [(f["key"], f["text"]) for f in state["fields"] if f["tick"].get() and f.get("text")]
        new = studio_sheet.apply_values(state["current"], chosen)
        if not new:
            status_var.set("Nothing to change for this song (nothing ticked or the song already has these values).")
            return
        try:
            studio_swap.write_fields(song, new, BACKUP_ROOT)
        except Exception as exc:
            messagebox.showerror("Import from Spreadsheet", f"Could not write the tags:\n{exc}", parent=win)
            return
        remember_choices()
        state["done"] += 1
        log(f"Import from Spreadsheet: {Path(song).name}: " + ", ".join(f"{k} = {v}" for k, v in new.items()))
        _SYNCED_LYRICS_CACHE.clear()
        show_song(state["index"] + 1)

    def skip_song():
        if state["index"] >= 0 and not state["busy"]:
            show_song(state["index"] + 1)

    def import_all():
        """Do the same for this song and every song after it: the picked row's tab, the ticked columns and what each
        is imported as are used for all; a song that is not in the spreadsheet is skipped on its own."""
        index = state["index"]
        selection = box.curselection()
        if state["busy"] or not state["sheet"] or index < 0 or index >= len(songs):
            return
        if not selection or not state["found"]:
            status_var.set("Pick the matching row of this song first (or use Skip song).")
            return
        settings = {f["header"]: f["key"] for f in state["fields"] if f.get("key") and f["tick"].get() and f.get("text")}
        if not settings:
            status_var.set("Nothing is ticked — tick the columns to import first.")
            return
        remember_choices()
        first_tab = state["found"][selection[0]][1]["name"]
        picked = state["found"][selection[0]]
        stats = {"done": 0, "unchanged": 0, "missing": [], "failed": []}
        state["busy"] = True
        for button in (import_btn, skip_btn, all_btn):
            button.configure(state="disabled")

        def step(i):
            if not win.winfo_exists():
                return
            if i >= len(songs):
                state["busy"] = False
                for button in (import_btn, skip_btn, all_btn):
                    button.configure(state="normal")
                state["index"] = len(songs)
                finish_all()
                extra = (f" {stats['unchanged']} already had these values." if stats["unchanged"] else "")
                extra += (f" Not found in the spreadsheet (skipped): " + ", ".join(stats["missing"][:6])
                          + ("…" if len(stats["missing"]) > 6 else "") + ".") if stats["missing"] else ""
                extra += (f" Failed: " + "; ".join(stats["failed"][:3]) + ".") if stats["failed"] else ""
                status_var.set(f"Done — {stats['done']} of {len(songs)} song(s) updated.{extra} A backup of each is in BACKUPS.")
                return
            song = songs[i]
            song_var.set(f"Song {i + 1} of {len(songs)}:  {Path(song).name}   (doing the same for all…)")
            if i == index:
                found = [picked]
            else:
                try:
                    found, _by_title = locate(song)
                except Exception:
                    found = []
                found.sort(key=lambda item: item[1]["name"] != first_tab)  # the tab that was picked goes first
            if not found:
                stats["missing"].append(Path(song).name)
                win.after(1, step, i + 1)
                return
            _strength, table, row = found[0]
            chosen = []
            for header, cell in zip(table["header"], row["cells"]):
                key = settings.get(header)
                if key:
                    value = studio_sheet.column_value(header, cell, key)
                    if value:
                        chosen.append((key, value))
            try:
                current = studio_swap.read(song)
                new = studio_sheet.apply_values(current, chosen)
                if new:
                    studio_swap.write_fields(song, new, BACKUP_ROOT)
                    stats["done"] += 1
                    state["done"] += 1
                    log(f"Import from Spreadsheet: {Path(song).name}: " + ", ".join(f"{k} = {v}" for k, v in new.items()))
                else:
                    stats["unchanged"] += 1
            except Exception as exc:
                stats["failed"].append(f"{Path(song).name}: {exc}")
            win.after(1, step, i + 1)

        _SYNCED_LYRICS_CACHE.clear()
        step(index)

    def finish_all():
        song_var.set("")
        state["found"] = []
        box.delete(0, "end")
        build_fields()
        status_var.set(f"Done — {state['done']} of {len(songs)} song(s) updated. A backup of each is in BACKUPS.")
        try:
            refresh_tracks_from_disk([str(p) for p in songs])
        except Exception:
            pass

    def sheet_ready(sheet, error=None):
        state["busy"] = False
        load_btn.configure(state="normal")
        if not win.winfo_exists():
            return
        if error:
            status_var.set("Could not load the link: " + error)
            return
        if not sheet:
            status_var.set("No table with a song title column was found in this link. "
                           "Is the sheet shared as “anyone with the link can view”?")
            return
        state["sheet"] = sheet
        config["sheet_link"] = link_var.get().strip()
        save_config(config)
        age = time.strftime("%Y-%m-%d %H:%M", time.localtime(sheet.fetched)) if sheet.fetched else "?"
        tabs = ", ".join(f"{tb['name']} ({len(tb['songs'])})" for tb in sheet.tables[:6])
        status_var.set(f"Loaded {sheet.song_count} songs from {len(sheet.tables)} tab(s) (downloaded {age}): {tabs}")
        show_song(max(0, state["index"]))

    def load(force):
        if state["busy"]:
            return
        link = link_var.get().strip()
        try:
            studio_sheet.parse_link(link)
        except ValueError as exc:
            status_var.set(str(exc))
            return
        if not force:
            cached = studio_sheet.load_cached(link)
            if cached:
                sheet_ready(cached)
                return
        state["busy"] = True
        load_btn.configure(state="disabled")
        status_var.set("Downloading the spreadsheet…")

        def worker():
            try:
                sheet = studio_sheet.download(link, lambda text: ui_call(status_var.set, text))
                ui_call(sheet_ready, sheet)
            except Exception as exc:
                ui_call(sheet_ready, None, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    box.bind("<<ListboxSelect>>", build_fields)
    search_entry.bind("<Return>", search_other)
    tk.Button(search_row, text="Search", command=search_other, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left")
    load_btn = tk.Button(link_row, text="Load / refresh spreadsheet", command=lambda: load(True), bg=t["btn_bg"],
                         fg=t["btn_fg"])
    load_btn.pack(side="left")
    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=(4, 12), side="bottom", before=preview)
    import_btn = tk.Button(buttons, text="Import ticked fields into this song", command=import_now, bg=t["sel_bg"],
                           fg=t["sel_fg"], font=("Segoe UI", 10, "bold"))
    import_btn.pack(side="left")
    skip_btn = tk.Button(buttons, text="Skip song", command=skip_song, bg=t["btn_bg"], fg=t["btn_fg"])
    skip_btn.pack(side="left", padx=(8, 0))
    all_btn = tk.Button(buttons, text="Do the same for all remaining songs", command=import_all, bg=t["btn_bg"],
                        fg=t["btn_fg"], font=("Segoe UI", 10, "bold"))
    all_btn.pack(side="left", padx=(8, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")
    state["index"] = 0
    load(False)
    if not state["sheet"]:
        status_var.set("Click “Load / refresh spreadsheet” to download the sheet from the link.")


# ------------------ About / Help ------------------

def about():
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Help — How To Use")
    win.configure(bg=t["bg"])
    popup_over_root(win, 1060, 820)
    win.minsize(760, 480)
    style = ttk.Style(win)
    style.configure("Help.TNotebook", background=t["bg"], borderwidth=0)
    style.configure("Help.TNotebook.Tab", font=("Segoe UI", 13, "bold"),
                    padding=(24, 13))
    style.map("Help.TNotebook.Tab", background=[("selected", "#ffffff"), ("!selected", "#dddddd")],
              foreground=[("selected", "#000000"), ("!selected", "#222222")])
    notebook = ttk.Notebook(win, style="Help.TNotebook")
    notebook.pack(fill="both", expand=True, padx=12, pady=12)
    tab_logo = studio_dialogs.heading_icon(("Segoe UI", 13, "bold"), ICON_FILE, scale=1.1) \
        if os.path.isfile(ICON_FILE) else None
    win.tab_logo = tab_logo  # keep it alive while the window is open
    for title, body in PAGES.items():
        page = tk.Frame(notebook, bg=t["bg"])
        tab_text = title.replace("T9 ", "") if tab_logo else title  # the logo stands in for "T9"
        notebook.add(page, text=" " + tab_text, **({"image": tab_logo, "compound": "left"} if tab_logo else {}))
        navigation = tk.Frame(page, bg=t["bg"])
        navigation.pack(fill="x", pady=(8, 10))
        body_frame = tk.Frame(page, bg=t["bg"])
        body_frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(body_frame, orient="vertical")
        scroll.pack(side="right", fill="y")
        text = tk.Text(body_frame, wrap="word", font=("Segoe UI", 11),
                       bg=t["panel"], fg=t["fg"], padx=24, pady=18,
                       spacing1=2, spacing2=3, spacing3=4,
                       relief="flat", yscrollcommand=scroll.set)
        anchors = render_page(text, body)
        for label, heading in NAVIGATION[title]:
            tk.Button(navigation, text=label, font=("Segoe UI", 10, "bold"),
                      bg=t["btn_bg"], fg=t["btn_fg"], padx=10, pady=6,
                      activebackground=t["btn_bg"], activeforeground=t["btn_fg"],
                      command=lambda box=text, mark=anchors[heading]: box.yview(mark)
                      ).pack(side="left", padx=(0, 8))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        scroll.configure(command=text.yview)
    tk.Button(win, text="Close", command=win.destroy,
              bg=t["btn_bg"], fg=t["btn_fg"]).pack(anchor="e", padx=12, pady=(0, 12))


# ------------------ Options Window (WITH PRIORITY REORDERING) ------------------

def open_options_window():
    if downloading:
        messagebox.showinfo("Busy", "Wait for the current download to finish first.")
        return

    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Options")
    win.configure(bg=t["bg"])
    win.grab_set()
    popup_over_root(win, 980, 860)

    enabled_map = dict(config.get("providers_enabled", {p: True for p in ALL_PROVIDERS}))
    order = list(config.get("providers_order", ALL_PROVIDERS))
    lang = config.get("lang", "en")
    plain_ok = bool(config.get("allow_plain_fallback", True))
    strip_cjk = bool(config.get("strip_cjk", True))
    reject_non_ascii = bool(config.get("reject_non_ascii", True))
    ratio = float(config.get("reject_non_ascii_ratio", 0.15))
    lyrics_subfolder = bool(config.get("lyrics_subfolder", False))
    lyrics_transfer_dir = str(config.get("lyrics_transfer_dir", "") or "")
    genius_output_format = str(config.get("genius_output_format", "txt")).lower()
    if genius_output_format not in ("lrc", "txt"):
        genius_output_format = "txt"
    saved_genius_token = get_genius_api_token()

    # One scrollable Options window:
    # frequently changed settings first, rarely changed compatibility settings last.
    outer = tk.Frame(win, bg=t["bg"])
    outer.pack(fill="both", expand=True, padx=10, pady=(10, 6))

    canvas = tk.Canvas(outer, bg=t["bg"], highlightthickness=0)
    scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    content = tk.Frame(canvas, bg=t["bg"])
    content_id = canvas.create_window((0, 0), window=content, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    def _on_content_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event):
        canvas.itemconfigure(content_id, width=event.width)

    content.bind("<Configure>", _on_content_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def _wheel(event):
        try:
            delta = -1 if event.delta > 0 else 1
            canvas.yview_scroll(delta * 3, "units")
        except Exception:
            pass

    canvas.bind_all("<MouseWheel>", _wheel)

    developer_var = tk.BooleanVar(value=studio_preferences.developer_mode())
    developer_toggle = tk.Checkbutton(
        content, text="Developer mode — show technical and testing controls",
        variable=developer_var, bg=t["bg"], fg=t["fg"], selectcolor=t["panel"])
    developer_toggle.pack(anchor="w", padx=4, pady=(0, 10))

    # ---------------- Lyrics library transfer ----------------
    transfer_box = tk.LabelFrame(
        content, text="Lyrics Library Between Computers", bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold"), padx=10, pady=8)
    transfer_box.pack(fill="x", padx=4, pady=(0, 10))
    tk.Label(
        transfer_box,
        text=("Transfer every TXT/LRC and its manifest in one small ZIP. Audio files are never included. "
              "Import changes only differing files and backs up replaced or removed lyrics."),
        bg=t["bg"], fg=t["fg"], justify="left", wraplength=900
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 7))
    tk.Label(transfer_box, text="Export folder:", bg=t["bg"], fg=t["fg"]).grid(
        row=1, column=0, sticky="w", padx=(0, 8))
    transfer_dir_var = tk.StringVar(value=lyrics_transfer_dir)
    transfer_entry = tk.Entry(
        transfer_box, textvariable=transfer_dir_var,
        bg=t["panel"], fg=t["fg"], insertbackground=t["fg"],
        highlightbackground=t["border"], highlightcolor=t["border"])
    transfer_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8))
    transfer_box.columnconfigure(1, weight=1)

    def choose_transfer_folder():
        folder = filedialog.askdirectory(
            parent=win, title="Choose where Lyrics Library ZIP files are saved",
            initialdir=transfer_dir_var.get().strip() or MUSIC_DIR or str(Path.home()))
        if folder:
            transfer_dir_var.set(folder)

    transfer_browse_btn = tk.Button(
        transfer_box, text="Browse…", command=choose_transfer_folder,
        bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"],
        activeforeground=t["btn_fg"], width=11)
    transfer_browse_btn.grid(row=1, column=2, sticky="e")
    transfer_actions = tk.Frame(transfer_box, bg=t["bg"])
    transfer_actions.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
    export_library_btn = tk.Button(
        transfer_actions, text="Export Lyrics Library", bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    import_library_btn = tk.Button(
        transfer_actions, text="Import Lyrics Library", bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    export_library_btn.pack(side="left", padx=(0, 8))
    import_library_btn.pack(side="left")

    def export_from_options():
        folder = transfer_dir_var.get().strip()
        if not folder:
            choose_transfer_folder()
            folder = transfer_dir_var.get().strip()
        if not folder:
            return
        config["lyrics_transfer_dir"] = folder
        save_config(config)
        start_export_lyrics_library(folder, parent=win, button=export_library_btn)

    def import_from_options():
        initial = transfer_dir_var.get().strip() or MUSIC_DIR or str(Path.home())
        package = filedialog.askopenfilename(
            parent=win, title="Select a Lyrics Library package", initialdir=initial,
            filetypes=[("Lyrics Library ZIP", "*.zip"), ("ZIP archives", "*.zip")])
        if package:
            config["lyrics_transfer_dir"] = str(Path(package).parent)
            transfer_dir_var.set(config["lyrics_transfer_dir"])
            save_config(config)
            start_import_lyrics_library(package, parent=win, button=import_library_btn)

    export_library_btn.configure(command=export_from_options)
    import_library_btn.configure(command=import_from_options)

    # ---------------- Lyrics sources and priority ----------------
    providers_box = tk.LabelFrame(
        content, text="Lyrics Sources & Priority", bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold"), padx=10, pady=8
    )
    providers_box.pack(fill="x", padx=4, pady=(2, 10))

    tk.Label(
        providers_box,
        text="Enable any provider you want and choose the order in which providers are tried.",
        bg=t["bg"], fg=t["fg"], justify="left", wraplength=900
    ).pack(anchor="w", pady=(0, 8))

    provider_area = tk.Frame(providers_box, bg=t["bg"])
    provider_area.pack(fill="x")
    left = tk.Frame(provider_area, bg=t["bg"])
    left.pack(side="left", fill="y", padx=(0, 18))
    mid = tk.Frame(provider_area, bg=t["bg"])
    mid.pack(side="left", fill="both", expand=True)

    plain_var = tk.BooleanVar(value=plain_ok)
    provider_vars = {}

    tk.Label(left, text="Enabled providers", bg=t["bg"], fg=t["fg"],
             font=("Segoe UI", 10, "bold")).pack(anchor="w")

    # This switch is required by the Genius plain-text workflow and is rarely
    # something a user should change, so its checkbox is shown later in the
    # Legacy / Advanced section instead of beside everyday provider controls.
    plain_cb = None

    genius_cb = None

    tk.Label(mid, text="Priority (top = tried first)", bg=t["bg"], fg=t["fg"],
             font=("Segoe UI", 10, "bold")).pack(anchor="w")

    pri_frame = tk.Frame(mid, bg=t["bg"])
    pri_frame.pack(fill="both", expand=True)
    pri_list = tk.Listbox(
        pri_frame, exportselection=False, height=6,
        bg=t["panel"], fg=t["fg"],
        selectbackground=t["sel_bg"], selectforeground=t["sel_fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    )
    pri_scroll = tk.Scrollbar(pri_frame, orient="vertical", command=pri_list.yview)
    pri_list.configure(yscrollcommand=pri_scroll.set)
    pri_list.pack(side="left", fill="both", expand=True)
    pri_scroll.pack(side="right", fill="y")

    def render_priority_list(select_index=None):
        pri_list.delete(0, tk.END)
        for p in order:
            is_enabled = bool(provider_vars[p].get())
            if p == "Genius" and not plain_var.get():
                is_enabled = False
            pri_list.insert(tk.END, f"✔ {p}" if is_enabled else f"✖ {p} (disabled)")
        if select_index is not None and 0 <= select_index < pri_list.size():
            pri_list.selection_set(select_index)
            pri_list.activate(select_index)

    def on_toggle_plain():
        if not plain_var.get() and "Genius" in provider_vars:
            provider_vars["Genius"].set(False)
        if genius_cb is not None:
            genius_cb.configure(state=("normal" if plain_var.get() else "disabled"))
        render_priority_list(pri_list.curselection()[0] if pri_list.curselection() else None)

    # plain_cb is created in the Legacy / Advanced section below.

    def on_toggle_provider(pname: str):
        if pname == "Genius" and provider_vars["Genius"].get() and not plain_var.get():
            plain_var.set(True)
            if genius_cb is not None:
                genius_cb.configure(state="normal")
        render_priority_list(pri_list.curselection()[0] if pri_list.curselection() else None)

    for p in ALL_PROVIDERS:
        v = tk.BooleanVar(value=enabled_map.get(p, True))
        provider_vars[p] = v
        cb = tk.Checkbutton(
            left, text=p, variable=v,
            bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
            activebackground=t["bg"], activeforeground=t["fg"],
            command=lambda name=p: on_toggle_provider(name)
        )
        cb.pack(anchor="w")
        if p == "Genius":
            genius_cb = cb

    if genius_cb is not None:
        genius_cb.configure(state=("normal" if plain_var.get() else "disabled"))
    if not plain_var.get():
        provider_vars["Genius"].set(False)

    render_priority_list(0 if order else None)

    pri_btns = tk.Frame(mid, bg=t["bg"])
    pri_btns.pack(fill="x", pady=(6, 0))

    def move_up():
        sel = pri_list.curselection()
        if not sel:
            return
        i = sel[0]
        if i == 0:
            return
        order[i - 1], order[i] = order[i], order[i - 1]
        render_priority_list(i - 1)

    def move_down():
        sel = pri_list.curselection()
        if not sel:
            return
        i = sel[0]
        if i >= len(order) - 1:
            return
        order[i + 1], order[i] = order[i], order[i + 1]
        render_priority_list(i + 1)

    tk.Button(
        pri_btns, text="Up", command=move_up, width=10,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left")
    tk.Button(
        pri_btns, text="Down", command=move_down, width=10,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left", padx=(8, 0))

    # ---------------- Output ----------------
    output_box = tk.LabelFrame(
        content, text="Output", bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold"), padx=10, pady=8
    )
    output_box.pack(fill="x", padx=4, pady=(0, 10))

    lyrics_folder_var = tk.BooleanVar(value=lyrics_subfolder)
    tk.Checkbutton(
        output_box, text='Store lyrics in a "LYRICS" folder next to the track',
        variable=lyrics_folder_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).grid(row=0, column=0, columnspan=3, sticky="w")

    tk.Label(output_box, text="Genius output format:", bg=t["bg"], fg=t["fg"]).grid(
        row=1, column=0, sticky="w", pady=(8, 0)
    )
    genius_format_var = tk.StringVar(value=genius_output_format)
    format_frame = tk.Frame(output_box, bg=t["bg"])
    format_frame.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
    tk.Radiobutton(
        format_frame, text=".txt  (recommended for Syncalong)", value="txt",
        variable=genius_format_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).pack(side="left")
    tk.Radiobutton(
        format_frame, text=".lrc", value="lrc",
        variable=genius_format_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).pack(side="left", padx=(14, 0))

    # ---------------- Genius API ----------------
    token_box = tk.LabelFrame(
        content, text="Genius API", bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold"), padx=10, pady=8
    )
    token_box.pack(fill="x", padx=4, pady=(0, 10))
    token_box.grid_columnconfigure(1, weight=1)

    tk.Label(token_box, text="Client Access Token:", bg=t["bg"], fg=t["fg"]).grid(
        row=0, column=0, sticky="w"
    )
    token_var = tk.StringVar(value=saved_genius_token)
    token_entry = tk.Entry(
        token_box, textvariable=token_var, width=54, show="•",
        bg=t["panel"], fg=t["fg"], insertbackground=t["fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    )
    token_entry.grid(row=0, column=1, sticky="we", padx=(10, 0))

    token_controls = tk.Frame(token_box, bg=t["bg"])
    token_controls.grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))
    show_token_var = tk.BooleanVar(value=False)

    def toggle_token_visibility():
        token_entry.configure(show="" if show_token_var.get() else "•")

    def save_token_now():
        value = token_var.get().strip()
        if not value:
            messagebox.showwarning("Genius API", "Enter a token first.", parent=win)
            return
        try:
            save_genius_api_token(value)
            messagebox.showinfo(
                "Genius API",
                "Token saved in your private per-user credentials folder.\n\n"
                f"{GENIUS_TOKEN_FILE}\n\n"
                "It is not stored in the program source or lyrics_gui_config.json.",
                parent=win,
            )
        except Exception as exc:
            messagebox.showerror("Genius API", f"Could not save token:\n{exc}", parent=win)

    def remove_token_now():
        if not get_genius_api_token():
            token_var.set("")
            return
        if messagebox.askyesno(
            "Genius API", "Remove the saved Genius API token from this computer?", parent=win
        ):
            delete_genius_api_token()
            token_var.set("")
            token_feedback.set("Saved token removed. Add a token before downloading.")

    def test_token_now():
        value = token_var.get().strip()
        token_feedback.set("Checking token… You can continue using this window.")
        def worker():
            ok, message = _test_genius_token_once(value)
            def finish():
                if win.winfo_exists() and value == token_var.get().strip():
                    token_feedback.set(("✓ " if ok else "") + message)
            ui_call(finish)
        threading.Thread(target=worker, daemon=True).start()

    token_feedback = tk.StringVar()
    tk.Label(token_box, textvariable=token_feedback, bg=t["bg"], fg=t["fg"],
             wraplength=750, justify="left").grid(row=3, column=0, columnspan=2, sticky="w")

    tk.Checkbutton(
        token_controls, text="Show token", variable=show_token_var,
        command=toggle_token_visibility,
        bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).pack(side="left")
    tk.Button(
        token_controls, text="Test Token", command=test_token_now, width=11,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left", padx=(8, 0))
    tk.Button(
        token_controls, text="Save Token", command=save_token_now, width=11,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left", padx=(8, 0))
    tk.Button(
        token_controls, text="Remove Token", command=remove_token_now, width=12,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left", padx=(8, 0))

    tk.Label(
        token_box,
        text="Stored separately per Windows user; distributing the program does not include your token.",
        bg=t["bg"], fg=t["fg"]
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))

    # ---------------- Genius cache ----------------
    cache_box = tk.LabelFrame(
        content, text="Genius Local Cache", bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold"), padx=10, pady=8
    )
    cache_box.pack(fill="x", padx=4, pady=(0, 10))

    cache_size_var = tk.StringVar()

    def refresh_cache_size():
        cache_size_var.set("Checking cache size…")
        def worker():
            size_bytes, lyrics_count = _genius_cache_stats()
            def finish():
                if win.winfo_exists():
                    cache_size_var.set(f"Size: {_format_cache_size(size_bytes)} • Lyrics cached: {lyrics_count}")
            ui_call(finish)
        threading.Thread(target=worker, daemon=True).start()

    def clear_cache_now():
        size_bytes, lyrics_count = _genius_cache_stats()
        if messagebox.askyesno(
            "Genius Cache",
            "Clear the complete Genius cache?\n\n"
            f"Current size: {_format_cache_size(size_bytes)}\n"
            f"Cached lyrics: {lyrics_count}\n\n"
            "This deletes saved song matches AND locally cached lyrics. "
            "Future downloads may need to search Genius and download the lyrics page again.",
            parent=win,
        ):
            clear_genius_match_cache()
            _GENIUS_SEARCH_CACHE.clear()
            refresh_cache_size()
            messagebox.showinfo("Genius Cache", "Complete Genius cache cleared.", parent=win)

    cache_row = tk.Frame(cache_box, bg=t["bg"])
    cache_row.pack(fill="x")
    tk.Button(
        cache_row, text="Clear Genius Cache", command=clear_cache_now, width=18,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="left")
    tk.Label(
        cache_row, textvariable=cache_size_var, bg=t["bg"], fg=t["fg"],
        font=("Segoe UI", 10, "bold")
    ).pack(side="left", padx=(12, 0))
    refresh_cache_size()
    tk.Label(
        cache_box, text=f"Cache location: {_cache_dir()}",
        bg=t["bg"], fg=t["fg"], wraplength=900, justify="left"
    ).pack(anchor="w", pady=(6, 0))

    # ---------------- Windows ----------------
    windows_box = tk.LabelFrame(content, text="Windows", bg=t["bg"], fg=t["fg"],
                                font=("Segoe UI", 10, "bold"), padx=10, pady=8)
    windows_box.pack(fill="x", padx=4, pady=(0, 10))
    tk.Label(windows_box, text=("Every window remembers where you put it and how big it is. If a window ended up "
                                "somewhere you cannot reach, reset: all remembered places are forgotten and the open "
                                "windows move to the middle of the screen."),
             bg=t["bg"], fg=t["fg"], wraplength=900, justify="left").pack(anchor="w")

    def reset_window_positions():
        moved = studio_windows.reset_all(root)
        config.pop("window_geometry", None)
        save_config(config)
        messagebox.showinfo("Reset window positions",
                            f"Forgot all remembered window places; {moved} open window(s) moved to the middle "
                            "of the screen.", parent=win)

    reset_windows_btn = tk.Button(windows_box, text="Reset window positions", command=reset_window_positions)
    reset_windows_btn.pack(anchor="w", pady=(6, 0))

    # ---------------- Backups ----------------
    build_backup_panel(content, t).pack(fill="x", padx=4, pady=(0, 10))

    # ---------------- Legacy / Advanced ----------------
    legacy_box = tk.LabelFrame(
        content, text="Legacy / Advanced Options — usually leave unchanged",
        bg=t["bg"], fg=t["fg"], font=("Segoe UI", 10, "bold"),
        padx=10, pady=8
    )
    legacy_box.pack(fill="x", padx=4, pady=(0, 10))

    tk.Label(
        legacy_box,
        text=(
            "These options are mainly for older automatic plain→synced behavior, "
            "language filtering and special downloaded-LRC cleanup. "
            "They normally do not need to be changed for Genius + cache + Syncalong."
        ),
        bg=t["bg"], fg=t["fg"], justify="left", wraplength=900
    ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

    # Required by the current Genius downloader. It remains editable for
    # compatibility with the old provider engine, but the wording makes the
    # dependency explicit. Enabling Genius above will automatically turn it on.
    plain_cb = tk.Checkbutton(
        legacy_box,
        text="Genius plain-lyrics support — REQUIRED for Genius downloads (keep enabled)",
        variable=plain_var, command=on_toggle_plain,
        bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    )
    plain_cb.grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 7))

    tk.Label(
        legacy_box,
        text="Disabling this also disables Genius. Leave it enabled unless you intentionally want only synced/legacy providers.",
        bg=t["bg"], fg=t["fg"], justify="left", wraplength=880
    ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))

    upgrade_var = tk.BooleanVar(value=config.get("upgrade_plain_to_synced", True))
    auto_upgrade_var = tk.BooleanVar(value=config.get("auto_upgrade_plain", False))

    tk.Checkbutton(
        legacy_box, text="Auto-upgrade plain → synced (prompt for each)",
        variable=upgrade_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))

    tk.Checkbutton(
        legacy_box, text="Auto-upgrade plain → synced (no prompt)",
        variable=auto_upgrade_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))

    tk.Label(legacy_box, text="Lyrics Language:", bg=t["bg"], fg=t["fg"]).grid(
        row=7, column=0, sticky="w", pady=(9, 0)
    )
    lang_var = tk.StringVar(value=lang)
    tk.Entry(
        legacy_box, textvariable=lang_var, width=7,
        bg=t["panel"], fg=t["fg"], insertbackground=t["fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    ).grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(9, 0))
    tk.Label(
        legacy_box, text="(en, es, fr… or blank = auto)",
        bg=t["bg"], fg=t["fg"]
    ).grid(row=5, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(9, 0))

    strip_var = tk.BooleanVar(value=strip_cjk)
    tk.Checkbutton(
        legacy_box, text="Strip CJK lines from downloaded LRC",
        variable=strip_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(7, 0))

    rej_var = tk.BooleanVar(value=reject_non_ascii)
    tk.Checkbutton(
        legacy_box, text="Reject lyrics files that are mostly non-ASCII",
        variable=rej_var, bg=t["bg"], fg=t["fg"], selectcolor=t["bg"],
        activebackground=t["bg"], activeforeground=t["fg"]
    ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(5, 0))

    tk.Label(
        legacy_box, text="Reject threshold (0.05–0.50):",
        bg=t["bg"], fg=t["fg"]
    ).grid(row=8, column=0, sticky="w", pady=(5, 0))
    ratio_var = tk.StringVar(value=str(ratio))
    tk.Entry(
        legacy_box, textvariable=ratio_var, width=8,
        bg=t["panel"], fg=t["fg"], insertbackground=t["fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    ).grid(row=8, column=1, sticky="w", padx=(8, 0), pady=(5, 0))

    # ---------------- Footer ----------------
    footer = tk.Frame(win, bg=t["bg"])
    footer.pack(fill="x", padx=12, pady=(4, 12))

    def preview_developer_mode():
        if developer_var.get():
            legacy_box.pack(fill="x", padx=4, pady=(0, 10))
        else:
            legacy_box.pack_forget()
    developer_toggle.configure(command=preview_developer_mode)
    preview_developer_mode()

    def on_save():
        if (str(export_library_btn.cget("state")) == "disabled" or
                str(import_library_btn.cget("state")) == "disabled"):
            messagebox.showinfo("Options", "Wait for the Lyrics Library operation to finish first.", parent=win)
            return
        try:
            studio_preferences.set_developer_mode(developer_var.get())
        except OSError as exc:
            messagebox.showerror("Options", f"Could not save Developer mode: {exc}", parent=win)
            return
        fixed, seen = [], set()
        for p in order:
            if p in ALL_PROVIDERS and p not in seen:
                fixed.append(p)
                seen.add(p)
        for p in ALL_PROVIDERS:
            if p not in seen:
                fixed.append(p)

        try:
            r = float(ratio_var.get())
            r = max(0.05, min(0.50, r))
        except Exception:
            r = 0.15

        plain = bool(plain_var.get())
        if not plain:
            provider_vars["Genius"].set(False)

        config["allow_plain_fallback"] = plain
        config["upgrade_plain_to_synced"] = bool(upgrade_var.get())
        config["auto_upgrade_plain"] = bool(auto_upgrade_var.get())
        config["providers_enabled"] = {p: bool(provider_vars[p].get()) for p in ALL_PROVIDERS}
        config["providers_order"] = fixed
        config["lang"] = lang_var.get()
        config["strip_cjk"] = bool(strip_var.get())
        config["reject_non_ascii"] = bool(rej_var.get())
        config["reject_non_ascii_ratio"] = r
        config["lyrics_subfolder"] = bool(lyrics_folder_var.get())
        config["lyrics_transfer_dir"] = transfer_dir_var.get().strip()
        config["genius_output_format"] = (
            genius_format_var.get() if genius_format_var.get() in ("lrc", "txt") else "txt"
        )

        token_value = token_var.get().strip()
        if token_value:
            save_genius_api_token(token_value)
        else:
            delete_genius_api_token()

        save_config(config)
        lyrics_cache.clear()
        missing_targets.clear()
        try:
            canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        win.destroy()
        load_artists()

    def close_options():
        if (str(export_library_btn.cget("state")) == "disabled" or
                str(import_library_btn.cget("state")) == "disabled"):
            messagebox.showinfo("Options", "Wait for the Lyrics Library operation to finish first.", parent=win)
            return
        try:
            canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", close_options)

    tk.Label(
        footer,
        text="Save applies settings and Developer mode",
        bg=t["bg"], fg=t["fg"]
    ).pack(side="left")
    tk.Button(
        footer, text="Cancel", command=close_options, width=10,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="right")
    tk.Button(
        footer, text="Save", command=on_save, width=10,
        bg=t["btn_bg"], fg=t["btn_fg"],
        activebackground=t["btn_bg"], activeforeground=t["btn_fg"]
    ).pack(side="right", padx=(0, 8))


# ------------------ Selection helpers ------------------

def select_all_in_listbox(lb: tk.Listbox):
    if lb.size() > 0:
        lb.selection_set(0, tk.END)
        lb.activate(0)


def clear_all_selections():
    global missing_targets
    artist_list.selection_clear(0, tk.END)
    if "album_list" in globals():
        album_list.selection_clear(0, tk.END)
    track_list.selection_clear(0, tk.END)
    missing_targets = []
    missing_scan_btn.configure(text="Find & Download Missing Lyrics TXT [0]")
    set_status("Selection cleared")
    
    # Reset all toggle buttons to "Select All"
    artist_select_btn.configure(text="Select All", command=toggle_artist_selection)
    if "album_select_btn" in globals():
        album_select_btn.configure(text="Select All", command=toggle_album_selection)
    track_select_btn.configure(text="Select All", command=toggle_track_selection)
    if 'track_selected_count_label' in globals():
        update_track_selection_counter()


def toggle_artist_selection():
    if artist_list.size() == 0:
        # No items to select
        return
    
    # Check if ALL items are selected
    all_selected = (len(artist_list.curselection()) == artist_list.size())
    
    if all_selected:
        # All selected - clear everything
        artist_list.selection_clear(0, tk.END)
        artist_select_btn.configure(text="Select All", command=toggle_artist_selection)
        on_artist_select(None)
    else:
        # Not all selected (even if some are) - select all
        select_all_in_listbox(artist_list)
        artist_select_btn.configure(text="Clear All", command=toggle_artist_selection)
        on_artist_select(None)


def toggle_album_selection():
    """Select/Clear all album folders. No album selection means show all."""
    if album_list.size() == 0:
        return

    all_selected = (len(album_list.curselection()) == album_list.size())
    if all_selected:
        album_list.selection_clear(0, tk.END)
        album_select_btn.configure(text="Select All")
    else:
        select_all_in_listbox(album_list)
        album_select_btn.configure(text="Clear All")

    on_album_select(None)


def toggle_track_selection():
    if track_list.size() == 0:
        # No items to select
        return
    
    # Check if ALL items are selected
    all_selected = (len(track_list.curselection()) == track_list.size())
    
    if all_selected:
        # All selected - clear everything
        track_list.selection_clear(0, tk.END)
        track_select_btn.configure(text="Select All", command=toggle_track_selection)
    else:
        # Not all selected (even if some are) - select all
        select_all_in_listbox(track_list)
        track_select_btn.configure(text="Clear All", command=toggle_track_selection)
    if 'track_selected_count_label' in globals():
        on_track_select(None)


def bind_ctrl_a(lb: tk.Listbox):
    def _ctrl_a(event=None):
        select_all_in_listbox(lb)
        if lb is album_list:
            on_album_select(None)
        elif lb is track_list and 'track_selected_count_label' in globals():
            on_track_select(None)
        return "break"

    lb.bind("<Control-a>", _ctrl_a)
    lb.bind("<Control-A>", _ctrl_a)


# ------------------ Theme apply ------------------

def apply_theme(theme_name: str):
    t = THEMES[theme_name]

    root.configure(bg=t["bg"])
    top.configure(bg=t["bg"])
    help_hint.configure(bg=t["bg"], fg=t["fg"])
    hint_row.configure(bg=t["bg"])
    hover_hint.configure(bg=t["bg"], fg="#ffcf70" if theme_name == "dark" else "#8a5a00")
    app_heading.configure(bg=t["bg"], fg="#858b95" if theme_name == "dark" else "#68707c")
    main.configure(bg=t["bg"])
    bottom.configure(bg=t["bg"])
    list_frame.configure(bg=t["bg"])
    btn_row.configure(bg=t["bg"])
    legend_row.configure(bg=t["bg"])
    for widget in (log_header, log_title):
        widget.configure(bg=t["bg"], fg=t["fg"]) if widget is log_title else widget.configure(bg=t["bg"])
    log_toggle_btn.configure(bg=t["bg"], fg=t["fg"], activebackground=t["bg"], activeforeground=t["fg"])
    status_bar.configure(bg=t["status_bg"])
    
    # Theme list cards
    for card in (artist_card, album_card, track_card):
        card.configure(bg=t["bg"], highlightbackground=t["border"], highlightcolor=t["border"])
    
    # Theme card headers
    for header in (artist_header, album_header, track_header):
        header.configure(bg=t["panel"])
    
    # Theme title labels
    for title_lbl in (artist_title, album_title, track_title):
        title_lbl.configure(bg=t["panel"], fg=t["fg"])
    track_selected_count_label.configure(bg=t["panel"], fg=t["fg"])
    if "track_sort_label" in globals():
        track_sort_label.configure(bg=t["panel"], fg=t["fg"])
    if "track_sort_menu" in globals():
        track_sort_menu.configure(bg=t["btn_bg"], fg=t["btn_fg"],
                                  activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
        try:
            track_sort_menu["menu"].configure(bg=t["panel"], fg=t["fg"],
                                              activebackground=t["sel_bg"], activeforeground=t["sel_fg"])
        except Exception:
            pass
    
    # Theme Select All buttons
    for btn in (artist_select_btn, album_select_btn, track_select_btn):
        btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], 
                     activebackground=t["btn_bg"], activeforeground=t["btn_fg"])

    open_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    
    # Theme search widgets in top bar
    search_label.configure(bg=t["bg"], fg=t["fg"])
    search_entry.configure(
        bg=t["panel"], fg=t["fg"],
        insertbackground=t["fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    )
    
    artist_dl_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    track_dl_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    syncalong_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    builder_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    cancel_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    pause_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    edit_lyrics_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    play_song_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    transcription_row.configure(bg=t["bg"])
    for button in (rename_song_btn, remove_embedded_btn, reference_btn, delete_reference_btn,
                   track_refresh_btn, track_columns_btn, shared_lrc_btn, repair_lyrics_btn, embed_lrc_btn, manual_embed_btn,
                   cleanup_backups_btn, fix_synced_btn, fix_synced_repair_btn, year_comment_btn, tracker_dates_btn, duplicates_btn, bracket_lines_btn, swap_song_btn, edit_meta_btn):
        button.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    transcription_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    missing_scan_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    clear_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    delete_lyrics_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    delete_synced_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    undo_lyrics_btn.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_bg"], activeforeground=t["btn_fg"])
    context_row.configure(bg=t["bg"])
    scope_label.configure(bg=t["bg"], fg=t["fg"])
    selected_path_entry.configure(bg=t["bg"], readonlybackground=t["bg"], fg=t["fg"], relief="flat")
    for frame in (track_actions_row, operation_controls):
        frame.configure(bg=t["bg"])
    for label in action_group_labels:
        label.configure(bg=t["bg"], fg=t["fg"])
    operation_label.configure(bg=t["bg"], fg=t["fg"])
    for button in studio_action_buttons:
        button.configure(relief="flat", bd=0, font=("Segoe UI", 9),
                         padx=10, pady=6, cursor="hand2",
                         activebackground=t["sel_bg"], activeforeground=t["sel_fg"])
    for button in globals().get("MENU_BUTTONS", ()):
        button.configure(bg=t["btn_bg"], fg=t["btn_fg"], disabledforeground=t.get("muted", "#777777"))
        button.menu.configure(bg=t["panel"], fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["sel_fg"],
                              bd=0)
    # the main action of the window stands out
    track_dl_btn.configure(bg=t["sel_bg"], fg=t["sel_fg"], font=("Segoe UI", 9, "bold"))
    action_panel.apply_theme(t)

    for lb in (artist_list, album_list, track_list):
        lb.configure(
            bg=t["panel"], fg=t["fg"],
            selectbackground=t["sel_bg"], selectforeground=t["sel_fg"],
            highlightbackground=t["border"], highlightcolor=t["border"]
        )

    track_list.style_headings(t["btn_bg"], t["btn_fg"])
    log_box.configure(
        bg=t["log_bg"], fg=t["log_fg"],
        insertbackground=t["log_fg"],
        highlightbackground=t["border"], highlightcolor=t["border"]
    )

    status_label.configure(bg=t["status_bg"], fg=t["status_fg"])

    legend_frame.configure(bg=t["legend_bg"], highlightbackground=t["legend_border"], highlightthickness=1)
    legend_title.configure(bg=t["legend_bg"], fg=t["fg"])
    legend_track_synced.configure(bg=t["legend_bg"], fg=t["ok"])
    legend_track_transcription.configure(bg=t["legend_bg"], fg=t["warn"])
    legend_track_none.configure(bg=t["legend_bg"], fg=t["none"])

    set_titlebar_theme(theme_name == "dark")

    config["theme"] = theme_name
    save_config(config)


# ------------------ Syncalong Lyrics Sync ------------------

SYNCALONG_MODELS = [
    ("tiny — ~1 GB VRAM", "tiny", 1.0),
    ("tiny.en — ~1 GB VRAM (English)", "tiny.en", 1.0),
    ("base — ~1 GB VRAM", "base", 1.0),
    ("base.en — ~1 GB VRAM (English)", "base.en", 1.0),
    ("small — ~2 GB VRAM", "small", 2.0),
    ("small.en — ~2 GB VRAM (English)", "small.en", 2.0),
    ("medium — ~5 GB VRAM", "medium", 5.0),
    ("medium.en — ~5 GB VRAM (English)", "medium.en", 5.0),
    ("turbo — ~6 GB VRAM (recommended)", "turbo", 6.0),
    ("large-v3 — ~10 GB VRAM", "large-v3", 10.0),
]
SYNCALONG_MODEL_BY_LABEL = {label: (name, vram) for label, name, vram in SYNCALONG_MODELS}
SYNCALONG_DEFAULT_MODEL_LABEL = next(label for label, name, _ in SYNCALONG_MODELS if name == "turbo")
_SYNC_GPU_INFO_CACHE = None


def _syncalong_gpu_info():
    """Return NVIDIA and PyTorch CUDA status from a fresh Python process."""
    info = {
        "torch": False,
        "nvidia": False,
        "cuda": False,
        "cuda_build": None,
        "gpu": "Not detected",
        "vram_gb": 0.0,
        "detail": "PyTorch not available",
    }
    try:
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            probe = subprocess.run(
                [nvidia_smi, "--list-gpus"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            info["nvidia"] = probe.returncode == 0 and bool(probe.stdout.strip())
    except Exception:
        pass
    try:
        probe_code = (
            "import json\n"
            "try:\n"
            " import torch\n"
            " ok=bool(torch.cuda.is_available())\n"
            " p=torch.cuda.get_device_properties(torch.cuda.current_device()) if ok else None\n"
            " print(json.dumps({'torch':True,'cuda':ok,'cuda_build':torch.version.cuda,"
            "'gpu':torch.cuda.get_device_name(0) if ok else '',"
            "'vram_gb':float(p.total_memory)/(1024**3) if p else 0.0}))\n"
            "except Exception as e:\n"
            " print(json.dumps({'torch':False,'error':str(e)}))\n"
        )
        probe = subprocess.run(
            [sys.executable, "-c", probe_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(probe.stdout.strip().splitlines()[-1])
        info.update({key: data[key] for key in ("torch", "cuda", "cuda_build", "gpu", "vram_gb") if key in data})
        if info["cuda"]:
            info["detail"] = f"CUDA {info['cuda_build']} available • {info['vram_gb']:.1f} GB VRAM"
        elif info["nvidia"] and info["torch"]:
            info["detail"] = "NVIDIA detected • installed PyTorch is CPU-only"
        elif info["nvidia"]:
            info["detail"] = "NVIDIA detected • PyTorch is not available"
        else:
            info["detail"] = "CUDA unavailable — syncalong will use CPU"
    except Exception as e:
        info["detail"] = f"PyTorch/CUDA check unavailable: {e}"
    return info


def _find_plain_lyrics_for_audio(audio_path: str) -> str:
    """Lyrics text for a single-track sync: the plain TXT first, an LRC only as a fallback.

    The TXT lookup is shared with batch jobs; an LRC is used only when no TXT
    exists (its timestamps are stripped before alignment).
    """
    txt = _find_plain_txt_for_audio(audio_path)
    if txt:
        return txt
    p = Path(audio_path)
    candidates = []
    try:
        candidates.append(Path(get_lyrics_path(str(p), ".lrc", ensure_dir=False)))
    except Exception:
        pass
    candidates.extend([p.with_suffix(".lrc"), p.parent / "LYRICS" / (p.stem + ".lrc")])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _find_plain_txt_for_audio(audio_path: str) -> str:
    """Find matching plain .txt lyrics for batch Syncalong jobs."""
    p = Path(audio_path)
    candidates = []
    try:
        candidates.append(Path(get_lyrics_path(str(p), ".txt", ensure_dir=False)))
    except Exception:
        pass
    candidates.extend([
        p.with_suffix(".txt"),
        p.parent / "LYRICS" / (p.stem + ".txt"),
    ])
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return str(candidate)
    return ""


def _prepare_syncalong_lyrics_text(lyrics_path: str) -> str:
    """Read lyrics and remove Triplenine metadata/footer before alignment.

    Genius downloads created by this app may end with Match accuracy and the
    Genius URL. Those lines are useful provenance, but they are not sung and
    should never be fed to Syncalong. Existing LRC timestamps are also stripped
    when a user deliberately chooses an .lrc as the input text.
    """
    text = Path(lyrics_path).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"(?i)match accuracy:\s*\d{1,3}%", stripped):
            continue
        if re.match(r"(?i)^https?://(?:www\.)?genius\.com/", stripped):
            continue
        # Provenance footer written by _save_genius_result; never sung.
        if re.fullmatch(r"(?i)source (?:selected manually|confirmed) by user", stripped):
            continue
        # If an existing LRC is selected, make it plain text for a fresh sync.
        line = re.sub(r"^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*", "", line)
        cleaned.append(line)
    return "\n".join(cleaned).strip() + "\n"


def _parse_sync_lyrics(lyrics_module, text):
    """Syncalong's parse_lyrics_text, but a sung line that starts and ends with brackets stays a lyric.

    Syncalong treats every line that starts with "(" and ends with ")" as a section header, so
    "(Yeah) Bad bitches, fuck 'em then dismiss 'em (Bad, woo)" was left out of the LRC. Lines made only of
    brackets — "[Chorus]", "(Cha-ching, cha-ching, brrt, hello?)" — still stay out, unless Lyrics Sync's
    "Allow sync of lines only in brackets" is on: then only real section headers ("[Chorus]", "(Verse 2)") do."""
    lines = lyrics_module.parse_lyrics_text(text)
    textnorm = importlib.import_module("syncalong.textnorm")
    fixed = []
    for line in lines:
        raw = line.raw.strip()
        allow_brackets = bool(config.get("sync_bracket_only_lines", False))
        skip = studio_resync.is_section_name(raw) if allow_brackets else studio_resync.is_section_header(raw)
        if line.is_blank and raw and not skip:
            words = (textnorm.normalize(raw) or "").split()
            line = lyrics_module.LyricLine(index=line.index, raw=raw, words=words, is_blank=not words)
        fixed.append(line)
    return fixed


_LRC_TIMED_TEXT_RE = re.compile(
    r"^\s*(\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\])\s*(.*?)\s*$"
)


def _lrc_timestamp_milliseconds(minutes: str, seconds: str, fraction: str = "") -> int:
    """Return one canonical value for equivalent LRC timestamp spellings."""
    fraction = (fraction or "")[:3].ljust(3, "0")
    return (int(minutes) * 60 + int(seconds)) * 1000 + int(fraction or 0)


ENDS_EARLY_MIN_GAP = 30   # seconds the song may go on after the last lyric line before it is worth a look
ENDS_EARLY_ERROR_GAP = 60  # ...and from this gap it counts as a real error


def _find_lrc_ends_early(text: str, lrc_path) -> list[dict]:
    """The lyrics stop (last timed line) while the song goes on for 30+ s: a possible cut-off sync."""
    try:
        audio = studio_resync.song_for_lrc(lrc_path)
        length = studio_resync.song_length(audio) if audio else None
        if not length:
            return []
        last = None
        for file_line, raw in enumerate((text or "").splitlines(), 1):
            match = _LRC_TIMED_TEXT_RE.match(raw)
            if match and match.group(5).strip():
                last = (file_line, match)
        if not last:
            return []
        file_line, match = last
        start_ms = _lrc_timestamp_milliseconds(match.group(2), match.group(3), match.group(4) or "")
        gap = length - start_ms / 1000
        if gap < ENDS_EARLY_MIN_GAP:
            return []
        return [{
            "kind": "ends_early", "severity": "error" if gap >= ENDS_EARLY_ERROR_GAP else "minor",
            "timestamp": match.group(1), "time_ms": start_ms, "count": max(1, int(gap // 10)),
            "first_file_line": file_line, "last_file_line": file_line, "at_start": False,
            "first_text": match.group(5).strip(), "gap_seconds": round(gap, 1), "duration_seconds": round(length, 1),
        }]
    except Exception:
        return []


def _find_lrc_runs_for_file(lrc_path, text: str) -> list[dict]:
    """Every scan warning of one LRC: the timing runs plus 'lyrics end while the song goes on'."""
    return _find_lrc_duplicate_timestamp_runs(text) + _find_lrc_ends_early(text, lrc_path)


def _find_lrc_duplicate_timestamp_runs(text: str, min_run: int = 2) -> list[dict]:
    """Find repeated timestamps, implausibly dense runs and guessed (evenly spaced) lines.

    Empty timed lines and section spacing are ignored. Reported are:
    * SAME TIME: two or more lines on one timestamp (always an error);
    * DENSE: lines less than 0.8 s apart that would need 9+ words per second
      (short lines inside such a run no longer split it). An error when 4+
      lines hold 24+ words, or 3+ lines need 13+ words per second — nobody
      raps that fast; otherwise a potential smaller error;
    * GUESSED: 5+ lines whose starts are exactly evenly spaced. Syncalong
      fills lines Whisper did not hear with linearly interpolated times, so
      such a run was guessed, not heard. An error when the step is under 1 s
      or the run has 8+ lines; otherwise a potential smaller error.
    Ad-libs in parentheses are not counted as words: they are usually
    background vocals sung over the main line. When the line after a flagged
    passage comes more than 4 s later, the next two lines join the passage.
    """
    timed_lyrics = []
    for file_line, raw in enumerate((text or "").splitlines(), 1):
        match = _LRC_TIMED_TEXT_RE.match(raw)
        if not match:
            continue
        lyric = match.group(5).strip()
        if not lyric:
            continue
        timed_lyrics.append({
            "timestamp": match.group(1),
            "time_ms": _lrc_timestamp_milliseconds(
                match.group(2), match.group(3), match.group(4) or ""
            ),
            "file_line": file_line,
            "text": lyric,
        })

    failures = []
    start = 0
    while start < len(timed_lyrics):
        end = start + 1
        while (end < len(timed_lyrics)
               and timed_lyrics[end]["time_ms"] == timed_lyrics[start]["time_ms"]):
            end += 1
        count = end - start
        if count >= max(2, int(min_run)):
            failures.append({
                "severity": "error",  # identical timestamps always count as errors
                "timestamp": timed_lyrics[start]["timestamp"],
                "time_ms": timed_lyrics[start]["time_ms"],
                "count": count,
                "first_file_line": timed_lyrics[start]["file_line"],
                "last_file_line": timed_lyrics[end - 1]["file_line"],
                "at_start": start == 0,
                "first_text": timed_lyrics[start]["text"],
            })
        start = end
    # LRC has start times only: count words before the final start, not words
    # in the final line (whose duration is unknown). Ad-libs are excluded.
    word_counts = [len(re.findall(r"\b\w+(?:['’]\w+)*\b", re.sub(r"\([^)]*\)", " ", line["text"])))
                   for line in timed_lyrics]

    def overlaps(first, last):
        return any(item["first_file_line"] <= timed_lyrics[last]["file_line"]
                   and item["last_file_line"] >= timed_lyrics[first]["file_line"] for item in failures)

    def run_fields(first, last):
        return {
            "timestamp": timed_lyrics[first]["timestamp"],
            "end_timestamp": timed_lyrics[last]["timestamp"],
            "time_ms": timed_lyrics[first]["time_ms"],
            "span_seconds": (timed_lyrics[last]["time_ms"] - timed_lyrics[first]["time_ms"]) / 1000,
            "count": last - first + 1,
            "first_file_line": timed_lyrics[first]["file_line"],
            "last_file_line": timed_lyrics[last]["file_line"],
            "at_start": first == 0,
            "first_text": timed_lyrics[first]["text"],
        }

    start = 0
    while start < len(timed_lyrics):
        end = start + 1
        while (end < len(timed_lyrics)
               and 0 <= timed_lyrics[end]["time_ms"] - timed_lyrics[end - 1]["time_ms"] <= 800):
            end += 1
        # Trim short lines at the edges; inside the run they no longer split it.
        first, last = start, end - 1
        while first < last and word_counts[first] < 5:
            first += 1
        while last > first and word_counts[last] < 5:
            last -= 1
        span_ms = timed_lyrics[last]["time_ms"] - timed_lyrics[first]["time_ms"]
        words = sum(word_counts[first:last])
        long_lines = sum(1 for count in word_counts[first:last + 1] if count >= 5)
        rate = words * 1000 / span_ms if span_ms > 0 else 0
        if last > first and long_lines >= 2 and words >= 5 and rate >= 9 and not overlaps(first, last):
            count = last - first + 1
            failures.append(dict(
                run_fields(first, last), kind="dense_lines",
                severity="error" if (count >= 4 and words >= 24) or (count >= 3 and rate >= 13) else "minor"))
        start = end
    # Evenly spaced starts (within 15 ms) are Syncalong's interpolation of lines it did not hear.
    start = 0
    while start + 4 < len(timed_lyrics):
        step = timed_lyrics[start + 1]["time_ms"] - timed_lyrics[start]["time_ms"]
        end = start + 1
        while (end + 1 < len(timed_lyrics) and 0 < step <= 6000
               and abs(timed_lyrics[end + 1]["time_ms"] - timed_lyrics[end]["time_ms"] - step) <= 15):
            end += 1
        count = end - start + 1
        if count >= 5 and 0 < step <= 6000:
            inside = any(item["first_file_line"] <= timed_lyrics[start]["file_line"]
                         and item["last_file_line"] >= timed_lyrics[end]["file_line"] for item in failures)
            if not inside:  # already fully covered by a SAME TIME / DENSE warning
                failures.append(dict(
                    run_fields(start, end), kind="even_spacing", step_seconds=step / 1000,
                    severity="error" if step < 1000 or count >= 8 else "minor"))
            start = end
        else:
            start += 1
    failures.sort(key=lambda item: item["first_file_line"])
    # A broken passage often drags the lines after it along: when the next line comes more than
    # 4 s after the flagged ones, the next two lines are flagged with the passage (just in case).
    position = {line["file_line"]: i for i, line in enumerate(timed_lyrics)}
    for k, item in enumerate(failures):
        last = position.get(item["last_file_line"])
        if last is None or last + 1 >= len(timed_lyrics):
            continue
        if timed_lyrics[last + 1]["time_ms"] - timed_lyrics[last]["time_ms"] <= 4000:
            continue
        end = min(last + 2, len(timed_lyrics) - 1)
        if k + 1 < len(failures):  # never run into the next warning
            next_first = position.get(failures[k + 1]["first_file_line"], len(timed_lyrics))
            end = min(end, next_first - 1)
        if end <= last:
            continue
        item["extended_lines"] = end - last
        item["count"] += end - last
        item["last_file_line"] = timed_lyrics[end]["file_line"]
        item["end_timestamp"] = timed_lyrics[end]["timestamp"]
    return failures


def _lrc_timed_entries(lrc_text: str) -> list[dict]:
    """Timestamped, non-empty LRC lyric lines with their 1-based file line."""
    entries = []
    for file_line, raw in enumerate((lrc_text or "").splitlines(), 1):
        match = _LRC_TIMED_TEXT_RE.match(raw)
        if match and match.group(5).strip():
            entries.append({
                "file_line": file_line,
                "time_ms": _lrc_timestamp_milliseconds(match.group(2), match.group(3), match.group(4) or ""),
                "text": match.group(5).strip(),
            })
    return entries


def _replace_lrc_line_times(lrc_text: str, new_times: dict) -> str:
    """Replace only the timestamps of the given file lines; text and other lines stay byte-identical."""
    lines = (lrc_text or "").splitlines(keepends=True)
    for file_line, seconds in new_times.items():
        original = lines[file_line - 1]
        content = original.rstrip("\r\n")
        match = _LRC_TIMED_TEXT_RE.match(content)
        if not match:
            raise ValueError(f"LRC line {file_line} has no timestamp.")
        total_cs = max(0, int(round(seconds * 100)))
        stamp = f"[{total_cs // 6000:02d}:{(total_cs % 6000) // 100:02d}.{total_cs % 100:02d}]"
        lines[file_line - 1] = content[:match.start(1)] + stamp + content[match.end(1):] + original[len(content):]
    return "".join(lines)


def _lrc_repeated_block_candidate(entries: list[dict], start: int, end: int):
    """Copy timing from an earlier identical block, e.g. a repeated chorus.

    ``entries[start:end]`` are the lines to fix; ``entries[start - 1]`` is the
    last good line (anchor). The anchor plus the lines to fix must match an
    earlier run of lines (ad-libs in parentheses ignored). Returns
    (times_in_seconds, first_file_line_of_source_block) or None.
    """
    if start <= 0 or end <= start:
        return None

    def norm(text):
        return _norm_match_text(re.sub(r"\([^)]*\)", " ", text))

    anchor = start - 1
    target = [norm(entry["text"]) for entry in entries[anchor:end]]
    source = None
    for j in range(0, anchor - len(target) + 1):
        if all(norm(entries[j + k]["text"]) == target[k] for k in range(len(target))):
            block = [entries[j + k]["time_ms"] for k in range(len(target))]
            if all(b > a for a, b in zip(block, block[1:])):
                source = j  # the latest earlier repetition wins
    if source is None:
        return None
    base = entries[source]["time_ms"]
    anchor_ms = entries[anchor]["time_ms"]
    times = [(anchor_ms + entries[source + k]["time_ms"] - base) / 1000 for k in range(1, len(target))]
    return times, entries[source]["file_line"]


def _lrc_later_repeat_candidate(entries: list[dict], start: int, end: int, region_start: float):
    """Copy timing backwards from a LATER identical block (e.g. the first chorus).

    ``entries[end]`` is the first good line after the selection. The longest
    suffix of the selection (at least 3 lines) that, together with that good
    line, repeats later in the song is timed from that repetition. Leading
    selected lines not covered (producer tags, intros) are spread evenly
    between ``region_start`` and the first copied line. Returns
    (times, source_file_line, copied_count) or None.
    """
    if end >= len(entries) or end <= start:
        return None

    def norm(text):
        return _norm_match_text(re.sub(r"\([^)]*\)", " ", text))

    texts = [norm(entry["text"]) for entry in entries]
    for suffix_start in range(start, end - 2):
        target = texts[suffix_start:end + 1]
        size = len(target)
        for j in range(end + 1, len(entries) - size + 1):
            if texts[j:j + size] != target:
                continue
            block = [entries[j + k]["time_ms"] for k in range(size)]
            if not all(b > a for a, b in zip(block, block[1:])):
                continue
            anchor_ms = entries[end]["time_ms"]
            copied = [(anchor_ms - (block[-1] - block[k])) / 1000 for k in range(size - 1)]
            lead = suffix_start - start
            if copied[0] <= region_start:
                return None
            step = (copied[0] - region_start) / (lead + 1)
            leading = [region_start + step * (k + 1) for k in range(lead)]
            return leading + copied, entries[j]["file_line"], size - 1
    return None


# Words that appear in almost every line and every transcript. A lyric line is
# never pinned to a time by one of these alone (e.g. "I" heard in an intro tag).
_WEAK_MATCH_WORDS = {
    "a", "an", "the", "i", "im", "id", "ill", "ive", "me", "my", "you", "your", "yo", "we", "us",
    "he", "she", "it", "its", "they", "them", "to", "of", "on", "in", "at", "by", "for", "with",
    "and", "or", "but", "so", "is", "am", "are", "be", "do", "no", "not", "oh", "uh", "yeah",
    "ya", "ay", "ayy", "that", "this", "up", "all", "got", "get", "like", "just", "what",
}


def _strong_line_times(flat_words, word_lines, mapping, words, line_count, region_start, region_end):
    """Line start times from reliable word matches only, interpolating the rest.

    A lyric word counts when it is a content word (3+ letters, not in
    ``_WEAK_MATCH_WORDS``) or when a neighbouring lyric word matched the
    neighbouring heard word too. Returns (times, anchored_line_count).
    """
    anchors = {}
    for lyric_index, heard_index in sorted(mapping.items()):
        word = _norm_match_text(str(flat_words[lyric_index])).replace(" ", "")
        chained = (mapping.get(lyric_index - 1) == heard_index - 1
                   or mapping.get(lyric_index + 1) == heard_index + 1)
        if (len(word) >= 3 and word not in _WEAK_MATCH_WORDS) or chained:
            line = word_lines[lyric_index]
            anchors.setdefault(line, words[heard_index].start)
    ordered = {}
    last = region_start - 0.01
    for line in sorted(anchors):
        if anchors[line] > last + 0.05:
            ordered[line] = anchors[line]
            last = anchors[line]
    if not ordered:
        return None, 0
    known = sorted(ordered)
    gaps = [(ordered[b] - ordered[a]) / (b - a) for a, b in zip(known, known[1:])]
    step = min(4.0, max(1.2, sorted(gaps)[len(gaps) // 2])) if gaps else 2.0
    times = [None] * line_count
    for line, time in ordered.items():
        times[line] = time
    first, final = known[0], known[-1]
    for line in range(first):
        times[line] = max(region_start + 0.01 * (line + 1), ordered[first] - step * (first - line))
    for line in range(final + 1, line_count):
        times[line] = min(region_end - 0.01 * (line_count - line), ordered[final] + step * (line - final))
    for a, b in zip(known, known[1:]):
        for line in range(a + 1, b):
            times[line] = ordered[a] + (ordered[b] - ordered[a]) * (line - a) / (b - a)
    return times, len(ordered)


def _retry_lrc_line_range(lrc_text: str, first_line: int, last_line: int, audio_end: float,
                          threshold: int, transcribe, progress=None, prompt_modes=(True, False),
                          extra_methods=True) -> dict:
    """Propose new timings for LRC file lines ``first_line..last_line`` only.

    ``prompt_modes`` chooses the Whisper passes (lyrics prompt ON and/or OFF);
    ``extra_methods`` adds copies of identical repeats (earlier / later in the song).

    ``transcribe(clip_start, clip_end, prompt)`` returns Whisper words with
    absolute times. Lines outside the range are never used as outputs; the
    previous and next timed lines only bound the search window. Returns
    {"lines": [...], "region": (start, end), "candidates": [...], "notes": [...]}.
    """
    align_module = importlib.import_module("syncalong.align")
    lyrics_module = importlib.import_module("syncalong.lyrics")
    entries = _lrc_timed_entries(lrc_text)
    indices = [i for i, entry in enumerate(entries) if first_line <= entry["file_line"] <= last_line]
    if not indices:
        raise ValueError("Select at least one timed lyric line.")
    start, end = indices[0], indices[-1] + 1
    region_start = entries[start - 1]["time_ms"] / 1000 + 0.05 if start else 0.0
    region_end = entries[end]["time_ms"] / 1000 - 0.05 if end < len(entries) else float(audio_end)
    if region_end - region_start < 0.5:
        raise ValueError("There is no audio time between the surrounding good lines.")
    selected = entries[start:end]
    count = len(selected)

    def valid(times):
        return (len(times) == count and all(t is not None for t in times)
                and all(region_start <= t <= region_end for t in times)
                and all(round(b * 100) > round(a * 100) for a, b in zip(times, times[1:])))

    candidates, notes = [], []
    lyric_lines = _parse_sync_lyrics(lyrics_module, "\n".join(entry["text"] for entry in selected))
    flat_words, word_lines = [], []
    for index, line in enumerate(lyric_lines):
        flat_words.extend(line.words)
        word_lines.extend([index] * len(line.words))
    clip_start = max(0.0, region_start - 0.5)
    clip_end = region_end + 0.5
    for prompt_enabled in prompt_modes:
        label = f"Whisper, prompt {'ON' if prompt_enabled else 'OFF'}"
        if progress:
            progress(f"{label}: transcribing {region_start:.2f}s–{region_end:.2f}s…")
        prompt = lyrics_module.lyrics_prompt(lyric_lines) if prompt_enabled else None
        words = [word for word in transcribe(clip_start, clip_end, prompt)
                 if region_start <= word.start <= region_end]
        if not words:
            notes.append(f"{label}: Whisper heard no words in this part.")
            continue
        if len(lyric_lines) != count or any(line.is_blank or not line.words for line in lyric_lines):
            notes.append(f"{label}: rejected (a selected line has no words to match).")
            continue
        mapping = align_module._dp_align(flat_words, words, threshold)
        times, anchored = _strong_line_times(flat_words, word_lines, mapping, words,
                                             count, region_start, region_end)
        if not times or not valid(times):
            notes.append(f"{label}: rejected (no reliable word matches or unsafe timestamps).")
            continue
        candidates.append({"label": label, "times": times,
                           "note": (f"{anchored}/{count} lines pinned by a reliable heard word "
                                    "(single 'I/to/of…' matches ignored); the rest interpolated")})
    if not extra_methods:
        return {"lines": selected, "region": (region_start, region_end),
                "candidates": candidates, "notes": notes}
    pattern = _lrc_repeated_block_candidate(entries, start, end)
    if pattern and valid(pattern[0]):
        candidates.append({"label": "Copy earlier repeat", "times": pattern[0],
                           "note": f"relative timing copied from the identical block at LRC line {pattern[1]}"})
    elif pattern:
        notes.append("Copy earlier repeat: the copied timing does not fit before the next good line / song end.")
    else:
        notes.append("Copy earlier repeat: no earlier identical block (last good line + selected lines) found.")
    later = _lrc_later_repeat_candidate(entries, start, end, region_start)
    if later and valid(later[0]):
        lead = count - later[2]
        candidates.append({"label": "Copy later repeat", "times": later[0],
                           "note": (f"last {later[2]} line(s) timed backwards from the identical block at "
                                    f"LRC line {later[1]}" + (f"; first {lead} line(s) spread evenly" if lead else ""))})
    elif later:
        notes.append("Copy later repeat: the copied timing does not fit after the previous good line.")
    else:
        notes.append("Copy later repeat: no later identical block (selected lines + next good line) found.")
    return {"lines": selected, "region": (region_start, region_end),
            "candidates": candidates, "notes": notes}


def _auto_fix_lrc_error(lrc_text, warning, audio_end, threshold, transcribe, progress=None):
    """Retime one flagged passage with Whisper: lyrics prompt ON first, then OFF.

    Not connected to any button: "Fix Error Automatically" was taken out of the
    Edit and Fix window (2026-10-01) because "error gone" did not mean the timing
    was actually right. Kept, with its tests, for a later and stricter version.

    The passage plus two lines on each side is retimed. A result is accepted only
    when the scan finds no error left in that part (and, for a smaller warning,
    fewer smaller warnings than before); nothing outside the part is changed.
    Returns (new_lrc_text or None, method_label or None, notes).
    """
    entries = _lrc_timed_entries(lrc_text)
    flagged = [i for i, entry in enumerate(entries)
               if warning["first_file_line"] <= entry["file_line"] <= warning["last_file_line"]]
    if not flagged:
        return None, None, ["The warning no longer matches this LRC; scan again."]
    low, high = max(0, flagged[0] - 2), min(len(entries) - 1, flagged[-1] + 2)
    first, last = entries[low]["file_line"], entries[high]["file_line"]

    def problems(text):
        found = [w for w in _find_lrc_duplicate_timestamp_runs(text)
                 if w["first_file_line"] <= last and w["last_file_line"] >= first]
        errors = sum(1 for w in found if w.get("severity", "error") == "error")
        return errors, len(found) - errors

    _errors_before, smaller_before = problems(lrc_text)
    was_error = warning.get("severity", "error") == "error"
    notes = []
    for prompt_enabled in (True, False):
        label = f"Whisper, prompt {'ON' if prompt_enabled else 'OFF'}"
        result = _retry_lrc_line_range(lrc_text, first, last, audio_end, threshold, transcribe, progress,
                                       prompt_modes=(prompt_enabled,), extra_methods=False)
        if not result["candidates"]:
            notes.extend(result["notes"] or [f"{label}: no usable timing."])
            continue
        times = result["candidates"][0]["times"]
        new_text = _replace_lrc_line_times(
            lrc_text, {entry["file_line"]: t for entry, t in zip(result["lines"], times)})
        errors, smaller = problems(new_text)
        if errors == 0 and (smaller <= smaller_before if was_error else smaller < smaller_before):
            notes.append(f"{label}: the error is gone.")
            return new_text, label, notes
        notes.append(f"{label}: {errors} error(s) and {smaller} smaller warning(s) still here — not used.")
    return None, None, notes


def _default_synced_output(audio_path: str, lyrics_path: str = "") -> str:
    """Default Syncalong output: a dedicated ``Synced Lyrics`` folder."""
    p = Path(audio_path)
    target = p.parent / "SYNCED LYRICS" / (p.stem + ".lrc")
    # Avoid silently overwriting an LRC that is being used as the input text.
    if lyrics_path:
        try:
            if Path(lyrics_path).resolve() == target.resolve():
                target = target.with_name(target.stem + ".synced.lrc")
        except Exception:
            pass
    return str(target)


_DEMUCS_MODEL = {"separator": None, "failed": ""}


def _separate_sync_vocals(audio_path, progress):
    """Isolate vocals with Demucs, keeping the model loaded between songs.

    Uses the same ``demucs.api.Separator`` and ``save_audio`` defaults as the
    ``python -m demucs --two-stems vocals`` command (htdemucs, shifts 1,
    overlap 0.25, 16-bit WAV). Any in-process failure falls back to the
    command-line separation for the rest of the program run.
    """
    if not _DEMUCS_MODEL["failed"]:
        try:
            return _separate_sync_vocals_in_process(audio_path, progress)
        except Exception as exc:
            _DEMUCS_MODEL.update(separator=None, failed=f"{type(exc).__name__}: {exc}")
            progress("In-process Demucs failed (" + _DEMUCS_MODEL["failed"]
                     + "); using the Demucs command line from now on.")
    return _separate_sync_vocals_subprocess(audio_path, progress)


def _separate_sync_vocals_in_process(audio_path, progress):
    import atexit
    import tempfile
    import torch
    from demucs.api import Separator
    from demucs.audio import save_audio
    from syncalong import vocal_separator

    separator = _DEMUCS_MODEL["separator"]
    if separator is None:
        progress("Loading Demucs model (kept in memory for the next songs)…")
        separator = Separator(model="htdemucs", device="cuda" if torch.cuda.is_available() else "cpu",
                              shifts=1, overlap=0.25, split=True, progress=False)
        _DEMUCS_MODEL["separator"] = separator
    progress("Separating vocals with Demucs…")
    outdir = Path(tempfile.mkdtemp(prefix="syncalong_demucs_"))
    atexit.register(shutil.rmtree, outdir, ignore_errors=True)
    try:
        _original, stems = separator.separate_audio_file(Path(audio_path))
        vocals = outdir / "vocals.wav"
        save_audio(stems["vocals"], str(vocals), samplerate=separator.samplerate,
                   clip="rescale", bits_per_sample=16, as_float=False)
        vocal_separator._separation_dirs[vocals] = outdir
        progress("Demucs: isolated vocals ready.")
        return vocals
    except Exception:
        shutil.rmtree(outdir, ignore_errors=True)
        raise
    finally:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _separate_sync_vocals_subprocess(audio_path, progress):
    """Run Demucs with real pipes, including from the windowless pythonw launcher."""
    import atexit
    import tempfile
    from syncalong import vocal_separator

    interpreter = Path(sys.executable)
    if os.name == "nt" and interpreter.name.lower() == "pythonw.exe":
        console_python = interpreter.with_name("python.exe")
        if console_python.is_file():
            interpreter = console_python
    outdir = Path(tempfile.mkdtemp(prefix="syncalong_demucs_"))
    atexit.register(shutil.rmtree, outdir, ignore_errors=True)
    # Demucs prints the input path before separation. Force UTF-8 in the child
    # process so Windows cp1252 cannot crash on zero-width or non-Latin chars.
    command = [str(interpreter), "-X", "utf8", "-m", "demucs", "--two-stems", "vocals",
               "--filename", "{stem}.{ext}",
               "-o", str(outdir), str(audio_path)]
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    progress("Separating vocals with Demucs…")
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=False,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            # Demucs prints progress with carriage returns; retain its useful
            # final diagnostic without flooding the Tk log with progress bars.
            lines = [line.strip() for line in (result.stdout or "").replace("\r", "\n").splitlines()
                     if line.strip() and not line.lstrip().startswith(("0%|", "100%|"))]
            detail = "\n".join(lines[-12:]) or "Demucs produced no diagnostic output."
            raise RuntimeError(f"Demucs failed (exit {result.returncode}):\n{detail}")
        vocals = next(outdir.rglob("vocals.wav"), None)
        if vocals is None:
            raise RuntimeError("Demucs completed without producing vocals.wav.")
        vocal_separator._separation_dirs[vocals] = outdir
        progress("Demucs: isolated vocals ready.")
        return vocals
    except Exception:
        shutil.rmtree(outdir, ignore_errors=True)
        raise


def _separate_fragment_vocals(audio_path, clip_start, clip_end, progress):
    """Cut one local audio interval, run Demucs on it, and return cleanup info."""
    import tempfile
    clip_dir = Path(tempfile.mkdtemp(prefix="t9_fragment_clip_"))
    clip = clip_dir / "fragment.wav"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{clip_start:.3f}", "-t", f"{max(.1, clip_end - clip_start):.3f}",
        "-i", str(audio_path), "-vn", "-ac", "2", "-ar", "44100", str(clip),
    ]
    try:
        progress(f"Preparing local audio clip {clip_start:.2f}s–{clip_end:.2f}s…")
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not clip.is_file():
            raise RuntimeError("FFmpeg could not prepare the local Demucs clip: " +
                               ((result.stdout or "").strip() or f"exit {result.returncode}"))
        vocals = _separate_sync_vocals(clip, progress)
        def cleanup():
            from syncalong import vocal_separator
            vocal_separator.cleanup_separation(vocals)
            shutil.rmtree(clip_dir, ignore_errors=True)
        return vocals, float(clip_start), cleanup
    except Exception:
        shutil.rmtree(clip_dir, ignore_errors=True)
        raise


_SYNC_LANG_TO_ID3 = {
    "en": "eng",
    "pl": "pol",
    "de": "deu",
    "es": "spa",
    "fr": "fra",
    "it": "ita",
    "ja": "jpn",
    "ko": "kor",
}


def _sync_id3_language(language: str) -> str:
    """Return the 3-letter ISO-639-2 code expected by ID3 USLT."""
    value = (language or "").strip().lower()
    return _SYNC_LANG_TO_ID3.get(value, "eng")


def _embed_synced_lrc_as_mp3tag_unsyncedlyrics(mp3_path: str, lrc_text: str, language: str = "en") -> None:
    """Embed the timestamped LRC text in an MP3's Mp3tag UNSYNCEDLYRICS field.

    Mp3tag maps UNSYNCEDLYRICS to the ID3 USLT frame. We keep the timestamps
    exactly as text inside USLT so Mp3tag displays the same content. Existing
    USLT frames for other languages/descriptions are preserved; only the default
    USLT frame for the selected language is replaced.
    """
    if Path(mp3_path).suffix.lower() != ".mp3":
        raise ValueError("UNSYNCEDLYRICS embedding is supported here only for MP3 files.")
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, USLT
    except Exception as exc:
        raise RuntimeError("Mutagen is required for MP3 lyric tagging. Install it with: pip install mutagen") from exc

    try:
        tags = ID3(mp3_path)
        version = getattr(tags, "version", (2, 4, 0))
        v2_version = version[1] if len(version) > 1 and version[1] in (3, 4) else 4
    except ID3NoHeaderError:
        tags = ID3()
        v2_version = 4

    lang = _sync_id3_language(language)
    tags.delall(f"USLT::{lang}")
    tags.add(USLT(encoding=3, lang=lang, desc="", text=(lrc_text or "").rstrip()))
    tags.save(mp3_path, v2_version=v2_version)


def _two_pass_sync_score(lrc_text: str, direct_lines: int) -> dict:
    """Score one Lyrics Sync result; see ``_pick_two_pass_result`` for ordering.

    ``matched`` from Syncalong counts interpolated lines too, so it is nearly
    always 100%. Suspicious scan lines are the primary signal because a lyrics
    prompt can make Whisper "hear" the lyrics while timestamps collapse.
    """
    warnings = _find_lrc_duplicate_timestamp_runs(lrc_text or "")
    return {
        "suspicious_lines": sum(item["count"] for item in warnings),
        "warnings": len(warnings),
        "direct_lines": int(direct_lines),
    }


def _pick_two_pass_result(score_on: dict, score_off: dict) -> bool:
    """Return True when the prompt-ON result wins.

    Fewer suspicious scan lines win; then more lines backed by a direct word
    match. A full tie keeps prompt ON, the recommended default.
    """
    key_on = (score_on["suspicious_lines"], -score_on["direct_lines"])
    key_off = (score_off["suspicious_lines"], -score_off["direct_lines"])
    return key_on <= key_off


def _syncalong_align_with_diagnostics(tx, lyrics_text, audio, language, threshold,
                                      use_prompt, separate_vocals):
    """Align lyrics and recover an intro that full-song Whisper skipped."""
    align_module = importlib.import_module("syncalong.align")
    formatter_module = importlib.import_module("syncalong.formatter")
    lyrics_module = importlib.import_module("syncalong.lyrics")
    pipeline_module = importlib.import_module("syncalong.pipeline")
    transcribe_module = importlib.import_module("syncalong.transcribe")

    lyric_lines = _parse_sync_lyrics(lyrics_module, lyrics_text)
    prompt = lyrics_module.lyrics_prompt(lyric_lines) if use_prompt else None
    transcript = tx.transcribe(
        Path(audio), language=language or None, initial_prompt=prompt,
        separate_vocals=separate_vocals,
    )
    if not transcript:
        raise RuntimeError("Whisper returned no usable words for this audio.")
    original_first_word = transcript[0]

    flat_words = []
    word_to_line = []
    for line_index, line in enumerate(lyric_lines):
        for word in line.words:
            flat_words.append(word)
            word_to_line.append(line_index)
    def direct_matches(words):
        word_mapping = align_module._dp_align(flat_words, words, threshold)
        line_mapping = {}
        for lyric_word_index, transcript_word_index in sorted(word_mapping.items()):
            line_index = word_to_line[lyric_word_index]
            line_mapping.setdefault(
                line_index, (lyric_word_index, transcript_word_index)
            )
        return line_mapping

    direct = direct_matches(transcript)
    sung_indices = [index for index, line in enumerate(lyric_lines)
                    if not line.is_blank and line.words]
    first_sung = sung_indices[0] if sung_indices else None
    direct_indices = sorted(direct)
    first_direct = direct_indices[0] if direct_indices else None
    recovery_summary = ""

    # Whisper commonly suppresses quiet opening ad-libs in a full-song pass,
    # especially after Demucs. If that leaves several leading lyric lines with
    # a zero-width extrapolation interval, retry only that short opening on the
    # original audio. This adds little work and cannot disturb later timings.
    leading_unmatched = (first_direct - first_sung
                         if first_sung is not None and first_direct is not None else 0)
    first_direct_time = None
    if first_direct is not None:
        first_direct_time = transcript[direct[first_direct][1]].start
    needs_opening_recovery = (
        leading_unmatched >= 3
        and first_direct_time is not None
        and abs(first_direct_time - transcript[0].start) < 0.011
        and hasattr(tx, "_model")
    )
    if needs_opening_recovery:
        clip_end = max(2.0, min(float(first_direct_time) + 0.8, 90.0))
        opening_options = {
            "word_timestamps": True,
            "condition_on_previous_text": False,
            "clip_timestamps": f"0,{clip_end:.2f}",
        }
        if language:
            opening_options["language"] = language
        try:
            opening_result = tx._model.transcribe(str(audio), **opening_options)
            opening_words = transcribe_module._extract_words(opening_result)
            recovered_words = [
                word for word in opening_words
                if word.start < original_first_word.start - 0.01
            ]
            if recovered_words:
                transcript = recovered_words + transcript
                direct = direct_matches(transcript)
                direct_indices = sorted(direct)
                first_direct = direct_indices[0] if direct_indices else None
                recovery_summary = (
                    f"Auto opening recovery: heard {len(recovered_words)} usable word(s) "
                    f"from {recovered_words[0].start:.2f}s to "
                    f"{recovered_words[-1].end:.2f}s in original audio without Demucs."
                )
            else:
                recovery_summary = (
                    "Auto opening recovery was attempted, but focused Whisper still "
                    "found no usable words before the main transcription."
                )
        except Exception as recovery_error:
            recovery_summary = (
                "Auto opening recovery could not run: "
                f"{type(recovery_error).__name__}: {recovery_error}"
            )

    timed_lines = align_module.align_lyrics_to_transcript(
        lyric_lines, transcript, threshold=threshold
    )

    def duplicate_time_runs(lines, minimum=3):
        """Return consecutive sung lines that render to one LRC centisecond."""
        entries = [(index, timestamp, round(timestamp * 100))
                   for index, (line, timestamp) in enumerate(lines)
                   if not line.is_blank and line.words and timestamp is not None]
        runs = []
        start = 0
        while start < len(entries):
            end = start + 1
            while end < len(entries) and entries[end][2] == entries[start][2]:
                end += 1
            if end - start >= minimum:
                runs.append({
                    "indices": [entry[0] for entry in entries[start:end]],
                    "timestamp": entries[start][1],
                    "centiseconds": entries[start][2],
                    "entry_start": start,
                    "entry_end": end,
                    "entries": entries,
                })
            start = end
        return runs

    def duplicate_penalty(lines):
        return sum(len(run["indices"]) - 1 for run in duplicate_time_runs(lines))

    fragment_recovery_notes = []
    attempted_regions = set()
    # Re-run only damaged local regions. The candidate is committed only when
    # it reduces repeated timestamps and leaves all unaffected lines stable.
    for _attempt in range(12):
        runs = duplicate_time_runs(timed_lines)
        run = next((item for item in runs
                    if (item["indices"][0], item["indices"][-1], item["centiseconds"])
                    not in attempted_regions), None)
        if run is None or not hasattr(tx, "_model"):
            break
        signature = (run["indices"][0], run["indices"][-1], run["centiseconds"])
        attempted_regions.add(signature)
        entries = run["entries"]
        previous_time = (entries[run["entry_start"] - 1][1]
                         if run["entry_start"] > 0 else None)
        next_time = (entries[run["entry_end"]][1]
                     if run["entry_end"] < len(entries) else None)
        repeated_time = run["timestamp"]
        region_start = (previous_time + 0.01 if previous_time is not None
                        else max(0.0, repeated_time - 15.0))
        region_end = (next_time - 0.01 if next_time is not None
                      else min(transcript[-1].end, repeated_time + 15.0))
        clip_start = max(0.0, region_start - 0.8)
        clip_end = min(transcript[-1].end, region_end + 0.8)
        if clip_end - clip_start < 0.5 or region_end <= region_start:
            fragment_recovery_notes.append(
                f"Fragment recovery skipped lyric lines {run['indices'][0] + 1}–"
                f"{run['indices'][-1] + 1}: no safe audio interval."
            )
            continue
        options = {
            "word_timestamps": True,
            "condition_on_previous_text": False,
            "clip_timestamps": f"{clip_start:.2f},{clip_end:.2f}",
        }
        if language:
            options["language"] = language
        try:
            focused_result = tx._model.transcribe(str(audio), **options)
            focused_words = transcribe_module._extract_words(focused_result)
            replacement = [word for word in focused_words
                           if region_start <= word.start <= region_end]
            if not replacement:
                fragment_recovery_notes.append(
                    f"Fragment recovery found no words for lyric lines "
                    f"{run['indices'][0] + 1}–{run['indices'][-1] + 1} "
                    f"near {repeated_time:.2f}s; original timing kept."
                )
                continue
            candidate_transcript = [word for word in transcript
                                    if not (region_start <= word.start <= region_end)]
            candidate_transcript.extend(replacement)
            candidate_transcript.sort(key=lambda word: (word.start, word.end))
            candidate_direct = direct_matches(candidate_transcript)
            candidate_timed = align_module.align_lyrics_to_transcript(
                lyric_lines, candidate_transcript, threshold=threshold
            )
            old_penalty = duplicate_penalty(timed_lines)
            new_penalty = duplicate_penalty(candidate_timed)
            changed_outside = []
            affected = set(run["indices"])
            for line_index in sung_indices:
                if line_index in affected:
                    continue
                old_time = timed_lines[line_index][1]
                new_time = candidate_timed[line_index][1]
                if (old_time is not None and new_time is not None
                        and abs(old_time - new_time) > 1.0):
                    changed_outside.append(line_index)
            target_times = {round(candidate_timed[index][1] * 100)
                            for index in run["indices"]
                            if candidate_timed[index][1] is not None}
            if (new_penalty < old_penalty and len(target_times) > 1
                    and not changed_outside):
                transcript = candidate_transcript
                direct = candidate_direct
                timed_lines = candidate_timed
                fragment_recovery_notes.append(
                    f"Fragment recovery accepted for lyric lines "
                    f"{run['indices'][0] + 1}–{run['indices'][-1] + 1}: "
                    f"{len(run['indices'])} identical timestamps at "
                    f"{repeated_time:.2f}s were reduced using {len(replacement)} "
                    "words from focused original audio."
                )
            else:
                fragment_recovery_notes.append(
                    f"Fragment recovery rejected for lyric lines "
                    f"{run['indices'][0] + 1}–{run['indices'][-1] + 1}: "
                    "the focused result was not safely better; original timing kept."
                )
        except Exception as fragment_error:
            fragment_recovery_notes.append(
                f"Fragment recovery failed for lyric lines "
                f"{run['indices'][0] + 1}–{run['indices'][-1] + 1}: "
                f"{type(fragment_error).__name__}: {fragment_error}"
            )

    lrc = formatter_module.format_lrc(timed_lines)
    result = pipeline_module.AlignmentResult(
        timed_lines=timed_lines,
        lrc=lrc,
        matched=sum(1 for _, timestamp in timed_lines if timestamp is not None),
        total=len(timed_lines),
    )
    result.t9_opening_recovery = recovery_summary
    result.t9_fragment_recovery = fragment_recovery_notes
    result.t9_direct_lines = len(direct)
    result.t9_sung_lines = len(sung_indices)

    direct_indices = sorted(direct)
    first_direct = direct_indices[0] if direct_indices else None
    last_direct = direct_indices[-1] if direct_indices else None
    diagnostics = [
        "",
        "================ SYNC ALIGNMENT DIAGNOSTICS ================",
        f"Whisper usable words: {len(transcript)}",
        f"Whisper first word: {transcript[0].start:.2f}s–{transcript[0].end:.2f}s | {transcript[0].raw}",
        f"Whisper last word: {transcript[-1].start:.2f}s–{transcript[-1].end:.2f}s | {transcript[-1].raw}",
        f"Lyrics prompt: {'ON' if use_prompt else 'OFF'} | Demucs: {'ON' if separate_vocals else 'OFF'} | Threshold: {threshold}",
        f"Original full-song Whisper start: {original_first_word.start:.2f}s | {original_first_word.raw}",
        recovery_summary or "Auto opening recovery: not needed.",
        *(fragment_recovery_notes or ["Automatic fragment recovery: not needed."]),
        "Whisper opening words: " + " | ".join(
            f"{word.start:.2f}s {word.raw}" for word in transcript[:30]
        ),
        "",
        "Per-line timing source:",
    ]
    for line_index in sung_indices:
        line, timestamp = timed_lines[line_index]
        time_label = "NO TIME" if timestamp is None else f"{timestamp:.2f}s"
        if line_index in direct:
            lyric_word_index, transcript_word_index = direct[line_index]
            transcript_word = transcript[transcript_word_index]
            score = align_module._word_score(
                flat_words[lyric_word_index], transcript_word.word
            )
            source = (
                f"DIRECT match '{flat_words[lyric_word_index]}' ← "
                f"'{transcript_word.raw}' ({score:.0f}%)"
            )
        elif first_direct is None:
            source = "NO DIRECT MATCH; no lyric line matched the Whisper transcript"
        elif line_index < first_direct:
            source = "ESTIMATED before first direct match"
        elif line_index > last_direct:
            source = "ESTIMATED after last direct match"
        else:
            source = "INTERPOLATED between direct matches"
        diagnostics.append(
            f"[LYRIC {line_index + 1:03d}] {time_label} | {source} | {line.raw}"
        )

    if first_sung is not None and first_direct is not None and first_direct > first_sung:
        first_direct_time = timed_lines[first_direct][1]
        estimated_before = first_direct - first_sung
        diagnostics.extend([
            "",
            f"Opening diagnosis: {estimated_before} lyric line(s) before the first direct match.",
        ])
        if (first_direct_time is not None
                and abs(first_direct_time - transcript[0].start) < 0.011):
            diagnostics.append(
                "CAUSE OF IDENTICAL OPENING TIMES: Whisper's first usable word and "
                "Syncalong's first direct lyric match start at the same time. There is no "
                "earlier recognized-word interval in which to distribute the opening lines."
            )
        else:
            diagnostics.append(
                "The opening lines had no direct word match and their times were estimated "
                "between Whisper's first word and the first directly matched lyric line."
            )
    diagnostics.append("============================================================")
    return result, diagnostics


_SYNC_WINDOW = {}  # the one open Lyrics Sync window: {"win": Toplevel, "reopen": callable}


def open_syncalong_window(start_scan=False, sync_song=None, fix_song=None):
    """Open a dedicated Syncalong page for MP3 + plain lyrics -> timed LRC.

    Only one window exists: a second call brings it back to the front.
    ``start_scan`` runs "Edit and Fix Synced Lyrics" (library scan) right away.
    ``sync_song`` loads that audio file with its TXT lyrics (the sync is not started).
    """
    existing = _SYNC_WINDOW.get("win")
    if existing is not None and existing.winfo_exists():
        _SYNC_WINDOW["reopen"](start_scan, sync_song, fix_song)
        return
    win = create_toplevel(root)
    win.title("Triple 9 Studio - Lyrics Sync")
    win.geometry("1080x740")
    win.minsize(960, 680)
    try:
        if os.path.isfile(ICON_FILE):
            win.iconbitmap(ICON_FILE)
    except Exception:
        pass

    theme = THEMES.get(config.get("theme", "dark"), THEMES["dark"])
    win.configure(bg=theme["bg"])

    audio_var = tk.StringVar()
    lyrics_var = tk.StringVar()
    output_var = tk.StringVar()

    saved_model_name = str(config.get("sync_model", "turbo")).strip() or "turbo"
    saved_model_label = next(
        (label for label, name, _ in SYNCALONG_MODELS if name == saved_model_name),
        SYNCALONG_DEFAULT_MODEL_LABEL,
    )
    model_var = tk.StringVar(value=saved_model_label)
    language_var = tk.StringVar(value=str(config.get("sync_language", "en") or "en"))
    threshold_var = tk.StringVar(value=str(config.get("sync_threshold", 55)))
    prompt_var = tk.BooleanVar(value=bool(config.get("sync_use_lyrics_prompt", True)))
    vocals_var = tk.BooleanVar(value=bool(config.get("sync_separate_vocals", False)))
    bracket_lines_var = tk.BooleanVar(value=bool(config.get("sync_bracket_only_lines", False)))
    embed_mp3_var = tk.BooleanVar(value=bool(config.get("sync_embed_mp3", False)))
    sync_status_var = tk.StringVar(value="Ready.")
    gpu_status_var = tk.StringVar(value="Checking GPU...")
    running = {"value": False}
    stop_after_current = threading.Event()
    batch_jobs = []
    batch_summary_var = tk.StringVar(value="")
    # CUDA/PyTorch probing starts a fresh Python process and can take several
    # seconds. Keep window creation instant and update these values in-place
    # after the background probe finishes.
    gpu_info = {
        "torch": False, "nvidia": False, "cuda": False, "cuda_build": None,
        "gpu": "Checking...", "vram_gb": 0.0, "detail": "Checking in background",
        "checking": True,
    }

    outer = tk.Frame(win, bg=theme["bg"])
    outer.pack(fill="both", expand=True, padx=12, pady=8)

    title = tk.Label(outer, text="LYRICS SYNC", font=("Segoe UI", 20, "bold"),
                     bg=theme["bg"], fg="#858b95")
    studio_dialogs.add_heading_logo(title, ICON_FILE if os.path.isfile(ICON_FILE) else None)
    title.pack(anchor="w")
    subtitle = tk.Label(
        outer,
        text="Time plain .txt lyrics to the song → SYNCED LYRICS\\<song>.lrc.   New here? Downloader → Help > How To Use.",
        font=("Segoe UI", 9), bg=theme["bg"], fg=theme["fg"], anchor="w"
    )
    subtitle.pack(fill="x", pady=(2, 4))
    sync_help_hint = tk.Label(
        outer,
        text="Need help or not sure what to do? In the Downloader window, open Help > How To Use.",
        font=("Segoe UI", 10, "bold"), bg=theme["bg"], fg=theme["fg"], anchor="w"
    )
    # (the help hint is part of the subtitle now)

    form = tk.Frame(outer, bg=theme["bg"])
    form.pack(fill="x")
    form.grid_columnconfigure(1, weight=1)

    def styled_label(parent, text):
        return tk.Label(parent, text=text, bg=theme["bg"], fg=theme["fg"], anchor="w")

    def styled_entry(parent, variable):
        return tk.Entry(parent, textvariable=variable, bg=theme["panel"], fg=theme["fg"],
                        insertbackground=theme["fg"], highlightbackground=theme["border"],
                        highlightcolor=theme["border"])

    def styled_button(parent, text, command, width=None):
        return tk.Button(parent, text=text, command=command, width=width,
                         bg=theme["btn_bg"], fg=theme["btn_fg"],
                         activebackground=theme["btn_bg"], activeforeground=theme["btn_fg"])

    styled_label(form, "Audio (preview):").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
    audio_entry = styled_entry(form, audio_var)
    audio_entry.grid(row=0, column=1, sticky="ew", pady=5)

    def choose_audio():
        path = filedialog.askopenfilename(
            parent=win,
            title="Choose audio file",
            filetypes=[("Audio files", "*.mp3 *.flac *.wav *.m4a *.ogg *.opus"), ("All files", "*.*")]
        )
        if path:
            audio_var.set(path)
            if not lyrics_var.get().strip():
                found = _find_plain_lyrics_for_audio(path)
                if found:
                    lyrics_var.set(found)
            output_var.set(_default_synced_output(path, lyrics_var.get().strip()))

    audio_browse_btn = styled_button(form, "Browse", choose_audio, 10)
    audio_browse_btn.grid(row=0, column=2, padx=(8, 0), pady=5)

    styled_label(form, "Lyrics (.txt):").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
    lyrics_entry = styled_entry(form, lyrics_var)
    lyrics_entry.grid(row=1, column=1, sticky="ew", pady=5)

    def choose_lyrics():
        path = filedialog.askopenfilename(
            parent=win,
            title="Choose plain lyrics file",
            filetypes=[("Lyrics text", "*.txt *.lrc"), ("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            lyrics_var.set(path)
            if audio_var.get().strip():
                output_var.set(_default_synced_output(audio_var.get().strip(), path))

    lyrics_browse_btn = styled_button(form, "Browse", choose_lyrics, 10)
    lyrics_browse_btn.grid(row=1, column=2, padx=(8, 0), pady=5)

    styled_label(form, "Output LRC (preview):").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
    output_entry = styled_entry(form, output_var)
    output_entry.grid(row=2, column=1, sticky="ew", pady=5)

    def choose_output():
        initial = output_var.get().strip() or _default_synced_output(audio_var.get().strip()) if audio_var.get().strip() else "song.lrc"
        path = filedialog.asksaveasfilename(
            parent=win, title="Save synchronized LRC", defaultextension=".lrc",
            initialfile=Path(initial).name,
            initialdir=str(Path(initial).parent) if Path(initial).parent.exists() else None,
            filetypes=[("LRC lyrics", "*.lrc"), ("All files", "*.*")]
        )
        if path:
            output_var.set(path)

    output_browse_btn = styled_button(form, "Browse", choose_output, 10)
    output_browse_btn.grid(row=2, column=2, padx=(8, 0), pady=5)

    def _selected_main_track_paths():
        """Return exact selected audio paths from the main Tracks list."""
        artist = get_selected_artist_name()
        selection = list(track_list.curselection())
        if not selection:
            return []
        result = []
        seen = set()
        for row in selection:
            path = TRACK_ROW_PATHS.get(row)
            if not path:
                path = resolve_track_display_to_path(artist, track_list.get(row))
            path = os.path.abspath(path)
            key = os.path.normcase(path)
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            result.append(path)
        return result

    def _refresh_batch_ui():
        count = len(batch_jobs)
        if count:
            batch_summary_var.set(
                f"{count} song(s) ready  •  synced one by one  •  each song's .txt is found automatically"
            )
        else:
            batch_summary_var.set(
                "1 song  •  choose the audio and its lyrics above — or select several tracks in the main window "
                "and click “Use selected track(s)”"
            )
        try:
            start_btn.configure(text=f"▶  START SYNC ({count} songs)" if count > 1 else "▶  START SYNC")
            if count:
                batch_preview_note.pack(fill="x", pady=(3, 0))
            else:
                batch_preview_note.pack_forget()
        except NameError:
            pass

    def use_selected_track():
        paths = _selected_main_track_paths()
        if not paths:
            messagebox.showinfo(
                "Lyrics Sync",
                "Select one or more tracks in the main Tracks list first.",
                parent=win,
            )
            return

        batch_jobs.clear()

        # Single selection keeps the existing manual workflow.
        if len(paths) == 1:
            audio = paths[0]
            audio_var.set(audio)
            found = _find_plain_lyrics_for_audio(audio)
            lyrics_var.set(found or "")
            output_var.set(_default_synced_output(audio, found))
            _refresh_batch_ui()
            if not found:
                messagebox.showinfo(
                    "Lyrics Sync",
                    "Audio loaded. I could not find matching lyrics automatically, "
                    "so choose the lyrics file with Browse.",
                    parent=win,
                )
            return

        # Multiple selections switch to compact batch mode. Only matching plain
        # TXT files are accepted automatically; already-synced LRC files are not
        # recycled as plain input by accident.
        missing = []
        for audio in paths:
            lyrics = _find_plain_txt_for_audio(audio)
            if not lyrics:
                missing.append(audio)
                continue
            batch_jobs.append({
                "audio": audio,
                "lyrics": lyrics,
                "output": _default_synced_output(audio, lyrics),
            })

        if batch_jobs:
            # Show the first queued item in the normal fields as a preview, so
            # batch mode needs no large second list and does not clutter the window.
            first = batch_jobs[0]
            audio_var.set(first["audio"])
            lyrics_var.set(first["lyrics"])
            output_var.set(first["output"])
        else:
            audio_var.set("")
            lyrics_var.set("")
            output_var.set("")

        _refresh_batch_ui()

        if missing:
            messagebox.showinfo(
                "Lyrics Sync — Batch",
                f"Queued: {len(batch_jobs)} track(s)\n"
                f"Skipped: {len(missing)} track(s) without a matching .txt file.\n\n"
                "Batch mode only auto-uses plain .txt lyrics with the same track name.",
                parent=win,
            )

    selected_row = tk.Frame(outer, bg=theme["bg"])
    selected_row.pack(fill="x", pady=(8, 4))
    use_selected_btn = styled_button(
        selected_row,
        "Use selected track(s) from main window",
        use_selected_track,
    )
    use_selected_btn.pack(side="left")

    clear_batch_btn = styled_button(
        selected_row,
        "Clear batch",
        lambda: (batch_jobs.clear(), _refresh_batch_ui()),
        10,
    )
    clear_batch_btn.pack(side="left", padx=(8, 0))

    batch_info_box = tk.Frame(
        outer, bg=theme["panel"], highlightthickness=1,
        highlightbackground=theme["border"], padx=10, pady=8
    )
    batch_info_box.pack(fill="x", pady=(0, 10))

    batch_summary_label = tk.Label(
        batch_info_box,
        textvariable=batch_summary_var,
        font=("Segoe UI", 10, "bold"),
        bg=theme["panel"],
        fg=theme["fg"],
        anchor="w",
        justify="left",
    )
    batch_summary_label.pack(fill="x")

    batch_preview_note = tk.Label(
        batch_info_box,
        text="Audio / Lyrics / Output above show the first song of the list.",
        font=("Segoe UI", 8),
        bg=theme["panel"],
        fg=theme.get("muted", theme["fg"]),
        anchor="w",
        justify="left",
        wraplength=850,
    )
    _refresh_batch_ui()  # shows the note only for several songs

    configuration = tk.Frame(outer, bg=theme["bg"])
    configuration.pack(fill="x", pady=(0, 6))
    settings = tk.LabelFrame(configuration, text=" Sync settings ", bg=theme["bg"], fg=theme["fg"],
                             highlightbackground=theme["border"], highlightcolor=theme["border"])
    settings.pack(side="left", fill="both", expand=True, padx=(0, 8))
    settings.grid_columnconfigure(1, weight=1)

    tk.Label(settings, text="Whisper model:", bg=theme["bg"], fg=theme["fg"]).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
    model_combo = ttk.Combobox(settings, textvariable=model_var,
                               values=[x[0] for x in SYNCALONG_MODELS], state="readonly", width=28)
    model_combo.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(10, 5))

    tk.Label(settings, text="Language:", bg=theme["bg"], fg=theme["fg"]).grid(row=1, column=0, sticky="w", padx=10, pady=5)
    language_combo = ttk.Combobox(settings, textvariable=language_var,
                                  values=["en", "auto", "pl", "de", "es", "fr", "it", "ja", "ko"],
                                  state="normal", width=12)
    language_combo.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=5)

    two_pass_var = tk.BooleanVar(value=bool(config.get("sync_two_pass", False)))
    advanced_frame = tk.Frame(settings, bg=theme["bg"])  # shown by "Advanced settings ▸"
    threshold_label = tk.Label(settings, text="Match threshold:", bg=theme["bg"], fg=theme["fg"])
    threshold_label.grid(in_=advanced_frame, row=0, column=0, sticky="w", padx=10, pady=5)
    threshold_spin = tk.Spinbox(settings, from_=0, to=100, textvariable=threshold_var, width=8,
                                bg=theme["panel"], fg=theme["fg"], buttonbackground=theme["btn_bg"])
    threshold_spin.grid(in_=advanced_frame, row=0, column=1, sticky="w", padx=(0, 10), pady=5)

    prompt_check = tk.Checkbutton(settings, text="Use lyrics as Whisper prompt (recommended)", variable=prompt_var,
                                  bg=theme["bg"], fg=theme["fg"], selectcolor=theme["panel"],
                                  activebackground=theme["bg"], activeforeground=theme["fg"])
    prompt_check.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=5)

    studio_preferences.watch_visibility(win, [threshold_label, threshold_spin])

    vocals_check = tk.Checkbutton(settings, text="Separate vocals with Demucs (optional)", variable=vocals_var,
                                  bg=theme["bg"], fg=theme["fg"], selectcolor=theme["panel"],
                                  activebackground=theme["bg"], activeforeground=theme["fg"])
    vocals_check.grid(in_=advanced_frame, row=1, column=0, columnspan=2, sticky="w", padx=10, pady=5)

    embed_mp3_check = tk.Checkbutton(
        settings,
        text="After sync: embed timestamped LRC into MP3",
        variable=embed_mp3_var, bg=theme["bg"], fg=theme["fg"], selectcolor=theme["panel"],
        activebackground=theme["bg"], activeforeground=theme["fg"],
    )
    embed_mp3_check.grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=5)

    def bracket_lines_changed():
        config["sync_bracket_only_lines"] = bool(bracket_lines_var.get())  # read by every next sync
        save_config(config)

    bracket_lines_check = tk.Checkbutton(
        settings, text="Allow sync of lines only in brackets ( ) (Not Recommended)",
        variable=bracket_lines_var, command=bracket_lines_changed,
        bg=theme["bg"], fg=theme["fg"], selectcolor=theme["panel"],
        activebackground=theme["bg"], activeforeground=theme["fg"],
    )
    bracket_lines_check.grid(in_=advanced_frame, row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(5, 10))

    two_pass_check = tk.Checkbutton(
        settings, text="2 Pass sync — with and without the lyrics prompt, keeps the better one (about 2× slower)",
        variable=two_pass_var, bg=theme["bg"], fg=theme["fg"], selectcolor=theme["panel"],
        activebackground=theme["bg"], activeforeground=theme["fg"],
    )
    two_pass_check.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=5)

    advanced_open = {"value": bool(config.get("sync_advanced_open", False))}

    def show_advanced(open_it):
        advanced_open["value"] = open_it
        if open_it:
            advanced_frame.grid(row=6, column=0, columnspan=2, sticky="we", padx=(12, 0), pady=(0, 6))
        else:
            advanced_frame.grid_remove()
        advanced_btn.configure(text=("Advanced settings  ▾" if open_it else "Advanced settings  ▸"))

    def toggle_advanced():
        show_advanced(not advanced_open["value"])
        config["sync_advanced_open"] = advanced_open["value"]
        save_config(config)

    advanced_btn = tk.Button(settings, text="Advanced settings  ▸", command=toggle_advanced, relief="flat", bd=0,
                             bg=theme["bg"], fg=theme["fg"], activebackground=theme["bg"],
                             activeforeground=theme["fg"], cursor="hand2", font=("Segoe UI", 9, "bold"))
    advanced_btn.grid(row=5, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 8))
    show_advanced(advanced_open["value"])

    hardware = tk.LabelFrame(configuration, text=" Hardware / requirements ", bg=theme["bg"], fg=theme["fg"])
    hardware.pack(side="left", fill="both", expand=True)
    gpu_label = tk.Label(hardware, textvariable=gpu_status_var, bg=theme["bg"], fg=theme["fg"], anchor="w", justify="left", wraplength=360)
    gpu_label.pack(fill="x", padx=10, pady=(8, 3))
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    syncalong_ok = importlib.util.find_spec("syncalong") is not None
    whisper_ok = importlib.util.find_spec("whisper") is not None
    pytorch_label = "✗ not found"
    if gpu_info["torch"]:
        pytorch_label = f"✓ CUDA {gpu_info['cuda_build']}" if gpu_info["cuda"] else "⚠ CPU-only"
    req_lines = [
        f"FFmpeg: {'✓ found' if ffmpeg_ok else '✗ not found in PATH'}",
        f"Syncalong: {'✓ found' if syncalong_ok else '✗ not found'}",
        f"Whisper: {'✓ found' if whisper_ok else '✗ not found'}",
        f"PyTorch: {pytorch_label}",
    ]
    if not (syncalong_ok and whisper_ok):
        req_lines.append('Install missing package(s): pip install "syncalong[whisper]"')
    requirements_text = "    •    ".join(req_lines[:2]) + "\n" + "    •    ".join(req_lines[2:])
    if len(req_lines) > 4:
        requirements_text += "\n" + req_lines[4]
    requirements_label = tk.Label(
        hardware, text=requirements_text, bg=theme["bg"], fg=theme["fg"],
        anchor="w", justify="left", wraplength=360
    )
    requirements_label.pack(fill="x", padx=10, pady=(0, 8))

    gpu_status_var.set("Checking NVIDIA / CUDA in background…")

    def apply_gpu_probe(info):
        if not win.winfo_exists():
            return
        gpu_info.clear()
        gpu_info.update(info)
        gpu_info["checking"] = False
        gpu_status_var.set("✓ NVIDIA Graphics card" if gpu_info["cuda"]
                           else f"⚠ {gpu_info['detail']}")
        pytorch_status = "✗ not found"
        if gpu_info["torch"]:
            pytorch_status = (f"✓ CUDA {gpu_info['cuda_build']}"
                              if gpu_info["cuda"] else "⚠ CPU-only")
        requirements_label.configure(
            text=(f"FFmpeg: {'✓ found' if ffmpeg_ok else '✗ not found in PATH'}"
                  f"    •    Syncalong: {'✓ found' if syncalong_ok else '✗ not found'}\n"
                  f"Whisper: {'✓ found' if whisper_ok else '✗ not found'}"
                  f"    •    PyTorch: {pytorch_status}")
        )

    def probe_gpu_in_background():
        global _SYNC_GPU_INFO_CACHE
        if _SYNC_GPU_INFO_CACHE is None:
            info = _syncalong_gpu_info()
            _SYNC_GPU_INFO_CACHE = dict(info)
        else:
            info = dict(_SYNC_GPU_INFO_CACHE)
        # ui_call hands the result to the Tk thread safely (win.after from a worker thread
        # can fail silently and leave the window stuck in "GPU check still running").
        ui_call(apply_gpu_probe, info)

    threading.Thread(target=probe_gpu_in_background, daemon=True).start()

    # Keep the main action row above the expandable log so START SYNC is always visible,
    # even on smaller screens or with Windows display scaling.
    action_bar = tk.Frame(outer, bg=theme["bg"], height=48)
    action_bar.pack(fill="x", pady=(0, 10))
    action_bar.pack_propagate(True)
    status_label_sync = tk.Label(
        action_bar, textvariable=sync_status_var,
        bg=theme["bg"], fg=theme["fg"], anchor="w"
    )
    status_label_sync.pack(side="left", fill="x", expand=True)

    log_frame = tk.Frame(outer, bg=theme["bg"])
    # Reserve log space first, so it stays visible even with advanced controls hidden.
    log_frame.pack(side="bottom", fill="both", expand=True, pady=(0, 10), before=title)
    action_bar.pack(side="bottom", fill="x", pady=(4, 6), before=title)
    sync_log = tk.Text(log_frame, height=5, wrap="word", bg=theme["log_bg"], fg=theme["log_fg"],
                       insertbackground=theme["log_fg"], padx=8, pady=6)
    sync_scroll = tk.Scrollbar(log_frame, orient="vertical", command=sync_log.yview)
    sync_log.configure(yscrollcommand=sync_scroll.set)
    sync_log.pack(side="left", fill="both", expand=True)
    sync_scroll.pack(side="right", fill="y")

    def slog(text):
        def _append():
            if not win.winfo_exists():
                return
            sync_log.insert("end", str(text).rstrip() + "\n")
            sync_log.see("end")
        try:
            win.after(0, _append)
        except Exception:
            pass

    # One Whisper model stays loaded while this window is open, so batches,
    # 2-pass runs and every Fix fragment Retry skip the multi-second load.
    model_cache = {"name": None, "tx": None}

    def get_transcriber(model_name):
        """Return the loaded Whisper model, loading it only when the model changed."""
        if model_cache["tx"] is not None and model_cache["name"] == model_name:
            slog(f"Reusing loaded Whisper model: {model_name}")
            return model_cache["tx"]
        release_transcriber()
        import syncalong
        model_cache["tx"] = syncalong.Transcriber(model_name=model_name)
        model_cache["name"] = model_name
        return model_cache["tx"]

    def release_transcriber():
        """Free the cached Whisper model and its GPU memory."""
        if model_cache["tx"] is None:
            return
        model_cache.update(tx=None, name=None)
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    lrc_scan_running = {"value": False}
    resync_dir = Path(__file__).resolve().parent / "lyrics_sync_state"
    queue_path = resync_dir / "marked.json"
    ignored_path = resync_dir / "ignored_warnings.json"

    # Failures report, Fix fragment and the fix queue live in studio_failures.py.
    failure_ctx = SimpleNamespace()
    failure_tools = studio_failures.install(studio_failures.GlobalsView(globals()), failure_ctx)
    scan_lrc_library = failure_tools["scan_lrc_library"]
    open_marked_sync = failure_tools["open_marked_sync"]

    controls = [audio_entry, lyrics_entry, output_entry, audio_browse_btn, lyrics_browse_btn,
                output_browse_btn, use_selected_btn, clear_batch_btn, model_combo, language_combo, threshold_spin,
                prompt_check, vocals_check, embed_mp3_check, bracket_lines_check, two_pass_check]

    setup_running = {"value": False}

    def refresh_gpu_display():
        nonlocal gpu_info
        gpu_info = _syncalong_gpu_info()
        gpu_status_var.set((f"✓ {gpu_info['gpu']} • {gpu_info['detail']}") if gpu_info["cuda"]
                           else f"⚠ {gpu_info['detail']}")
        pytorch_label = "✗ not found"
        if gpu_info["torch"]:
            pytorch_label = f"✓ CUDA {gpu_info['cuda_build']}" if gpu_info["cuda"] else "⚠ CPU-only"
        requirements_label.configure(
            text=f"FFmpeg: {'✓ ready' if ffmpeg_ok else '✗ not found'}    •    Syncalong: {'✓ ready' if syncalong_ok else '✗ not found'}\n"
                 f"Whisper: {'✓ ready' if whisper_ok else '✗ not found'}    •    PyTorch: {pytorch_label}"
        )
        try:
            needed = gpu_info["nvidia"] and not gpu_info["cuda"]
            gpu_repair_btn.configure(state=("normal" if needed and not setup_running["value"] else "disabled"))
            if needed and not gpu_repair_btn.winfo_manager():
                gpu_repair_btn.pack(anchor="w", padx=10, pady=(0, 8))
            elif not needed and gpu_repair_btn.winfo_manager():
                gpu_repair_btn.pack_forget()
        except NameError:
            pass

    def setup_sync():
        if running["value"] or setup_running["value"]:
            return
        include_demucs = bool(vocals_var.get())
        setup_running["value"] = True
        set_running(True)
        sync_status_var.set("Checking / setting up Lyrics Sync…")

        def finish(status, error):
            nonlocal ffmpeg_ok, syncalong_ok, whisper_ok, gpu_info
            setup_running["value"] = False
            set_running(False)
            if not win.winfo_exists():
                return
            if error:
                sync_status_var.set("Setup needs attention — see log")
                messagebox.showerror("Lyrics Sync Setup", error, parent=win)
                return
            importlib.invalidate_caches()
            ffmpeg_ok = status["FFmpeg"]
            syncalong_ok = status["Syncalong"]
            whisper_ok = status["Whisper"]
            refresh_gpu_display()
            sync_status_var.set("Lyrics Sync is ready")

        def worker():
            try:
                from studio_sync_setup import setup
                status = setup(slog, include_demucs=include_demucs)
                ui_call(finish, status, None)
            except Exception as exc:
                slog("SETUP ERROR: " + str(exc))
                ui_call(finish, None, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    setup_btn = tk.Button(hardware, text="Check / Setup Lyrics Sync", command=setup_sync,
                          bg=theme["btn_bg"], fg=theme["btn_fg"],
                          activebackground=theme["btn_bg"], activeforeground=theme["btn_fg"])
    setup_btn.pack(anchor="w", padx=10, pady=(0, 8))
    controls.append(setup_btn)

    def repair_pytorch_gpu():
        if running["value"] or setup_running["value"]:
            return
        if not messagebox.askyesno(
            "Lyrics Sync — PyTorch GPU",
            "Replace the CPU-only PyTorch packages in Python 3.10 with the official CUDA build?\n\n"
            "This is a large download and can take several minutes. The NVIDIA driver will not be changed.",
            parent=win,
        ):
            return
        setup_running["value"] = True
        set_running(True)
        gpu_repair_btn.configure(state="disabled")
        sync_status_var.set("Installing / repairing PyTorch GPU…")

        def finish_repair(result, error):
            setup_running["value"] = False
            set_running(False)
            if not win.winfo_exists():
                return
            refresh_gpu_display()
            if error:
                sync_status_var.set("PyTorch GPU repair needs attention — see log")
                messagebox.showerror("Lyrics Sync — PyTorch GPU", error, parent=win)
                return
            sync_status_var.set("PyTorch GPU is ready")
            messagebox.showinfo(
                "Lyrics Sync — PyTorch GPU",
                f"PyTorch CUDA is ready for {result.get('device', 'the NVIDIA GPU')}.\n\n"
                "Restart Triple 9 Studio before starting Lyrics Sync.",
                parent=win,
            )

        def worker_repair():
            try:
                from studio_sync_setup import install_pytorch_gpu
                result = install_pytorch_gpu(slog)
                ui_call(finish_repair, result, None)
            except Exception as exc:
                slog("PYTORCH GPU REPAIR ERROR: " + str(exc))
                ui_call(finish_repair, None, str(exc))

        threading.Thread(target=worker_repair, daemon=True).start()

    gpu_repair_btn = tk.Button(
        hardware, text="Install/Repair PyTorch GPU", command=repair_pytorch_gpu,
        bg=theme["btn_bg"], fg=theme["btn_fg"],
        activebackground=theme["btn_bg"], activeforeground=theme["btn_fg"],
        state=("normal" if gpu_info["nvidia"] and not gpu_info["cuda"] else "disabled"),
    )
    if gpu_info["nvidia"] and not gpu_info["cuda"]:  # only when an NVIDIA card runs without CUDA PyTorch
        gpu_repair_btn.pack(anchor="w", padx=10, pady=(0, 8))
    controls.append(gpu_repair_btn)

    def save_sync_preferences():
        """Persist only Sync settings; never persist selected file paths."""
        try:
            model_name, _required_vram = SYNCALONG_MODEL_BY_LABEL.get(
                model_var.get(), ("turbo", 6.0)
            )
            config["sync_model"] = model_name
            config["sync_language"] = (language_var.get().strip() or "en")
            try:
                config["sync_threshold"] = max(0, min(100, int(float(threshold_var.get()))))
            except Exception:
                config["sync_threshold"] = 55
            config["sync_use_lyrics_prompt"] = bool(prompt_var.get())
            config["sync_separate_vocals"] = bool(vocals_var.get())
            config["sync_embed_mp3"] = bool(embed_mp3_var.get())
            config["sync_bracket_only_lines"] = bool(bracket_lines_var.get())
            config["sync_two_pass"] = bool(two_pass_var.get())
            save_config(config)
        except Exception:
            pass

    def close_sync_window():
        if setup_running["value"]:
            messagebox.showinfo("Lyrics Sync Setup", "Setup is still running. Wait for it to finish before closing this window.", parent=win)
            return
        if running["value"]:
            request_sync_stop()
            messagebox.showinfo("Lyrics Sync", "Finishing the current track before stopping. You can close this window when it finishes.", parent=win)
            return
        save_sync_preferences()
        release_transcriber()
        win.destroy()

    # Save the same preferences when the user closes the window with the X button.
    win.protocol("WM_DELETE_WINDOW", close_sync_window)

    def set_running(value):
        running["value"] = value
        if value:
            stop_after_current.clear()
        stop_sync_btn.configure(state="normal" if value and not setup_running["value"] else "disabled",
                                text="Stop after current track")
        state = "disabled" if value else "normal"
        for w in controls:
            try:
                if isinstance(w, ttk.Combobox):
                    w.configure(state="disabled" if value else ("readonly" if w is model_combo else "normal"))
                else:
                    w.configure(state=state)
            except Exception:
                pass
        try:
            start_btn.configure(state="disabled" if value else "normal")
            if not value:
                _refresh_batch_ui()
        except Exception:
            pass

    def _align_one_with_transcriber(tx, audio, lyrics, output, language, threshold,
                                   use_prompt, separate_vocals, embed_mp3, two_pass=False):
        """Align one file. Caller owns the shared Transcriber.

        With ``two_pass`` the song is aligned with the lyrics prompt ON and OFF;
        only the better result is saved/embedded and ``use_prompt`` is ignored.
        """
        lyrics_text = _prepare_syncalong_lyrics_text(lyrics)

        def run_pass(prompt_enabled):
            res, diagnostic_lines = _syncalong_align_with_diagnostics(
                tx, lyrics_text, audio, language, threshold,
                prompt_enabled, separate_vocals,
            )
            recovery_note = getattr(res, "t9_opening_recovery", "")
            if recovery_note:
                slog(recovery_note)
            for fragment_note in getattr(res, "t9_fragment_recovery", []):
                slog(fragment_note)
            if studio_preferences.developer_mode():
                for diagnostic_line in diagnostic_lines:
                    slog(diagnostic_line)
            return res

        if two_pass:
            passes = {}
            shared_vocals = None
            original_separate = getattr(tx, "_separate_vocals", None)
            if separate_vocals and original_separate is not None:
                # Both passes hear identical Demucs vocals: separate once. The
                # path is unregistered so Syncalong's per-pass cleanup keeps it.
                from syncalong import vocal_separator
                shared_vocals = original_separate(Path(audio))
                shared_dir = vocal_separator._separation_dirs.pop(shared_vocals, None)
                tx._separate_vocals = lambda _path: shared_vocals
            try:
                for prompt_enabled in (True, False):
                    label = "ON" if prompt_enabled else "OFF"
                    slog(f"   Pass {1 if prompt_enabled else 2}/2: lyrics prompt {label}"
                         + (" (reusing Demucs vocals)…" if shared_vocals and not prompt_enabled else "…"))
                    res = run_pass(prompt_enabled)
                    score = _two_pass_sync_score(res.lrc, getattr(res, "t9_direct_lines", 0))
                    slog(f"   Prompt {label}: {score['suspicious_lines']} suspicious line(s) in "
                         f"{score['warnings']} scan warning(s); directly matched lines "
                         f"{score['direct_lines']}/{getattr(res, 't9_sung_lines', 0)}")
                    passes[prompt_enabled] = (res, score)
            finally:
                if shared_vocals is not None:
                    tx._separate_vocals = original_separate
                    if shared_dir is not None:
                        shutil.rmtree(shared_dir, ignore_errors=True)
            winner = _pick_two_pass_result(passes[True][1], passes[False][1])
            res = passes[winner][0]
            slog(f"   ★ Kept prompt {'ON' if winner else 'OFF'} version; the other result was discarded.")
        else:
            res = run_pass(use_prompt)

        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(res.lrc, encoding="utf-8")

        embed_note = ""
        if embed_mp3:
            if Path(audio).suffix.lower() != ".mp3":
                embed_note = "Embedding skipped: audio is not MP3."
            else:
                try:
                    _embed_synced_lrc_as_mp3tag_unsyncedlyrics(
                        audio, res.lrc, language or "en"
                    )
                    embed_note = "Timestamped LRC embedded into MP3."
                except Exception as tag_exc:
                    embed_note = f"MP3 tagging failed: {tag_exc}"

        if not embed_mp3 or "embedded" in embed_note:
            studio_resync.complete(queue_path, audio)

        matched = int(getattr(res, "matched", 0))
        total = int(getattr(res, "total", 0))
        pct = (matched / total * 100.0) if total else 0.0
        # How much of the TXT did Whisper really hear? Very little usually means the wrong lyrics.
        try:
            direct, sung = int(getattr(res, "t9_direct_lines", 0)), int(getattr(res, "t9_sung_lines", 0))
            LYRICS_MATCH.record(audio, direct, sung, lyrics)
            entry = LYRICS_MATCH.suspect_entry(audio, lyrics)
            if entry:
                slog(f"⚠ Check the song lyrics — they might be wrong: only {direct} of {sung} lines "
                     f"({entry['ratio'] * 100:.0f}%) matched what Whisper heard in the audio.")
            else:
                slog(f"   Lyrics match: {direct}/{sung} lines heard directly.")
        except Exception as match_exc:
            slog(f"   (lyrics match not recorded: {match_exc})")
        return out_path, matched, total, pct, embed_note

    def sync_worker(audio, lyrics, output, model_name, language, threshold,
                    use_prompt, separate_vocals, embed_mp3, two_pass=False):
        try:
            _ensure_windowless_standard_streams()
            win.after(0, lambda: sync_status_var.set("Loading Syncalong / Whisper..."))
            slog(f"Audio: {audio}")
            slog(f"Lyrics: {lyrics}")
            slog(f"Model: {model_name}")
            slog(f"Language: {language or 'auto'}")
            slog(f"Threshold: {threshold}")
            if separate_vocals:
                slog("Vocal separation: enabled (Demucs)")
            if two_pass:
                slog("Mode: 2-pass (lyrics prompt ON, then OFF) — the better LRC is kept")
            slog("Loading Whisper model and transcribing audio... This can take a while.")

            warnings.filterwarnings(
                "ignore",
                message=r"Failed to launch Triton kernels.*",
                category=UserWarning,
                module=r"whisper\.timing",
            )
            tx = get_transcriber(model_name)
            if separate_vocals:
                tx._separate_vocals = lambda path: _separate_sync_vocals(path, slog)

            win.after(0, lambda: sync_status_var.set("Transcribing / aligning lyrics..."))
            out_path, matched, total, pct, embed_note = _align_one_with_transcriber(
                tx, audio, lyrics, output, language, threshold,
                use_prompt, separate_vocals, embed_mp3, two_pass,
            )

            slog("")
            slog("✓ Synchronization completed")
            slog(f"Matched lines: {matched}/{total} ({pct:.1f}%)")
            slog(f"Saved: {out_path}")
            if embed_note:
                slog(("✓ " if "embedded" in embed_note.lower() else "⚠ ") + embed_note)
            if total and matched < total:
                slog(f"⚠ {total - matched} line(s) were not confidently matched by Syncalong.")

            win.after(0, lambda: sync_status_var.set(
                f"Done — {matched}/{total} lines matched ({pct:.1f}%)."
            ))
            suspect = LYRICS_MATCH.suspect_entry(audio, lyrics)
            win.after(0, refresh_track_badges)
            if suspect:
                win.after(0, lambda e=suspect: messagebox.showwarning(
                    "Lyrics Sync — check the lyrics",
                    f"Synchronization completed, but the lyrics might be wrong: only {e['direct']} of {e['sung']} "
                    f"lines ({e['ratio'] * 100:.0f}%) matched what Whisper heard in the audio.\n\n"
                    "Check the TXT (right-click the song → View/Change Lyrics) before fixing the timing.\n\n"
                    f"Saved:\n{out_path}", parent=win))
            else:
                win.after(0, lambda: messagebox.showinfo(
                    "Lyrics Sync",
                    f"Synchronization completed.\n\nMatched: {matched}/{total} lines ({pct:.1f}%)\n\n"
                    f"Saved:\n{out_path}" + (f"\n\n{embed_note}" if embed_note else ""),
                    parent=win,
                ))

        except ModuleNotFoundError as e:
            name = getattr(e, "name", "") or str(e)
            msg = f"Missing dependency: {name}\n\nUse Check / Setup Lyrics Sync. Keep Separate vocals enabled to install Demucs."
            slog("✗ " + msg.replace("\n", " "))
            win.after(0, lambda: sync_status_var.set("Sync failed — missing dependency."))
            win.after(0, lambda m=msg: messagebox.showerror("Lyrics Sync", m, parent=win))
        except RuntimeError as e:
            err = str(e)
            if "out of memory" in err.lower() or ("cuda" in err.lower() and "memory" in err.lower()):
                release_transcriber()
                err += "\n\nTry a smaller Whisper model in the Lyrics Sync window."
            slog(f"✗ Sync failed: {err}")
            win.after(0, lambda: sync_status_var.set("Sync failed."))
            win.after(0, lambda m=err: messagebox.showerror("Lyrics Sync", m, parent=win))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            slog(f"✗ Sync failed: {err}")
            win.after(0, lambda: sync_status_var.set("Sync failed."))
            win.after(0, lambda m=err: messagebox.showerror("Lyrics Sync", m, parent=win))
        finally:
            try:
                win.after(0, lambda: set_running(False))
            except Exception:
                pass

    def batch_sync_worker(jobs, model_name, language, threshold,
                          use_prompt, separate_vocals, embed_mp3, overwrite_existing, two_pass=False):
        from studio_eta import OperationETA
        completed = []
        failed = []
        skipped = []
        suspect_songs = []
        summary = ""
        try:
            _ensure_windowless_standard_streams()
            warnings.filterwarnings(
                "ignore",
                message=r"Failed to launch Triton kernels.*",
                category=UserWarning,
                module=r"whisper\.timing",
            )

            total_jobs = len(jobs)
            win.after(0, lambda: sync_status_var.set(
                f"Batch: loading {model_name} once for {total_jobs} tracks..."
            ))
            slog(f"Batch sync started: {total_jobs} track(s)")
            slog(f"Model: {model_name} — loaded once and reused for the whole batch")
            slog(f"Language: {language or 'auto'}")
            slog(f"Threshold: {threshold}")
            if separate_vocals:
                slog("Vocal separation: enabled (Demucs)")
            if two_pass:
                slog("Mode: 2-pass (lyrics prompt ON, then OFF) — the better LRC is kept per song")
            slog("")

            # Major batch optimization: load Whisper once and reuse the same
            # Transcriber for every selected song instead of reloading per MP3.
            tx = get_transcriber(model_name)
            if separate_vocals:
                tx._separate_vocals = lambda path: _separate_sync_vocals(path, slog)

            eta = OperationETA(total_jobs)

            for index, job in enumerate(jobs, 1):
                if stop_after_current.is_set():
                    break
                audio = job["audio"]
                lyrics = job["lyrics"]
                output = job["output"]
                name = Path(audio).name

                if Path(output).exists() and not overwrite_existing:
                    skipped.append((audio, "output already exists"))
                    slog(f"[{index}/{total_jobs}] ↷ Skipped: {name} — LRC already exists")
                    eta.advance()
                    continue

                eta_text = eta.label()
                win.after(0, lambda i=index, n=total_jobs, nm=name, et=eta_text:
                          sync_status_var.set(f"Batch {i}/{n}: {nm} • {et}"))
                slog(f"[{index}/{total_jobs}] Syncing: {name}")
                slog(f"   TXT: {Path(lyrics).name}")

                try:
                    if job.get("resync_lrc"):
                        studio_resync.mark(
                            job["resync_lrc"], queue_path,
                            BACKUP_ROOT,
                            _find_plain_txt_for_audio, remove_embedded_lyrics,
                        )
                        slog("   Backed up and removed LRC + embedded lyrics; plain TXT kept.")
                    out_path, matched, total, pct, embed_note = _align_one_with_transcriber(
                        tx, audio, lyrics, output, language, threshold,
                        use_prompt, separate_vocals, embed_mp3, two_pass,
                    )
                    completed.append((audio, out_path, matched, total, pct))
                    if LYRICS_MATCH.suspect_entry(audio, lyrics):
                        suspect_songs.append(os.path.basename(audio))
                    slog(f"   ✓ {matched}/{total} lines ({pct:.1f}%)")
                    slog(f"   Saved: {out_path}")
                    if embed_note:
                        slog(("   ✓ " if "embedded" in embed_note.lower() else "   ⚠ ") + embed_note)
                except Exception as item_exc:
                    failed.append((audio, f"{type(item_exc).__name__}: {item_exc}"))
                    slog(f"   ✗ Failed: {type(item_exc).__name__}: {item_exc}")

                eta.advance()
                slog(f"   {eta.label()}")
                slog("")

            total_matched = sum(x[2] for x in completed)
            total_lines = sum(x[3] for x in completed)
            overall_pct = (total_matched / total_lines * 100.0) if total_lines else 0.0

            slog("Batch summary:")
            slog(f"   Completed: {len(completed)}")
            slog(f"   Failed: {len(failed)}")
            slog(f"   Skipped: {len(skipped)}")
            remaining = total_jobs - len(completed) - len(failed) - len(skipped)
            slog(f"   Not started: {remaining}")
            if completed:
                slog(f"   Matched lines overall: {total_matched}/{total_lines} ({overall_pct:.1f}%)")

            win.after(0, lambda: sync_status_var.set(
                f"Batch {'stopped' if remaining else 'done'} — {len(completed)} completed, {len(failed)} failed, {remaining} not started."
            ))
            summary = (
                f"Batch synchronization {'stopped after current track' if remaining else 'completed'}.\n\n"
                f"Completed: {len(completed)}\n"
                f"Failed: {len(failed)}\n"
                f"Skipped: {len(skipped)}\nNot started: {remaining}"
            )
            if completed:
                summary += f"\n\nMatched lines overall: {total_matched}/{total_lines} ({overall_pct:.1f}%)"
            if failed:
                summary += "\n\nFailed tracks are listed in the log."
            if suspect_songs:
                slog("⚠ Lyrics that might be wrong (few lines matched the audio): " + "; ".join(suspect_songs))
                summary += (f"\n\n⚠ Check the lyrics of {len(suspect_songs)} song(s) — few lines matched what "
                            "Whisper heard:\n" + "\n".join("  • " + name for name in suspect_songs[:12])
                            + ("\n  …" if len(suspect_songs) > 12 else ""))
            win.after(0, refresh_track_badges)
            win.after(0, lambda m=summary: messagebox.showinfo(
                "Lyrics Sync — Batch", m, parent=win
            ))

        except ModuleNotFoundError as e:
            name = getattr(e, "name", "") or str(e)
            msg = f"Missing dependency: {name}\n\nUse Check / Setup Lyrics Sync. Keep Separate vocals enabled to install Demucs."
            slog("✗ " + msg.replace("\n", " "))
            win.after(0, lambda: sync_status_var.set("Batch failed — missing dependency."))
            win.after(0, lambda m=msg: messagebox.showerror("Lyrics Sync — Batch", m, parent=win))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if "memory" in err.lower():
                release_transcriber()  # reload cleanly next time instead of reusing a broken model
            slog(f"✗ Batch failed before completion: {err}")
            win.after(0, lambda: sync_status_var.set("Batch failed."))
            win.after(0, lambda m=err: messagebox.showerror("Lyrics Sync — Batch", m, parent=win))
        finally:
            try:
                win.after(0, lambda: set_running(False))
            except Exception:
                pass

    def _common_sync_settings():
        try:
            threshold = max(0, min(100, int(float(threshold_var.get()))))
        except Exception:
            threshold = 55
            threshold_var.set("55")
        label = model_var.get()
        model_name, required_vram = SYNCALONG_MODEL_BY_LABEL.get(label, ("turbo", 6.0))
        language = language_var.get().strip().lower()
        if language == "auto":
            language = ""
        return threshold, model_name, required_vram, language

    def _preflight_sync(model_name, required_vram, check_demucs=True):
        if gpu_info.get("checking"):
            messagebox.showinfo(
                "GPU check still running",
                "Lyrics Sync opened immediately while NVIDIA / CUDA is checked in the "
                "background. Wait a moment for the Hardware status to update, then start again.",
                parent=win,
            )
            return False
        if check_demucs and vocals_var.get() and importlib.util.find_spec("demucs") is None:
            messagebox.showwarning("Demucs required", "Separate vocals needs the optional Demucs package.\nClick Check / Setup Lyrics Sync with Separate vocals enabled, then try again.", parent=win)
            return False
        if not ffmpeg_ok:
            if not messagebox.askyesno(
                "FFmpeg not detected",
                "FFmpeg was not found in PATH. Local Syncalong/Whisper normally requires it.\n\nStart anyway?",
                parent=win,
            ):
                return False

        if gpu_info["cuda"] and required_vram > gpu_info["vram_gb"]:
            if not messagebox.askyesno(
                "VRAM warning",
                f"Selected model: {model_name}\nApprox. VRAM requirement: ~{required_vram:g} GB\n"
                f"Detected GPU: {gpu_info['gpu']} ({gpu_info['vram_gb']:.1f} GB)\n\n"
                "This model may run out of GPU memory. A smaller model is recommended.\n\nStart anyway?",
                parent=win,
            ):
                return False
        elif not gpu_info["cuda"]:
            if not messagebox.askyesno(
                "CUDA not available",
                "CUDA is not available to this Python environment. Syncalong can continue on CPU, "
                "but it may be much slower.\n\nStart anyway?",
                parent=win,
            ):
                return False
        return True

    def start_sync(two_pass=False):
        if running["value"] or setup_running["value"]:
            return
        save_sync_preferences()

        threshold, model_name, required_vram, language = _common_sync_settings()
        if not _preflight_sync(model_name, required_vram):
            return

        # -------- Batch mode --------
        if batch_jobs:
            jobs = [dict(job) for job in batch_jobs]

            # Revalidate immediately before starting in case files were moved.
            valid_jobs = []
            vanished = 0
            for job in jobs:
                if os.path.isfile(job["audio"]) and os.path.isfile(job["lyrics"]):
                    valid_jobs.append(job)
                else:
                    vanished += 1
            jobs = valid_jobs
            if not jobs:
                messagebox.showwarning(
                    "Lyrics Sync — Batch",
                    "None of the queued jobs still has both the audio file and matching .txt lyrics.",
                    parent=win,
                )
                return

            if bool(embed_mp3_var.get()):
                non_mp3 = sum(1 for job in jobs if Path(job["audio"]).suffix.lower() != ".mp3")
                if non_mp3 and not messagebox.askyesno(
                    "MP3 tagging",
                    f"{non_mp3} queued audio file(s) are not MP3.\n\n"
                    "Their LRC files will still be created, but embedding will be skipped.\n\nContinue?",
                    parent=win,
                ):
                    return

            existing_count = sum(1 for job in jobs if Path(job["output"]).exists())
            overwrite_existing = True
            if existing_count:
                answer = messagebox.askyesnocancel(
                    "Existing synchronized lyrics",
                    f"{existing_count} output LRC file(s) already exist.\n\n"
                    "Yes = overwrite existing files\n"
                    "No = skip existing files\n"
                    "Cancel = do not start",
                    parent=win,
                )
                if answer is None:
                    return
                overwrite_existing = bool(answer)

            if two_pass and not messagebox.askyesno(
                "2 Pass sync",
                f"Sync {len(jobs)} track(s) twice each: lyrics prompt ON, then OFF.\n\n"
                "For every song only the better LRC is kept (fewer suspicious scan "
                "warnings first, then more directly matched lines). This takes about "
                "twice as long as a normal batch.\n\nStart?",
                parent=win,
            ):
                return

            sync_log.delete("1.0", "end")
            set_running(True)
            sync_status_var.set(f"Starting {'2-pass ' if two_pass else ''}batch — {len(jobs)} tracks...")
            threading.Thread(
                target=batch_sync_worker,
                args=(
                    jobs, model_name, language, threshold,
                    bool(prompt_var.get()), bool(vocals_var.get()),
                    bool(embed_mp3_var.get()), overwrite_existing, two_pass,
                ),
                daemon=True,
            ).start()
            return

        # -------- Existing single-file mode --------
        audio = audio_var.get().strip()
        lyrics = lyrics_var.get().strip()
        output = output_var.get().strip()
        if not audio or not os.path.isfile(audio):
            messagebox.showwarning("Lyrics Sync", "Choose a valid audio file first.", parent=win)
            return
        if not lyrics or not os.path.isfile(lyrics):
            messagebox.showwarning("Lyrics Sync", "Choose a valid plain lyrics .txt file first.", parent=win)
            return
        if not output:
            output = _default_synced_output(audio, lyrics)
            output_var.set(output)

        if bool(embed_mp3_var.get()) and Path(audio).suffix.lower() != ".mp3":
            if not messagebox.askyesno(
                "MP3 tagging",
                "The UNSYNCEDLYRICS option can only tag MP3 files.\n\n"
                "The LRC can still be created, but embedding will be skipped.\n\nContinue?",
                parent=win,
            ):
                return

        if Path(output).exists():
            if not messagebox.askyesno(
                "Overwrite LRC",
                f"This file already exists:\n\n{output}\n\nOverwrite it?",
                parent=win,
            ):
                return

        sync_log.delete("1.0", "end")
        set_running(True)
        sync_status_var.set("Starting...")
        threading.Thread(
            target=sync_worker,
            args=(
                audio, lyrics, output, model_name, language, threshold,
                bool(prompt_var.get()), bool(vocals_var.get()), bool(embed_mp3_var.get()),
                two_pass,
            ),
            daemon=True,
        ).start()

    def request_sync_stop():
        if not running["value"] or setup_running["value"] or stop_after_current.is_set():
            return
        stop_after_current.set()
        stop_sync_btn.configure(state="disabled", text="Stopping after this track…")
        sync_status_var.set("Finishing current track — queue will stop afterwards")
        slog("Stop requested: the current track will finish and save; no further tracks will start.")

    stop_sync_btn = styled_button(action_bar, "Stop after current track", request_sync_stop, 24)
    stop_sync_btn.configure(state="disabled")
    stop_sync_btn.pack(side="right", padx=(8, 0))
    start_btn = styled_button(action_bar, "▶  START SYNC", lambda: start_sync(two_pass=bool(two_pass_var.get())), 22)
    start_btn.configure(font=("Segoe UI", 11, "bold"), padx=12, pady=7, bg=theme["sel_bg"], fg=theme["sel_fg"],
                        activebackground=theme["sel_bg"], activeforeground=theme["sel_fg"])
    start_btn.pack(side="right", padx=(10, 0))
    two_pass_btn = styled_button(action_bar, "Start Sync 2 Pass (Slow)", lambda: start_sync(two_pass=True), 22)
    controls.append(two_pass_btn)  # kept for older code paths; the "2 Pass sync" tick box replaces it
    create_tooltip(two_pass_btn, "Syncs every queued song twice — lyrics prompt ON, then OFF — "
                                 "and keeps only the better LRC for each song. About twice as slow.")
    _refresh_batch_ui()
    close_btn = styled_button(action_bar, "Close", close_sync_window, 10)  # the window's X does the same
    scan_lrc_btn = styled_button(
        selected_row, "Edit and Fix Synced Lyrics", scan_lrc_library, 27
    )
    scan_lrc_btn.pack(side="right")
    controls.append(scan_lrc_btn)
    marked_sync_btn = styled_button(selected_row, "Marked for Lyrics Sync", open_marked_sync)
    marked_sync_btn.pack(side="right", padx=6)
    controls.append(marked_sync_btn)
    failure_ctx.__dict__.update(
        _common_sync_settings=_common_sync_settings,
        _preflight_sync=_preflight_sync,
        _refresh_batch_ui=_refresh_batch_ui,
        audio_var=audio_var,
        batch_jobs=batch_jobs,
        batch_sync_worker=batch_sync_worker,
        embed_mp3_var=embed_mp3_var,
        get_transcriber=get_transcriber,
        ignored_path=ignored_path,
        lrc_scan_running=lrc_scan_running,
        lyrics_var=lyrics_var,
        output_var=output_var,
        prompt_var=prompt_var,
        queue_path=queue_path,
        resync_dir=resync_dir,
        running=running,
        scan_lrc_btn=scan_lrc_btn,
        set_running=set_running,
        setup_running=setup_running,
        slog=slog,
        styled_button=styled_button,
        sync_status_var=sync_status_var,
        theme=theme,
        vocals_var=vocals_var,
        win=win,
    )

    def prefill_from_selection():
        # If the user opens Lyrics Sync with selected tracks, prefill single mode
        # or build a compact batch queue automatically.
        try:
            if get_selected_artist_name() and len(track_list.curselection()) >= 1:
                use_selected_track()
        except Exception:
            pass

    def sync_song_now(song):
        """Lyric Sync Now (View/Change Lyrics): load one song with its TXT; the user clicks Start."""
        if running["value"] or setup_running["value"]:
            messagebox.showinfo("Lyric Sync Now", "A sync is already running — wait until it finishes, "
                                "then click Lyric Sync Now again.", parent=win)
            return
        lyrics = _find_plain_txt_for_audio(song)
        if not os.path.isfile(song) or not lyrics:
            messagebox.showinfo("Lyric Sync Now", "This song has no TXT lyrics to sync: "
                                + os.path.basename(song), parent=win)
            return
        batch_jobs.clear()
        audio_var.set(song)
        lyrics_var.set(lyrics)
        output_var.set(_default_synced_output(song, lyrics))
        _refresh_batch_ui()
        log("Lyric Sync Now: loaded " + os.path.basename(song) + " — check the settings and click Start Sync.")

    def fix_song_now(song):
        """Right-click → Fix this song: Fix fragment at the song's first error passage."""
        WARNING_INDEX.begin()
        info = WARNING_INDEX.for_audio(song)
        if not info:  # no warning: still open the same window on the song's LRC, from its first line
            lrc = WARNING_INDEX.lrc_for(song)
            if not lrc:
                messagebox.showinfo("Fix fragment manually", "This song has no synced lyrics (LRC) yet.", parent=win)
                return
            failure_tools["open_fragment_fix"](str(lrc), 0, 0, win, on_finished=lambda saved: refresh_track_badges())
            return
        failure_tools["open_fragment_fix"](info["lrc"], info["first_line"], info["last_line"], win,
                                           on_finished=lambda saved: refresh_track_badges())

    def reopen(scan=False, song=None, fix=None):
        """Bring this window back instead of opening a second one (and a second model)."""
        win.deiconify()
        win.lift()
        win.focus_force()
        if fix:
            fix_song_now(fix)
            return
        if song:
            sync_song_now(song)
            return
        if running["value"] or setup_running["value"]:
            return
        if scan:
            scan_lrc_library()
        else:
            prefill_from_selection()

    _SYNC_WINDOW.update(win=win, reopen=reopen)
    if fix_song:
        win.after(300, lambda: fix_song_now(fix_song))
    elif sync_song:
        win.after(300, lambda: sync_song_now(sync_song))
    else:
        prefill_from_selection()
    if start_scan:
        win.after(300, scan_lrc_library)


_builder_process = None


def open_in_lyric_builder(song, lyrics_txt, parent=None):
    """Open Lyric Builder with ``song`` loaded and its TXT pasted as the shorter reference.

    The request goes through a small file the Builder checks every second, so a Builder that is
    already open loads the song too (it asks before replacing its current reference/result)."""
    request = Path(__file__).resolve().parent / "lyrics_sync_state" / "builder_request.json"
    try:
        request.parent.mkdir(parents=True, exist_ok=True)
        temporary = request.with_suffix(".tmp")
        temporary.write_text(json.dumps({"audio": str(song), "reference": str(lyrics_txt or ""), "time": time.time()},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, request)
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"Could not hand the song to Lyric Builder:\n{exc}", parent=parent or root)
        return
    log("Open in Lyric Builder: " + os.path.basename(song) + (" (shorter reference: " + lyrics_txt + ")" if lyrics_txt else ""))
    if _builder_process is not None and _builder_process.poll() is None:
        return  # the open Builder picks the request up within a second
    open_builder_window()


def open_builder_window():
    """Run Builder in its own interpreter so its Stop/CUDA lifecycle is isolated."""
    global _builder_process
    if _builder_process is not None and _builder_process.poll() is None:
        messagebox.showinfo(APP_NAME, "Lyric Builder is already open. Switch to its window using the taskbar.", parent=root)
        return
    script = Path(__file__).resolve().parent / 'Triplenine_Hybrid_Sessions_Lyrics_Builder.py'
    if not script.is_file():
        messagebox.showerror(APP_NAME, f"Lyric Builder was not found:\n{script}", parent=root)
        return
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name('pythonw.exe')
    if os.name == 'nt' and windowless.is_file():
        interpreter = windowless
    try:
        _builder_process = subprocess.Popen([str(interpreter), str(script)], cwd=str(script.parent))
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"Could not open Lyric Builder:\n{exc}", parent=root)


# ------------------ UI build ------------------

root = tk.Tk()
studio_scrollbars.install(root)  # light grey scroll bars instead of the white ones
root.title("Triple 9 Studio - Downloader")
# Put a valid Windows ICO named "triplenine.ico" next to the script/exe.
# Recommended ICO: square, with 16/32/48/64/128/256 px layers.
# PyInstaller EXE icon: --icon=triplenine.ico
# To make iconbitmap work inside --onefile as well, bundle it too:
# --add-data "triplenine.ico;."
try:
    if os.path.isfile(ICON_FILE):
        root.iconbitmap(ICON_FILE)
        # The default for every window created later, even one that forgets create_toplevel().
        root.iconbitmap(default=ICON_FILE)
except Exception:
    pass
# Message boxes drawn by the app (Windows' own ones never show the triplenine icon).
studio_dialogs.install(ICON_FILE if os.path.isfile(ICON_FILE) else None,
                       lambda: THEMES[config.get("theme", "dark")])

# Restore saved window geometry or use default
_saved_geo = config.get("window_geometry", "1180x800")
try:
    root.geometry(_saved_geo)
except:
    root.geometry("1180x800")

def save_window_geometry():
    try:
        config["window_geometry"] = root.geometry()
        save_config(config)
    except:
        pass

root.protocol("WM_DELETE_WINDOW", lambda: [save_window_geometry(), root.destroy()])

menubar = tk.Menu(root)

settings_menu = tk.Menu(menubar, tearoff=0)
theme_menu = tk.Menu(settings_menu, tearoff=0)
theme_var = tk.StringVar(value=config.get("theme", "dark"))
theme_menu.add_radiobutton(label="Dark", variable=theme_var, value="dark", command=lambda: apply_theme("dark"))
theme_menu.add_radiobutton(label="Light", variable=theme_var, value="light", command=lambda: apply_theme("light"))
settings_menu.add_cascade(label="Theme", menu=theme_menu)
settings_menu.add_separator()
settings_menu.add_command(label="Options…", command=open_options_window)
menubar.add_cascade(label="Settings", menu=settings_menu)

help_menu = tk.Menu(menubar, tearoff=0)
help_menu.add_command(label="Open GitHub", command=open_github)
help_menu.add_command(label="Check for updates…", command=lambda: check_for_updates(True))
auto_update_var = tk.BooleanVar(value=bool(config.get("auto_update_check", True)))


def _toggle_auto_update():
    config["auto_update_check"] = bool(auto_update_var.get())
    save_config(config)


help_menu.add_checkbutton(label="Check for updates at startup", variable=auto_update_var, command=_toggle_auto_update)
help_menu.add_command(label=f"Version {studio_version.VERSION}", state="disabled")
help_menu.add_separator()
help_menu.add_command(label="How To Use", command=about)
menubar.add_cascade(label="Help", menu=help_menu)

root.config(menu=menubar)

app_heading = tk.Label(root, text="DOWNLOADER", font=("Segoe UI", 20, "bold"), anchor="w")
studio_dialogs.add_heading_logo(app_heading, ICON_FILE if os.path.isfile(ICON_FILE) else None)
app_heading.pack(fill="x", padx=14, pady=(8, 0))

help_hint = tk.Label(
    root, text="New here? Help > How To Use.",
    font=("Segoe UI", 10, "bold"), anchor="w"
)
hint_row = tk.Frame(root)  # "Need help…" on the left, what the button under the mouse does next to it
hint_row.pack(fill="x", padx=14, pady=(2, 0))
help_hint.pack(in_=hint_row, side="left")
help_hint.lift()  # it was created before hint_row: keep it on top of it
hover_hint_var = tk.StringVar(value="💡  Hover over any button to see what it does.")
hover_hint = tk.Label(hint_row, textvariable=hover_hint_var, font=("Segoe UI", 10), anchor="w")
hover_hint.pack(side="left", fill="x", expand=True, padx=(18, 0))

# Every button explains itself in a small bubble next to it (the line by "Need help…" only tells you so).
button_hints = studio_hints.Hints(root, None, lambda: THEMES[config.get("theme", "dark")]).install()

top = tk.Frame(root)
top.pack(fill="x", padx=12, pady=(12, 6))

open_btn = tk.Button(top, text="Open Music Folder", command=choose_folder)
open_btn.pack(side="left")

builder_btn = tk.Button(top, text="Lyric Builder", command=open_builder_window)
studio_dialogs.add_heading_logo(builder_btn, ICON_FILE if os.path.isfile(ICON_FILE) else None, scale=1.15)
builder_btn.pack(side="right", padx=(10, 0))

# Song Search filters the Tracks column for the currently selected artist.
search_label = tk.Label(top, text="Song Search:", font=("Segoe UI", 9))
search_label.pack(side="left", padx=(20, 6))

song_search_var = tk.StringVar(value="")
search_entry = tk.Entry(top, textvariable=song_search_var, width=24)
search_entry.pack(side="left")

def on_song_search(*_):
    # Rebuild only Tracks. Artists and Albums stay untouched and folder names
    # are never part of the Song Search haystack.
    refresh_tracks_for_current_artist()

song_search_var.trace_add("write", on_song_search)

main = tk.Frame(root)
main.pack(fill="both", expand=True, padx=12, pady=6)

list_frame = tk.Frame(main)
list_frame.pack(fill="both", expand=True)


def make_scrolled_listbox(parent, width, selectmode=None):
    frame = tk.Frame(parent)
    lb = tk.Listbox(frame, width=width, exportselection=False, selectmode=selectmode)
    sb = tk.Scrollbar(frame, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, lb


def make_track_table(parent):
    """Tracks as a table: name on the left, the tags to the right; scrolls both ways."""
    frame = tk.Frame(parent)
    table = studio_tracktable.TrackTable(frame)
    scroll_y = tk.Scrollbar(frame, orient="vertical", command=table.yview)
    scroll_x = tk.Scrollbar(frame, orient="horizontal", command=table.xview)
    table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    table.pack(side="left", fill="both", expand=True)
    return frame, table


def make_list_card(parent, title, select_all_cmd, width, selectmode, table=False):
    """Create a styled listbox card with header and Select All button"""
    card = tk.Frame(parent, highlightthickness=1, bd=0)
    
    # Header with title and Select All button
    header = tk.Frame(card)
    header.pack(fill="x", padx=2, pady=2)
    
    title_label = tk.Label(header, text=title, font=("Segoe UI", 10, "bold"), anchor="w")
    title_label.pack(side="left", padx=4)
    
    select_btn = tk.Button(header, text="Select All", command=select_all_cmd, 
                          font=("Segoe UI", 8), padx=8, pady=2)
    select_btn.pack(side="right", padx=4)
    
    # Listbox with scrollbar
    list_frame, lb = make_track_table(card) if table else make_scrolled_listbox(card, width, selectmode)
    list_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
    
    return card, header, title_label, select_btn, lb


artist_card, artist_header, artist_title, artist_select_btn, artist_list = \
    make_list_card(list_frame, "Artists", toggle_artist_selection, 25, tk.EXTENDED)

album_card, album_header, album_title, album_select_btn, album_list = \
    make_list_card(list_frame, "Albums", toggle_album_selection, 28, tk.EXTENDED)

track_card, track_header, track_title, track_select_btn, track_list = \
    make_list_card(list_frame, "Tracks", toggle_track_selection, 58, tk.EXTENDED, table=True)

# Live counter for explicit selections in the Tracks list.
# It updates on mouse/keyboard selection and on Select All / Clear All actions.
track_selected_count_label = tk.Label(
    track_header, text="Selected: 0", font=("Segoe UI", 9), anchor="w"
)
track_selected_count_label.pack(side="left", padx=(10, 4))

TRACK_SORT_MODES = ("A-Z", "New", "Lyrics Synced First", "Downloaded first", "Failed first", "Marked first")
saved_track_sort = config.get("track_sort", "A-Z")
track_sort_var = tk.StringVar(value=saved_track_sort if saved_track_sort in TRACK_SORT_MODES else "A-Z")

def on_track_sort_change(_value=None):
    config["track_sort"] = track_sort_var.get()
    save_config(config)
    refresh_tracks_from_disk()

track_sort_menu = tk.OptionMenu(
    track_header, track_sort_var,
    *TRACK_SORT_MODES,
    command=on_track_sort_change
)
track_sort_menu.configure(font=("Segoe UI", 8), padx=4, pady=1)
track_sort_menu.pack(side="right", padx=(4, 8))
track_sort_label = tk.Label(track_header, text="Sort:", font=("Segoe UI", 9))
track_sort_label.pack(side="right", padx=(4, 0))
track_refresh_btn = tk.Button(track_header, text="Refresh", command=refresh_tracks_from_disk)
track_refresh_btn.pack(side="right", padx=4)
track_columns_btn = tk.Button(track_header, text="Columns…", command=lambda: open_track_columns())
track_columns_btn.pack(side="right", padx=4)
root.bind("<F5>", lambda event: refresh_tracks_from_disk())

artist_card.pack(side="left", fill="both", padx=(0, 6), pady=4)
track_card.pack(side="left", fill="both", padx=(0, 0), pady=4, expand=True)

artist_list.bind("<<ListboxSelect>>", on_artist_select)
album_list.bind("<<ListboxSelect>>", on_album_select)

def update_track_selection_counter():
    """Refresh the live number of explicitly highlighted rows in Tracks."""
    count = len(track_list.curselection())
    track_selected_count_label.configure(text=f"Selected: {count}")


def on_track_select(event=None):
    # Update track button text and the live selection counter.
    selection_count = len(track_list.curselection())
    if selection_count:
        if selection_count == track_list.size() and track_list.size() > 0:
            track_select_btn.configure(text="Clear All")
        else:
            track_select_btn.configure(text="Select All")
    else:
        track_select_btn.configure(text="Select All")
    update_track_selection_counter()


def _sync_operation_row():
    """Pause / Cancel appear above the log only while a download or scan runs."""
    busy = downloading or str(cancel_btn.cget("state")) == "normal"
    shown = bool(operation_controls.winfo_manager())
    if busy and not shown:
        operation_controls.pack(fill="x", pady=(0, 6), before=log_header)
    elif not busy and shown:
        operation_controls.pack_forget()


def update_action_context():
    rows = tuple(track_list.curselection())
    artists = tuple(artist_list.curselection())
    idle = not downloading
    for button, enabled in (
        (track_dl_btn, bool(rows) or bool(artists)), (artist_dl_btn, bool(artists)),
        (delete_lyrics_btn, bool(rows)), (transcription_btn, bool(rows)),
        (delete_synced_btn, bool(rows)),
        (delete_reference_btn, bool(rows)), (remove_embedded_btn, bool(rows)),
        (embed_lrc_btn, len(rows) == 1 and Path(TRACK_ROW_PATHS.get(rows[0], '')).suffix.lower() == '.mp3'),
        (manual_embed_btn, any(Path(TRACK_ROW_PATHS.get(row, '')).suffix.lower() == '.mp3' for row in rows)),
        (edit_lyrics_btn, len(rows) == 1), (rename_song_btn, len(rows) == 1),
        (play_song_btn, len(rows) == 1), (undo_lyrics_btn, bool(_last_deleted_lyrics)),
        *(((lrc_tools_btn, any(Path(TRACK_ROW_PATHS.get(row, '')).suffix.lower() == '.mp3' for row in rows)),
           (delete_menu_btn, bool(rows))) if "delete_menu_btn" in globals() else ())):
        state = "normal" if idle and enabled else "disabled"
        if str(button.cget("state")) != state:
            button.configure(state=state)
    scope = ""  # the number of selected tracks is in the Tracks header; here only the song's state
    if len(rows) == 1:
        # Use the already rendered status to avoid disk scans in the UI timer.
        display = track_list.get(rows[0])
        if display.startswith("…"):
            scope += "Checking lyrics status…"
        elif display.startswith("🎶"):
            scope += "Synced lyrics embedded in audio"
            song = TRACK_ROW_PATHS.get(rows[0], "")
            if song and os.path.isfile(missing_txt_path(song)):
                scope += " • Lyrics TXT available"
        elif display.startswith("📄"):
            scope += "Synced LRC file available • Not embedded in audio"
        elif display.startswith(("✅", "⏱")):
            scope += "Lyrics downloaded • Not synced"
        elif display.startswith("📝✅"):
            scope += "Marked for transcription • Shorter reference ready"
        elif display.startswith("📝❌"):
            scope += "Marked for transcription • Shorter reference missing"
        elif display.startswith("❌"):
            scope += "Download failed • No lyrics"
        else:
            scope += "No lyrics • Not synced"
    if scope_var.get() != scope:
        scope_var.set(scope)
    path = TRACK_ROW_PATHS.get(rows[0], "") if rows else ""
    text = (str(Path(path).absolute()) if path else "Select a track to see its full file path")
    if len(rows) > 1:
        text = f"First of {len(rows)} selected: " + text
    if selected_path_var.get() != text:
        selected_path_var.set(text)

track_list.bind("<<ListboxSelect>>", on_track_select)


def on_track_heading(key):
    """Heading click: sort by that tag column (again = descending, once more = back to the Sort menu's order)."""
    if key not in studio_tracktable.TAG_KEYS:  # the Name column: back to the order of the Sort menu
        COLUMN_SORT.update(key="", reverse=False)
    elif COLUMN_SORT["key"] != key:
        COLUMN_SORT.update(key=key, reverse=False)
    elif not COLUMN_SORT["reverse"]:
        COLUMN_SORT["reverse"] = True
    else:
        COLUMN_SORT.update(key="", reverse=False)
    config["track_col_sort"], config["track_col_reverse"] = COLUMN_SORT["key"], COLUMN_SORT["reverse"]
    save_config(config)
    track_list.mark_sort(COLUMN_SORT["key"], COLUMN_SORT["reverse"])
    refresh_track_badges()


def open_track_columns():
    """Tracks → Columns: tick the tag columns that should be shown (applied at once, remembered)."""
    t = THEMES[config.get("theme", "dark")]
    win = create_toplevel(root)
    win.title("Columns")
    win.configure(bg=t["bg"])
    win.transient(root)
    popup_over_root(win, 380, 400)
    shown = set(config.get("track_columns", studio_tracktable.TAG_KEYS))
    tk.Label(win, text="Show these columns in Tracks:", bg=t["bg"], fg=t["fg"], anchor="w").pack(
        fill="x", padx=14, pady=(12, 6))
    variables = {}

    def apply_choice(_key=None):
        chosen = [key for key in studio_tracktable.TAG_KEYS if variables[key].get()]
        config["track_columns"] = chosen
        track_list.set_visible(chosen)
        config["track_col_layout"] = track_list.layout()
        save_config(config)

    for key, heading, *_rest in studio_tracktable.COLUMNS[1:]:
        variables[key] = tk.BooleanVar(value=key in shown)
        check_row(win, heading, variables[key], apply_choice, t).pack(fill="x", padx=14)

    def show_all():
        for variable in variables.values():
            variable.set(True)
        apply_choice()

    def default_order():
        track_list.default_layout()
        apply_choice()

    buttons = tk.Frame(win, bg=t["bg"])
    buttons.pack(fill="x", padx=14, pady=12)
    tk.Button(buttons, text="Show all columns", command=show_all, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="left")
    tk.Button(buttons, text="Default order", command=default_order, bg=t["btn_bg"], fg=t["btn_fg"]).pack(
        side="left", padx=(6, 0))
    tk.Button(buttons, text="Close", command=win.destroy, bg=t["btn_bg"], fg=t["btn_fg"]).pack(side="right")


COLUMN_SORT.update(key=config.get("track_col_sort", ""), reverse=bool(config.get("track_col_reverse", False)))
if COLUMN_SORT["key"] not in studio_tracktable.TAG_KEYS:
    COLUMN_SORT["key"] = ""
def save_track_layout():
    """Remember the order and widths of the Tracks columns (after dragging a heading or a border)."""
    layout = track_list.layout()
    if config.get("track_col_layout") != layout:
        config["track_col_layout"] = layout
        save_config(config)


track_list.apply_layout(config.get("track_col_layout"))
track_list.on_layout_changed = save_track_layout
track_list.set_heading_command(on_track_heading)
track_list.mark_sort(COLUMN_SORT["key"], COLUMN_SORT["reverse"])
if "track_columns" in config and not config.get("track_col_modified_added"):
    config["track_columns"] = list(config["track_columns"]) + ["modified"]  # the new Date modified column starts visible
    config["track_col_modified_added"] = True
    save_config(config)
track_list.set_visible(config.get("track_columns", studio_tracktable.TAG_KEYS))
track_list.bind("<Double-Button-1>", lambda e: open_replace_lyrics() if track_list.identify_region(e.x, e.y) == "cell" else None)

_row_tip = {"win": None, "row": None}


def _hide_row_tip(_event=None):
    if _row_tip["win"] is not None:
        _row_tip["win"].destroy()
    _row_tip["win"], _row_tip["row"] = None, None


def _on_track_motion(event):
    """Tooltip when the mouse is over the icons (⚠ ❕ ❓) at the start of a row."""
    row = track_list.nearest(event.y)
    box = track_list.bbox(row) if row >= 0 else None
    tip = TRACK_ROW_TIPS.get(row) if box and box[1] <= event.y <= box[1] + box[3] else None
    if tip:
        from tkinter import font as tkfont
        match = ICON_STRIP_RE.match(track_list.get(row))
        icon_width = tkfont.Font(font=track_list.cget("font")).measure(match.group(0) if match else "") + 10
        if event.x > icon_width:
            tip = None
    if not tip:
        _hide_row_tip()
        return
    if _row_tip["row"] == row:
        return
    _hide_row_tip()
    t = THEMES[config.get("theme", "dark")]
    tw = tk.Toplevel(track_list)
    tw.wm_overrideredirect(True)
    tw.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 18}")
    tk.Label(tw, text=tip, justify="left", background=t["panel"], foreground=t["fg"], relief="solid",
             borderwidth=1, font=("Segoe UI", 9), padx=8, pady=5, wraplength=520).pack()
    _row_tip["win"], _row_tip["row"] = tw, row


def fix_song_in_editor(song):
    """Right-click → Fix this song: open the Fix fragment window at the song's first error."""
    open_syncalong_window(fix_song=song)


def _on_track_right_click(event):
    row = track_list.nearest(event.y)
    if row not in TRACK_ROW_PATHS:
        return
    if row not in track_list.curselection():
        track_list.selection_clear(0, tk.END)
        track_list.selection_set(row)
        on_track_select()
    song = TRACK_ROW_PATHS[row]
    try:
        WARNING_INDEX.begin()
        info = WARNING_INDEX.for_audio(song)
    except Exception:
        info = None
    menu = tk.Menu(root, tearoff=0)
    if info or WARNING_INDEX.lrc_for(song):
        icon = ("⚠ " if info["level"] == "error" else "❕ ") if info else ""
        menu.add_command(label=icon + "Fix fragment manually…", command=lambda: fix_song_in_editor(song))
    if "❓" in track_list.get(row)[:8]:
        menu.add_command(label="❓ Lyrics might be wrong — View/Change Lyrics…", command=open_replace_lyrics)
    else:
        menu.add_command(label="View/Change Lyrics…", command=open_replace_lyrics)
    menu.add_command(label="Change Metadata…", command=open_edit_metadata)
    menu.add_command(label="Play Song", command=play_song)
    menu.add_command(label="Sync this song (Lyrics Sync)…", command=lambda: open_syncalong_window(sync_song=song))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


track_list.bind("<Motion>", _on_track_motion)
track_list.bind("<Leave>", _hide_row_tip)
track_list.bind("<Button-3>", _on_track_right_click)

bind_ctrl_a(artist_list)
bind_ctrl_a(album_list)
bind_ctrl_a(track_list)

bottom = tk.Frame(root)  # actions, legend and log: the track lists get all the free height
bottom.pack(fill="x", padx=12, pady=(6, 8))

context_row = tk.Frame(bottom)
context_row.pack(fill="x", pady=(0, 6))
scope_var = tk.StringVar()
scope_label = tk.Label(context_row, textvariable=scope_var, anchor="w", font=("Segoe UI", 9, "bold"))
scope_label.pack(side="left")
selected_path_var = tk.StringVar()
selected_path_entry = tk.Entry(context_row, textvariable=selected_path_var, state="readonly", font=("Segoe UI", 9))
selected_path_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

action_panel = ActionPanel(bottom)
action_panel.pack(fill="x", pady=(0, 10))
btn_row = action_panel.add('download', 'Download')
track_actions_row = action_panel.add('edit', 'Track & lyrics')
transcription_row = action_panel.add('transcription', 'Transcription')
metadata_row = action_panel.add('metadata', 'Metadata')
library_actions_row = action_panel.add('library', 'Repair library')
cleanup_actions_row = action_panel.add('cleanup', 'Cleanup & undo')
action_group_labels = []

def download_selected_smart():
    """Download lyrics for the selected tracks; with no track selected, for the selected artist(s)."""
    if track_list.curselection():
        start_download_tracks()
    elif artist_list.curselection():
        start_download_artist()
    else:
        messagebox.showinfo(APP_NAME, "Select tracks, or an artist, first.", parent=root)


track_dl_btn = tk.Button(btn_row, text="Download Lyrics for Selected", command=download_selected_smart)
artist_dl_btn = tk.Button(btn_row, text="Download Lyrics for Selected Artist", command=start_download_artist)
missing_scan_btn = tk.Button(btn_row, text="Find & Download Missing Lyrics TXT [0]", command=build_missing_for_selection)
shared_lrc_btn = tk.Button(btn_row, text="Get synced lyrics from the shared sheet…", command=open_shared_lrc_import)
create_tooltip(shared_lrc_btn, "Look up the songs in Tracks in the public SYNCED LYRICS DATABASE sheet; download the "
               "matching LRC files into SYNCED LYRICS and embed them (you confirm first).")
delete_lyrics_btn = tk.Button(cleanup_actions_row, text="Delete ALL Lyrics (TXT + LRC)", command=delete_lyrics_for_selected)
delete_synced_btn = tk.Button(cleanup_actions_row, text="Delete Synced LRC for Selected", command=delete_synced_lyrics_for_selected)
create_tooltip(delete_synced_btn, "Delete only timestamped LRC files. Plain TXT and embedded lyrics are kept. Backups support Undo Delete Lyrics.")

play_song_btn = tk.Button(track_actions_row, text="Play Song", command=play_song)
edit_lyrics_btn = tk.Button(track_actions_row, text="View/Change Lyrics…", command=open_replace_lyrics)
embed_lrc_btn = tk.Button(track_actions_row, text="Change LRC Manually", command=change_lrc_manually)
create_tooltip(embed_lrc_btn, "Edit the selected MP3's current LRC. Save updates the LRC and replaces embedded timed lyrics. Both original files are backed up.")
manual_embed_btn = tk.Button(track_actions_row, text="Manually Embed Available LRC", command=embed_selected_lrc)
fix_synced_btn = tk.Button(track_actions_row, text="Edit and Fix Synced Lyrics",
                           command=lambda: open_syncalong_window(start_scan=True))
create_tooltip(fix_synced_btn, "Scan all LRC files for timing problems and open the list: Fix fragment with the "
               "waveform player, manual timing, View/Change Lyrics, resync, ignore.")
create_tooltip(manual_embed_btn, "Embed the existing, already synced LRC of each selected MP3 without syncing again. "
               "The LRC is taken from SYNCED LYRICS, LYRICS or next to the MP3; each MP3 is backed up first.")
rename_song_btn = tk.Button(metadata_row, text="Change Song Name + Title", command=open_change_song_name)
year_comment_btn = tk.Button(metadata_row, text="Set Year from comment (Recorded …)", command=open_year_from_comment)
create_tooltip(year_comment_btn, "Read “(Recorded July 15, 2019)” from the MP3 comments and set the Year tag to 2019. "
               "Uses the selected tracks, else the selected albums / artist, else the whole library. Leaked dates are ignored.")
remove_embedded_btn = tk.Button(cleanup_actions_row, text="Remove embedded lyrics for selected", command=remove_selected_embedded_lyrics, wraplength=230)
repair_lyrics_btn = tk.Button(library_actions_row, text="Check missing TXT / LRC — old names…", command=open_missing_lyrics_repair)
create_tooltip(repair_lyrics_btn, "Scan the opened library for orphan TXT/LRC under old or alternative song names. "
               "Review every match; existing current-name files are backed up before an approved replacement.")
create_tooltip(remove_embedded_btn, "Remove embedded lyrics tags from selected MP3 files. Separate TXT and LRC files are kept. Tracks with a synchronized LRC remain Lyrics Synced.")
clear_btn = tk.Button(top, text="Clear All Selections", command=clear_all_selections)
clear_btn.pack(side="left", after=search_entry, padx=(8, 0))
transcription_btn = tk.Button(transcription_row, text="Mark for Transcription", command=mark_selected_for_transcription)
reference_btn = tk.Button(transcription_row, text="Find shorter references", command=find_shorter_references)
delete_reference_btn = tk.Button(transcription_row, text="Delete shorter reference", command=delete_shorter_references)
action_panel.set_actions('download', (track_dl_btn, missing_scan_btn, shared_lrc_btn))


def make_menu_button(parent, text, entries):
    """A button that opens a small menu of related actions ("▾" = more choices)."""
    button = tk.Menubutton(parent, text=text + "  ▾", direction="below", indicatoron=False, relief="flat")
    menu = tk.Menu(button, tearoff=0)
    for label, command in entries:
        menu.add_command(label=label, command=command)
    button.configure(menu=menu)
    button.menu = menu
    return button


lrc_tools_btn = make_menu_button(track_actions_row, "LRC tools", (
    ("Edit the LRC of this song…", change_lrc_manually),
    ("Embed the available LRC into the MP3(s)", embed_selected_lrc),
))
action_panel.set_actions('edit', (play_song_btn, edit_lyrics_btn, lrc_tools_btn))
action_panel.set_actions('transcription', (transcription_btn, reference_btn, delete_reference_btn))
fix_synced_repair_btn = tk.Button(library_actions_row, text="Edit and Fix Synced Lyrics",
                                  command=lambda: open_syncalong_window(start_scan=True))
create_tooltip(fix_synced_repair_btn, "Scan all LRC files for timing problems and open the list of songs with "
               "possible sync errors (the ⚠ in Tracks): Fix fragment, Quick listen, fix songs one by one.")
cleanup_backups_btn = tk.Button(cleanup_actions_row, text="Clean old backups…", command=open_backup_cleanup)
create_tooltip(cleanup_backups_btn, "Show how much space song backups use and delete those older than N days. "
               "Program code backups are never deleted.")
tracker_dates_btn = tk.Button(metadata_row, text="Import from Spreadsheet…", command=open_sheet_import)
create_tooltip(tracker_dates_btn, "Paste a Google Sheets / Docs link: the selected songs are found in it (you confirm the "
               "match) and you choose which columns (date, producer, location, artist …) go into the tags.")
swap_song_btn = tk.Button(metadata_row, text="Swap Song and Transfer Metadata…", command=open_swap_metadata)
create_tooltip(swap_song_btn, "Copy the tags, file name, lyrics and front cover of one song onto another, then swap "
               "the two files' places (like Mp3tag copy / paste tag).")
edit_meta_btn = tk.Button(metadata_row, text="Change Metadata…", command=open_edit_metadata)
create_tooltip(edit_meta_btn, "Edit the selected song's file name, title, artist, album, track, year, genre and comment by hand.")
action_panel.set_actions('metadata', (rename_song_btn, edit_meta_btn, swap_song_btn, year_comment_btn, tracker_dates_btn))
duplicates_btn = tk.Button(library_actions_row, text="Search for song duplicates…", command=open_song_duplicates)
create_tooltip(duplicates_btn, "List songs that are probably the same recording (same name without album marks), "
               "with both file locations. Uses the selected albums, else the selected artist folder. (Sessions) etc. are never duplicates.")
bracket_lines_btn = tk.Button(library_actions_row, text="LRC missing bracket lines…", command=open_bracket_lines_check)
create_tooltip(bracket_lines_btn, "Find LRC files that lack TXT lines starting with “(” and ending with “)” — e.g. "
               "“(Yeah) Get right witcha … (Rah, woo)” — and mark those songs for Lyrics Sync.")
check_files_btn = make_menu_button(library_actions_row, "Check lyrics files", (
    ("TXT / LRC under old song names…", open_missing_lyrics_repair),
    ("LRC missing lines in brackets…", open_bracket_lines_check),
))
action_panel.set_actions('library', (fix_synced_repair_btn, duplicates_btn, check_files_btn))
syncalong_btn = tk.Button(top, text="Lyrics Sync", command=open_syncalong_window)
studio_dialogs.add_heading_logo(syncalong_btn, ICON_FILE if os.path.isfile(ICON_FILE) else None, scale=1.15)
syncalong_btn.pack(side="right", padx=(10, 0))
create_tooltip(delete_reference_btn, "Clear the selected tracks' shorter references and keep them marked.\nBackups are saved; MP3 files, finished Lyrics and Ready for Lyric Sync tracks are kept.")

legend_row = tk.Frame(bottom)
legend_row.pack(fill="x", pady=(0, 8))

legend_frame = tk.Frame(legend_row, padx=10, pady=4)
legend_frame.pack(side="left", fill="x", expand=True)
legend_frame.pack_propagate(True)

legend_title = tk.Label(legend_frame, text="Legend:", font=("Segoe UI", 9, "bold"))
legend_title.pack(side="left", padx=(0, 10))

legend_track_synced = tk.Label(
    legend_frame,
    text="🎶 Synced in audio   📄 Timed LRC file   ✅ Downloaded   ⏱ Ready for Lyric Sync   "
         "⚠ Possible sync errors   ❓ Lyrics might be wrong",
    font=("Segoe UI", 9))
legend_track_synced.pack(side="left", padx=(0, 10))
legend_track_transcription = tk.Label(
    legend_frame,
    text="📝✅ / 📝❌ Marked (shorter reference found / not)",
    justify="left", font=("Segoe UI", 9))
legend_track_transcription.pack(side="left", padx=(0, 10))
legend_track_none = tk.Label(legend_frame, text="❌ Failed", font=("Segoe UI", 9))
legend_track_none.pack(side="left")

operation_controls = tk.Frame(bottom)  # shown only while an operation runs (see _sync_operation_row)
operation_label = tk.Label(operation_controls, text="CURRENT OPERATION", font=("Segoe UI", 9, "bold"))
operation_label.pack(side="left", padx=(10, 12))
pause_btn = tk.Button(operation_controls, text="Pause", command=request_pause_resume, state="disabled")
pause_btn.pack(side="left", padx=(0, 6))
cancel_btn = tk.Button(operation_controls, text="Cancel", command=request_cancel, state="disabled")
cancel_btn.pack(side="left")
undo_lyrics_btn = tk.Button(cleanup_actions_row, text="Undo Delete Lyrics", command=undo_delete_lyrics)
delete_menu_btn = make_menu_button(cleanup_actions_row, "Delete lyrics", (
    ("Delete TXT + LRC of selected tracks…", delete_lyrics_for_selected),
    ("Delete only the synced LRC…", delete_synced_lyrics_for_selected),
    ("Remove lyrics embedded in the MP3(s)…", remove_selected_embedded_lyrics),
))
action_panel.set_actions('cleanup', (delete_menu_btn, undo_lyrics_btn, cleanup_backups_btn))
MENU_BUTTONS = (lrc_tools_btn, check_files_btn, delete_menu_btn)
create_tooltip(undo_lyrics_btn, "Restore the last deleted batch from this session. Existing files are never overwritten; older backups remain in BACKUPS.")

studio_action_buttons = (open_btn, builder_btn, syncalong_btn, track_dl_btn, artist_dl_btn,
                         missing_scan_btn, delete_lyrics_btn, delete_synced_btn, undo_lyrics_btn, pause_btn, cancel_btn, play_song_btn, edit_lyrics_btn,
                         rename_song_btn, remove_embedded_btn, clear_btn, transcription_btn, reference_btn, delete_reference_btn,
                         repair_lyrics_btn, embed_lrc_btn, manual_embed_btn, cleanup_backups_btn, fix_synced_btn, fix_synced_repair_btn,
                         year_comment_btn, tracker_dates_btn, duplicates_btn, bracket_lines_btn, swap_song_btn, edit_meta_btn,
                         *MENU_BUTTONS)

log_header = tk.Frame(bottom)
log_header.pack(fill="x")
log_title = tk.Label(log_header, text="Log", font=("Segoe UI", 9, "bold"), anchor="w")
log_title.pack(side="left")
LOG_HEIGHTS = {False: 5, True: 18}
log_expanded = {"value": bool(config.get("log_expanded", False))}


def toggle_log():
    log_expanded["value"] = not log_expanded["value"]
    log_box.configure(height=LOG_HEIGHTS[log_expanded["value"]])
    log_toggle_btn.configure(text="Show less log  ▴" if log_expanded["value"] else "Show more log  ▾")
    config["log_expanded"] = log_expanded["value"]
    save_config(config)
    log_box.see("end")


log_toggle_btn = tk.Button(log_header, text="Show less log  ▴" if log_expanded["value"] else "Show more log  ▾",
                           command=toggle_log, relief="flat", bd=0, padx=8, pady=2, font=("Segoe UI", 8), cursor="hand2")
log_toggle_btn.pack(side="right")

log_frame = tk.Frame(bottom)
log_frame.pack(fill="x")

log_box = tk.Text(log_frame, height=LOG_HEIGHTS[log_expanded["value"]], padx=8, pady=6, wrap="none")
log_scroll = tk.Scrollbar(log_frame, orient="vertical", command=log_box.yview)
log_box.configure(yscrollcommand=log_scroll.set)
log_box.pack(side="left", fill="x", expand=True)
log_scroll.pack(side="right", fill="y")

status_var = tk.StringVar(value="Ready.")
status_bar = tk.Frame(root)
status_bar.pack(fill="x", side="bottom")

status_label = tk.Label(status_bar, textvariable=status_var, anchor="w", padx=10, pady=4)
status_label.pack(fill="x")

# Apply theme
apply_theme(config.get("theme", "dark"))
root.update()
set_titlebar_theme(config.get("theme", "dark") == "dark")

# Setup keyboard shortcuts
setup_keyboard_shortcuts()

# Add tooltips to buttons
create_tooltip(open_btn, "Open a music folder to scan (F5 to refresh)")
create_tooltip(artist_dl_btn, "Download every track for the selected artist(s) (Ctrl+Shift+D)")
create_tooltip(track_dl_btn, "Download lyrics for the highlighted track(s) (Ctrl+D). With no track highlighted, "
               "for every track of the selected artist(s) (Ctrl+Shift+D).")
create_tooltip(syncalong_btn, "Open the Syncalong page: MP3/audio + plain TXT lyrics → timestamped LRC")
create_tooltip(builder_btn, "Build lyrics from audio and shorter references. Open Marked Tracks in Builder to use your transcription queue.")
create_tooltip(cancel_btn, "Cancel current operation (Escape). Saved lyrics and Genius cache are kept.")
create_tooltip(pause_btn, "Pause the batch; click Resume to continue from the current queue position.")
create_tooltip(edit_lyrics_btn, "View the current lyrics/source and add or replace lyrics using a Genius link or TXT file")
create_tooltip(play_song_btn, "Open the selected audio file in your default player")
create_tooltip(track_columns_btn, "Choose which tag columns (File name, Title, Artist, Album …) are shown. "
               "Click a column heading to sort by it.")
create_tooltip(track_refresh_btn, "Refresh files from disk (F5); keep sorting, filters and existing selections")
create_tooltip(rename_song_btn, "Rename the selected audio file and Title tag together, including matching lyrics and transcription reminder")
create_tooltip(transcription_btn, "Create empty TXT reminders for selected tracks in songs left to transcribe; existing files are kept")
create_tooltip(missing_scan_btn, "Check exact TXT files and download missing lyrics: selected tracks, selected artists, or the whole library")
create_tooltip(clear_btn, "Clear all selections in all listboxes")
create_tooltip(artist_select_btn, "Select/Clear all artists")
create_tooltip(track_select_btn, "Select/Clear all tracks")

pump_ui_queue()
if config.get("auto_update_check", True):
    root.after(8000, lambda: check_for_updates(False))  # quietly, a few seconds after the window is up

if MUSIC_DIR and os.path.isdir(MUSIC_DIR):
    load_artists()
else:
    set_status("Ready. Click 'Open Music Folder'.")

_audio_rename_monitor = studio_audio_renames.RenameMonitor(
    root, lambda: MUSIC_DIR,
    lambda: downloading or root.grab_current() is not None,
    lambda title, text: messagebox.askyesno(title, text, parent=root),
    log, refresh_tracks_from_disk,
    Path(__file__).resolve().parent / 'lyrics_sync_state' / 'audio-renames',
)

root.mainloop()
