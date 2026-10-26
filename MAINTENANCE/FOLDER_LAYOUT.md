# Folder layout

- `Triplenine_Lyrics_Studio.py`: retained main launcher for **T9 Downloader**.
- `SYNCED-LYRICS-DOWNLOADER-MAIN`: active application modules, assets and `TESTS`.
- `MAINTENANCE`: GUI validation and replay tools.
- `PRIVATE`: retained local token note; configured credentials are managed separately through Options.
- `BACKUPS`: recovery copies, including `T9_UPDATE_BEFORE/STATUS_MIGRATION` for the status rename.
- `BUILDER_REPLAY` and `COLOR-RESEARCH`: retained test/research data.
- `MARKED FOR TRANSCRIPTION/.REFERENCES`: shared track/reference metadata.
- `LYRICS`: plain text beside each recording's folder, created when saving or downloading with the folder option.
- `SYNCED LYRICS`: default timed LRC output beside the audio.
- `TESTING_EXPORTS` inside the application folder: Builder diagnostic exports.

The two existing `Juice WRLD` directories retain their spelling at the user's request. Python-managed `__pycache__` directories keep Python's standard name.

The visible completion status is **Ready for Lyric Sync**. Existing ` - done.txt` markers were renamed without changing their contents. Legacy status recognition remains for compatibility, while all new saved states use the new name.

English instructions: **T9 Downloader > Help > How To Use**.
