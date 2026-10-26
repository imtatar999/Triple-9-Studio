import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import triplenine_lyrics_library as library


class LyricsLibraryTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'
        self.target = self.base / 'target'
        self.source.mkdir()
        self.target.mkdir()

    @staticmethod
    def _track(root, relative='Artist/Album/Song.mp3'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio-is-not-exported')
        return path

    def test_export_contains_manifest_and_sidecars_but_no_audio(self):
        song = self._track(self.source)
        lyrics = song.parent / 'LYRICS' / 'Song.txt'
        synced = song.parent / 'SYNCED LYRICS' / 'Song.lrc'
        lyrics.parent.mkdir()
        synced.parent.mkdir()
        lyrics.write_text('plain lyrics', encoding='utf-8')
        synced.write_text('[00:01.00]timed lyrics', encoding='utf-8')
        package = self.base / 'library.zip'

        result = library.export_package(self.source, package)

        self.assertEqual(result['tracks'], 1)
        self.assertEqual(result['files'], 2)
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            self.assertIn('manifest.json', names)
            self.assertIn('files/Artist/Album/LYRICS/Song.txt', names)
            self.assertIn('files/Artist/Album/SYNCED LYRICS/Song.lrc', names)
            self.assertFalse(any(name.endswith('.mp3') for name in names))

    def test_import_creates_updates_deletes_and_keeps_verified_backup(self):
        source_song = self._track(self.source)
        (source_song.parent / 'LYRICS').mkdir()
        (source_song.parent / 'LYRICS' / 'Song.txt').write_text('new plain', encoding='utf-8')
        (source_song.parent / 'SYNCED LYRICS').mkdir()
        (source_song.parent / 'SYNCED LYRICS' / 'Song.lrc').write_text('[00:01.00]new', encoding='utf-8')
        package = self.base / 'library.zip'
        library.export_package(self.source, package)

        target_song = self._track(self.target)
        (target_song.parent / 'LYRICS').mkdir()
        old_txt = target_song.parent / 'LYRICS' / 'Song.txt'
        old_txt.write_text('old plain', encoding='utf-8')
        obsolete = target_song.with_suffix('.lrc')
        obsolete.write_text('[00:01.00]obsolete', encoding='utf-8')

        preview = library.inspect_import(self.target, package)
        self.assertEqual(preview['counts'], {'create': 1, 'update': 1, 'delete': 1})
        result = library.import_package(self.target, package, self.base / 'backups')

        self.assertEqual(old_txt.read_text(encoding='utf-8'), 'new plain')
        self.assertEqual((target_song.parent / 'SYNCED LYRICS' / 'Song.lrc').read_text(encoding='utf-8'),
                         '[00:01.00]new')
        self.assertFalse(obsolete.exists())
        backup = Path(result['backup_dir'])
        self.assertEqual((backup / 'Artist/Album/LYRICS/Song.txt').read_text(encoding='utf-8'), 'old plain')
        self.assertEqual((backup / 'Artist/Album/Song.lrc').read_text(encoding='utf-8'),
                         '[00:01.00]obsolete')

    def test_tracks_missing_on_target_are_reported_and_not_extracted(self):
        song = self._track(self.source)
        song.with_suffix('.txt').write_text('lyrics', encoding='utf-8')
        package = self.base / 'library.zip'
        library.export_package(self.source, package)

        preview = library.inspect_import(self.target, package)

        self.assertEqual(preview['matched_tracks'], 0)
        self.assertEqual(preview['missing_tracks'], ['Artist/Album/Song.mp3'])
        self.assertFalse((self.target / 'Artist/Album/Song.txt').exists())

    def test_rejects_unlisted_or_unsafe_package_content(self):
        package = self.base / 'bad.zip'
        manifest = {
            'format': library.FORMAT_NAME,
            'version': library.FORMAT_VERSION,
            'complete_snapshot': True,
            'tracks': {},
        }
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('../outside.txt', 'bad')

        with self.assertRaises(ValueError):
            library.inspect_import(self.target, package)
        self.assertFalse((self.base / 'outside.txt').exists())


if __name__ == '__main__':
    unittest.main()
