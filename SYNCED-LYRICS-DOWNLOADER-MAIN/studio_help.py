"""English user guide. Headings and **quoted controls** render with Tk text tags; "T9" shows as the logo."""
import re
from pathlib import Path


LOGO_FILE = Path(__file__).resolve().with_name("triplenine.ico")
TAG_FONTS = {"title": ("Segoe UI", 19, "bold"), "heading": ("Segoe UI", 13, "bold"),
             "bold": ("Segoe UI", 11, "bold"), "folder": ("Consolas", 11, "bold")}


def _logo(widget, tag):
    """The triplenine logo sized for the text of ``tag`` (kept on the widget so Tk keeps it alive)."""
    cache = widget.__dict__.setdefault("_t9_logos", {})
    if tag not in cache:
        try:
            import studio_dialogs
            cache[tag] = studio_dialogs.heading_icon(TAG_FONTS.get(tag, ("Segoe UI", 11)), LOGO_FILE, scale=1.15)
        except Exception:
            cache[tag] = None
    return cache[tag]


def _insert(widget, text, tag=()):
    """Insert text; every "T9" is drawn as the logo instead."""
    for index, piece in enumerate(re.split(r"\bT9\b ?", text)):
        if index:
            logo = _logo(widget, tag if isinstance(tag, str) else "")
            if logo is not None:
                widget.image_create("end", image=logo, padx=2, align="center")
                if tag:
                    widget.tag_add(tag, "end-2c")
                widget.insert("end", " ", tag)
            else:
                widget.insert("end", "T9 ", tag)
        if piece:
            widget.insert("end", piece, tag)


def render_page(widget, body):
    """Render the small guide format without displaying markup characters."""
    for name, font in TAG_FONTS.items():
        widget.tag_configure(name, font=font)
    widget.tag_configure('title', spacing1=4, spacing3=14)
    widget.tag_configure('heading', spacing1=14, spacing3=8)
    widget.tag_configure('folder', spacing1=0, spacing3=0)
    anchors = {}
    for line in body.splitlines():
        if line.startswith('# '):
            _insert(widget, line[2:] + '\n', 'title')
        elif line.startswith('## '):
            anchor = 'section_' + str(len(anchors))
            widget.mark_set(anchor, 'end-1c')
            widget.mark_gravity(anchor, 'left')
            anchors[line[3:]] = anchor
            _insert(widget, line[3:] + '\n', 'heading')
        elif line.startswith('    '):
            _insert(widget, line + '\n', 'folder')
        else:
            for index, part in enumerate(re.split(r'\*\*(.*?)\*\*', line)):
                _insert(widget, part, 'bold' if index % 2 else ())
            widget.insert('end', '\n')
    return anchors


NAVIGATION = {
    'T9 Downloader': [('Using the program', 'Choose the workflow you need'), ('Between computers', 'Move lyrics between computers'), ('Buttons explained', 'Buttons and controls: main window'), ('Installation and disk space', 'Installation and disk space')],
    'T9 Lyrics Sync': [('Using the program', 'Step by step: one song'), ('Multiple songs', 'Step by step: multiple songs'), ('Buttons explained', 'Buttons and controls')],
    'T9 Lyric Builder': [('Using the program', 'Step by step: load a marked track'), ('Buttons explained', 'Buttons and controls'), ('Common mistakes', 'Common Builder mistakes: check these before saving')],
}


PAGES = {
'T9 Downloader': '''# Start here: your music → finished lyrics

T9 Downloader brings together three tools:

**"T9 Downloader"** is the main window: it finds existing lyrics online and saves them for your music files.

**"T9 Lyric Builder"** creates or completes lyrics from audio, optionally guided by an existing text; you review and correct its result.

**"T9 Lyrics Sync"** adds timestamps to finished lyrics so each line can appear at the right time during playback.

A TXT is plain text without timing. An LRC contains timed lines when it has timestamps such as [00:12.50]. Choosing an LRC extension alone does not synchronize lyrics.

**15 GB of free disk space is recommended for the complete program.**

## Before you start: folders and files
Choose any folder containing MP3/FLAC files with **"Open Music Folder"**. All its tracks, including subfolders, appear immediately. If audio is directly in the selected folder, its folder name also appears in **"Artists"**. You can select that row to work with the whole folder.

**"Albums"** appears only when subfolders contain audio. Selecting an album filters Tracks; leaving albums unselected shows all tracks in the current folder scope. A LYRICS folder containing only text does not enable Albums.

You can optionally organize a larger library like this:

    YOURMUSICFOLDER/
      ARTIST NAME/
        ALBUM NAME/
          Song.mp3
          LYRICS/
            Song.txt
          SYNCED LYRICS/
            Song.lrc

Choose YOURMUSICFOLDER with **"Open Music Folder"**. ARTIST NAME will appear under **"Artists"** and ALBUM NAME under **"Albums"**. Album folders are optional: audio can also sit directly inside ARTIST NAME.

You provide the music folders and audio files. The program does not automatically create artist or album folders or move music into them.

**"LYRICS"** is created beside the audio when you save with **"Save Finished TXT for Sync (Match MP3 Filename)"** in **"T9 Lyric Builder"**, or download with the **"Store lyrics in a ‘LYRICS’ folder next to the track"** option enabled. Without that download option, downloaded lyrics go beside the audio instead.

**"SYNCED LYRICS"** is created beside the audio when **"T9 Lyrics Sync"** writes its default output. You do not need to create it in advance. A manually chosen output location overrides that default for a single track.

The **"MARKED FOR TRANSCRIPTION"** folder holds reminders and reference information for tracks sent to **"T9 Lyric Builder"**. T9 Downloader uses an existing folder of that name one level above its program modules; otherwise its marking action creates it beside those modules. The shared T9 Lyric Builder queue also stores its information in **"MARKED FOR TRANSCRIPTION"**/.REFERENCES one level above the modules. Keep the supplied application layout and these support folders in place.

The **"BACKUPS"** folders are created as needed for supported actions such as marking, lyric replacement, renaming or reference deletion. Depending on the action, they are inside the program folder or one level above it; the operation log reports the backup path. This is not a backup of your entire music library.

Provider cache and credentials are managed separately in your Windows user profile. Cache means saved lookup results/text reused to avoid unnecessary downloads. Models may also download support files on first use. You do not need to put any of these inside ARTIST NAME or ALBUM NAME.

The names **"LYRICS"**, **"SYNCED LYRICS"** and **"MARKED FOR TRANSCRIPTION"** are application folder names: keep those names. Song.mp3 and Song.txt must have the same base filename for automatic pairing.

## Choose the workflow you need
If a complete, correct text already exists: **"T9 Downloader"** → review the TXT → **"T9 Lyrics Sync"** → check the timed LRC.

If text is missing or incomplete: **"T9 Downloader"** → **"Mark for Transcription"** → **"T9 Lyric Builder"** → review and save the TXT → **"T9 Lyrics Sync"** → check the timed LRC.

You can also open **"T9 Lyric Builder"** directly and choose an ordinary song with **"Browse…"**; marking it first is optional.

## Move lyrics between computers
You do not need to copy your full music library whenever only lyrics changed. Open **"Settings"** → **"Options…"** and use **"Lyrics Library Between Computers"**.

Choose an **"Export folder"** once; the program remembers it. **"Export Lyrics Library"** creates a small dated ZIP containing the complete TXT/LRC snapshot and a verification manifest. Audio files are never included. Move that ZIP to the other computer using a USB drive, private cloud folder or another private transfer method.

On the other computer, open the same music-library root and click **"Import Lyrics Library"**. The program verifies the package, matches tracks by their paths relative to the selected music folder, and previews new, changed and removed files. Missing audio tracks are skipped. Replaced and removed TXT/LRC files are backed up before the import.

Choose **"Yes"** to import sidecar files and make local MP3 timed-lyrics tags match the imported LRC files. Choose **"No"** to import TXT/LRC files without changing MP3 metadata. Importing a complete snapshot also carries intentional TXT/LRC deletions to the other computer after the confirmation preview.

Keep the same artist/album/song folder structure on both computers. The ZIP is a lyrics transfer package, not a backup of the audio library.

## Step by step: download a complete text
Before downloading, add your personal Genius Client Access Token. If it is missing, a dialog explains why it is needed, links to https://genius.com/api-clients and lets you paste it with **"Save and Continue"**. Closing the dialog cancels the download attempt. The same saved token is used in Options. Never paste a Client Secret or share your token. This checks that a token is present; use the token test in Options to verify it with Genius.

1. Click **"Open Music Folder"** and select YOURMUSICFOLDER.

2. Songs appear immediately in **"Tracks"**. Select songs with Ctrl/Shift for several items. Artists and Albums are optional filters.

3. Use **"Song Search"** to filter song names. It does not search artist or album names. Check the highlighted tracks before downloading.

To remove lyrics, select tracks and open **"Delete lyrics ▾"** (Cleanup & undo): **"Delete only the synced LRC…"** keeps the plain TXT; **"Delete TXT + LRC of selected tracks…"** removes both; **"Remove lyrics embedded in the MP3(s)…"** clears only the lyrics tag inside the MP3. Every choice asks for confirmation, saves recoverable copies in BACKUPS and keeps the audio file and transcription references. The failed-download state is cleared: a track with no other lyrics or transcription marker has no icon. **"Failed"** means a download attempt failed, not that you intentionally deleted lyrics. Use Refresh after deleting files manually in Windows.

On Windows, launch **"Triplenine_Lyrics_Studio.py"**. It automatically switches to windowless Python when pythonw.exe is available; Windows may briefly show a console before that switch.

4. Click **"Download Lyrics for Selected"**: it downloads the highlighted tracks — or, when no track is highlighted, every track of the selected artist(s).

5. Alternatively, **"Find & Download Missing Lyrics TXT"** checks missing text files: selected tracks take priority, then selected artists; with no selection, it checks the whole library. The number in brackets is the current missing count.

6. If a match needs review, use **"Play Song"** and **"Open Genius"** to check it. Use **"Confirm"** for the right lyrics, **"Reject"** for a wrong match or **"Skip"** to leave the item for later. A similar title is not enough: the lyrics must fit this recording.

7. Open **"View/Change Lyrics…"** or double-click a track to inspect its text/source and correct or replace it. A Genius link or your own TXT can be used when automatic search fails.

8. When the text covers the entire audio, select the track(s) and open **"T9 Lyrics Sync"**. Continue in that help tab.

Genius normally provides plain TXT, not timed lyrics. Other enabled providers may return timed LRC. The **"Downloaded"** status means a lyric file was found; it does not guarantee correct words or timing.

The **"🎶 Synced in audio"** status means timestamped lyrics are embedded in the MP3. **"📄 Timed LRC file"** means a matching timestamped LRC exists beside the audio, inside **"LYRICS"** or inside **"SYNCED LYRICS"**, but synchronized lyrics are not embedded in the MP3. Use **"Refresh"** after changing lyrics outside the program.

## Step by step: send a track to T9 Lyric Builder
1. Select the song(s) needing transcription and click **"Mark for Transcription"**. This adds reminders to the queue used by **"T9 Lyric Builder"**; audio stays in its original folder, and recognition does not start yet.

Existing matching TXT/LRC files beside the audio or inside **"LYRICS"** are backed up and removed from their active location by this action. Use marking when you intend to rebuild the text.

2. A shorter reference is an existing, preferably accurate lyric text used as a guide, often from a shorter released version of a Sessions recording. Click **"Find shorter references"** to look for one for your marked tracks. Review uncertain matches rather than accepting a wrong song.

3. The marked status shows whether a reference was found. A reference is only a guide, not a completed transcript of the full recording. If the reference is wrong, use **"Delete shorter reference"** and find or choose a better one.

4. Open **"T9 Lyric Builder"**, click **"Marked tracks…"**, select the queued song and click **"Load Marked Track"**. A ready reference loads into the left field together with the audio path. A track without a reference loads audio only.

5. In that queue window, **"Choose reference TXT…"** lets you supply a reference yourself. You can also paste text after loading. With no reference, you can still transcribe the whole song, but expect more corrections.

6. In **"T9 Lyric Builder"**, run **"BUILD LYRICS"**, review the right-hand result while listening and save with **"Save Finished TXT for Sync (Match MP3 Filename)"**. This creates **"LYRICS"**/Song.txt and marks the loaded queue entry **"Ready for Lyric Sync"**.

7. Return to T9 Downloader, use **"Refresh"** if needed, select the song and open **"T9 Lyrics Sync"**. **"Ready for Lyric Sync"** means the text stage is finished; it does not mean that synchronization has run.

Keep queued audio and support folders in place. Moving audio manually after marking can break stored paths.

## Installation and disk space
Double-click **"Install Triple 9 Studio.cmd"** in the main program folder and press Enter. That is all: it installs everything the Downloader and T9 Lyrics Sync need, shows its progress and puts a **"Triple 9 Studio"** icon on the desktop and in the Start menu. It needs no administrator rights and changes nothing in Windows — everything goes into the **runtime** folder next to the program, and deleting that folder removes it again. If the installation stops (for example the internet connection drops), run it again: it continues where it stopped and also repairs a broken installation.

What it installs into **runtime**:

1. **"Python 3.11"** — the program's own Python (with the window toolkit Tkinter). One Python version is enough for everything; Lyric Builder's Xingyu gets its own separate package folder made from the same Python.

2. **"runtime\\app"** — the packages of T9 Downloader, T9 Lyrics Sync and the Builder window: syncedlyrics for online providers, Syncalong and OpenAI Whisper for alignment, Demucs for "Separate vocals", PyTorch, Mutagen for MP3/FLAC tags and Pillow. With an NVIDIA graphics card PyTorch is installed with CUDA right away. About 3–5 GiB.

3. **"runtime\\ffmpeg"** — FFmpeg reads the audio for Whisper. About 0.2 GiB.

4. **"runtime\\uv"** and **"runtime\\python"** — the installer tool (uv) and the Python files it downloaded.

Lyric Builder's own parts install later, only if you use the Builder: **"Check / Setup Builder"** creates **"runtime\\builder"** with Xingyu Lyrics Aligner and faster-whisper (about 1–2 GiB) and, with an NVIDIA card, the CUDA 12 / cuDNN 9 libraries Xingyu needs (about 1.5 GiB). Git, 7-Zip or a second Python version are no longer needed. If the T9 Subtitles Premiere plugin has installed faster-whisper (its **"Install Whisper"** button), Xingyu uses that copy instead of its own.

**Speech-recognition models** download the first time they are used: the recommended Whisper turbo file is about 1.51 GiB, Xingyu's faster-whisper medium model about 1.43 GiB and Demucs about 0.08 GiB.

A complete installation is roughly **6–7 GiB** without the Builder and **9–10 GiB** with it. Keep at least **12 GB free**; **15 GB free is recommended** for downloads, caches and temporary audio. Your music library and the TXT/LRC files are not included.

Older installations that used Python 3.10 (and Python 3.11 for Xingyu) installed in Windows keep working; after running **"Install Triple 9 Studio.cmd"** the program uses its own runtime folder instead, and Builder needs **"Check / Setup Builder"** once more.

## Buttons and controls: main window
**"Open Music Folder"** — selects a folder with audio or a larger library; Tracks fills immediately.

**"Artists"** — optionally filters the library by folder. The chosen main folder also appears here when it directly contains audio.

**"Albums"** — limits tracks to selected subfolders; no album selection shows all folders for the artist.

**"Tracks"** — selects the audio files for track-level actions. To the right of each name the table shows the song's tags like Mp3tag (File name, Title, Artist, Album artist, Album, Track, Year, Genre, Comment); scroll sideways or drag the column borders. The tags are read in the background and remembered, so the list never waits for them. **Click a column heading** to sort by it (Album, Artist, Year, Genre …; click again for descending, a third time to switch it off; the Name heading returns to the Sort menu's order). **Columns…** hides or shows columns (a hidden column comes back in its own place; **Default order** resets the order and widths). Right-click a song → **Change Metadata…** to edit them (with a small preview of the front cover).

**"Song Search"** — filters the track list by song name only.

**"Select All"** / **"Clear All"** — selects or clears the items in that column.

**"Clear All Selections"** — clears selections in all three columns.

**"Sort"** — changes the track display order; **"Lyrics Synced First"** places tracks with embedded synchronized lyrics (🎶) at the top.

**"Refresh"** (F5) — rescans files while preserving selection and filters where possible.

**"🎶 Synced in audio"** — indicates timestamped lyrics embedded in the MP3. **"📄 Timed LRC file"** indicates a matching timed file without embedded synchronized lyrics.

**"Download Lyrics for Selected"** — downloads lyrics for the highlighted tracks; with no track highlighted, for every track of the selected artist(s) (Ctrl+D / Ctrl+Shift+D).

**"Delete lyrics ▾"** (Cleanup & undo) — one button for the three ways of deleting lyrics: **"Delete TXT + LRC of selected tracks…"**, **"Delete only the synced LRC…"** (plain TXT and embedded tags stay) and **"Remove lyrics embedded in the MP3(s)…"** (TXT and LRC files stay). Each asks for confirmation and makes a recoverable backup first.

**"Undo Delete Lyrics"** — restores the last deleted batch in this program session. It never overwrites a file that already exists. Earlier backups remain in BACKUPS, including after closing the program.

Buttons that require selected tracks are disabled until you select them. Play, Rename and View/Change Lyrics need exactly one track; batch actions accept several. Buttons are also disabled while a download is running.

Above the action buttons, **"Selected tracks"** shows the selection count. For one track it also summarizes its lyrics, synchronization or transcription-reference status. The path field below supports selecting/copying the full audio path; for multiple tracks it shows the first selected file. Find & Download Missing Lyrics TXT uses selected tracks first, then selected artist folders, or the entire music folder when nothing is selected.

In T9 Lyrics Sync, **"Check / Setup Lyrics Sync"** checks FFmpeg, Whisper, Syncalong and PyTorch, and installs missing components in the Sync Python 3.10 environment. Installation runs in the background and may take several minutes. FFmpeg installation needs Windows App Installer (WinGet). Existing working packages are kept. CUDA availability is checked separately. If an NVIDIA card is present but the installed PyTorch is CPU-only, the button **"Install/Repair PyTorch GPU"** appears — use it to replace it with the official CUDA build. This is a large download and requires restarting Triple 9 Studio afterward. Whisper models download on their first use. If setup fails, the log shows the error; the program does not report Ready until verification passes.

**"Find & Download Missing Lyrics TXT"** — checks the selected scope for missing TXT files and downloads them.

**"Check lyrics files ▾" → "TXT / LRC under old song names…"** (Repair library) — scans the whole opened library for sidecar files left under earlier song names. It also recognizes meaningful alternative titles in parentheses without requiring identical spacing, for example `Show Stopper` and `Two Cups (Showstopper) (AI)`. When both an old-name and current-name file exist, a replacement is proposed only if their normalized lyric text is at least 98% similar. You must review and confirm it; the current-name file is backed up before replacement.

**"Pause"** / **"Resume"** — pauses or continues the current download batch.

**"Cancel"** — requests download cancellation; an active request may take a moment to finish.

**"Play Song"** — opens the selected audio in your default player.

**"View/Change Lyrics…"** — opens the lyric/source editor for a track. It is also available in the LRC failure scan report for the selected warning row.

**"LRC tools ▾" → "Embed the available LRC into the MP3(s)"** (Track & lyrics) — embeds the existing, already synced LRC into each selected MP3, with no new sync. The LRC is looked up in SYNCED LYRICS, LYRICS or next to the MP3. Each MP3 is backed up to BACKUPS\LRC-EMBED first. A song is skipped when it has no timed LRC or has several different LRC versions.

**"Fix Fragment Manually…"** (Edit and Fix Synced Lyrics) — retimes only the selected LRC lines.
- **Fix lines automatically** first puts the playhead 2 s before the first selected line (or at the very start of the song), so you can listen right away; then it tries several methods on the selected lines: Whisper (prompt ON/OFF) and copies of identical repeats (earlier or later in the song). Each method gets its own colour. Pick the one that matches the song and click the big **Use selected fix** (right of the coloured choices): its timing replaces **Current** in that fragment and the coloured lanes disappear (**Show automatic fixes again** brings them back). If none of them fits, click **✖ Hide all fixes (none fit)**. Then fine-tune Current by hand if needed and click **💾 Save manual timing** or **💾 Save** (bottom right) — both save everything changed in Current. **↶ Undo last change** (Ctrl+Z) takes back only the last step (one Set, one ±0.1 s or one Use selected fix); **↺ Undo all changes** puts Current back to the timing in the LRC. **Close** asks whether to save when there are unsaved changes.
- **▲ Show console / ▼ Hide console** slides the panel with the detailed times per method in and out.
- The built-in player keeps the whole song in memory and sends it straight to the sound card, so Play starts almost at once (a few hundredths of a second) and the red playhead follows the samples the device has really played — what you hear and the time T sets match.
- The built-in player shows the waveform with a red playhead. Under it is the **Current** lane (the LRC timing plus your unsaved changes) and, while you are choosing, one coloured lane per automatic fix. Each marker shows the line number and its first words (e.g. “11 get geeked get…”). **Right-click a line number** under the waveform to choose that line right there (no need to find it in the list; the playhead stays), then press T. **🎯 Auto-scroll: ON/OFF** — while the song plays the waveform glides smoothly under the playhead; after you scroll by hand it waits 3 s before following again. Above the waveform, in the middle, you see the line Current is singing at the playhead (bold) and, in blue, the **Next line**.
- Space plays or pauses. Click the waveform or a marker to jump there. Ctrl+mouse wheel zooms.
- **Error lines in red:** every line inside a scan warning of the song (all its errors, not only the one you opened; ignored warnings excluded) is red in the list and has a red marker under the waveform. A line turns white as soon as you give it a new time. **⏭ Skip to next error line** selects the next red passage and puts the playhead 1 s before it (after the last one it goes back to the first); the button shows how many passages are left and is greyed out when none are.
- **✏ Edit Line** changes the text of one selected line in the LRC, the TXT lyrics and the MP3's embedded lyrics at once (backed up first); its timestamp stays. A repeated line (chorus) is changed in the TXT at the same occurrence.
- **⏱✖ Clear time of selected** (key **Delete**) takes the time off the selected line(s) in this window only: they stay in the list (e.g. still “line 95”, shown as --:--.--), but their markers leave the waveform and they are not shown as Current any more, so markers sitting on top of each other stop getting in the way. Choose the line in the list and press **T** where it really starts: it gets the new time and its marker comes back. Ctrl+Z brings the cleared time back. Nothing is written until Save; a line that still has no time then keeps its old time (Save asks first).
- **Clicking the list:** one click only chooses a line (the waveform does not move); a double click jumps there and plays from it. The markers under the waveform are thicker and easier to click.
- While automatic fixes are shown, each method gets its own row above the waveform, separated by a thin line, without “Next line” (that appears again when you are just listening).
- After **Remove Line** or **Edit Line** the files are already saved, so **💾 Save** just closes the window (as saved) when no timing was changed.
- **🗑 Remove Line from song** deletes the selected line(s) (Shift/Ctrl+click for several) from the LRC, the TXT lyrics and the MP3's embedded lyrics at once, after a backup of all three (Options → Restore from backups brings them back). A repeated line (chorus) is removed from the TXT at the same occurrence. The window updates in place: the lines disappear and the rest are renumbered, while the waveform, zoom and the song that is playing keep going. Unsaved timing changes on the other lines are kept.
- **🎤 Live tap:** click the caption above the waveform (“Current: … Next line: …”). The caption is replaced by a big green bar that shows only the line waiting for **T** (above it, in its own box, **Current:** shows the whole line that is playing at the moment — the one you tapped last); it works the same whether or not you cleared any times. Play the song and press **T** each time that line starts — it gets the playhead's time and the next line is chosen in the list at once, so you never have to click a line. **Best together with Clear time:** clear the time of the lines that are wrong (Delete), turn Live tap on, play and press T for each of them — Live tap starts with the first cleared line and goes through the cleared lines in list order, wherever the song is playing; when they all have a time again it simply carries on with the following lines. Rewind or click/right-click a line to continue from somewhere else. Click the bar or the caption again, or press Esc, to stop.
- **Markers under the waveform:** right-click a marker to choose its line; **Ctrl+click** adds or removes a line, **Shift+click** selects every line from the last chosen one, just like in the list (then −0.1 / +0.1 s move them all). Clicking a marker's words works the same as clicking the marker itself. Selected lines have a blue block behind their marker. When several markers sit on top of each other, a small menu lists those lines so you can pick the right one.
- **⇤ Move selected to playhead** (next to −0.1 / +0.1 s) moves the selected line to the red playhead; with several lines selected the first one starts at the playhead and the others keep their distance to it.
- **🔊 Volume** (next to Auto-scroll) sets how loud the song plays in this window; it is remembered.
- **Space** always plays / pauses in this window, even when a button or the volume slider has the focus (it never presses that button).
- **Zoom:** the **−** and **=** keys (or + / − on the number pad) zoom the waveform out and in, no Ctrl needed; Ctrl + mouse wheel still works too.
- **💡 Best way to sync by hand:** play the song, **right-click the line number** under the waveform (the line is chosen at once, the playhead keeps going) and press **T** at the moment that line starts. Wrong? Ctrl+Z undoes the last step. Repeat for the next line, then click **💾 Save**.
- **Manual timing**: click a line, then press T at the right moment (it moves to the next line, so you can tap along). , and . move the line 0.1 s.
- −0.1 s / +0.1 s move every selected line together (Shift/Ctrl+click to select several).
- Save writes Current (the applied fix plus your manual changes). Only changed lines are written, with a backup; embedded MP3 lyrics are updated too.

**Metadata tab** — **Change Song Name + Title** (moved here) and **Set Year from comment (Recorded …)**: many MP3s carry a comment like “Era - JW3 (Outsiders) (Recorded July 15, 2019) (Leaked Jan 24, 2026)”. The window lists every song whose comment says when it was recorded and whose Year tag differs (e.g. “— → 2019”), all selected; **Set Year for selected songs** writes it. Only the Recorded date is used — never Leaked or Released. “(Recorded ?)” without a year changes nothing; “(Recorded June 19-20, 2018?)” gives 2018 (only the day is unsure). It works on the selected tracks, else the selected albums / artist, else the whole library. The old years are saved: **Undo last Year change** puts them back.

**Change Metadata…** (Metadata) — select one song and edit its **File name, Title, Artist, Album artist, Album, Track, Year, Genre and Comment** by hand, in the same fields as in Swap Song. **Save Metadata** writes only what you changed (a backup is saved in BACKUPS\\METADATA-EDITS); the audio and embedded lyrics stay as they are. **Replace front cover…** picks a JPG / PNG that removes the old front cover and embeds the new one on Save. A new file name renames the file and its TXT / LRC. **Set name same as Title** copies the Title into the file name field; **Remove brackets from Title** removes everything in (…) or […] from the Title — “Swerve(JW3)” and “swerve (jw3)” both become the bare name — and tidies the spaces. With **several songs selected**, Change Metadata lets you give all of them the same **Artist, Album artist, Album, Year, Genre or Comment** (like Mp3tag): open the arrow of a box to pick a value the songs already have (each is listed once), <keep> leaves a field alone, <clear> empties it; **Apply to selected songs** writes it. **Remove brackets from Titles…** opens a list of every Title that contains brackets and how it will look; **Remove brackets from selected Titles** changes them all at once (untick the ones to leave alone; an option also renames the files to the new Title). For one song, the two buttons only fill the fields; nothing is written until you click Save Metadata.\n\n**Swap Song and Transfer Metadata…** (Metadata) — like Mp3tag's copy / paste tag. Choose **1. the song to replace** (its metadata is copied) and **2. the new song** (for example a better-quality file), or select both in Tracks first. The table shows both songs side by side; tick what to transfer — **All metadata** (file name, title, artist, album artist, album, track, year, genre, comment), **Embedded lyrics** and **Front cover** — or only some of them. **Transfer Metadata + Swap** copies it to the new song, then the new song moves into the old song's folder (with its file name when ticked) and the old song moves to a new subfolder **Old Metadata swapped songs** in the new song's former folder. Both files are backed up first (BACKUPS\\swap_…). MP3 → MP3 and FLAC → FLAC.\n\n**Import from Spreadsheet…** (Metadata) — select songs in Tracks, paste the link of a **Google Sheets** (or a Google Docs with tables) shared as “anyone with the link can view” and click **Load / refresh spreadsheet**. The sheet is downloaded once and kept on disk. For each selected song the window lists every row of the sheet that could be it (found by the file name, and when that fails by the Title tag) (tab, row, names, artist, date) — check that it is **the same song** and pick the right row when it is in several places (you can also search another name). Below, every filled column of that row is listed: tick what to import and choose **Import as** (Year / date, Artist, Album artist, Album, Title, Genre, or add to the comment as “(Studio: …)” / “(Producer: …)”). Columns such as Record Date(s), Date(s), Producer(s), Credited Artist(s) and Recording Location(s) are recognised automatically; your choices are remembered per column name. **Will write** shows the result and **Song now** the current tag. **Do the same for all remaining songs** applies the same ticked columns to this song and every song after it (the best match in the same tab is used; a song that is not in the spreadsheet is skipped on its own). **Import ticked fields into this song** saves a backup (BACKUPS\\METADATA-EDITS) and opens the next song.

**"Check lyrics files ▾" → "LRC missing lines in brackets…"** (Repair library) — the sync engine used to treat every TXT line that starts with “(” and ends with “)” as a section header, so lines like “(Yeah) Bad bitches, fuck 'em then dismiss 'em (Bad, woo)” were left out of the LRC. New syncs keep a line when it has sung text outside the brackets — “(Cha-ching, cha-ching,) I'm gon' drop it in the mix” — and leave out lines made only of brackets, such as “[Chorus]” or “(Cha-ching, cha-ching, brrt, hello?)”. This window lists songs whose LRC still lacks such lines, with how many are missing; **Mark selected for Lyrics Sync** backs up and deletes their LRC + embedded lyrics (the TXT is kept) — then sync them from Lyrics Sync → Marked for Lyrics Sync.

**Search for song duplicates…** (Repair library) — lists songs that are probably the same recording, with the full location and length of every file. Album marks like (JW3) (WOD) (GB&GR) and featured artists are ignored; version marks — (Sessions), (Session Edit), (V2), (OG), (Stem), (Remaster) … — make a different song, so a longer Sessions version is never called a duplicate. **SAME NAME** groups have identical names; **POSSIBLE** groups only differ by an alternative name in brackets (e.g. “Tropical Storm (I Wonder)” / “Tropical Storm (Thrift)”) — listen before deciding. It searches the selected albums, else the selected artist folder(s), else the whole library, and never changes or deletes anything.

**Button hints** — hold the mouse over any button for a moment: a small bubble next to it says what it does, in every window. A disabled button says so. The line next to “Need help…” reminds you of this.

**Windows remember their place** — every window (including Lyric Builder) reopens where you left it and as big as you left it, also after restarting the program. If a window ended up somewhere you cannot reach, use **Options → Windows → Reset window positions**: all remembered places are forgotten and the open windows move to the middle of the screen.

**Quick listen volume** — the 🔊 slider in the Quick listen window applies at once, stays the same for the next passages and is remembered for next time.

**⚠ / ❕ / ❓ next to a song in Tracks** — a song with an LRC shows **⚠** when the last scan found sync errors in it (**❕** = only small possible timing issues) and **❓** when, at its last Lyrics Sync, very few lines of the TXT matched what Whisper heard in the audio (the lyrics might be wrong). Hold the mouse over the icon for the details. The marks use the saved scan results, so they appear at once and follow every new scan, fix or sync; ignored warnings are not marked. **Right-click a song** → **Fix this song** opens Fix fragment at its first error, and there are shortcuts to View/Change Lyrics, Play Song and Sync this song.

**"▶ Fix songs one by one"** (Edit and Fix report) — opens the listed songs one after another, starting with the selected one, each at its first error. In the Fix fragment window **💾 Save & next song ▶** (Ctrl+Enter) saves and opens the next song; **Skip song ▶** leaves this one (asking about unsaved changes); Close ends the queue. No “song fixed” pop-ups in between.

**"🎧 Quick listen"** (Edit and Fix report) — goes through the listed passages and plays about 10 seconds around each (looping) with the lines shown and the playing one highlighted; the next clip is prepared while you listen. Decide with one key: **O / Enter** sounds OK → the warning is ignored; **F** needs fixing → Fix fragment opens and you return to the listening afterwards; **W** the lyrics are wrong → View/Change Lyrics; **S / →** skip for now; **Space** replay. A quick way to sort real errors from false alarms before fixing anything.

In the Fix fragment window: **⏩ Speed 1× / 0.75× / 0.5×** slows the song down without changing its pitch (good for fast rap); **T reaction −0.00 s** moves every line you set with T earlier by that amount, because people press T a little after they hear a line start (try 0.15).

While Lyrics Sync runs it compares the lyrics with what Whisper heard; if fewer than 30% of the lines were heard directly it warns (log, final message, ❓ in Tracks) that the TXT might be wrong for the song — check it in View/Change Lyrics before fixing the timing.

**"Edit and Fix history"** (in the Edit and Fix Synced Lyrics window) — songs you changed, the most recently changed first: Fix fragment saves, 2-pass resyncs, "Wrong lyrics", text edits and newly downloaded lyrics. Select a song to see all its changes, or open it again in Fix fragment or View/Change Lyrics. Older changes are reconstructed from the backups.

**♻ Restore from backups…** (in the Backups panel of Options) — lists every copy the program made before it changed or deleted a song file (deleted lyrics, Fix fragment and lyrics edits, resyncs, new lyrics, LRC embedding …), newest first, with a type filter and song search. The details show where each file goes back to. Restore puts the selected backups back into the library; the files they replace are saved first as “Before a restore”, so a restore can be undone the same way. A restored LRC can also update the MP3's embedded lyrics. Older backups that only kept a file name are placed by finding the song in the library; files whose song is no longer there are skipped.

**Options → "Backups — free disk space"** — shows how much space song backups use, split into lyrics text (.txt), synced lyrics (.lrc), audio (.mp3) and other files. Tick the types you no longer need (optionally only those older than N days) and delete them. Program code backups are never shown or deleted. The same panel opens from **"Clean old backups…"** (Cleanup & undo) — shows how much space song backups use and permanently deletes those older than the chosen number of days, after confirmation. Only backups of lyrics, LRC and MP3 files made by edits, deletions, resyncs and fixes are deleted. Program code backups are never touched. All new backups go to one BACKUPS folder, next to the program folder.

**"🧩 Open in Lyric Builder"** (inside View/Change Lyrics) — opens T9 Lyric Builder with this song loaded and its current TXT pasted as the shorter reference (the Genius link / Match accuracy footer is left out). If the Builder is already open it loads the song there and asks first when it already has a reference or a result; while it is busy with a run it asks you to finish first.

**"🎤 Lyric Sync Now"** (inside View/Change Lyrics) — closes the window and loads this song with its saved TXT into Lyrics Sync (audio, lyrics and output LRC filled in). The sync does not start by itself: check the settings and click Start Sync. If you have unsaved text edits it asks first; if a sync is already running it waits for you to try again later.

**"Find different lyrics…"** (inside View/Change Lyrics) — searches Genius for other versions of the song, such as other version labels, remixes or other credited artists. It shows up to 5 candidates, best match first, and never the current source. Select one to preview its lyrics and see how similar it is to your current TXT, then click **"Use selected — download and replace"**.

**"Change Song Name + Title"** — changes the audio filename and title metadata, with related file handling and a backup.

**"Remove lyrics embedded in the MP3(s)…"** (under **"Delete lyrics ▾"**) — removes all embedded lyrics tags from the MP3 songs selected in **"Tracks"**, preserving other metadata and separate TXT/LRC files. A song with a timed LRC still shows **"📄 Timed LRC file"**, while **"🎶 Synced in audio"** disappears after the embedded lyrics are removed.

**"Mark for Transcription"** — queues selected tracks for T9 Lyric Builder and backs up/removes matching active lyrics.

**"Find shorter references"** — searches for guide lyrics for marked tracks.

**"Delete shorter reference"** — removes the assigned guide with a backup while keeping the track marked.

**"T9 Lyric Builder"** — opens the text-building tool in a separate window.

**"T9 Lyrics Sync"** — opens the tool for turning finished text into timed LRC.

**"Help"** → **"How To Use"** — opens these three guides.

## Buttons and controls: Options and review
**"Settings"** → **"Options…"** — opens provider, output, token and developer settings.

**"Export Lyrics Library"** — creates a complete, verified ZIP of all matching TXT/LRC files without copying audio. The selected export folder is remembered.

**"Import Lyrics Library"** — previews and imports a T9 lyrics ZIP, backs up replaced/removed sidecars and can update timed lyrics embedded in local MP3 files.

**"Enabled providers"** — chooses which lyric services may be used.

**"Up"** / **"Down"** — changes which provider is tried first in the priority list.

**"Store lyrics in a ‘LYRICS’ folder next to the track"** — saves downloads in the audio folder's **"LYRICS"** subfolder.

**"Genius output format"** — chooses TXT or LRC extension without creating timestamps.

**"Client Access Token"** — holds your Genius API credential used for access to Genius search.

**"Show token"** — reveals or masks the entered credential.

**"Test Token"** — checks whether Genius accepts the token.

**"Save Token"** — stores the token for your Windows user.

**"Remove Token"** — removes the stored token.

**"Clear Genius Cache"** — clears local cached Genius data so later lookups may need downloading again.

**"Developer mode"** — shows technical/testing controls in T9 Downloader, T9 Lyric Builder and Sync without changing their values.

**"Save"** — applies Options, including the developer-mode switch.

**"Cancel"** in Options — closes Options without applying pending changes; separate token actions take effect when clicked.

**"Legacy / Advanced Options"** — groups older plain/synced upgrade, language and filtering settings that normally stay unchanged.

**"Genius plain-lyrics support"** — enables plain-text fallback required by Genius.

**"Auto-upgrade plain → synced"** — enables older replacement behavior with prompts or automatic acceptance, depending on the chosen switch.

**"Lyrics Language"** — sets the requested provider language.

**"Strip CJK lines from downloaded LRC"** — removes Chinese/Japanese/Korean lines from downloaded LRC when enabled.

**"Reject lyrics files that are mostly non-ASCII"** — filters results using the configured character ratio.

**"Reject threshold"** — changes that filter's acceptance limit and normally needs no adjustment.

**"Confirm"** / **"Reject"** / **"Skip"** — accepts a reviewed match, rejects it, or leaves it unresolved for later.

**"Open Genius"** / **"Copy link"** — opens or copies the candidate's source URL.

**"Download from this link"** — uses the corrected Genius link entered in the review window.

**"Load reference TXT…"** — supplies a local guide when reviewing a reference match.

## If something does not look right
No lyrics found: try a corrected title or supply a valid Genius link/TXT through **"View/Change Lyrics…"**.

Wrong version: replace it before synchronization; a short released text may not cover a long Sessions recording.

A marked track is absent from T9 Lyric Builder: use **"Refresh"** in **"Marked tracks…"**, check that audio still exists and that the entry is not already **"Ready for Lyric Sync"**.

Downloads require internet access and working provider libraries. A rejected token can be checked with **"Test Token"**.''',
'T9 Lyrics Sync': '''# T9 Lyrics Sync: put each line on the timeline
Recommended player for synchronized lyrics: our own **"Triple 9 Music Player"** — it shows your embedded and LRC lyrics line by line while the song plays. Download it here: https://github.com/imtatar999/triple-9-music-player

**"T9 Lyrics Sync"** takes audio plus finished lyrics and produces an LRC with timestamps. It uses Syncalong, an alignment tool, and Whisper, an audio-recognition model, to estimate when lines are sung.

**"T9 Downloader"** obtains existing lyrics online. **"T9 Lyric Builder"** creates or completes text from audio. Use one of those tools first if your text is missing or incomplete: synchronization does not write missing verses for you.

## Files created and where they go
Default output is created automatically beside the audio:

    YOURMUSICFOLDER/
      ARTIST NAME/
        ALBUM NAME/
          Song.mp3
          LYRICS/Song.txt
          SYNCED LYRICS/Song.lrc

Song.txt may also sit directly beside Song.mp3. Automatic pairing needs exactly matching base filenames, including version labels such as (Sessions).

**"SYNCED LYRICS"** is created when output is written, not simply when this window opens. For a single track, you can choose a different output path. If a file would be overwritten, check the confirmation before proceeding.

## Step by step: one song
Lyrics Sync and Builder open in resizable windows. Settings use a compact layout, with space reserved for the action buttons and log. Removing embedded lyrics rechecks the selected files in the background. If a timestamped LRC remains beside the song or in LYRICS / SYNCED LYRICS, the track shows 📄 Timed LRC file. Plain text without timestamps does not count as synced.

**"Separate vocals with Demucs"** requires an extra package beyond Syncalong and Whisper. Enable the option and click **"Check / Setup Lyrics Sync"** to check/install Demucs as well. The first separation may download its model and take longer. Separation can help alignment but does not guarantee a better result on every song.

1. Finish and review your TXT first. Its lines should follow the recording, including repeated choruses and extra verses. One lyric line per line of text makes the intended structure clearer.

2. Select a track in T9 Downloader and open **"T9 Lyrics Sync"**. Alternatively, use the three **"Browse"** buttons to choose audio, lyrics and output yourself.

3. Check **"Audio"**, **"Lyrics"** and **"Output"**. A TXT for another version of a song can produce poor timing even if the title matches.

4. Choose **"Whisper model"** and **"Language"**. Use a language code such as en or pl, or auto for automatic language detection. A larger model needs more memory and processing time; a smaller one may be less accurate.

5. Usually begin with **"Separate vocals with Demucs (optional)"** off. Demucs separates the voice from the instrumental and can help difficult audio, but adds processing time and requires another installed component.

6. Enable **"After sync: embed timestamped LRC into MP3"** if you want the MP3 file itself to contain your synchronized lyrics: this option is required for embedding and modifies the MP3 metadata. If you leave it off, the synchronized lyrics are still saved as a separate .lrc file in the **"SYNCED LYRICS"** folder (or your chosen output location). Some players can display this separate LRC without modifying the MP3; their folder and filename settings must point to that file.

After the output LRC is saved, T9 Downloader shows the song with the **"📄 Timed LRC file"** icon. If timed lyrics are embedded in the MP3, the icon is **"🎶 Synced in audio"**.

7. Click **"▶ START SYNC"** and watch the status/log. The first model run may download files; CPU processing can take much longer than GPU processing.

8. Wait for completion and inspect the resulting LRC with Triple 9 Music Player (https://github.com/imtatar999/triple-9-music-player) or another LRC-capable player. Some players do not search **"SYNCED LYRICS"** automatically: open the LRC manually or copy it beside the MP3 using the same base filename.

9. If words are wrong or missing, correct the TXT and synchronize again. If the words are right but timing is imperfect, review or adjust the LRC timing.

## Step by step: multiple songs
Use **"Stop after current track"** to finish and save the current song, including MP3 embedding when enabled, then stop before the next song. The summary lists tracks not started separately from failed tracks. Closing the Sync window while it is running also requests this graceful stop and keeps the window open until processing finishes.

1. In **"T9 Downloader"**, highlight songs in the **"Tracks"** list whose complete, reviewed TXT files are ready.

2. Open **"T9 Lyrics Sync"** or click **"Use selected track(s) from main window"** in an already-open Sync window.

3. Read the batch preview and missing-file message. Pairing searches for the same-named TXT beside each audio file or in its **"LYRICS"** folder. Tracks without a matching TXT are skipped from the prepared batch.

4. The path fields show the first queued track as a preview; they do not describe every batch item. Check the queue count before starting.

5. Use **"Clear batch"** if the selection is wrong, then prepare the correct selection again. This does not delete audio or lyrics.

6. Click **"▶ START SYNC"**. Each queued track gets its own output. Review the summary for failures and inspect the finished files.

## Requirements and technical choices
**"Hardware / requirements"** reports component availability. Sync needs Syncalong, Whisper/PyTorch and FFmpeg; optional vocal separation also needs Demucs. PyTorch runs the model calculations and FFmpeg reads/converts audio.

A compatible GPU can speed up processing; CPU is slower. If memory is insufficient, choose a smaller **"Whisper model"**. Missing-component messages mean the environment needs setup, not that the lyrics themselves are wrong.

Technical controls appear with **"Developer mode"** enabled in T9 Downloader's **"Options…"**. **"Match threshold"** affects match acceptance. **"Use lyrics as Whisper prompt (recommended)"** gives the recognizer the lyric text as guidance. Keep the established values unless you are deliberately testing.

With **"Developer mode"** enabled, the Lyrics Sync log also explains every synchronized lyric line. **"DIRECT"** shows the lyric word matched to a Whisper word and its similarity score. **"INTERPOLATED"** means the timestamp was calculated between two direct matches. **"ESTIMATED before/after"** means Whisper did not directly match that line. The opening summary lists Whisper's first usable word and explicitly reports when missing opening recognition caused several first lines to receive one identical timestamp.

## Buttons and controls
**"Browse"** beside **"Audio"** — selects the recording to synchronize.

**"Browse"** beside **"Lyrics"** — selects the finished lyric text for that recording.

**"Browse"** beside **"Output"** — chooses the LRC destination for single-track mode.

**"Use selected track(s) from main window"** — prepares the T9 Downloader selection as one track or a batch.

**"Clear batch"** — clears the prepared batch without deleting files.

**"Whisper model"** — selects the recognition model and its speed/memory tradeoff.

**"Language"** — selects the lyric language or automatic detection.

**"Separate vocals with Demucs (optional)"** — isolates vocals before alignment.

**"After sync: embed timestamped LRC into MP3"** — must be enabled to store synchronized lyrics inside the MP3; a separate LRC is saved even when it is off.

**"Match threshold"** — sets the technical acceptance threshold for alignment.

**"Use lyrics as Whisper prompt (recommended)"** — on by default: the lyrics are given to Whisper as guidance, which gives the best result for most songs. Now and then a song syncs better with this option **off** — that is the rarer case. For such songs (or when you are not sure) use **"2 Pass sync"**: it syncs with the prompt on and off and keeps the better result automatically, so you do not have to guess.

**"Marked for Lyrics Sync"** — loads selected or all marked songs into the sync batch. Choose settings and press START SYNC. Successfully saved songs leave the queue; failed or unstarted songs remain.

**"Delete Current .lrc and Embedded Lyrics"** (Edit and Fix Synced Lyrics) backs up the selected songs' MP3 and LRC files, removes the LRC and embedded lyric tags, keeps the plain TXT and adds the songs to Marked for Lyrics Sync to sync them again later. Reports are ordered by total suspicious lines per song; scan results are cached and unchanged files are reused.

**"Edit and Fix Synced Lyrics"** — reads every LRC under the music folder opened in T9 Downloader. It flags three kinds of problems: **SAME TIME** — two or more lines sharing one timestamp; **DENSE** — lines less than 0.8 s apart that would have to be sung faster than 9 words per second (short lines inside such a run do not split it); **GUESSED** — 5 or more lines starting exactly evenly spaced: the sync could not hear them, so their times were spread evenly (interpolated) and are usually wrong. The **Errors** tab holds every SAME TIME warning, DENSE runs of 4+ long lines or 13+ words per second, and GUESSED runs of 8+ lines or less than 1 s apart; the **Potential Smaller Errors** tab holds the shorter DENSE and GUESSED runs. A song that has any error keeps all its passages in **Errors** (the smaller ones are marked “(small)”), because in an already broken song the smaller warnings can be dangerous too. Songs with the most suspicious lines come first; filter by type (SAME TIME / DENSE / GUESSED) or search by song name. When the line after a flagged passage comes more than 4 s later, the next two lines join the passage too — a broken passage often drags the following lines along. Ad-libs in parentheses are not counted as words. An ignored warning stays hidden until the timing of those lines changes; **Restore ignored** brings them back. These are listening warnings, not proof of incorrect alignment. The report marks runs at the beginning as START and lets you open the selected LRC or its folder. Scanning does not change LRC or audio files; deletion requires the separate marking action. Unchanged files reuse cached results; updated detection rules trigger a fresh scan.


**"▶ START SYNC"** — starts alignment of the chosen single track or prepared batch.

**"2 Pass sync"** (tick box under the prompt option) — **"▶ START SYNC"** then syncs each track twice, with the lyrics prompt ON and then OFF, and keeps only the better LRC. Fewer scan warnings win first, then more directly matched lines; a tie keeps prompt ON. It takes about twice as long, so use it for songs that did not sync well.

**"Stop after current track"** — finishes and saves the song currently being processed, then prevents the next queued song from starting.

**"Advanced settings ▸"** — shows the less used options: **"Match threshold"** (Developer mode), **"Separate vocals with Demucs (optional)"** and **"Allow sync of lines only in brackets ( ) (Not Recommended)"**. Closing the window with X saves the Sync preferences (it asks first if processing is active).

## Common problems to check
Wrong TXT version: sections may align incorrectly or remain unmatched.

Missing or repeated lyric lines: add/correct them in the TXT before running again.

Quiet vocals, ad-libs or instrumental sections: timestamps can be uncertain or drift.

When at least three opening lyric lines have no direct match and would receive one identical timestamp, Lyrics Sync automatically performs a short second Whisper pass over the original opening audio without Demucs. Recovered opening words are merged with the main transcription before alignment; the rest of the song keeps its original timing pass. This happens only for the detected exceptional case.

The same protection applies anywhere in the song. If at least three consecutive non-empty lyric lines would be written with one identical timestamp, Lyrics Sync re-transcribes only the affected interval from the original audio without Demucs. It accepts the local repair only when repeated timestamps decrease and timings outside that interval remain stable; otherwise it keeps the original alignment. The normal log reports whether each attempted repair was accepted or rejected.

Several opening lines with one identical timestamp: run **"Edit and Fix Synced Lyrics"** to find the same pattern elsewhere. For that song, first retry with **"Use lyrics as Whisper prompt"** disabled; if needed, also try without Demucs and use a match threshold near 48.

Some lines are not confidently matched: listen to those passages rather than assuming every timestamp is correct.

No lyrics in the player: check its LRC support, filename matching and where it searches for sidecar files.

Recommended player: Triple 9 Music Player
https://github.com/imtatar999/triple-9-music-player''',
'T9 Lyric Builder': '''# T9 Lyric Builder: turn audio into complete text
**"T9 Lyric Builder"** creates or completes a song's text from audio. It supports ordinary songs as well as long Sessions / Extended versions; choose either type in the **"Song audio"** field.

**I'm still working on Builder and trying to improve it. It won't always be 100% perfect.**

**"T9 Downloader"** is T9 Downloader's main tool for finding existing online lyrics. **"T9 Lyrics Sync"** takes finished text and adds playback timestamps. T9 Lyric Builder is the text-preparation stage between them when lyrics are missing or incomplete.

A shorter reference is an existing lyric text used as a guide, often from a shorter released version of the same song. T9 Lyric Builder works best with an accurate reference. It can also create lyrics without one: leaving the reference field empty enables **"Full Transcription Mode"**.

This part of the program is still being developed and is not perfect. Always listen, read the result and correct mistakes before saving a finished text. The result may need small word corrections or larger structural edits.

## Files created and where they go
Saving the finished text automatically creates the **"LYRICS"** folder beside the selected audio file:

    YOURMUSICFOLDER/
      ARTIST NAME/
        ALBUM NAME/
          Song.mp3
          LYRICS/Song.txt

**"Save Finished TXT for Sync (Match MP3 Filename)"** uses the audio's exact base filename. It writes the current result to that path and can replace an existing TXT there, so keep a separate copy if needed.

A queued track is loaded from the **"MARKED FOR TRANSCRIPTION"** support folder and its stored reference information. The original audio stays in YOURMUSICFOLDER. Do not manually move audio or the queue while you are working on it.

**"Export Result + Log for Testing"** creates a separate diagnostic file inside **"TESTING_EXPORTS"** in the program folder. That folder is created as needed. This export is for troubleshooting, not the finished TXT to give to **"T9 Lyrics Sync"**.

## Step by step: load a marked track
1. In **"T9 Downloader"**, select a track and use **"Mark for Transcription"**. This makes it available to T9 Lyric Builder's queue; it does not start transcription or move the audio.

2. Use **"Find shorter references"** in T9 Downloader if you want an automatically located guide, then open **"T9 Lyric Builder"**.

3. Click **"Marked tracks…"**. Select the song and inspect its audio/reference details. **"Reference ready"** means guide text is available; **"Reference missing"** means the track can still load but has no guide.

4. Click **"Load Marked Track"**. Audio and an available guide are loaded into the main T9 Lyric Builder window. Loading another track clears the existing reference/result after confirmation, so save work you want to keep first.

5. If you have a local guide, select the queue entry and click **"Choose reference TXT…"** to assign it and load the track. Otherwise paste the correct text into the left reference field after loading, or leave it empty for audio-only recognition.

## Step by step: start directly, including ordinary songs
1. Click **"Browse…"** under **"Song audio"** and choose any supported song recording, including an ordinary non-Sessions song.

2. Use **"Play Song"** to confirm you selected the right audio.

3. Paste an accurate reference into the left field if available. For an ordinary song, suitable known lyrics can also guide recognition; a reference does not have to come from an extended recording.

4. To create lyrics without a reference, leave the left field completely empty. **"Full Transcription Mode"** recognizes the entire recording from audio. It is useful when no lyrics exist, but generally requires more checking than a run with a good reference.

## Step by step: build, review and finish
1. Use **"Check / Setup T9 Lyric Builder"** if this is your first run or the environment is not ready. It checks dependencies and guides preparation of missing components. Setup and first model downloads need internet access and disk space.

2. Choose **"Whisper model"** and **"Language"**. Whisper is an audio-recognition model used in the processing pipeline. Larger models need more memory/time; keep a tested model and use the correct language.

3. **"TripleNine Engine"** is the fixed processing engine, combining Xingyu recognition with Whisper alignment. It handles ordinary songs and extended recordings, both with and without reference lyrics. There is no engine selector and the same engine is used in both reference modes. Xingyu uses a separate Python 3.11 environment.

4. Usually keep the tested **"Xingyu preset"**. Choose **"CUDA"** under **"Xingyu device"** for a compatible NVIDIA GPU, **"CPU"** if GPU processing does not work, or **"AUTO"** for automatic selection. CPU can be much slower.

5. Click **"BUILD LYRICS"**. Watch the status while it runs. **"Stop"** interrupts T9 Lyric Builder processing; do not treat an unfinished result as a reviewed song.

6. Read the right-hand **"Hybrid result"** while listening with **"Play Song"**. Correct words, repeated sections and misplaced lines directly in the result field. Check the opening and ending too, especially when no reference was used.

7. Click **"Save Finished TXT for Sync (Match MP3 Filename)"**. It saves the edited result as **"LYRICS"**/Song.txt. If this is the loaded marked track, it normally changes its queue status to **"Ready for Lyric Sync"**; entries with this status disappear from the pending transcription list and are now ready for the synchronization step.

8. Return to T9 Downloader and use **"Refresh"** if needed. Select the song and open **"T9 Lyrics Sync"**. It can automatically pair Song.mp3 with **"LYRICS"**/Song.txt, then create **"SYNCED LYRICS"**/Song.lrc. Saving text in T9 Lyric Builder alone does not create timed lyrics.

## Buttons and controls
**"Browse…"** — chooses audio for an ordinary song or Sessions / Extended recording.

**"Play Song"** — opens the chosen audio in your default player.

**"Marked tracks…"** — opens the unfinished track queue shared with T9 Downloader.

**"Refresh"** in the queue — reloads entries and their current completion/reference states.

**"Load Marked Track"** — loads the selected audio and available reference into T9 Lyric Builder.

**"Choose reference TXT…"** — assigns a local reference TXT and loads that marked track.

**"Song audio"** — shows the selected recording path, for an ordinary song or an extended recording.

**"Paste reference lyrics here"** — holds guide lyrics, or stays empty for full audio transcription.

**"Hybrid result"** — contains generated text that you can edit before saving.

**"Whisper model"** — selects the recognition model used by the processing pipeline.

**"Language"** — sets the recognition language.

**"Advanced ▸" → "Xingyu preset"** — selects the Xingyu recognition preset.

**"Advanced ▸" → "Xingyu device"** — chooses CPU, automatic selection or CUDA GPU processing (CUDA needs a compatible NVIDIA card; CPU is slower).

**"Check / Setup T9 Lyric Builder"** — checks and helps prepare the required runtime and components.

Colours — Lyric Builder uses the same theme as the Downloader (**Settings → Theme** there).

**"BUILD LYRICS"** — starts text generation/completion for the loaded recording.

**"Stop"** — interrupts T9 Lyric Builder processing.

**"Clear result"** — clears the result and diagnostic log in the window without deleting saved files.

**"Save Finished TXT for Sync (Match MP3 Filename)"** — writes the edited result to the same-named TXT in **"LYRICS"** and completes the loaded queue entry when applicable.

**"Reference match threshold"** — controls technical reference-match acceptance and normally stays unchanged.

**"Diagnostic log"** — shows processing details for troubleshooting while developer mode is enabled.

**"Export Result + Log for Testing"** — saves text and diagnostics in **"TESTING_EXPORTS"** for analysis.

**"Developer mode"** in T9 Downloader's **"Options…"** — shows or hides T9 Lyric Builder's threshold, diagnostic log and testing export after **"Save"**, preserving their values.

## Common Builder mistakes: check these before saving
A line from a chorus may be placed in a verse, or a verse line may attach to the chorus; move it back to the correct section while listening.

A word may be misheard or spelled incorrectly, especially slang, names or fast phrases; replace it with what is actually sung.

A chorus or individual line may be repeated too many times, or a real repetition may be missing; compare the number and order of repetitions with the recording.

Quiet words, ad-libs or overlapping voices may be missed; add them if they belong in your lyrics.

The beginning or ending may contain missing text or invented words over silence/instrumental audio; check those parts rather than trusting them automatically.

A wrong or incomplete reference can guide the result toward the wrong wording or structure; verify the guide as well as the output.

**I'm still working on Builder and trying to improve it. It won't always be 100% perfect.** Even when most of the text looks right, always check it yourself and correct errors before sending it to **"T9 Lyrics Sync"**.'''
}
