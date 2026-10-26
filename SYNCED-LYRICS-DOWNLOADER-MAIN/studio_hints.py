"""A short explanation for every button, shown in a small bubble next to it while the mouse is over it.

Works in every window. ``install()`` binds the whole program at once (all Button / Checkbutton /
Radiobutton classes), so a button added later is covered as soon as it has an entry in ``HINTS``.
A button that has its own, longer tooltip keeps showing that one.
"""
import re
import tkinter as tk

CLASSES = ("Button", "TButton", "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton", "Menubutton",
           "TMenubutton")


def hint_key(text):
    """The button text without counters ([0], (3), (2/5)), icons and trailing marks, lower case."""
    text = str(text or "")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\(\s*\d+(\s*/\s*\d+)?(\s+\w+)?\s*\)", " ", text)
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"^[^\w+−.\-]+", "", text)  # leading icons / symbols (keep + − . - and word characters)
    text = re.sub(r"[^\w)]+$", "", text)             # trailing … ▶ : etc.
    return re.sub(r"\s+", " ", text).strip().casefold()


_RAW = {
    # ---- main window: download ----
    "Download Lyrics for Selected": "Download lyrics from Genius for the selected tracks — or, with no track "
        "selected, for the selected artist.",
    "A-Z": "How the Tracks list is sorted — click to choose another order.",
    "New": "Sort: songs with no downloaded lyrics and nothing synced (no status) come first.",
    "Lyrics Synced First": "How the Tracks list is sorted — click to choose another order.",
    "Downloaded first": "How the Tracks list is sorted — click to choose another order.",
    "Failed first": "How the Tracks list is sorted — click to choose another order.",
    "Marked first": "How the Tracks list is sorted — click to choose another order.",
    "LRC tools": "Edit the song's LRC by hand, or embed the available LRC into the MP3.",
    "Check lyrics files": "Find TXT/LRC under old song names, or LRC files missing lines in brackets.",
    "Delete lyrics": "Delete TXT + LRC, only the synced LRC, or the lyrics embedded in the MP3 (all backed up).",
    "Show more log": "Make the log bigger.",
    "Show less log": "Make the log smaller so the track lists get more room.",
    "Download Lyrics for Selected Artist": "Download lyrics for every track of the selected artist.",
    "Find & Download Missing Lyrics TXT": "Find tracks that have no lyrics TXT yet and download them.",
    "Clear All Selections": "Deselect artists, albums and tracks.",
    "Select All": "Select every item in this list.",
    "Select": "Select every item in this list.",
    "Lyrics Sync": "Open the Lyrics Sync window: time lyrics to a song and repair sync errors.",
    "Lyric Builder": "Open the Lyric Builder: build lyrics from a full transcription of the song.",
    "Marked for Lyrics Sync": "Songs whose old sync was deleted and that wait for a new Lyrics Sync.",
    "Open Music Folder": "Choose the folder with your music.",
    # ---- main window: track & lyrics ----
    "Play Song": "Play the selected song in your default player.",
    "View/Change Lyrics": "See the lyrics and source of the song; fix or replace them, find different lyrics.",
    "Change Song Name + Title": "Rename the song file and its title tag together.",
    "Change LRC Manually": "Edit the song's synced lyrics (LRC) as text; the MP3's embedded lyrics are updated too.",
    "Manually Embed Available LRC": "Put the song's existing LRC into the MP3 as embedded timed lyrics.",
    "Edit and Fix Synced Lyrics": "Scan all LRC files for timing errors and fix them (the ⚠ in Tracks).",
    # ---- main window: transcription / repair / cleanup ----
    "Mark for Transcription": "Mark the selected tracks for a full transcription in Lyric Builder.",
    "Find shorter references": "Look for a shorter reference text for the marked tracks.",
    "Delete shorter reference": "Clear the shorter references of the selected tracks; they stay marked.",
    "Check missing TXT / LRC — old names": "Find TXT/LRC files that belong to songs under an old name.",
    "Clean old backups": "See how much space song backups use and delete the ones you do not need.",
    "Delete ALL Lyrics (TXT + LRC)": "Delete the lyrics TXT and the synced LRC of the selected tracks (backed up).",
    "Delete Synced LRC for Selected": "Delete only the timed LRC of the selected tracks; the plain TXT stays.",
    "Remove embedded lyrics for selected": "Remove the lyrics tag from the selected MP3 files; TXT and LRC stay.",
    "Undo Delete Lyrics": "Bring back the lyrics deleted last, from the backup.",
    "Restore from backups": "Put song files back from the backups the program made.",
    "After restoring an LRC, also update the MP3's embedded lyrics": "After restoring an LRC, also put it into the MP3 (when the MP3 has lyrics).",
    "Restore selected": "Restore the selected backups to where their files came from.",
    "Delete ticked backups": "Delete the backups of the ticked file types to free disk space.",
    "Recount": "Count the backup sizes again.",
    "Export Lyrics Library": "Save all your lyrics into one file you can keep or move.",
    "Import Lyrics Library": "Load lyrics from a file made by Export Lyrics Library.",
    # ---- Options ----
    "Save": "Save and keep what you entered.",
    "Save Token": "Save the Genius API token.",
    "Test Token": "Check that the Genius API token works.",
    "Remove Token": "Forget the saved Genius API token.",
    "Show token": "Show or hide the token text.",
    "Clear Genius Cache": "Delete downloaded Genius search results so they are fetched fresh.",
    "Up": "Move the selected provider higher in the search order.",
    "Down": "Move the selected provider lower in the search order.",
    "Reset window positions": "Forget where every window was and bring the open ones to the middle of the screen.",
    ".lrc": "Save downloaded lyrics as timed LRC files.",
    ".txt (recommended for Syncalong)": "Save downloaded lyrics as plain text (needed for Lyrics Sync).",
    "Auto-upgrade plain → synced (no prompt)": "Replace plain lyrics with synced ones automatically when found.",
    "Auto-upgrade plain → synced (prompt for each)": "Ask before replacing plain lyrics with synced ones.",
    "Strip CJK lines from downloaded LRC": "Remove Chinese/Japanese/Korean lines from downloaded LRC files.",
    "Reject lyrics files that are mostly non-ASCII": "Skip downloaded lyrics that are mostly non-Latin text.",
    "Store lyrics in a \"LYRICS\" folder next to the track": "Keep lyrics TXT files in a LYRICS folder beside each song.",
    "Genius plain-lyrics support — REQUIRED for Genius downloads (keep enabled)": "Needed for Genius downloads; leave it on.",
    "Developer mode — show technical and testing controls": "Show extra technical controls and logs.",
    # ---- View/Change Lyrics ----
    "Copy current link": "Copy the Genius link of the current lyrics.",
    "Open current source": "Open the Genius page of the current lyrics in your browser.",
    "Find different lyrics": "Search Genius for other versions of this song's lyrics.",
    "Turn on editing": "Edit the lyrics text here.",
    "Load from TXT": "Load the text from a .txt file.",
    "Save Text": "Save the edited lyrics TXT.",
    "Save and Change Lyrics and Synced Lyrics": "Save the text and remove the same lines from the synced LRC and the MP3 (backed up).",
    "Download and replace": "Download the lyrics from the pasted Genius link and replace the current ones.",
    "Use selected — download and replace": "Download the selected version and replace the current lyrics.",
    "Open on Genius": "Open the selected version on Genius.",
    "Copy link": "Copy this Genius link.",
    "Download from this link": "Download the lyrics from the pasted Genius link.",
    "Lyric Sync Now": "Load this song and its TXT into Lyrics Sync (it does not start the sync).",
    "Open in Lyric Builder": "Open Lyric Builder with this song and its TXT as the shorter reference.",
    "Save Name + Title": "Save the new file name and title tag.",
    "Save LRC + update MP3": "Save the LRC and update the MP3's embedded lyrics (both backed up).",
    "Load reference TXT": "Load a shorter reference text from a file.",
    # ---- Lyrics Sync window ----
    "START SYNC": "Start syncing the chosen song, or all queued songs (twice each with 2 Pass sync).",
    "Start Sync 2 Pass (Slow)": "Sync twice (lyrics prompt on, then off) and keep the better result.",
    "Stop after current track": "Finish the song being synced, then stop the batch.",
    "Pause": "Pause or continue the running download.",
    "Clear batch": "Empty the list of queued songs.",
    "Use selected track(s) from main window": "Take the tracks selected in the main window as the songs to sync.",
    "Browse": "Choose a file.",
    "Browse…": "Choose a file.",
    "Check / Setup Lyrics Sync": "Check that the sync tools are installed and install what is missing.",
    "Install/Repair PyTorch GPU": "Install or repair the GPU version of PyTorch for faster syncing.",
    "After sync: embed timestamped LRC into MP3": "Write the finished LRC into the MP3 as embedded lyrics.",
    "Use lyrics as Whisper prompt (recommended)": "Give Whisper the lyrics as a hint; usually more accurate.",
    "Allow sync of lines only in brackets": "(Not Recommended) Also sync lines made only of brackets, "
        "e.g. “(Cha-ching, brrt, hello?)”.",
    "Advanced": "Show or hide the Xingyu preset and device (CPU / AUTO / CUDA).",
    "2 Pass sync": "Sync each song twice (lyrics prompt on and off) and keep the better result; about twice as slow.",
    "Advanced settings": "Show or hide the less used settings: match threshold, Demucs, lines only in brackets.",
    "Separate vocals with Demucs (optional)": "Isolate the vocals before listening; slower but helps with loud beats.",
    # ---- Edit and Fix report ----
    "Fix Fragment Manually": "Open the waveform window to retime the selected passage by hand.",
    "Fix Fragment Manually (any LRC)": "Choose any LRC file and fix lines in it by hand.",
    "Fix songs one by one": "Open the listed songs one after another, each at its first error.",
    "Quick listen": "Hear each passage for a few seconds and decide: OK, fix, or wrong lyrics.",
    "Ignore selected warning": "Hide the selected warnings (the LRC is not changed).",
    "Restore ignored": "Show all ignored warnings again.",
    "Edit and Fix history": "See what was changed in which song, and open it again.",
    "Delete Current .lrc and Embedded Lyrics": "Delete the song's LRC and embedded lyrics (backed up) to sync it again.",
    "Open selected LRC": "Open the LRC file of the selected row.",
    "Open containing folder": "Open the folder of the selected song.",
    "Refresh": "Reload this list.",
    "Remove from history": "Remove the selected songs from the history list.",
    "Load selected for sync": "Load the selected songs into Lyrics Sync.",
    "Errors": "Show the real errors (songs with errors keep all their passages here).",
    "Potential Smaller Errors": "Show only small possible timing issues of otherwise clean songs.",
    "Ends Early": "Songs whose lyrics end 30+ s before the song does (longest gap on top). No ⚠ in Tracks.",
    "All types": "Show every kind of warning.",
    "SAME TIME": "Show lines that share one timestamp.",
    "DENSE": "Show lines squeezed together too tightly to be sung.",
    "GUESSED": "Show lines whose times were guessed (spread evenly).",
    "Swap song (new audio)…": "Replace the selected songs with new audio; tags, lyrics and cover move over, then each is checked again.",
    # ---- Fix fragment window ----
    "Full screen": "Make this window fill the screen (F11).",
    "Fix lines automatically": "Try several automatic methods on the selected lines; you pick the best one.",
    "Open in system player": "Play the song in your default player.",
    "Wrong lyrics — delete sync & find new text": "The text is wrong: delete the sync and look for other lyrics.",
    "Remove Line from song": "Delete the selected lines from the LRC, the TXT and the MP3 (backed up).",
    "Edit Line": "Change the text of one line in the LRC, the TXT and the MP3.",
    "Clear time of selected": "Take the time off the selected lines; set it again with T or Live tap.",
    "No error lines left": "Every red (error) line of this song has a new time.",
    "Scanning LRC library": "The library is being scanned; the list opens when it is done.",
    "Skip to next error line": "Jump to the next red (error) passage.",
    "Set new timecode to playhead": "Give the chosen line the time of the red playhead (key T).",
    "Save manual timing": "Save every change made in Current to the LRC and the MP3.",
    "+0.1 s": "Move the selected lines 0.1 s later.",
    "−0.1 s": "Move the selected lines 0.1 s earlier.",
    "⇤ Move selected to playhead": "Move the selected lines so the first starts at the playhead.",
    "Move selected to playhead": "Move the selected lines so the first starts at the playhead.",
    "Undo last change": "Take back the last step (Ctrl+Z).",
    "Undo all changes": "Put Current back to the timing in the LRC.",
    "Tap mode (after Set, go to the next line)": "After each T, choose the next line automatically.",
    "Use selected fix": "Put the chosen automatic fix into Current; then fine-tune and save.",
    "Hide all fixes (none fit)": "Hide the automatic fixes when none of them is right.",
    "Show automatic fixes again": "Bring the automatic fixes back.",
    "Save": "Save everything changed in Current and close.",
    "Save line": "Save the edited line text.",
    "Save & next song": "Save this song and open the next one in the queue (Ctrl+Enter).",
    "Skip song": "Leave this song without saving and open the next one.",
    "Close": "Close this window.",
    "Cancel": "Close without changing anything.",
    "Start": "Start.",
    "Don't show this again": "Do not show this explanation again.",
    "Hide console": "Hide the details panel of the automatic fixes.",
    "Show console": "Show the details panel of the automatic fixes.",
    # ---- player ----
    "5 s ⏩": "Jump 5 seconds forward.",
    "⏪ 5 s": "Jump 5 seconds back.",
    "Play": "Play or pause the song (Space).",
    "Pause ": "Pause the song (Space).",
    "Zoom +": "Zoom the waveform in (key =).",
    "Zoom −": "Zoom the waveform out (key −).",
    "Auto-scroll": "The waveform follows the playhead while playing; click to switch it off or on.",
    "1×": "Normal playback speed.",
    "0.75×": "Play at 0.75× speed without changing the pitch.",
    "0.5×": "Play at half speed without changing the pitch.",
    # ---- Quick listen ----
    "Sounds OK — ignore": "The timing sounds right: ignore this warning (O).",
    "Needs fixing": "Open Fix fragment for this passage; you come back afterwards (F).",
    "Lyrics wrong": "The text is wrong: open View/Change Lyrics (W).",
    "Skip": "Leave this one for later and play the next (S).",
    "Replay": "Play the clip again (Space).",
    "Loop the clip until I decide": "Keep repeating the clip until you choose.",
    # ---- Lyric Builder ----
    "Check / Setup Builder": "Check that the Builder tools are installed and install what is missing.",
    "Choose reference TXT": "Choose a shorter reference text for this song.",
    "Clear result": "Clear the result and the log.",
    "Dark theme": "Switch between dark and light colours.",
    "Export Result + Log for Testing": "Save the result and the log into a file for testing.",
    "Load Marked Track": "Load a marked track with its shorter reference.",
    "Marked tracks": "Choose a track marked for transcription.",
    "Save Finished TXT for Sync": "Save the result as the song's lyrics TXT, ready for Lyrics Sync.",
    "Stop": "Stop the running transcription.",
    "BUILD LYRICS": "Transcribe the song and build the lyrics.",
    # ---- button groups, providers, backup types ----
    "Using the program": "Jump to the step-by-step part of this guide.",
    "Between computers": "Jump to moving lyrics between computers.",
    "Buttons explained": "Jump to the list of buttons and what they do.",
    "Installation and disk space": "Jump to installation and disk space.",
    "Multiple songs": "Jump to syncing many songs at once.",
    "Common mistakes": "Jump to the mistakes to check before saving.",
    "Metadata": "Show the buttons for song tags: name and title, Year from the comment.",
    "LRC missing bracket lines": "Find LRC files missing TXT lines that start and end with brackets.",
    "Mark selected for Lyrics Sync": "Back up and delete the LRC of the selected songs and queue them for Lyrics Sync.",
    "Open Lyrics Sync": "Open the Lyrics Sync window (Marked for Lyrics Sync lists the queued songs).",
    "Check again": "Check the LRC files again.",
    "Search for song duplicates": "List songs that are probably the same recording, with both file locations.",
    "Change Metadata": "Edit the selected song's file name, title, artist, album, year and other tags by hand.",
    "Set name same as Title": "Put the Title into the file name field (illegal characters removed).",
    "Remove brackets from Title": "Remove (…) and […] from the Title: “Swerve(JW3)” and “swerve (jw3)” become the bare name.",
    "Also rename each file to its new Title": "Rename every file to its new Title too; its TXT / LRC follow.",
    "Remove brackets from selected Titles": "Remove (…) and […] from the Title of every ticked song.",
    "Replace front cover…": "Pick a JPG / PNG; on Save it replaces the song's old front cover.",
    "Keep old cover": "Forget the picked picture; the song keeps its current front cover.",
    "Save Metadata": "Write the fields you changed into the song.",
    "Swap Song and Transfer Metadata": "Copy one song's tags, name, lyrics and cover onto another and swap their places.",
    "Use selected track": "Fill in the track selected in the main window (two selected: both songs).",
    "All metadata": "Tick everything: file name, all tags, embedded lyrics and front cover.",
    "None": "Untick everything.",
    "Transfer Metadata + Swap": "Copy the ticked metadata to the new song, then swap the two files' places.",
    "Apply to selected songs": "Write the changed boxes (everything except <keep>) into every selected song; a backup is made first.",
    "Remove brackets from Titles…": "Open the list of Titles with (…) or […] and remove the brackets from the ticked ones.",
    "Get synced lyrics from the shared sheet…": "Find the songs of Tracks in the shared SYNCED LYRICS DATABASE sheet and download + embed their LRC.",
    "Check again": "Read the sheet again and compare it with the songs.",
    "Download and embed": "Download the LRC of the selected rows into SYNCED LYRICS and embed it into the MP3s.",
    "Columns…": "Choose which tag columns are shown in Tracks. Clicking a column heading sorts by it.",
    "Default order": "Put every column back in its original order and width.",
    "Show all columns": "Tick every tag column again.",
    "File name": "Show or hide the File name column in Tracks.",
    "Track": "Show or hide the Track number column in Tracks.",
    "Year": "Show or hide the Year column in Tracks.",
    "Comment": "Show or hide the Comment column in Tracks.",
    "Import from Spreadsheet…": "Paste a Google Sheets link, find the selected songs and import dates, producer, location, artist.",
    "Load / refresh spreadsheet": "Download the spreadsheet from the link again (it is kept on disk, so this is only needed for new rows).",
    "Do the same for all remaining songs": "Import the same ticked columns into this and all next songs; songs not in the sheet are skipped.",
    "Import ticked fields into this song": "Write the ticked values into the song's tags (a backup is saved), then open the next song.",
    "Search": "Look for another name in the spreadsheet.",
    "Don't import": "This column is not written to the song.",
    "Year / date": "Write the date into the Year tag (a full date when the sheet has one).",
    "Artist": "Write the value into the Artist tag.",
    "Album artist": "Write the value into the Album artist tag.",
    "Album": "Write the value into the Album tag.",
    "Title": "Write the value into the Title tag.",
    "Genre": "Write the value into the Genre tag.",
    "Comment: (Studio: …)": "Add “(Studio: …)” to the comment.",
    "Comment: (Producer: …)": "Add “(Producer: …)” to the comment.",
    "Comment: add at the end": "Add the value at the end of the comment.",
    "Set Year from comment (Recorded …)": "Set the Year tag from “(Recorded … 2019)” in the MP3 comment.",
    "Open file location": "Open the folder of the selected file.",
    "Play selected file": "Play the selected file in your default player.",
    "Search again": "Search the same folders again.",
    "Set Year for selected songs": "Write the new Year into the selected songs (the old Year is saved).",
    "Undo last Year change": "Put back the Year the songs had before the last change.",
    "Download": "Show the download buttons.",
    "Track & lyrics": "Show the buttons for one track: play, edit lyrics, embed, fix sync.",
    "Transcription": "Show the buttons for transcription and shorter references.",
    "Repair library": "Show the library repair buttons: Edit and Fix, old names, backups.",
    "Cleanup & undo": "Show the delete buttons and Undo.",
    "Genius": "Use Genius as a lyrics source.",
    "Lrclib": "Use Lrclib as a source of synced lyrics.",
    "Megalobiz": "Use Megalobiz as a source of synced lyrics.",
    "Musixmatch": "Use Musixmatch as a lyrics source.",
    "NetEase": "Use NetEase as a source of synced lyrics.",
    "Audio (.mp3)": "Include the backed-up MP3 files.",
    "Lyrics text (.txt)": "Include the backed-up lyrics text files.",
    "Synced lyrics (.lrc)": "Include the backed-up synced lyrics files.",
    "Other (restore lists, other files)": "Include other backed-up files (restore lists and so on).",
    # ---- generic dialog buttons ----
    "Yes": "Yes, go ahead.",
    "No": "No, do not do it.",
    "OK": "Close this message.",
    "Retry": "Try again.",
    "Yes to All": "Yes, and do the same for every remaining item.",
    "Confirm": "Confirm and apply.",
    "Reject": "Reject this one.",
    "Save and Continue": "Save and go on to the next step.",
    "Apply selected matches": "Apply the selected matches (backed up first).",
    "Scan again": "Scan the library again.",
}

HINTS = {hint_key(k): v for k, v in _RAW.items()}
_PREFIXES = sorted((k for k in HINTS if len(k) >= 10), key=len, reverse=True)


METHODS = {  # the coloured automatic fixes in Fix fragment ("#1 Whisper, prompt ON" …)
    "whisper, prompt on": "Whisper listens with the lyrics as a hint; show this result as a coloured lane.",
    "whisper, prompt off": "Whisper listens without a hint (often truer for mumbled rap); choose this result.",
    "copy earlier repeat": "Copy the timing of an identical earlier part of the song (e.g. a chorus).",
    "copy later repeat": "Copy the timing of an identical later part of the song.",
}


def lookup(text):
    key = hint_key(text)
    if not key:
        return None
    method = re.match(r"^\d+ (.+)$", key)
    if method and method.group(1) in METHODS:
        return METHODS[method.group(1)]
    if key in HINTS:
        return HINTS[key]
    for prefix in _PREFIXES:  # texts with a changing tail ("Errors 12 songs", "Save and … + Update Embedded")
        if key.startswith(prefix):
            return HINTS[prefix]
    return None


def widget_text(widget):
    try:
        return str(widget.cget("text"))
    except tk.TclError:
        return ""


def describe(widget):
    """The explanation for a button: its entry, or the text of its own tooltip."""
    text = lookup(widget_text(widget))
    if text is None:
        own = getattr(widget, "_t9_tooltip_obj", None)
        if own is not None and getattr(own, "text", ""):
            text = str(own.text).replace("\n", " ")
            text = text if len(text) <= 150 else text[:147].rstrip() + "…"
    return text


def missing(window):
    """Buttons inside ``window`` that have no explanation (for the test that checks coverage)."""
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            try:
                kind = child.winfo_class()
            except tk.TclError:
                continue
            if kind in CLASSES and widget_text(child).strip() and describe(child) is None:
                found.append(widget_text(child).strip())
            walk(child)

    walk(window)
    return found


class Hints:
    def __init__(self, root, hint_var=None, theme_colors=None, delay=450):
        self.root, self.hint_var, self.delay = root, hint_var, delay
        self.theme_colors = theme_colors or (lambda: {"bg": "#2a2a2a", "fg": "#eeeeee"})
        self.job, self.popup = None, None

    def install(self):
        for name in CLASSES:
            self.root.bind_class(name, "<Enter>", self._enter, add="+")
            self.root.bind_class(name, "<Leave>", self._leave, add="+")
            self.root.bind_class(name, "<ButtonPress>", self._leave, add="+")
        return self

    def _in_main(self, widget):
        try:
            return widget.winfo_toplevel() is self.root
        except tk.TclError:
            return False

    def _enter(self, event):
        widget = event.widget
        text = describe(widget)
        if not text:
            return
        try:
            if str(widget.cget("state")) == "disabled":
                text += "  (not available right now)"
        except tk.TclError:
            pass
        if self._in_main(widget) and self.hint_var is not None:
            self.hint_var.set("ⓘ  " + text)
            return
        if getattr(widget, "_t9_tooltip_obj", None) is not None:
            return  # the widget shows its own, longer tooltip
        self._cancel()
        self.job = self.root.after(self.delay, lambda: self._show(widget, text))  # cancelled on root too

    def _leave(self, event):
        if self.hint_var is not None and self._in_main(event.widget):
            self.hint_var.set("")
        self._cancel()

    def _cancel(self):
        if self.job is not None:
            try:
                self.root.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.popup is not None:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None

    def _show(self, widget, text):
        self.job = None
        try:
            if not widget.winfo_exists():
                return
            colors = self.theme_colors()
            tip = tk.Toplevel(self.root)  # not a child of the button: closing its window must not touch it
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)  # always above the window it belongs to
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=text, justify="left", bg=colors.get("panel", colors["bg"]), fg=colors["fg"],
                     relief="solid", borderwidth=1, font=("Segoe UI", 9), padx=8, pady=4, wraplength=420).pack()
            self.popup = tip
        except tk.TclError:
            self.popup = None
