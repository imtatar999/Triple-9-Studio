"""Update Triple 9 Studio from GitHub: a new version is a new tag (vX.Y.Z) of the repository.

* ``latest()`` finds the highest version tag; ``is_newer`` compares it with the installed ``VERSION``.
* ``download`` fetches that tag's zip; ``apply`` copies the program files out of it over the installed ones.
  Your settings, runtime (Python and packages), tracker cache, backups and music are never touched, and every
  file that is replaced is first saved into a zip in BACKUPS/PROGRAM-UPDATES.
* A git checkout (a folder with ``.git``) is a development copy: it is never updated.
"""
import json
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import studio_version

TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# paths (relative to the program folder, forward slashes) that an update must never overwrite
PROTECTED = (
    "runtime/", "backups/", "private/", ".git/", "synced-lyrics-downloader-main/lyrics_sync_state/",
    "synced-lyrics-downloader-main/backups/", "synced-lyrics-downloader-main/__pycache__/",
)
PROTECTED_NAMES = ("lyrics_gui_config.json", "studio_preferences.json", "window_positions.json")
# files whose change means the installed packages may be out of date: the installer runs again
INSTALL_FILES = ("synced-lyrics-downloader-main/studio_install.py", "synced-lyrics-downloader-main/studio_runtime.py")


def parse(version):
    match = TAG.match(str(version).strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate, current=studio_version.VERSION):
    candidate, current = parse(candidate), parse(current)
    return bool(candidate and current and candidate > current)


def _get(url, limit=None):
    request = urllib.request.Request(url, headers={"User-Agent": "Triple9Studio-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(limit) if limit else response.read()


def latest(repo=studio_version.REPO, fetch=_get):
    """{"version": "1.2.0", "tag": "v1.2.0", "zip": url, "page": url, "notes": text} for the highest version tag, or None."""
    tags = json.loads(fetch(f"https://api.github.com/repos/{repo}/tags?per_page=100"))
    versions = sorted(((parse(t["name"]), t["name"]) for t in tags if parse(t["name"])), reverse=True)
    if not versions:
        return None
    number, tag = versions[0]
    notes = ""
    try:
        notes = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")).get("body") or ""
    except Exception:
        pass
    return {"version": ".".join(map(str, number)), "tag": tag,
            "zip": f"https://github.com/{repo}/archive/refs/tags/{tag}.zip",
            "page": f"https://github.com/{repo}/releases/tag/{tag}", "notes": notes.strip()}


def program_root():
    """The folder that holds Triplenine_Lyrics_Studio.py."""
    return Path(__file__).resolve().parent.parent


def is_development_copy(root=None):
    return (Path(root or program_root()) / ".git").exists()


def download(url, destination, progress=None):
    request = urllib.request.Request(url, headers={"User-Agent": "Triple9Studio-updater"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            out.write(block)
            done += len(block)
            if progress and total:
                progress(done, total)
    return Path(destination)


def protected(relative):
    low = relative.lower()
    if any(low.startswith(prefix) for prefix in PROTECTED) or Path(low).name in PROTECTED_NAMES:
        return True
    return bool(re.search(r"(^|/)tracker/sheet_[^/]+\.json$", low)) or low.endswith((".pyc", ".bundle"))


def apply(zip_path, root=None, backup_dir=None):
    """Copy the files of the downloaded zip over the program. Returns {"changed": [...], "new": [...], "install": bool}."""
    root = Path(root or program_root())
    changed, new = [], []
    with tempfile.TemporaryDirectory() as temporary, zipfile.ZipFile(zip_path) as archive:
        archive.extractall(temporary)
        folders = [p for p in Path(temporary).iterdir() if p.is_dir()]
        source = folders[0] if len(folders) == 1 and not any(p.is_file() for p in Path(temporary).iterdir()) else Path(temporary)
        plan = []
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            if protected(relative):
                continue
            target = root / relative
            if target.is_file():
                if target.read_bytes() == path.read_bytes():
                    continue
                changed.append(relative)
            else:
                new.append(relative)
            plan.append((path, target, relative))
        if backup_dir and changed:
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            with zipfile.ZipFile(backup_dir / f"program_before_update_{stamp}.zip", "w", zipfile.ZIP_DEFLATED) as saved:
                for relative in changed:
                    saved.write(root / relative, relative)
        for path, target, _relative in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    install = any(name in INSTALL_FILES for name in (c.lower() for c in changed + new))
    return {"changed": changed, "new": new, "install": install}
