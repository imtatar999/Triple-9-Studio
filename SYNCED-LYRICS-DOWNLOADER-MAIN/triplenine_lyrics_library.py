"""Portable TXT/LRC library packages for Triple 9 Studio."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


FORMAT_NAME = "t9-lyrics-library"
FORMAT_VERSION = 1
AUDIO_SUFFIXES = {".mp3", ".flac"}
IGNORED_DIRS = {"lyrics", "synced lyrics", "marked for transcription", "backups"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe relative path in package: {value!r}")
    return path


def _disk_path(root: Path, relative: str) -> Path:
    rel = _safe_relative(relative)
    candidate = root.joinpath(*rel.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Package path escapes the music folder: {relative!r}") from exc
    return candidate


def _candidate_relatives(audio_relative: str) -> list[str]:
    audio = _safe_relative(audio_relative)
    parent = audio.parent
    stem = audio.stem
    values = []
    for folder in (parent, parent / "LYRICS", parent / "SYNCED LYRICS"):
        values.extend((folder / f"{stem}.txt", folder / f"{stem}.lrc"))
    return [path.as_posix() for path in values]


def _audio_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name.casefold() not in IGNORED_DIRS]
        for name in sorted(files, key=str.casefold):
            path = Path(current) / name
            if path.suffix.lower() in AUDIO_SUFFIXES:
                yield path


def build_manifest(music_root) -> tuple[dict, dict[str, Path]]:
    root = Path(music_root).resolve()
    if not root.is_dir():
        raise ValueError("Choose a valid music folder first.")
    tracks = {}
    payload = {}
    for audio in _audio_files(root):
        audio_rel = audio.relative_to(root).as_posix()
        sidecars = []
        for sidecar_rel in _candidate_relatives(audio_rel):
            sidecar = _disk_path(root, sidecar_rel)
            if not sidecar.is_file():
                continue
            item = {
                "path": sidecar_rel,
                "sha256": _sha256_file(sidecar),
                "size": sidecar.stat().st_size,
            }
            sidecars.append(item)
            payload[sidecar_rel] = sidecar
        tracks[audio_rel] = {
            "audio_size": audio.stat().st_size,
            "sidecars": sidecars,
        }
    manifest = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root_name": root.name,
        "complete_snapshot": True,
        "tracks": tracks,
    }
    return manifest, payload


def export_package(music_root, zip_path) -> dict:
    manifest, payload = build_manifest(music_root)
    destination = Path(zip_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for relative, source in sorted(payload.items()):
                archive.write(source, "files/" + relative)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "package": str(destination),
        "tracks": len(manifest["tracks"]),
        "files": len(payload),
        "bytes": destination.stat().st_size,
    }


def _read_validated_package(zip_path) -> tuple[dict, dict[str, bytes]]:
    package = Path(zip_path)
    if not package.is_file():
        raise ValueError("The selected ZIP package does not exist.")
    with zipfile.ZipFile(package, "r") as archive:
        names = archive.namelist()
        if names.count("manifest.json") != 1 or len(names) != len(set(names)):
            raise ValueError("The package has a missing or duplicated manifest/file entry.")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("The package manifest is invalid.") from exc
        if manifest.get("format") != FORMAT_NAME or manifest.get("version") != FORMAT_VERSION:
            raise ValueError("This is not a supported T9 Lyrics Library package.")
        if manifest.get("complete_snapshot") is not True or not isinstance(manifest.get("tracks"), dict):
            raise ValueError("The package is not a complete lyrics-library snapshot.")

        payload = {}
        expected_members = {"manifest.json"}
        for audio_rel, track in manifest["tracks"].items():
            audio = _safe_relative(audio_rel)
            if audio.suffix.lower() not in AUDIO_SUFFIXES or not isinstance(track, dict):
                raise ValueError(f"Invalid track entry in manifest: {audio_rel!r}")
            allowed = set(_candidate_relatives(audio_rel))
            sidecars = track.get("sidecars", [])
            if not isinstance(sidecars, list):
                raise ValueError(f"Invalid sidecar list for: {audio_rel}")
            seen = set()
            for item in sidecars:
                if not isinstance(item, dict):
                    raise ValueError(f"Invalid sidecar entry for: {audio_rel}")
                relative = _safe_relative(item.get("path", "")).as_posix()
                digest = str(item.get("sha256", "")).lower()
                if relative not in allowed or relative in seen:
                    raise ValueError(f"Unexpected sidecar path for {audio_rel}: {relative}")
                if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    raise ValueError(f"Invalid SHA-256 value for: {relative}")
                member = "files/" + relative
                expected_members.add(member)
                try:
                    data = archive.read(member)
                except KeyError as exc:
                    raise ValueError(f"Package file is missing: {relative}") from exc
                if _sha256_bytes(data) != digest:
                    raise ValueError(f"Package file failed verification: {relative}")
                payload[relative] = data
                seen.add(relative)
        if set(names) != expected_members:
            raise ValueError("The package contains files not listed in its manifest.")
    return manifest, payload


def inspect_import(music_root, zip_path) -> dict:
    root = Path(music_root).resolve()
    if not root.is_dir():
        raise ValueError("Choose a valid music folder first.")
    manifest, payload = _read_validated_package(zip_path)
    actions = []
    missing_tracks = []
    matched_track_paths = []
    tracks_with_lrc = []
    unchanged = 0
    matched_tracks = 0
    for audio_rel, track in manifest["tracks"].items():
        audio = _disk_path(root, audio_rel)
        if not audio.is_file():
            missing_tracks.append(audio_rel)
            continue
        matched_tracks += 1
        matched_track_paths.append(audio_rel)
        incoming = {item["path"]: item for item in track["sidecars"]}
        if any(str(path).lower().endswith(".lrc") for path in incoming):
            tracks_with_lrc.append(audio_rel)
        for sidecar_rel in _candidate_relatives(audio_rel):
            target = _disk_path(root, sidecar_rel)
            item = incoming.get(sidecar_rel)
            if item is None:
                if target.is_file():
                    actions.append({"kind": "delete", "audio": audio_rel, "path": sidecar_rel})
                continue
            if not target.is_file():
                actions.append({"kind": "create", "audio": audio_rel, "path": sidecar_rel})
            elif _sha256_file(target) != item["sha256"]:
                actions.append({"kind": "update", "audio": audio_rel, "path": sidecar_rel})
            else:
                unchanged += 1
    counts = {kind: sum(1 for action in actions if action["kind"] == kind)
              for kind in ("create", "update", "delete")}
    return {
        "manifest": manifest,
        "payload": payload,
        "actions": actions,
        "counts": counts,
        "unchanged": unchanged,
        "package_tracks": len(manifest["tracks"]),
        "matched_tracks": matched_tracks,
        "matched_track_paths": matched_track_paths,
        "tracks_with_lrc": tracks_with_lrc,
        "missing_tracks": missing_tracks,
    }


def import_package(music_root, zip_path, backup_root) -> dict:
    root = Path(music_root).resolve()
    plan = inspect_import(root, zip_path)
    actions = plan["actions"]
    backup_dir = None
    backups = []
    created = []
    changed_tracks = set()
    if actions:
        backup_dir = Path(backup_root) / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
        backup_dir.mkdir(parents=True, exist_ok=False)
    try:
        for action in actions:
            target = _disk_path(root, action["path"])
            if target.is_file():
                backup = backup_dir.joinpath(*_safe_relative(action["path"]).parts)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                if _sha256_file(target) != _sha256_file(backup):
                    raise OSError(f"Backup verification failed: {target}")
                backups.append((target, backup))
            if action["kind"] == "delete":
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(plan["payload"][action["path"]])
                os.replace(temporary, target)
                if action["kind"] == "create":
                    created.append(target)
            changed_tracks.add(action["audio"])
    except Exception:
        for target in created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        for original, backup in reversed(backups):
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, original)
        raise
    plan.pop("payload", None)
    plan.pop("manifest", None)
    plan["backup_dir"] = str(backup_dir) if backup_dir else ""
    plan["changed_tracks"] = sorted(changed_tracks)
    return plan
