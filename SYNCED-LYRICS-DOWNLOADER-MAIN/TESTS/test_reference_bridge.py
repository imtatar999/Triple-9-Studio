import ast
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
sys.path.insert(0, str(ROOT / "MAINTENANCE"))
import triplenine_reference_queue as bridge


class ReferenceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(bridge, "ROOT", self.folder).start()
        patch.object(bridge, "QUEUE", self.folder / "MARKED FOR TRANSCRIPTION" / ".REFERENCES").start()
        self.audio = self.folder / "Another Way (Sessions).mp3"
        self.audio.write_bytes(b"original audio")
        self.marker = self.folder / "MARKED FOR TRANSCRIPTION" / (self.audio.stem + ".txt")
        self.marker.parent.mkdir()
        self.marker.touch()
        self.reference = self.folder / "Another Way.txt"
        self.reference.write_text("[Chorus]\nReference lyrics\n\nhttps://genius.com/example-lyrics\n")

    def test_base_title_preserves_aliases(self):
        for edition in ("Sessions", "Session Edit", "Sessions Edit", "Extended"):
            self.assertEqual(bridge.base_title(f"Another Way (Alias) ({edition})"), "Another Way (Alias)")

    def test_delete_reference_preserves_library_and_clears_cache(self):
        entry = bridge.read_entry(self.audio)
        original = self.reference.read_bytes()
        found = bridge.assign_reference(entry, self.reference, "local")
        cached = bridge.entry_path(self.audio).with_suffix(".txt")
        cached.write_text("Cached reference")
        lyrics = bridge.completed_lyrics_path(self.audio)
        lyrics.parent.mkdir()
        lyrics.write_text("Finished text")
        backup = bridge.delete_reference(self.audio)
        self.assertTrue((backup / found.name).is_file())
        self.assertTrue((backup / cached.name).is_file())
        self.assertFalse(found.exists())
        self.assertFalse(cached.exists())
        self.assertFalse(bridge.ready(bridge.read_entry(self.audio)))
        self.assertEqual(bridge.existing_marker(self.audio), bridge.marker_paths(self.audio)[1])
        self.assertEqual(self.audio.read_bytes(), b"original audio")
        self.assertEqual(self.reference.read_bytes(), original)
        self.assertEqual(lyrics.read_text(), "Finished text")

    def test_delete_reference_keeps_done_and_unmarked_tracks(self):
        entry = bridge.read_entry(self.audio)
        bridge.assign_reference(entry, self.reference, "local")
        done = bridge.mark_ready_for_sync(entry)
        original = done.read_bytes()
        self.assertIsNone(bridge.delete_reference(self.audio))
        self.assertEqual(done.read_bytes(), original)
        self.assertIsNone(bridge.delete_reference(self.folder / "Unmarked.mp3"))

    def test_delete_reference_rolls_back_on_cache_write_error(self):
        entry = bridge.read_entry(self.audio)
        found = bridge.assign_reference(entry, self.reference, "local")
        original = found.read_bytes()
        with patch.object(bridge, "write_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                bridge.delete_reference(self.audio)
        self.assertEqual(found.read_bytes(), original)
        self.assertTrue(bridge.ready(bridge.read_entry(self.audio)))
        self.assertFalse(bridge.marker_paths(self.audio)[1].exists())

    def test_marker_status_follows_song_name(self):
        found, missing, legacy = bridge.marker_paths(self.audio)
        self.assertEqual(found.name, "Another Way (Sessions) - reference found.txt")
        self.assertEqual(missing.name, "Another Way (Sessions) - reference not found.txt")
        self.assertEqual(legacy.name, "Another Way (Sessions).txt")
        self.assertEqual(bridge.ready_for_sync_marker_path(self.audio).name,
                         "Another Way (Sessions) - Ready for Lyric Sync.txt")

    def test_mark_ready_for_sync_renames_only_marker(self):
        lyrics = self.folder / "LYRICS" / (self.audio.stem + ".txt")
        lyrics.parent.mkdir()
        lyrics.write_text("Finished transcription", encoding="utf-8")
        entry = {"audio": str(self.audio), "marker": str(self.marker)}
        found = bridge.assign_reference(entry, self.reference, "test")
        marker_text = found.read_text(encoding="utf-8-sig")
        audio_bytes = self.audio.read_bytes()
        lyrics_text = lyrics.read_text(encoding="utf-8")
        done = bridge.mark_ready_for_sync(entry)
        self.assertEqual(done.name, "Another Way (Sessions) - Ready for Lyric Sync.txt")
        self.assertFalse(found.exists())
        self.assertEqual(done.read_text(encoding="utf-8-sig"), marker_text)
        self.assertEqual(self.audio.read_bytes(), audio_bytes)
        self.assertEqual(lyrics.read_text(encoding="utf-8"), lyrics_text)
        self.assertTrue(bridge.is_ready_for_sync(bridge.read_entry(self.audio)))

    def test_existing_finished_lyrics_are_reconciled_to_done(self):
        entry = {"audio": str(self.audio), "marker": str(self.marker)}
        found = bridge.assign_reference(entry, self.reference, "test")
        lyrics = bridge.completed_lyrics_path(self.audio)
        lyrics.parent.mkdir()
        lyrics.write_text("Finished transcription", encoding="utf-8")
        audio_bytes = self.audio.read_bytes()
        lyrics_bytes = lyrics.read_bytes()
        changed = bridge.reconcile_completed()
        self.assertEqual(changed, [str(bridge.ready_for_sync_marker_path(self.audio))])
        self.assertFalse(found.exists())
        self.assertTrue(bridge.ready_for_sync_marker_path(self.audio).is_file())
        self.assertEqual(self.audio.read_bytes(), audio_bytes)
        self.assertEqual(lyrics.read_bytes(), lyrics_bytes)

    def test_missing_reference_can_be_marked_done(self):
        missing = bridge.ensure_marker(self.audio)
        entry = bridge.read_entry(self.audio)
        done = bridge.mark_ready_for_sync(entry)
        self.assertFalse(missing.exists())
        self.assertEqual(done.read_bytes(), b"")
        self.assertTrue(bridge.is_ready_for_sync(bridge.read_entry(self.audio)))
        self.assertEqual(self.audio.read_bytes(), b"original audio")

    def test_missing_reference_finished_lyrics_reconciles_once(self):
        missing = bridge.ensure_marker(self.audio)
        bridge.write_entry(bridge.read_entry(self.audio))
        lyrics = bridge.completed_lyrics_path(self.audio)
        lyrics.parent.mkdir()
        lyrics.write_text("Finished transcription", encoding="utf-8")
        self.assertEqual(bridge.reconcile_completed(), [str(bridge.ready_for_sync_marker_path(self.audio))])
        self.assertFalse(missing.exists())
        self.assertEqual(lyrics.read_text(), "Finished transcription")
        self.assertEqual(bridge.reconcile_completed(), [])

    def test_empty_lyrics_does_not_mark_ready_for_sync(self):
        entry = {"audio": str(self.audio), "marker": str(self.marker)}
        found = bridge.assign_reference(entry, self.reference, "test")
        lyrics = bridge.completed_lyrics_path(self.audio)
        lyrics.parent.mkdir()
        lyrics.touch()
        self.assertEqual(bridge.reconcile_completed(), [])
        self.assertTrue(found.is_file())

    def test_queue_readiness_and_marker_lifecycle(self):
        entry = {"audio": str(self.audio), "marker": str(self.marker), "reference": str(self.reference)}
        bridge.write_entry(entry)
        self.assertTrue(bridge.ready(bridge.read_entry(self.audio)))
        self.assertEqual(len(bridge.entries()), 1)
        self.assertNotIn("genius.com", bridge.reference_text(self.reference))
        self.reference.unlink()
        self.assertFalse(bridge.ready(entry))
        self.marker.unlink()
        self.assertEqual(bridge.entries(), [])

    def test_old_markers_discovered_from_library_without_writing_queue(self):
        config = self.folder / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "lyrics_gui_config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"music_dir": str(self.folder)}))
        self.assertEqual(len(bridge.entries()), 1)
        self.assertFalse(bridge.QUEUE.exists())

    def worker_namespace(self):
        source = (ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "triplenine_lyrics_downloader.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_shorter_references")
        ns = dict(reference_queue=bridge, Path=Path, downloading=False, require_genius_token=lambda: True,
                  TRANSCRIPTION_TODO_DIR=self.marker.parent,
                  cancel_btn=Mock(), pause_btn=Mock(), should_cancel=lambda: False,
                  transcription_marker_path=lambda song: self.marker,
                  get_track_metadata=lambda song: (self.audio.stem, "Artist"),
                  get_artist_candidates=lambda *args: ["Artist"],
                  _strip_artist_prefix_from_title=lambda title, artists: title,
                  _local_shorter_reference=Mock(return_value=self.reference),
                  _genius_find_verified_song=Mock(), _save_genius_result=Mock(),
                  _genius_review_state=SimpleNamespace(), GENIUS_REVIEW_THRESHOLD=85,
                  _ask_genius_review=Mock(return_value={"action": "skip"}),
                  log=Mock(), set_status=Mock(), refresh_tracks_from_disk=Mock(), ui_call=lambda f, *a: f(*a),
                  threading=SimpleNamespace(Thread=lambda target, **kw: SimpleNamespace(start=target)))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "worker", "exec"), ns)
        return ns

    def test_local_then_cache_never_search_or_write_song_lyrics(self):
        ns = self.worker_namespace()
        ns["start_shorter_references"]([str(self.audio)])
        ns["_local_shorter_reference"].reset_mock()
        ns["start_shorter_references"]([str(self.audio)])
        ns["_local_shorter_reference"].assert_not_called()
        ns["_genius_find_verified_song"].assert_not_called()
        ns["_save_genius_result"].assert_not_called()
        self.assertEqual(self.audio.read_bytes(), b"original audio")
        self.assertFalse(self.audio.with_suffix(".txt").exists())
        marker = bridge.existing_marker(self.audio)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.name, "Another Way (Sessions) - reference found.txt")
        self.assertIn("Reference lyrics", marker.read_text(encoding="utf-8-sig"))

    def test_uncertain_match_skip_keeps_missing_reference(self):
        ns = self.worker_namespace()
        ns["_local_shorter_reference"].return_value = None
        ns["_genius_find_verified_song"].return_value = {"url": "https://genius.com/test-lyrics", "_match_score_percent": 45}
        ns["start_shorter_references"]([str(self.audio)])
        ns["_save_genius_result"].assert_not_called()
        self.assertFalse(bridge.ready(bridge.read_entry(self.audio)))
        marker = bridge.existing_marker(self.audio)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.name, "Another Way (Sessions) - reference not found.txt")

    def test_manual_txt_assigns_reference_only(self):
        ns = self.worker_namespace()
        ns["_local_shorter_reference"].return_value = None
        ns["_genius_find_verified_song"].return_value = None
        ns["_ask_genius_review"].return_value = {"action": "local", "url": str(self.reference)}
        ns["start_shorter_references"]([str(self.audio)])
        self.assertTrue(bridge.ready(bridge.read_entry(self.audio)))
        self.assertEqual(bridge.existing_marker(self.audio).name,
                         "Another Way (Sessions) - reference found.txt")
        ns["_save_genius_result"].assert_not_called()
        self.assertFalse(self.audio.with_suffix(".txt").exists())


if __name__ == "__main__":
    unittest.main()
