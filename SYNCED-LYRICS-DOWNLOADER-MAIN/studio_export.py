"""Export for sharing: the synced lyrics (.lrc) of a music folder plus a song list for Google Sheets.

``build(source folders, output folder)`` writes

* ``LRC/<Album>/<song file name>.lrc`` — a copy of every song's LRC, grouped by the album tag of the song,
* ``songs.csv`` — one row per song, sorted by album: Album, Song (from the file name), Artist, Length, Version,
  Download LRC (empty: filled by the Apps Script), LRC path (the key the script matches the Drive files with),
* ``fill_links.gs`` — the Google Apps Script that writes the Drive download links into the sheet.

Put the ``LRC`` folder on Google Drive (shared: anyone with the link can view), import ``songs.csv`` into Google
Sheets, paste ``fill_links.gs`` under Extensions → Apps Script and run ``fillLinks``.
"""
import csv
import re
import shutil
from pathlib import Path

import studio_resync
import studio_swap

NO_ALBUM = "(No album)"
AUDIO = (".mp3", ".flac")
HEADERS = ["Album", "Song", "Artist", "Length", "Version", "Download LRC", "LRC path"]


def safe_name(text, fallback="_"):
    """A folder / file name Windows and Drive accept."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text)).strip(" .")
    return text or fallback


def length_text(audio):
    seconds = studio_resync.song_length(audio)
    if not seconds:
        return ""
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def songs_with_lrc(folders):
    """[(audio path, lrc path, version label)] for every song under the folders that has a synced LRC."""
    found = []
    for folder in folders:
        folder = Path(folder)
        for audio in sorted(folder.rglob("*")):
            if audio.suffix.lower() not in AUDIO or not audio.is_file():
                continue
            if any(part.lower().startswith(("backup", "old metadata")) for part in audio.relative_to(folder).parts):
                continue
            lrc = next((p for p in studio_resync.lrc_locations(audio) if p.is_file()), None)
            if lrc:
                found.append((audio, lrc, folder.name))
    return found


def build(folders, output, progress=None):
    """Write the export; returns {"songs": n, "albums": n, "folder": Path}."""
    output = Path(output)
    lrc_root = output / "LRC"
    if lrc_root.exists():
        shutil.rmtree(lrc_root)
    lrc_root.mkdir(parents=True)
    rows, used = [], set()
    songs = songs_with_lrc(folders)
    for index, (audio, lrc, version) in enumerate(songs, 1):
        if progress and index % 25 == 0:
            progress(f"Reading {index} of {len(songs)}…")
        try:
            tags = studio_swap.read(audio)
        except Exception:
            tags = {}
        album = (tags.get("album") or "").strip() or NO_ALBUM
        album_folder = safe_name(album, NO_ALBUM)
        name = safe_name(audio.stem, "song")
        relative = f"{album_folder}/{name}.lrc"
        number = 2
        while relative.casefold() in used:  # the same name twice in one album (e.g. Released and Unreleased)
            relative = f"{album_folder}/{name} ({number}).lrc"
            number += 1
        used.add(relative.casefold())
        target = lrc_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lrc, target)
        rows.append([album, audio.stem.strip(), (tags.get("artist") or "").strip(), length_text(audio), version, "", relative])
    rows.sort(key=lambda row: (row[0] == NO_ALBUM, row[0].casefold(), row[1].casefold()))
    with open(output / "songs.csv", "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    (output / "fill_links.gs").write_text(APPS_SCRIPT, encoding="utf-8")
    return {"songs": len(rows), "albums": len({row[0] for row in rows}), "folder": output}


APPS_SCRIPT = r'''/**
 * Fills the "Download LRC" column with a direct download link for every song.
 *
 * 1. Upload the "LRC" folder to Google Drive and share it: Share -> General access -> "Anyone with the link" (Viewer).
 * 2. Copy the folder's ID (the last part of its address:  drive.google.com/drive/folders/THIS_PART) into FOLDER_ID.
 * 3. Run fillLinks() once (Run button). Run it again after adding or replacing files.
 * The sheet needs the columns "Download LRC" and "LRC path" (they come from songs.csv).
 */
const FOLDER_ID = 'PASTE_THE_FOLDER_ID_HERE';
const SHEET_NAME = ''; // empty = the first sheet

function onOpen() {
  SpreadsheetApp.getUi().createMenu('LRC').addItem('Fill download links', 'fillLinks').addToUi();
}

function collect_(folder, prefix, map) {
  const files = folder.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    map[(prefix + file.getName()).toLowerCase()] = file.getId();
  }
  const folders = folder.getFolders();
  while (folders.hasNext()) {
    const sub = folders.next();
    collect_(sub, prefix + sub.getName() + '/', map);
  }
}

function fillLinks() {
  const map = {};
  collect_(DriveApp.getFolderById(FOLDER_ID), '', map);
  const sheet = SHEET_NAME ? SpreadsheetApp.getActive().getSheetByName(SHEET_NAME) : SpreadsheetApp.getActiveSheet();
  const data = sheet.getDataRange().getValues();
  const head = data[0];
  const linkCol = head.indexOf('Download LRC');
  const pathCol = head.indexOf('LRC path');
  if (linkCol < 0 || pathCol < 0) throw new Error('Columns "Download LRC" and "LRC path" are needed.');
  let filled = 0, missing = 0;
  const out = [];
  for (let r = 1; r < data.length; r++) {
    const id = map[String(data[r][pathCol]).toLowerCase()];
    if (id) {
      // a clickable text, not a formula: works in every language / region setting of the sheet
      out.push([SpreadsheetApp.newRichTextValue().setText('Download')
        .setLinkUrl('https://drive.google.com/uc?export=download&id=' + id).build()]);
      filled++;
    } else {
      out.push([SpreadsheetApp.newRichTextValue().setText('').build()]);
      missing++;
    }
  }
  sheet.getRange(2, linkCol + 1, out.length, 1).setRichTextValues(out);
  SpreadsheetApp.getActive().toast(filled + ' links written, ' + missing + ' files not found on Drive.');
}
'''
