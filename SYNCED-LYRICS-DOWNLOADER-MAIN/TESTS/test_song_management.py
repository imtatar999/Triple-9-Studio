import ast
import os
import shutil
import re
import tempfile
import unittest
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from mutagen.id3 import ID3, TIT2, TPE1, USLT, SYLT, TXXX
from mutagen.flac import FLAC

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triplenine_reference_queue as reference_queue

SOURCE = Path(__file__).resolve().parents[1] / 'triplenine_lyrics_downloader.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


class SongManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.song = self.folder / 'Old.mp3'
        tags = ID3()
        tags.add(TIT2(encoding=3, text=['Old title']))
        tags.add(TPE1(encoding=3, text=['Keep artist']))
        tags.save(self.song)
        self.original = self.song.read_bytes()
        self.ns = {'Path': Path, 'os': os, 'shutil': shutil, 'datetime': datetime,
                   '__file__': str(self.folder / 'app.py'), 'BACKUP_ROOT': self.folder / 'BACKUPS',
                   'TRANSCRIPTION_TODO_DIR': self.folder / 'todo',
                   'reference_queue': reference_queue,
                   '_synced_lyrics_sources': lambda _song: (False, False), '_SYNCED_LYRICS_CACHE': {},
                   '_LRC_TIMESTAMP_RE': re.compile(r"(?m)^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")}
        names = {'_validate_song_name', '_write_audio_title', 'rename_song_and_title',
                 'refresh_tracks_from_disk', '_find_transcription_todo_dir',
                 'transcription_marker_path', '_is_marked_for_transcription',
                 '_track_display_status', 'create_transcription_markers', '_track_sort_key',
                 '_lyrics_files_for_song', 'mark_songs_for_transcription', 'remove_embedded_lyrics',
                 '_remove_timed_embedded_lyrics', 'delete_lyrics_for_selected', '_write_restore_map'}
        self.ns['json'] = __import__('json')
        exec(compile(ast.Module(body=[n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), '<functions>', 'exec'), self.ns)

    def test_remove_embedded_preserves_other_tags_audio_and_sidecars(self):
        tags = ID3(self.song)
        tags.add(USLT(encoding=3, lang='eng', desc='', text='Lyrics'))
        tags.add(USLT(encoding=3, lang='pol', desc='translation', text='Tekst'))
        tags.add(SYLT(encoding=3, lang='eng', format=2, type=1, text=[('Lyrics', 1000)]))
        tags.add(TXXX(encoding=3, desc='LYRICS', text=['Lyrics']))
        tags.add(TXXX(encoding=3, desc='Keep custom', text=['Value']))
        tags.save(self.song, v2_version=3)
        with self.song.open('ab') as stream:
            stream.write(b'audio-payload-preserved')
        sidecars = [self.song.with_suffix('.txt'), self.song.with_suffix('.lrc')]
        for path in sidecars:
            path.write_bytes(b'keep sidecar')
        self.assertTrue(self.ns['remove_embedded_lyrics'](str(self.song)))
        remaining = ID3(self.song)
        self.assertFalse(remaining.getall('USLT'))
        self.assertFalse(remaining.getall('SYLT'))
        self.assertNotIn('TXXX:LYRICS', remaining)
        self.assertEqual(str(remaining['TIT2']), 'Old title')
        self.assertEqual(str(remaining['TPE1']), 'Keep artist')
        self.assertEqual(str(remaining['TXXX:Keep custom']), 'Value')
        self.assertEqual(remaining.version[1], 3)
        self.assertTrue(self.song.read_bytes().endswith(b'audio-payload-preserved'))
        for path in sidecars:
            self.assertEqual(path.read_bytes(), b'keep sidecar')
        unchanged = self.song.read_bytes()
        self.assertFalse(self.ns['remove_embedded_lyrics'](str(self.song)))
        self.assertEqual(self.song.read_bytes(), unchanged)

    def test_delete_synced_only_keeps_plain_and_distinct_backups(self):
        txt = self.song.with_suffix('.txt')
        txt.write_text('keep plain')
        first = self.song.with_suffix('.lrc')
        folder = self.folder / 'LYRICS'
        folder.mkdir()
        second = folder / first.name
        first.write_text('[00:01.00]first')
        second.write_text('[00:02.00]second')
        self.ns.update(downloading=False, track_list=Mock(), messagebox=Mock(),
                       TRACK_ROW_PATHS={0: str(self.song)}, _last_deleted_lyrics=[],
                       _set_persistent_track_failed=Mock(), attempted_track_paths=set(),
                       _track_status_key=str, refresh_tracks_from_disk=Mock(),
                       log=Mock(), set_status=Mock(), __file__=str(self.folder / 'app' / 'main.py'),
                       BACKUP_ROOT=self.folder / 'BACKUPS')
        self.ns['track_list'].curselection.return_value = [0]
        self.ns['messagebox'].askyesno.return_value = True
        self.ns['delete_lyrics_for_selected'](synced_only=True)
        self.assertEqual(txt.read_text(), 'keep plain')
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        copies = [item[2] for item in self.ns['_last_deleted_lyrics']]
        self.assertEqual({p.read_text() for p in copies}, {'[00:01.00]first', '[00:02.00]second'})
        self.assertTrue(self.song.exists())

    def test_remove_timed_embedding_keeps_plain_lyrics(self):
        tags = ID3(self.song)
        tags.add(USLT(encoding=3, lang='eng', desc='plain', text='Plain lyrics stay'))
        tags.add(USLT(encoding=3, lang='eng', desc='timed', text='[00:01.00]Timed lyrics'))
        tags.add(TXXX(encoding=3, desc='LYRICS', text=['[00:02.00]Timed custom lyrics']))
        tags.add(TXXX(encoding=3, desc='Keep custom', text=['Value']))
        tags.save(self.song)

        self.assertTrue(self.ns['_remove_timed_embedded_lyrics'](self.song))

        remaining = ID3(self.song)
        self.assertEqual(str(remaining['USLT:plain:eng']), 'Plain lyrics stay')
        self.assertNotIn('USLT:timed:eng', remaining)
        self.assertNotIn('TXXX:LYRICS', remaining)
        self.assertEqual(str(remaining['TXXX:Keep custom']), 'Value')

    def test_mp3_title_sidecars_and_backup(self):
        (self.folder / 'LYRICS').mkdir()
        (self.folder / 'todo').mkdir()
        old_files = [self.folder / 'Old.txt', self.folder / 'LYRICS' / 'Old.lrc', self.folder / 'todo' / 'Old.txt']
        for path in old_files:
            path.write_text('Keep lyrics', encoding='utf-8')
        new, backup = self.ns['rename_song_and_title'](str(self.song), 'Nowy żółty (Sessions)')
        self.assertFalse(self.song.exists())
        self.assertEqual(Path(backup).read_bytes(), self.original)
        self.assertEqual(str(ID3(new)['TIT2']), 'Nowy żółty (Sessions)')
        self.assertEqual(str(ID3(new)['TPE1']), 'Keep artist')
        for old in old_files:
            self.assertFalse(old.exists())
            self.assertEqual(old.with_name('Nowy żółty (Sessions)' + old.suffix).read_text(), 'Keep lyrics')

    def test_rename_preserves_reference_status_suffix(self):
        marker_dir = self.folder / 'todo'
        marker_dir.mkdir()
        marker = marker_dir / ('Old' + reference_queue.FOUND_SUFFIX + '.txt')
        marker.write_text('Short reference', encoding='utf-8')
        self.ns['rename_song_and_title'](str(self.song), 'New title')
        renamed = marker_dir / ('New title' + reference_queue.FOUND_SUFFIX + '.txt')
        self.assertFalse(marker.exists())
        self.assertEqual(renamed.read_text(encoding='utf-8'), 'Short reference')

    def test_same_filename_updates_title(self):
        new, _ = self.ns['rename_song_and_title'](str(self.song), 'Old')
        self.assertEqual(str(ID3(new)['TIT2']), 'Old')

    def test_flac_title_and_artist(self):
        song = self.folder / 'Old.flac'
        streaminfo = bytearray(34)
        streaminfo[0:2] = (16).to_bytes(2, 'big')
        streaminfo[2:4] = (16).to_bytes(2, 'big')
        streaminfo[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36) | 100).to_bytes(8, 'big')
        song.write_bytes(b'fLaC\x80\x00\x00\x22' + streaminfo)
        tags = FLAC(song)
        tags['artist'] = ['Keep artist']
        tags.save()
        new, _ = self.ns['rename_song_and_title'](str(song), 'New FLAC')
        self.assertEqual(FLAC(new)['title'], ['New FLAC'])
        self.assertEqual(FLAC(new)['artist'], ['Keep artist'])

    def test_collision_does_not_modify_anything(self):
        (self.folder / 'New.mp3').write_bytes(b'keep destination')
        with self.assertRaises(FileExistsError):
            self.ns['rename_song_and_title'](str(self.song), 'New')
        self.assertEqual(self.song.read_bytes(), self.original)
        self.assertEqual((self.folder / 'New.mp3').read_bytes(), b'keep destination')

    def test_sidecar_collision_does_not_modify_audio(self):
        (self.folder / 'Old.txt').touch()
        (self.folder / 'New.txt').touch()
        with self.assertRaises(FileExistsError):
            self.ns['rename_song_and_title'](str(self.song), 'New')
        self.assertEqual(self.song.read_bytes(), self.original)

    def test_rollback_when_sidecar_rename_fails(self):
        old_text = self.folder / 'Old.txt'
        old_text.write_text('keep')
        proxy = Mock(wraps=os)
        proxy.path = os.path
        def rename(old, new):
            if Path(old) == old_text:
                raise PermissionError('File locked')
            return os.rename(old, new)
        proxy.rename.side_effect = rename
        self.ns['os'] = proxy
        with self.assertRaises(PermissionError):
            self.ns['rename_song_and_title'](str(self.song), 'New')
        self.assertEqual(self.song.read_bytes(), self.original)
        self.assertFalse((self.folder / 'New.mp3').exists())
        self.assertEqual(old_text.read_text(), 'keep')

    def test_invalid_names(self):
        for name in ('', '../outside', 'bad:name', 'CON', 'LPT1.txt', 'end.', 'Song.mp3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ns['_validate_song_name'](name)

    def test_spaces_inside_title_are_allowed_and_edges_are_trimmed(self):
        validate = self.ns['_validate_song_name']
        self.assertEqual(validate('Song (SSSS) (XDXD)'), 'Song (SSSS) (XDXD)')
        self.assertEqual(validate('  Song (SSSS) (XDXD)  '), 'Song (SSSS) (XDXD)')

    def test_refresh_preserves_selection_and_album_filter(self):
        ns = self.ns
        ns.update(downloading=False, TRACK_ROW_PATHS={2: str(self.song)},
                  _track_status_key=lambda p: p.lower(), track_list=Mock(), artist_list=Mock(), album_list=Mock(),
                  strip_icon=lambda s:s, _selected_album_dirs=lambda:['Album'], lyrics_cache={'stale':1},
                  refresh_artist_list=Mock(), get_selected_artist_name=lambda:'Artist',
                  _populate_albums_for_artist=Mock(), ALBUM_ROW_DIRS={0:'Album'}, _invalidate_view_status=Mock(),
                  on_track_select=Mock(), missing_scan_btn=Mock(), tk=Mock())
        ns['track_list'].curselection.return_value = (2,)
        ns['artist_list'].curselection.return_value = (0,)
        ns['artist_list'].get.return_value = 'Artist'
        ns['artist_list'].size.return_value = 1
        ns['refresh_tracks_for_current_artist'] = lambda: ns.update(TRACK_ROW_PATHS={5: str(self.song)})
        ns['refresh_tracks_from_disk']()
        self.assertFalse(ns['lyrics_cache'])
        ns['track_list'].selection_set.assert_called_once_with(5)
        ns['album_list'].selection_set.assert_called_once_with(0)

    def test_existing_project_marker_folder_is_preferred(self):
        script_dir = self.folder / 'downloader'
        script_dir.mkdir()
        project_markers = self.folder / 'MARKED FOR TRANSCRIPTION'
        project_markers.mkdir()
        old_file = self.ns['__file__']
        self.ns['__file__'] = str(script_dir / 'app.py')
        try:
            self.assertEqual(self.ns['_find_transcription_todo_dir'](), project_markers)
        finally:
            self.ns['__file__'] = old_file

    def test_missing_project_marker_folder_defaults_beside_script(self):
        script_dir = self.folder / 'downloader'
        script_dir.mkdir()
        old_file = self.ns['__file__']
        self.ns['__file__'] = str(script_dir / 'app.py')
        try:
            self.assertEqual(self.ns['_find_transcription_todo_dir'](),
                             script_dir / 'MARKED FOR TRANSCRIPTION')
        finally:
            self.ns['__file__'] = old_file

    def test_marker_persists_on_disk_and_overrides_failed_icon(self):
        marker_dir = self.folder / 'todo'
        self.ns['TRANSCRIPTION_TODO_DIR'] = marker_dir
        created, existing, errors = self.ns['create_transcription_markers'](
            [str(self.song)], marker_dir)
        self.assertEqual(len(created), 1)
        self.assertFalse(existing)
        self.assertFalse(errors)
        self.assertTrue(self.ns['_is_marked_for_transcription'](self.song))
        self.assertEqual(self.ns['_track_display_status'](self.song, '', True, True),
                         ('📝❌ ', 'transcription'))

    def test_reference_icon_reads_filename_without_queue_cache(self):
        marker_dir = self.ns['TRANSCRIPTION_TODO_DIR']
        marker_dir.mkdir()
        found = marker_dir / ('Old' + reference_queue.FOUND_SUFFIX + '.txt')
        found.write_text('Shorter reference', encoding='utf-8')
        status = self.ns['_track_display_status']
        self.assertEqual(status(self.song, '', True), ('📝✅ ', 'transcription'))
        missing = marker_dir / ('Old' + reference_queue.MISSING_SUFFIX + '.txt')
        found.rename(missing)
        self.assertEqual(status(self.song, '', True), ('📝❌ ', 'transcription'))
        missing.rename(marker_dir / 'Old.txt')
        self.assertEqual(status(self.song, '', False), ('📝❌ ', 'transcription'))

    def test_downloaded_icon_has_priority_over_marker(self):
        marker_dir = self.folder / 'todo'
        self.ns['TRANSCRIPTION_TODO_DIR'] = marker_dir
        self.ns['create_transcription_markers']([str(self.song)], marker_dir)
        self.assertEqual(self.ns['_track_display_status'](self.song, 'Old.txt', True),
                         ('✅ ', 'downloaded'))

    def test_synced_icon_has_priority_over_downloaded_and_marked(self):
        self.ns['_synced_lyrics_sources'] = lambda _song: (True, False)
        self.assertEqual(self.ns['_track_display_status'](self.song, 'Old.txt', True),
                         ('🎶 ', 'synced'))

    def test_lrc_only_has_its_own_status(self):
        self.ns['_synced_lyrics_sources'] = lambda _song: (False, True)
        self.assertEqual(self.ns['_track_display_status'](self.song, 'Old.txt', False),
                         ('📄 ', 'lrc'))

    def test_detects_timestamped_sidecar_and_embedded_mp3_lyrics(self):
        detect_ns = {'Path': Path, 'os': os, 're': re, 'time': __import__('time'),
                     'studio_resync': __import__('studio_resync')}
        wanted = {'_SYNCED_LYRICS_CACHE', '_LRC_TIMESTAMP_RE'}
        nodes = [node for node in TREE.body
                 if ((isinstance(node, (ast.Assign, ast.AnnAssign)) and
                      any(isinstance(target, ast.Name) and target.id in wanted
                          for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)))
                     or (isinstance(node, ast.FunctionDef) and node.name in {'_synced_lyrics_sources', '_has_synced_lyrics'}))]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<sync-detection>', 'exec'), detect_ns)
        detect = detect_ns['_has_synced_lyrics']
        self.assertFalse(detect(self.song))

        synced_folder = self.folder / 'SYNCED LYRICS'
        synced_folder.mkdir()
        lrc = synced_folder / 'Old.lrc'
        lrc.write_text('[00:01.20]First line\n', encoding='utf-8')
        self.assertTrue(detect(self.song))
        self.assertEqual(detect_ns['_synced_lyrics_sources'](self.song), (False, True))
        lrc.unlink()

        tags = ID3(self.song)
        tags.add(USLT(encoding=3, lang='eng', desc='', text='embedded lyrics'))
        tags.save(self.song)
        self.assertFalse(detect(self.song))
        tags.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.20]Embedded line'))
        tags.save(self.song)
        self.assertTrue(detect(self.song))
        self.assertEqual(detect_ns['_synced_lyrics_sources'](self.song), (True, False))
        self.ns['remove_embedded_lyrics'](str(self.song))
        self.assertFalse(detect(self.song, force=True))
        lrc.write_text('[00:01.20]Sidecar remains\n', encoding='utf-8')
        self.assertTrue(detect(self.song, force=True))
        lrc.unlink()
        self.assertFalse(detect(self.song))

    def test_existing_marker_is_not_overwritten(self):
        marker_dir = self.folder / 'todo'
        marker_dir.mkdir()
        marker = marker_dir / 'Old.txt'
        marker.write_text('keep notes', encoding='utf-8')
        created, existing, errors = self.ns['create_transcription_markers'](
            [str(self.song)], marker_dir)
        self.assertFalse(created)
        self.assertEqual(existing, [str(marker)])
        self.assertFalse(errors)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep notes')

    def test_marking_downloaded_song_removes_exact_lyrics_after_backup(self):
        marker_dir = self.folder / 'todo'
        lyrics_dir = self.folder / 'LYRICS'
        lyrics_dir.mkdir()
        synced_dir = self.folder / 'SYNCED LYRICS'
        synced_dir.mkdir()
        lyrics_files = [self.folder / 'Old.txt', self.folder / 'Old.lrc',
                        lyrics_dir / 'Old.txt', lyrics_dir / 'Old.lrc',
                        synced_dir / 'Old.txt', synced_dir / 'Old.lrc']
        for index, path in enumerate(lyrics_files):
            path.write_text(f'lyrics {index}', encoding='utf-8')
        unrelated = lyrics_dir / 'Another song.txt'
        unrelated.write_text('keep', encoding='utf-8')
        created, existing, removed, backup_dirs, errors = self.ns['mark_songs_for_transcription'](
            [str(self.song)], marker_dir)
        self.assertEqual(created, [str(marker_dir / 'Old - reference not found.txt')])
        self.assertFalse(existing)
        self.assertFalse(errors)
        self.assertEqual(set(removed), {str(path) for path in lyrics_files})
        self.assertTrue(all(not path.exists() for path in lyrics_files))
        self.assertEqual(unrelated.read_text(encoding='utf-8'), 'keep')
        backups = list(Path(backup_dirs[0]).iterdir())
        self.assertEqual(len(backups), 6)
        self.assertEqual({path.read_text(encoding='utf-8') for path in backups},
                         {f'lyrics {index}' for index in range(6)})

    def test_existing_marker_still_removes_downloaded_lyrics(self):
        marker_dir = self.folder / 'todo'
        marker_dir.mkdir()
        marker = marker_dir / 'Old.txt'
        marker.write_text('keep marker', encoding='utf-8')
        lyrics = self.folder / 'Old.txt'
        lyrics.write_text('downloaded lyrics', encoding='utf-8')
        created, existing, removed, _backups, errors = self.ns['mark_songs_for_transcription'](
            [str(self.song)], marker_dir)
        self.assertFalse(created)
        self.assertEqual(existing, [str(marker)])
        self.assertEqual(removed, [str(lyrics)])
        self.assertFalse(errors)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep marker')

    def test_backup_failure_keeps_lyrics_and_does_not_leave_new_marker(self):
        marker_dir = self.folder / 'todo'
        lyrics = self.folder / 'Old.txt'
        lyrics.write_text('downloaded lyrics', encoding='utf-8')
        original_copy = shutil.copy2
        proxy = Mock(wraps=shutil)
        proxy.copy2.side_effect = OSError('backup unavailable')
        self.ns['shutil'] = proxy
        try:
            created, existing, removed, _backups, errors = self.ns['mark_songs_for_transcription'](
                [str(self.song)], marker_dir)
        finally:
            self.ns['shutil'] = shutil
        self.assertFalse(created or existing or removed)
        self.assertTrue(errors)
        self.assertEqual(lyrics.read_text(encoding='utf-8'), 'downloaded lyrics')
        self.assertFalse((marker_dir / 'Old.txt').exists())

    def test_failed_first_status_order(self):
        rows = [
            ('D.mp3', '', '', 'downloaded', ''),
            ('U.mp3', '', '', 'untouched', ''),
            ('M.mp3', '', '', 'transcription', ''),
            ('F.mp3', '', '', 'failed', ''),
        ]
        rows.sort(key=lambda row: self.ns['_track_sort_key'](row, 'Failed first'))
        self.assertEqual([row[3] for row in rows],
                         ['failed', 'transcription', 'downloaded', 'untouched'])

    def test_lyrics_synced_first_status_order(self):
        rows = [
            ('Downloaded.mp3', '', '✅ ', 'downloaded', ''),
            ('Untouched.mp3', '', '', 'untouched', ''),
            ('Synced B.mp3', '', '🎶 ', 'synced', ''),
            ('Failed.mp3', '', '❌ ', 'failed', ''),
            ('Synced A.mp3', '', '🎶 ', 'synced', ''),
        ]
        rows.sort(key=lambda row: self.ns['_track_sort_key'](row, 'Lyrics Synced First'))
        self.assertEqual([row[0] for row in rows[:2]], ['Synced A.mp3', 'Synced B.mp3'])
        self.assertTrue(all(row[3] != 'synced' for row in rows[2:]))

    def test_downloaded_first_excludes_synced_and_timed_lrc(self):
        rows = [
            ('Synced.mp3', '', '🎶 ', 'synced', ''),
            ('Plain B.mp3', '', '✅ ', 'downloaded', ''),
            ('Timed LRC.mp3', '', '📄 ', 'lrc', ''),
            ('Untouched.mp3', '', '', 'untouched', ''),
            ('Plain A.mp3', '', '✅ ', 'downloaded', ''),
        ]
        rows.sort(key=lambda row: self.ns['_track_sort_key'](row, 'Downloaded first'))
        self.assertEqual([row[0] for row in rows[:2]], ['Plain A.mp3', 'Plain B.mp3'])
        self.assertTrue(all(row[3] == 'downloaded' for row in rows[:2]))
        self.assertTrue(all(row[3] != 'downloaded' for row in rows[2:]))

    def test_marked_first_status_order(self):
        rows = [
            ('D.mp3', '', '', 'downloaded', ''),
            ('U.mp3', '', '', 'untouched', ''),
            ('F.mp3', '', '', 'failed', ''),
            ('A missing.mp3', '', '📝❌ ', 'transcription', ''),
            ('Z ready.mp3', '', '📝✅ ', 'transcription', ''),
        ]
        rows.sort(key=lambda row: self.ns['_track_sort_key'](row, 'Marked first'))
        self.assertEqual([row[0] for row in rows],
                         ['Z ready.mp3', 'A missing.mp3', 'F.mp3', 'D.mp3', 'U.mp3'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
