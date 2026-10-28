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
    skipped_picture = False
    for line in body.splitlines():
        if line.startswith('[[image:'):  # pictures only appear in the GitHub guide (docs/HOW-TO-USE.md)
            skipped_picture = True
            continue
        if skipped_picture and not line.strip():  # no empty gap where a picture would have been
            continue
        skipped_picture = False
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
    'T9 Quick Start': [('1. Set up', '1. Set up (once)'), ('2. Download', '2. Download the lyrics'), ('3. Sync', '3. Sync: add the timing'),
                       ('4. Fix ⚠', '4. A ⚠ next to a song? Fix it'), ('5. Builder', '5. No lyrics anywhere? Use Lyric Builder')],
    'T9 Downloader': [('Your music folder', 'Your music folder'), ('Buttons', 'The main window'), ('Song tags', 'Song tags (metadata)'),
                      ('Backups and undo', 'Backups and undo'), ('Problems', 'If something does not look right')],
    'T9 Lyrics Sync': [('Sync a song', 'Sync one song'), ('Many songs', 'Sync many songs'), ('Settings', 'Settings explained'),
                       ('Edit and Fix', 'Edit and Fix Synced Lyrics'), ('Problems', 'Common problems')],
    'T9 Lyric Builder': [('Build lyrics', 'Build lyrics step by step'), ('Settings', 'Settings explained'), ('Check the result', 'Check the result before saving')],
}


PAGES = {
'T9 Quick Start': '''# Quick start: download and sync your first song
Triple 9 Studio finds the lyrics of your MP3 files, adds timing to them (so each line shows at the right moment while the song plays) and helps you fix the mistakes.

The whole job is three steps: **Download** the lyrics, **Sync** them, **Fix** what the program marks with ⚠. Lyric Builder is only for songs that have no lyrics anywhere.

[[image: main.png | The main window: your songs on the right, icons show what each song already has]]

## 1. Set up (once)
1. Get a free Genius token: open https://genius.com/api-clients, create an API client and copy its **Client Access Token** (never the Client Secret).

2. In the program open **"Settings"** → **"Options…"**, paste the token and click **"Save Token"**. The program also asks for it the first time you download.

3. Click **"Open Music Folder"** and choose the folder with your MP3 / FLAC files. All songs, including the ones in sub-folders, appear in **"Tracks"**.

## 2. Download the lyrics
1. Click a song in **"Tracks"**. Hold Ctrl or Shift to choose several.

2. On the **"Download"** tab click **"Download Lyrics for Selected"**.

[[image: main-download.png | Select a song, then click Download Lyrics for Selected]]

3. The lyrics are saved as a text file in a **LYRICS** folder next to the song. The song gets the green ✔ **Downloaded** icon. This text has no timing yet.

**The program is not sure it found the right song?** A window opens with the closest match it found and how sure it is. Listen with **"Play Song"** and look at the lyrics with **"Open Genius"**, then choose:
- **"Confirm"** — it is the right song, use these lyrics.
- **"Reject"** — it is the wrong song. Paste the correct Genius link of the song into the box and click **"Download from this link"**.
- **"Skip"** — leave this song for later.

[[image: review.png | The program is not sure: Confirm or Reject]]
[[image: review-reject.png | After Reject: paste the correct Genius link]]

A similar title is not enough: the lyrics must fit your version of the song.

## 3. Sync: add the timing
1. Select the song(s) that now have lyrics and click **"Lyrics Sync"** (top right).

2. Check the three lines at the top: **Audio**, **Lyrics** and **Output** should already point to your song, its lyrics and the new LRC file. The other settings are fine as they are.

3. Click **"▶ START SYNC"** and wait. The first time the program downloads a speech model (about 1.5 GB), so it takes longer. A graphics card makes it much faster.

[[image: sync.png | Lyrics Sync: check Audio / Lyrics / Output, then START SYNC]]

4. When it is done the song shows 📄 **Timed LRC file** (a timed file next to the song) or 🎶 **Synced in audio** (the timing is also saved inside the MP3). Open the song in a player that shows synchronized lyrics, for example our own **Triple 9 Music Player** (https://github.com/imtatar999/triple-9-music-player).

## 4. A ⚠ next to a song? Fix it
**⚠** means the program found lines that probably have the wrong time, for example several lines at the same moment. **❕** is a small doubt. **❓** means the lyrics may be the wrong text for this song: check them first (**"View/Change Lyrics…"**).

**Fastest way:** right-click the song → **"Fix this song"**. It opens the song at its first problem.

**To see all problems:** in the **"Lyrics Sync"** window click **"Edit and Fix Synced Lyrics"**. The program scans your library and lists every problem passage. Click a row to see which lines it means. The types are:
- **SAME TIME** — several lines have exactly the same time.
- **DENSE** — too many words in too little time to be sung.
- **GUESSED** — the program could not hear these lines, so it spread them out evenly.
- **ENDS EARLY** (own tab) — the song goes on long after the last lyric line.

[[image: editfix.png | Edit and Fix Synced Lyrics: every problem passage of your library]]

Not sure if a row is a real problem? **"Quick listen"** plays about 10 seconds around each one: press **O** if it sounds fine, **F** to fix it, **S** to skip. **"▶ Fix songs one by one"** opens the songs one after another.

**To fix a song** click **"Fix Fragment Manually…"** (or **"Fix this song"**). The lines with errors are red:

1. Click **"Fix lines automatically"**. The program tries several ways to time the red lines; each way is a coloured row under the waveform. Play it, and when one matches the song click **"Use selected fix"**.

2. None fits? Set the time yourself: play the song, **right-click the number of the line** under the waveform and press **T** the moment that line starts. Do this for each red line.

3. Click **"Save"**. Only the lines you changed are written, and a backup is made first. A mistake? Press Ctrl+Z.

[[image: fix.png | Fix Fragment Manually: red lines are the errors]]

Wrong words in a line? **"Edit Line"** changes the text. The whole text wrong for the song? **"Wrong lyrics — delete sync & find new text…"**.

## 5. No lyrics anywhere? Use Lyric Builder
When no website has the lyrics of a song (for example an unreleased one), Lyric Builder listens to the song and writes the lyrics for you. It is still a beta: always check the result.

1. Click **"Lyric Builder"** (top right) and **"Browse…"** for the song.

2. Optional but better: paste the lyrics of a similar or shorter version into the left box. Without them, leave it empty.

3. Click **"BUILD LYRICS"**. When it finishes, listen to the song and correct the right-hand text.

4. Click **"Save Finished TXT for Sync"**, then go back to step 3 above and sync the song.

[[image: builder.png | Lyric Builder: reference on the left, result on the right]]
''',

'T9 Downloader': '''# T9 Downloader: the main window
This page explains the main window. To get started quickly, read **Quick Start** first.

## Your music folder
Choose any folder with MP3 / FLAC files using **"Open Music Folder"**. You can keep the files in a simple folder or organise them like this:

    YOURMUSICFOLDER/
      ARTIST NAME/
        ALBUM NAME/
          Song.mp3
          LYRICS/Song.txt
          SYNCED LYRICS/Song.lrc

- **"Artists"** and **"Albums"** are optional filters taken from the folders. Select nothing to see every song.
- **LYRICS** holds the plain text (TXT). **SYNCED LYRICS** holds the timed lyrics (LRC). The program creates both when needed; keep these folder names.
- A text file and its song must have the same name (**Song.mp3** and **Song.txt**) to be paired automatically.
- The program never moves your music.

A **TXT** is plain text. An **LRC** has a time before every line, like [00:12.50]. Only an LRC shows lyrics in time with the song.

## The main window
**"Tracks"** — your songs. Next to the name you see their tags (artist, album, year …) like in Mp3tag. Click a column heading to sort, **"Columns…"** to hide or show columns. The icons:
- 🎶 **Synced in audio** — timed lyrics are inside the MP3.
- 📄 **Timed LRC file** — a timed LRC is next to the song.
- ✔ **Downloaded** — plain text only, no timing yet.
- ⚠ / ❕ / ❓ — possible timing errors / small doubts / the lyrics may be wrong (see Quick Start, step 4).

**"Song Search"** — filters the list by song name. **"Refresh"** (F5) rescans the folder after you changed files in Windows.

**"Download Lyrics for Selected"** — downloads lyrics for the chosen songs. **"Find & Download Missing Lyrics TXT"** does it for every song that has no text yet.

**"Get synced lyrics from the shared sheet…"** — downloads ready-made timed lyrics from our community sheet for the songs that are in it. The file is named like your MP3, saved in SYNCED LYRICS and embedded in the MP3. The program asks you when it is not sure it is the same song.

**"Lyrics Sync"** and **"Lyric Builder"** open the other two tools.

**"View/Change Lyrics…"** (or double-click a song) shows the lyrics and where they came from. You can replace them with another Genius link or your own TXT, or search other versions with **"Find different lyrics…"**.

**"Delete lyrics ▾"** (Cleanup & undo tab) removes only the LRC, both TXT and LRC, or only the lyrics inside the MP3. It always asks first and keeps a backup. **"Undo Delete Lyrics"** brings the last deletion back.

**"Mark for Transcription"** (Transcription tab) sends songs to Lyric Builder's list.

Hold the mouse over any button for a moment to see what it does. Right-click a song for quick actions: **Change Metadata…**, **Fix this song**, **Play Song** and more.

## Song tags (metadata)
On the **"Metadata"** tab:
- **"Change Metadata…"** — edit the file name, title, artist, album, track, year, genre, comment and the front cover. Select **several songs** to give them all the same artist, album and so on; open the arrow of a box to pick a value one of your songs already has.
- **"Remove brackets from Titles…"** — cleans titles like “Swerve (JW3)”. Words like Session, OG, Alt and ft./feat./with stay.
- **"Swap Song and Transfer Metadata…"** — you have a better file of a song? Copy the tags, lyrics and cover of the old file to the new one and replace the old one.
- **"Import from Spreadsheet…"** — paste the link of a shared Google Sheet; the program finds your songs in it and lets you pick which columns (dates, producers, album …) to write into the tags.
- **"Set Year from comment"** — takes the year from a comment such as “(Recorded July 15, 2019)”.

## Backups and undo
Before the program changes or deletes anything of yours, it saves a copy in a **BACKUPS** folder.
- **Settings → Options → "Restore from backups…"** lists every copy and puts the ones you choose back.
- **"Clean old backups…"** deletes old copies to free disk space.

## Settings
**"Settings"** → **"Options…"**: which lyrics websites are used and in which order, the Genius token, and where the LYRICS folder is made. The usual settings are fine for most people.
- **"Export Lyrics Library"** / **"Import Lyrics Library"** move all your lyrics to another computer in one small ZIP (no music inside).
- **Help → "Check for updates…"** looks for a new version; the program also checks by itself at startup. **Help → "Join our Discord"** and **"Support the project on Ko-fi"** open our community pages.
- **Settings → Theme** switches between the dark and light look.

## If something does not look right
- **No lyrics found:** open **"View/Change Lyrics…"** and paste the correct Genius link, or your own TXT.
- **Wrong version of the song:** a short released text will not fit a long Sessions recording. Replace it before syncing.
- **The Download button is grey:** choose a song first; the buttons are also off while a download runs.
- **Genius says the token is wrong:** use **"Test Token"** in Options.
- **Nothing in the list:** check the folder and press **"Refresh"**.
- Still stuck? Ask in our Discord (Help → Join our Discord).
''',

'T9 Lyrics Sync': '''# T9 Lyrics Sync: put every line on the timeline
Lyrics Sync takes a song and its finished lyrics (TXT) and creates an LRC with the time of every line. It listens to the song with an audio-recognition model (Whisper) to find when each line is sung. It does not write missing words: if the text is incomplete, complete it first (Downloader or Lyric Builder).

## Sync one song
1. Select the song in the main window and click **"Lyrics Sync"** (or use the three **"Browse"** buttons for **Audio**, **Lyrics** and **Output**).

2. Check that the TXT is the right text for this exact version of the song.

3. Click **"▶ START SYNC"** and watch the log. The result is saved in **SYNCED LYRICS** next to the song. With **"After sync: embed timestamped LRC into MP3"** ticked the timing is also saved inside the MP3.

4. Listen to the result in a player with synchronized lyrics. Words wrong or missing? Correct the TXT and sync again. Timing wrong in places? See **Edit and Fix** below.

## Sync many songs
1. In the main window select all the songs that have a finished TXT.

2. Click **"Lyrics Sync"** (or **"Use selected track(s) from main window"**). The window shows how many songs are queued; songs without a TXT are skipped.

3. Click **"▶ START SYNC"**. Every song gets its own LRC. **"Stop after current track"** finishes the song in progress and stops. **"Clear batch"** empties the queue without deleting anything.

## Settings explained
**"Whisper model"** — bigger models are more exact but slower and need more memory. Keep the recommended one unless it runs out of memory.

**"Language"** — the language of the lyrics (en, pl …) or auto.

**"Use lyrics as Whisper prompt"** — on by default, best for most songs.

**"2 Pass sync"** — syncs every song twice (prompt on and off) and keeps the better result. Twice as slow; use it for songs that did not sync well.

**"After sync: embed timestamped LRC into MP3"** — also saves the timing inside the MP3 file. Without it you only get the separate LRC file.

**"Advanced settings ▸"** — vocal separation (Demucs) and other options that normally stay as they are.

**"Check / Setup Lyrics Sync"** — checks that everything needed is installed and fixes what is missing.

**"Marked for Lyrics Sync"** — loads the songs you marked for syncing again (for example after "Delete Current .lrc and Embedded Lyrics").

## Edit and Fix Synced Lyrics
**"Edit and Fix Synced Lyrics"** scans the LRC files of your library and lists the passages that probably have wrong times. Nothing is changed by the scan. The tabs:
- **Errors** — the problems that are very likely real.
- **Potential Smaller Errors** — shorter doubts: worth a listen.
- **Ends Early** — the song goes on 30+ seconds after the last lyric line (60+ s counts as an error). The sync may have stopped half way, or the song has a long outro.

Buttons under the list:
- **"Fix Fragment Manually…"** — fix the chosen passage (steps in Quick Start, step 4).
- **"▶ Fix songs one by one"** — all listed songs in a row; in the fix window **"Save & next song"** (Ctrl+Enter) saves and continues.
- **"Quick listen"** — plays 10 s around each passage: **O** = fine (the warning is ignored), **F** = fix it, **W** = wrong lyrics, **S** = skip.
- **"Ignore selected warning"** — it was a false alarm. **"Restore ignored"** brings it back.
- **"Swap song (new audio)…"** — replace the file with a better recording, keeping the tags and lyrics.
- **"Delete Current .lrc and Embedded Lyrics"** — starts the song again: the LRC and the lyrics inside the MP3 are deleted (backed up), the TXT stays and the song goes to **Marked for Lyrics Sync**.
- **"Edit and Fix history"** — every song you changed, newest first.

In the fix window:
- **Space** plays or pauses, click the waveform to jump, **−** / **=** zoom.
- **T** sets the time of the chosen line to the playhead and moves to the next line, so you can tap along. **Clear time of selected** (Delete) first removes the time of wrong lines; then **🎤 Live tap** (click the line caption above the waveform) lets you play the song and press T for each one.
- **⏩ Speed** slows the song down without changing the pitch. **T reaction** moves your taps earlier, because we press a little after we hear it (try 0.15 s).
- **"Edit Line"**, **"Remove Line from song"** change the text in the LRC, TXT and MP3 at once, after a backup.
- **"Skip to next error line"** jumps to the next red passage.

## Common problems
- **Wrong TXT version:** lines are placed wrongly or not found. Use the text of exactly this recording.
- **Missing or repeated lines in the TXT:** add or correct them and sync again.
- **Quiet vocals or ad-libs:** times can be uncertain. Listen to those parts.
- **Several first lines have the same time:** try again with **"Use lyrics as Whisper prompt"** off (or use **"2 Pass sync"**).
- **The player shows no lyrics:** check that it supports LRC and that the file has the same name as the song and sits next to it (or in SYNCED LYRICS).
- Recommended player: **Triple 9 Music Player** — https://github.com/imtatar999/triple-9-music-player
''',

'T9 Lyric Builder': '''# T9 Lyric Builder: turn audio into a complete text
Lyric Builder listens to a song and writes its lyrics. Use it when no website has them or the text you found is incomplete (for example a long Sessions version).

**Lyric Builder is still being improved and is not always 100% right. Always listen and correct the result before you save it.**

## Build lyrics step by step
1. Click **"Lyric Builder"** in the main window. If you first select a song, use **"Mark for Transcription"** and then **"Marked tracks…"** → **"Load Marked Track"** in the Builder: the song (and a reference, if the program found one) is loaded for you.

2. Or choose any song yourself: **"Browse…"** under **Song audio**. **"Play Song"** helps to check it is the right one.

3. Optional, but it gives a better result: paste a **reference** into the left box: the lyrics of a similar or shorter version of the song. With an empty box the Builder transcribes the whole song from the audio (**Full Transcription Mode**); expect more to correct.

4. The first time click **"Check / Setup Builder"**. It installs what the Builder needs (a few GB, internet needed).

5. Click **"BUILD LYRICS"** and wait. **"Stop"** interrupts it.

6. Read the right-hand **Hybrid result** while you listen to the song and correct it (see the checklist below).

7. Click **"Save Finished TXT for Sync"**. The text is saved in the **LYRICS** folder next to the song, with the same name as the MP3 (it can replace an old TXT there). The song is then **"Ready for Lyric Sync"**.

8. Back in the main window press **"Refresh"** and sync the song with **Lyrics Sync**. Saving the text does not add timing.

## Settings explained
**"Whisper model"** and **"Language"** — the recognition model (bigger = slower but better) and the language of the song.

**"Advanced ▸"** — **Xingyu device**: CUDA for an NVIDIA graphics card, CPU if the card does not work (slower), AUTO to let the program decide. The preset normally stays as it is.

**"Clear result"** — empties the result window; saved files stay.

## Check the result before saving
- A chorus line placed in a verse, or the other way round.
- A word misheard or misspelled: slang, names and fast phrases most often.
- A chorus repeated too many or too few times.
- Quiet words, ad-libs and overlapping voices that were missed.
- The beginning and the end: invented words over silence or music.
- A wrong reference leads to wrong words, so check the reference too.
''',
}
