import ast
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock

SOURCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_DIR))
SOURCE = (SOURCE_DIR / 'triplenine_lyrics_downloader.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NAMES = {'_pending_genius_reviews', '_has_pending_genius_review', '_validated_genius_url',
         '_save_genius_result', '_run_genius_strict', 'run_provider', '_review_pending_genius',
         'missing_txt_path', 'collect_missing_txt', 'download_missing_queue', '_ask_genius_review',
         'download_selected', '_current_lyrics_source', '_replace_track_lyrics',
         '_save_edited_lyrics', '_artist_folder', '_fetch_genius_lyrics'}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.song = str(self.folder / 'Exact audio name.mp3')
        Path(self.song).touch()
        self.out = str(self.folder / 'LYRICS' / 'Exact audio name.txt')
        self.result = {'url': 'https://genius.com/Juice-wrld-test-lyrics', 'title': 'Test',
                       '_match_score_percent': 84}
        ns = dict(os=os, threading=threading, urllib=urllib, Path=Path,
                  __file__=str(self.folder / 'triplenine_lyrics_downloader.py'),
                  BACKUP_ROOT=self.folder / 'BACKUPS',
                  MUSIC_DIR=str(self.folder), ROOT_ARTIST_LABEL=None,
                  GENIUS_REVIEW_THRESHOLD=85, _genius_review_state=threading.local(),
                  config={'strip_cjk': False}, LAST_GENIUS_MATCH_PERCENT=None,
                  log=Mock(), should_cancel=lambda: False, get_artist_candidates=lambda *a: ['Juice WRLD'],
                  _genius_find_verified_song=lambda *a: dict(self.result),
                  _get_cached_genius_lyrics=Mock(return_value='Example lyric line\n' * 8),
                  _store_cached_genius_lyrics=Mock(), _http_get=Mock(),
                  reject_if_mostly_non_ascii=lambda p: False, ui_call=Mock(),
                  pause_btn=Mock(), set_status=Mock(), attempted_track_paths=set(),
                  _track_status_key=lambda s: s, _set_persistent_track_failed=Mock())
        exec(compile(ast.Module(body=[n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in NAMES], type_ignores=[]), '<functions>', 'exec'), ns)
        self.ns = ns

    def defer(self):
        return self.ns['run_provider']('', 'Genius', self.out, '', False, 'Test', 'Juice WRLD', self.song)

    def test_84_deferred_even_cached_and_existing_file_untouched(self):
        Path(self.out).parent.mkdir()
        Path(self.out).write_text('existing lyrics')
        self.assertFalse(self.defer())
        self.assertEqual(Path(self.out).read_text(), 'existing lyrics')
        self.ns['_get_cached_genius_lyrics'].assert_not_called()
        self.ns['_http_get'].assert_not_called()
        self.assertTrue(self.ns['_has_pending_genius_review'](self.song))

    def test_85_downloads_automatically(self):
        self.result['_match_score_percent'] = 85
        self.assertTrue(self.defer())
        self.assertTrue(Path(self.out).is_file())
        self.assertFalse(self.ns['_pending_genius_reviews']())

    def test_confirm_saves_exact_txt_with_original_score(self):
        self.defer()
        self.ns['_ask_genius_review'] = lambda *a: {'action': 'confirm'}
        self.assertEqual(self.ns['_review_pending_genius'](), (1, 0))
        self.assertIn('Match accuracy: 84%', Path(self.out).read_text())
        self.assertIn('Source confirmed by user', Path(self.out).read_text())

    def test_correct_link_has_no_invented_score(self):
        self.defer()
        url = 'https://genius.com/Juice-wrld-correct-lyrics'
        self.ns['_ask_genius_review'] = lambda *a: {'action': 'correct', 'url': url}
        self.assertEqual(self.ns['_review_pending_genius'](), (1, 0))
        text = Path(self.out).read_text()
        self.assertIn(url, text)
        self.assertNotIn('84%', text)
        self.assertNotIn('100%', text)

    def test_skip_and_stop_never_fetch(self):
        for action in ('skip', 'stop'):
            self.defer()
            self.ns['_ask_genius_review'] = lambda *a: {'action': action}
            self.assertEqual(self.ns['_review_pending_genius'](), (0, 1))
            self.assertFalse(Path(self.out).exists())
        self.ns['_get_cached_genius_lyrics'].assert_not_called()

    def test_invalid_urls(self):
        validate = self.ns['_validated_genius_url']
        for value in ('file:///etc/passwd', 'https://genius.com.evil.org/test-lyrics',
                      'https://evil.org/test-lyrics', 'https://genius.com/artists/Juice-wrld',
                      'https://user@genius.com/test-lyrics'):
            with self.assertRaises(ValueError):
                validate(value)
        self.assertEqual(validate(' http://www.genius.com/Test-lyrics?x=1#part '),
                         'https://genius.com/Test-lyrics')

    def test_failed_fetch_retries_same_item(self):
        self.defer()
        self.ns['_get_cached_genius_lyrics'].side_effect = ['', 'Valid lyrics line\n' * 8]
        self.ns['_http_get'].side_effect = RuntimeError('network unavailable')
        ask = Mock(return_value={'action': 'confirm'})
        self.ns['_ask_genius_review'] = ask
        self.assertEqual(self.ns['_review_pending_genius'](), (1, 0))
        self.assertEqual(ask.call_count, 2)
        self.assertIn('network unavailable', ask.call_args.args[3])

    def test_failure_keeps_existing_file_and_removes_temp(self):
        Path(self.out).parent.mkdir()
        Path(self.out).write_text('keep')
        self.ns['reject_if_mostly_non_ascii'] = lambda p: True
        with self.assertRaises(ValueError):
            self.ns['_save_genius_result'](self.result, self.out)
        self.assertEqual(Path(self.out).read_text(), 'keep')
        self.assertEqual(list(Path(self.out).parent.glob('*.tmp')), [])

    def test_cancel_leaves_missing_txt_for_next_scan(self):
        self.defer()
        self.ns['should_cancel'] = lambda: True
        self.assertEqual(self.ns['_review_pending_genius'](), (0, 1))
        self.assertEqual(self.ns['collect_missing_txt']([str(self.folder)]), [self.song])

    def test_no_accepted_match_can_download_manual_link(self):
        self.ns['_genius_find_verified_song'] = lambda *a: None
        self.assertFalse(self.defer())
        item = next(iter(self.ns['_pending_genius_reviews']().values()))
        self.assertEqual(item['result'], {})
        self.assertIsNone(item['score'])
        self.ns['_get_cached_genius_lyrics'].assert_not_called()
        self.ns['_ask_genius_review'] = lambda *a: {
            'action': 'correct', 'url': 'https://genius.com/Juice-wrld-luigi-sessions-lyrics'}
        self.assertEqual(self.ns['_review_pending_genius'](), (1, 0))
        text = Path(self.out).read_text()
        self.assertIn('luigi-sessions-lyrics', text)
        self.assertNotIn('Match accuracy:', text)

    def test_no_accepted_match_can_be_skipped(self):
        self.ns['_genius_find_verified_song'] = lambda *a: None
        self.defer()
        self.ns['_ask_genius_review'] = lambda *a: {'action': 'skip'}
        self.assertEqual(self.ns['_review_pending_genius'](), (0, 1))
        self.assertFalse(Path(self.out).exists())

    def test_rejected_closest_match_is_available_for_confirmation(self):
        candidate = dict(self.result, _match_score_percent=45, _review_only=True,
                         full_title='Juice WRLD - Blood On My Jeans (IG Live Freestyle)')
        def find(*args):
            self.ns['_genius_review_state'].closest_rejected = candidate
            return None
        self.ns['_genius_find_verified_song'] = find
        self.assertFalse(self.defer())
        item = next(iter(self.ns['_pending_genius_reviews']().values()))
        self.assertEqual(item['score'], 45)
        self.assertEqual(item['result']['full_title'], candidate['full_title'])
        self.ns['_get_cached_genius_lyrics'].assert_not_called()
        self.ns['_ask_genius_review'] = lambda *a: {'action': 'confirm'}
        self.assertEqual(self.ns['_review_pending_genius'](), (1, 0))
        self.assertIn('Match accuracy: 45%', Path(self.out).read_text())

    def test_rejected_candidate_never_auto_downloads_even_at_high_score(self):
        def find(*args):
            self.ns['_genius_review_state'].closest_rejected = dict(
                self.result, _match_score_percent=95, _review_only=True)
            return None
        self.ns['_genius_find_verified_song'] = find
        self.assertFalse(self.defer())
        self.ns['_get_cached_genius_lyrics'].assert_not_called()

    def test_previous_rejected_candidate_is_not_reused_for_next_song(self):
        self.ns['_genius_review_state'].closest_rejected = dict(self.result)
        self.ns['_genius_find_verified_song'] = lambda *a: None
        self.defer()
        item = next(iter(self.ns['_pending_genius_reviews']().values()))
        self.assertEqual(item['result'], {})

    def test_current_source_read_from_footer(self):
        read = self.ns['_current_lyrics_source']
        self.assertEqual(read('Lyrics\n\nMatch accuracy: 84%\nhttps://genius.com/Test-lyrics\n'),
                         'https://genius.com/Test-lyrics')
        self.assertEqual(read('Lyrics without a recorded source'), '')

    def test_manual_replacement_overwrites_old_txt(self):
        Path(self.out).parent.mkdir()
        Path(self.out).write_text('Old lyrics')
        result = self.ns['_replace_track_lyrics'](self.song, self.out, self.result['url'])
        self.assertEqual(result, self.out)
        self.assertNotIn('Old lyrics', Path(self.out).read_text())
        self.assertIn(self.result['url'], Path(self.out).read_text())

    def test_manual_replacement_removes_old_lrc_after_success(self):
        old = self.folder / 'Exact audio name.lrc'
        old.write_text('Old synced lyrics')
        self.ns['_replace_track_lyrics'](self.song, str(old), self.result['url'])
        self.assertFalse(old.exists())
        self.assertTrue(Path(self.out).is_file())

    def test_manual_replacement_failure_preserves_old_file(self):
        old = self.folder / 'Exact audio name.txt'
        old.write_text('Old lyrics')
        self.ns['_get_cached_genius_lyrics'].return_value = ''
        self.ns['_http_get'].side_effect = RuntimeError('Network error')
        with self.assertRaises(RuntimeError):
            self.ns['_replace_track_lyrics'](self.song, str(old), self.result['url'])
        self.assertEqual(old.read_text(), 'Old lyrics')
        self.assertFalse(Path(self.out).exists())

    def test_manual_replacement_invalid_url_preserves_old_file(self):
        old = self.folder / 'Exact audio name.txt'
        old.write_text('Old lyrics')
        with self.assertRaises(ValueError):
            self.ns['_replace_track_lyrics'](self.song, str(old), 'https://example.com/wrong')
        self.assertEqual(old.read_text(), 'Old lyrics')
        self.ns['_get_cached_genius_lyrics'].assert_not_called()

    def test_manual_text_edit_saves_and_backs_up_previous_text(self):
        target = self.folder / 'LYRICS' / 'Exact audio name.txt'
        target.parent.mkdir()
        target.write_text('Old line\nSource footer', encoding='utf-8')
        saved, backup = self.ns['_save_edited_lyrics'](target, 'Corrected line\nSource footer')
        self.assertEqual(Path(saved), target)
        self.assertEqual(target.read_text(encoding='utf-8'), 'Corrected line\nSource footer\n')
        self.assertEqual(Path(backup).read_text(encoding='utf-8'), 'Old line\nSource footer')
        self.assertIn('BACKUPS\\LYRICS-EDITS', backup)

    def test_manual_text_edit_can_create_missing_txt(self):
        target = self.folder / 'LYRICS' / 'Exact audio name.txt'
        saved, backup = self.ns['_save_edited_lyrics'](target, 'Manually entered lyrics')
        self.assertEqual(Path(saved), target)
        self.assertEqual(backup, '')
        self.assertEqual(target.read_text(encoding='utf-8'), 'Manually entered lyrics\n')

    def test_manual_text_edit_refuses_empty_content(self):
        target = self.folder / 'LYRICS' / 'Exact audio name.txt'
        target.parent.mkdir()
        target.write_text('Keep this', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.ns['_save_edited_lyrics'](target, '   \n')
        self.assertEqual(target.read_text(encoding='utf-8'), 'Keep this')

    def test_selected_track_with_no_accepted_match_opens_review(self):
        ns = self.ns
        for button in ('artist_dl_btn', 'track_dl_btn', 'open_btn', 'edit_lyrics_btn', 'missing_scan_btn', 'cancel_btn'):
            ns[button] = Mock()
        ns.update(MUSIC_DIR=str(self.folder), downloading=True, cancel_requested=False,
                  pause_requested=False, upgrade_all_session=False, scanned_artists=set(), lyrics_cache={},
                  artist_list=Mock(), track_list=Mock(), TRACK_ROW_PATHS={0: self.song},
                  strip_icon=lambda s: s, missing_targets=[],
                  get_synced_provider_order=lambda: [], get_plain_provider_order=lambda: ['Genius'],
                  get_lang_code=lambda: '', allow_plain_fallback=lambda: True,
                  wait_if_paused=lambda: True, get_track_metadata=lambda s: ('Luigi (Sessions)', 'Juice WRLD'),
                  infer_artist_from_path=lambda s: 'Juice WRLD', refresh_artist_list=Mock(), refresh_current_view=Mock(),
                  find_existing_lyrics=lambda s: self.out if Path(self.out).is_file() else None,
                  get_lyrics_path=lambda *a, **k: self.out,
                  get_provider_output_path=lambda *a, **k: self.out)
        ns['artist_list'].curselection.return_value = (0,)
        ns['artist_list'].get.return_value = 'Juice WRLD'
        ns['track_list'].curselection.return_value = (0,)
        ns['_genius_find_verified_song'] = lambda *a: None
        ask = Mock(return_value={'action': 'correct', 'url': 'https://genius.com/Juice-wrld-luigi-sessions-lyrics'})
        ns['_ask_genius_review'] = ask
        ns['download_selected']()
        ask.assert_called_once()
        self.assertTrue(Path(self.out).is_file())
        self.assertFalse(ns['downloading'])

    def test_batch_continues_before_review_and_stops_other_providers(self):
        other = str(self.folder / 'Second.mp3')
        Path(other).touch()
        ns = self.ns
        for button in ('artist_dl_btn', 'track_dl_btn', 'open_btn', 'edit_lyrics_btn', 'missing_scan_btn', 'cancel_btn'):
            ns[button] = Mock()
        ns.update(missing_targets=[self.song, other], MUSIC_DIR=str(self.folder), downloading=True,
                  cancel_requested=False, pause_requested=False, scanned_artists=set(), lyrics_cache={},
                  get_plain_provider_order=lambda: ['Genius', 'ForbiddenFallback'], get_lang_code=lambda: '',
                  wait_if_paused=lambda: True, get_track_metadata=lambda s: (Path(s).stem, 'Juice WRLD'),
                  infer_artist_from_path=lambda s: 'Juice WRLD', refresh_artist_list=Mock(), refresh_current_view=Mock())
        def find(title, artists):
            result = dict(self.result)
            result['_match_score_percent'] = 85 if title == 'Second' else 84
            return result
        ns['_genius_find_verified_song'] = find
        def ask(*args):
            self.assertTrue((self.folder / 'LYRICS' / 'Second.txt').is_file())
            self.assertFalse(Path(self.out).exists())
            return {'action': 'confirm'}
        ns['_ask_genius_review'] = ask
        ns['download_missing_queue']()
        self.assertTrue(Path(self.out).is_file())
        self.assertFalse(ns['downloading'])
        self.assertEqual(len(ns['attempted_track_paths']), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
