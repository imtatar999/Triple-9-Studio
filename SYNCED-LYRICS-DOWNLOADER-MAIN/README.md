# T9 lyrics toolkit

Launch `Triplenine_Lyrics_Studio.py` from the project folder or use `Start T9 Downloader.cmd` in this folder.

- **T9 Downloader** finds existing lyrics and manages the music/track selection.
- **T9 Lyric Builder** creates or completes text for ordinary and extended recordings with the fixed **TripleNine Engine** (Xingyu + Whisper), with or without reference lyrics.
- **T9 Lyrics Sync** aligns reviewed text to audio and writes timed LRC files.

Open **Help > How To Use** for the complete English instructions, folder examples, step-by-step workflows and button reference. Each tab has three section shortcuts.

Reviewed Builder text is saved to **LYRICS** with the same base filename as the audio. A marked track then becomes **Ready for Lyric Sync**. This is the end of text preparation, not the end of synchronization. Sync output goes to **SYNCED LYRICS** by default.

Program-created project/output folders use uppercase names. Keep **MARKED FOR TRANSCRIPTION** and its **.REFERENCES** metadata in place. Existing music paths remain valid on Windows after the case-only folder migration. The user's JUICE WRLD folder spelling was retained on request.

Developer mode is saved in Options and controls visibility of technical fields, the Builder diagnostic log and testing export. Testing exports are written to **TESTING_EXPORTS**. The engine is fixed even in developer mode.

Implementation filenames and the historical user-profile storage key are retained for compatibility. Application window titles and navigation use the T9 names.

Tests: `python -m unittest discover -s TESTS` from this directory. GUI validation scripts live in **MAINTENANCE** one level above it.
