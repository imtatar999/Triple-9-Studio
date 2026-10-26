"""Metadata → Swap Song and Transfer Metadata (like Mp3tag's copy / paste tag, then swap the files).

``old`` = the song being replaced: its tags are copied onto ``new``. Then the two files swap places:
``new`` goes into ``old``'s folder (with ``old``'s file name when "File name" is chosen) and ``old`` goes
into a subfolder "Old Metadata swapped songs" of ``new``'s former folder. Both files are backed up first. MP3 (ID3) and FLAC are supported.
"""
import os
import shutil
import tempfile
from pathlib import Path

FIELDS = (  # (key, label)
    ("filename", "File name"), ("title", "Title"), ("artist", "Artist"), ("albumartist", "Album artist"),
    ("album", "Album"), ("tracknumber", "Track"), ("date", "Year"), ("genre", "Genre"), ("comment", "Comment"),
)
EXTRAS = (("lyrics", "Embedded lyrics"), ("cover", "Front cover"))
KEEP_BRACKETS = ("session", "sessions", "session edit", "og", "alt")  # these brackets stay in a Title
KEEP_FEATURING = r"(?:ft|feat|featuring|with)\b\.?\s*.*"  # (ft. X) (feat. X) (ft X) (with X) stay too
OLD_SONGS_FOLDER = "Old Metadata swapped songs"  # made in the new song's former folder
ID3_TEXT = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "tracknumber": "TRCK",
            "date": "TDRC", "genre": "TCON"}


def _kind(path):
    suffix = Path(path).suffix.lower()
    if suffix not in (".mp3", ".flac"):
        raise ValueError(f"Only MP3 and FLAC are supported: {Path(path).name}")
    return suffix


def _id3(path):
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        return ID3(str(path))
    except ID3NoHeaderError:
        return ID3()


def read(path):
    """{key: text} for every field (file name included), plus 'lyrics' / 'cover' as short descriptions."""
    path = Path(path)
    values = {"filename": path.stem}
    if _kind(path) == ".mp3":
        tags = _id3(path)
        for key, frame_id in ID3_TEXT.items():
            frame = tags.get(frame_id)
            if frame_id == "TDRC" and frame is None:
                frame = tags.get("TYER")
            values[key] = str(frame.text[0]) if frame is not None and frame.text else ""
        comments = [str(f.text[0]) for f in tags.getall("COMM") if f.text]
        values["comment"] = comments[0] if comments else ""
        lyrics = [str(f.text) for f in tags.getall("USLT") if str(f.text).strip()]
        values["lyrics"] = f"{len(lyrics[0].splitlines())} lines" if lyrics else ""
        cover = [f for f in tags.getall("APIC") if f.type == 3] or tags.getall("APIC")
        values["cover"] = f"{cover[0].mime}, {len(cover[0].data) // 1024} KB" if cover else ""
    else:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        for key in ("title", "artist", "albumartist", "album", "tracknumber", "date", "genre", "comment"):
            values[key] = (audio.get(key) or audio.get("description" if key == "comment" else key) or [""])[0]
        lyrics = (audio.get("lyrics") or audio.get("unsyncedlyrics") or [""])[0]
        values["lyrics"] = f"{len(lyrics.splitlines())} lines" if lyrics.strip() else ""
        cover = [p for p in audio.pictures if p.type == 3] or audio.pictures
        values["cover"] = f"{cover[0].mime}, {len(cover[0].data) // 1024} KB" if cover else ""
    return values


def copy_tags(old, new, keys):
    """Copy the chosen tag fields of ``old`` onto ``new`` (an empty value in ``old`` clears the field)."""
    old, new = Path(old), Path(new)
    if _kind(old) != _kind(new):
        raise ValueError("Both songs must be the same type (MP3 → MP3 or FLAC → FLAC).")
    keys = set(keys)
    if _kind(new) == ".mp3":
        from mutagen.id3 import COMM, Frames
        src, dst = _id3(old), _id3(new)
        version = 3 if getattr(dst, "version", (2, 4))[:2] == (2, 3) else 4
        for key, frame_id in ID3_TEXT.items():
            if key not in keys:
                continue
            dst.delall(frame_id)
            if frame_id == "TDRC":
                dst.delall("TYER")
                dst.delall("TDAT")
            frame = src.get(frame_id) or (src.get("TYER") if frame_id == "TDRC" else None)
            if frame is not None and frame.text:
                dst.add(Frames[frame_id](encoding=3, text=[str(t) for t in frame.text]))
        if "comment" in keys:
            dst.delall("COMM")
            for frame in src.getall("COMM"):
                dst.add(COMM(encoding=3, lang=frame.lang, desc=frame.desc, text=frame.text))
        if "lyrics" in keys:
            dst.delall("USLT"); dst.delall("SYLT")
            for frame_id in ("USLT", "SYLT"):
                for frame in src.getall(frame_id):
                    dst.add(frame)
        if "cover" in keys:
            dst.delall("APIC")
            for frame in src.getall("APIC"):
                dst.add(frame)
        dst.save(str(new), v2_version=version)
    else:
        from mutagen.flac import FLAC
        src, dst = FLAC(str(old)), FLAC(str(new))
        names = {"comment": ("comment", "description"), "lyrics": ("lyrics", "unsyncedlyrics")}
        for key in keys - {"filename", "cover"}:
            for name in names.get(key, (key,)):
                values = src.get(name)
                if values:
                    dst[name] = values
                elif name in dst:
                    del dst[name]
        if "cover" in keys:
            dst.clear_pictures()
            for picture in src.pictures:
                dst.add_picture(picture)
        dst.save()


def _free(path):
    """``path`` or ``name (2).ext`` … when the name is taken."""
    path = Path(path)
    number = 2
    while path.exists():
        path = path.with_name(f"{path.stem.rsplit(' (', 1)[0]} ({number}){path.suffix}")
        number += 1
    return path


def swap(old, new, keys, backup_root):
    """Copy tags old → new, then swap the files' places. Returns (new's final path, old's final path, backup folder)."""
    old, new = Path(old), Path(new)
    if old.resolve() == new.resolve():
        raise ValueError("Choose two different songs.")
    if not old.is_file() or not new.is_file():
        raise FileNotFoundError("Both songs must still exist.")
    _kind(old); _kind(new)
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="swap_", dir=backup_root))
    for index, path in enumerate((old, new)):
        (backup / str(index)).mkdir()
        shutil.copy2(path, backup / str(index) / path.name)
    (backup / "restore.txt").write_text(f"{old}\n{new}\n", encoding="utf-8")

    copy_tags(old, new, keys)
    old_folder, new_folder = old.parent, new.parent
    new_name = (old.stem if "filename" in keys else new.stem) + new.suffix
    # park both, then put each into the other's place (works also when both are in one folder)
    park_old = old.with_name(old.name + ".t9swap")
    park_new = new.with_name(new.name + ".t9swap")
    old.rename(park_old)
    new.rename(park_new)
    new_final = _free(old_folder / new_name)
    park_new.rename(new_final)
    (new_folder / OLD_SONGS_FOLDER).mkdir(exist_ok=True)
    old_final = _free(new_folder / OLD_SONGS_FOLDER / old.name)
    park_old.rename(old_final)
    # the song's LRC did not change, but the audio did: touch it so cached sync checks run again
    for lrc in (new_final.parent / "SYNCED LYRICS" / (new_final.stem + ".lrc"),
                new_final.parent / "LYRICS" / (new_final.stem + ".lrc"), new_final.with_suffix(".lrc")):
        if lrc.is_file():
            try:
                os.utime(lrc)
            except OSError:
                pass
    return new_final, old_final, backup


def write_fields(path, values, backup_root=None):
    """Write tag fields ({key: text}, keys from FIELDS except "filename") into one MP3 / FLAC.

    An empty text clears the field. With ``backup_root`` the file is copied there first."""
    path = Path(path)
    kind = _kind(path)
    values = {k: v for k, v in values.items() if k not in ("filename", "lyrics", "cover")}
    if backup_root:
        folder = Path(backup_root) / "METADATA-EDITS"
        folder.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="edit_", dir=folder))
        shutil.copy2(path, folder / path.name)
    if kind == ".mp3":
        from mutagen.id3 import COMM, Frames
        tags = _id3(path)
        version = 3 if getattr(tags, "version", (2, 4))[:2] == (2, 3) else 4
        for key, text in values.items():
            if key == "comment":
                tags.delall("COMM")
                if text:
                    tags.add(COMM(encoding=3, lang="eng", desc="", text=[text]))
                continue
            frame_id = ID3_TEXT[key]
            tags.delall(frame_id)
            if frame_id == "TDRC":
                tags.delall("TYER")
                tags.delall("TDAT")
            if text:
                tags.add(Frames[frame_id](encoding=3, text=[text]))
        tags.save(str(path), v2_version=version)
    else:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        for key, text in values.items():
            names = ("comment", "description") if key == "comment" else (key,)
            for name in names:
                if name in audio:
                    del audio[name]
            if text:
                audio[names[0]] = [text]
        audio.save()


def _keeps(inner):
    import re
    inner = inner.strip().lower()
    return inner in KEEP_BRACKETS or bool(re.fullmatch(KEEP_FEATURING, inner))


def strip_brackets(text):
    """The text without (round) and [square] bracket groups: "Swerve(JW3)" → "Swerve", "swerve (jw3)" → "swerve",
    "Song (feat. X) (JW3)" → "Song". Brackets holding Session / Sessions / Session Edit or (ft. X) / (feat. X) / (with X) are kept. Spaces left around a removed group are tidied. When nothing would be left
    (the text is only brackets) the text is returned unchanged."""
    import re
    result = str(text)
    while True:  # nested groups: remove the innermost first
        shorter = re.sub(r"\s*[\(\[]([^()\[\]]*)[\)\]]",
                         lambda m: m.group(0) if _keeps(m.group(1)) else " ", result)
        if shorter == result:
            break
        result = shorter
    result = re.sub(r"\s{2,}", " ", result).strip(" -–—_.")
    return result if result.strip() else str(text).strip()


def filename_from_title(title):
    """A file name (without extension) made from a Title: characters Windows does not allow are removed."""
    import re
    name = re.sub(r'[<>:"/\|?*\x00-\x1f]', "", str(title))
    return re.sub(r"\s{2,}", " ", name).strip(" .")


def set_cover(path, image, backup_root=None):
    """Replace the front cover of an MP3 / FLAC with the picture file ``image`` (the old cover is removed)."""
    path, image = Path(path), Path(image)
    data = image.read_bytes()
    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    else:
        raise ValueError("The cover must be a JPG or PNG picture.")
    if backup_root:
        folder = Path(backup_root) / "METADATA-EDITS"
        folder.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="cover_", dir=folder))
        shutil.copy2(path, folder / path.name)
    if _kind(path) == ".mp3":
        from mutagen.id3 import APIC
        tags = _id3(path)
        version = 3 if getattr(tags, "version", (2, 4))[:2] == (2, 3) else 4
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(str(path), v2_version=version)
    else:
        from mutagen.flac import FLAC, Picture
        audio = FLAC(str(path))
        audio.clear_pictures()
        picture = Picture()
        picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
        audio.add_picture(picture)
        audio.save()


def cover_bytes(path):
    """The front cover picture of an MP3 / FLAC as bytes (None when the song has no cover)."""
    path = Path(path)
    if _kind(path) == ".mp3":
        tags = _id3(path)
        cover = [f for f in tags.getall("APIC") if f.type == 3] or tags.getall("APIC")
        return bytes(cover[0].data) if cover else None
    from mutagen.flac import FLAC
    pictures = FLAC(str(path)).pictures
    cover = [x for x in pictures if x.type == 3] or pictures
    return bytes(cover[0].data) if cover else None
