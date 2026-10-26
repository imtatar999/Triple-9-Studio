"""Hidden GUI smoke check; does not start audio processing."""
import runpy
import tkinter as tk
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
from unittest.mock import patch


def main():
    original = tk.Tk
    roots = []

    def hidden(*args, **kwargs):
        root = original(*args, **kwargs)
        root.withdraw()
        roots.append(root)
        return root

    try:
        with patch.object(tk, 'Tk', hidden), patch.object(tk.Misc, 'mainloop', lambda self: None):
            ctx = runpy.run_path(str(Path(__file__).parent.parent / 'SYNCED-LYRICS-DOWNLOADER-MAIN' / 'triplenine_lyrics_downloader.py'), run_name='studio_smoke')
        print(ctx['root'].title())
        assert ctx['APP_NAME'] == 'T9 Downloader'
        assert ctx['STORAGE_NAME'] == 'Triplenine Lyrics Downloader'
        assert ctx['builder_btn'].cget('bg') == ctx['syncalong_btn'].cget('bg')
        root = ctx['root']
        def settle_status():
            deadline = time.monotonic() + 10
            while ctx['_VIEW_STATUS_PENDING'] and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            assert not ctx['_VIEW_STATUS_PENDING'], 'Status scan did not finish'
        assert ctx['help_hint'].cget('text') == 'Need help or not sure what to do? Open Help > How To Use.'
        assert '🎶 Synced in audio' in ctx['legend_track_synced'].cget('text')
        assert '📄 Timed LRC file' in ctx['legend_track_synced'].cget('text')
        root.geometry('1180x800+30000+30000')
        root.deiconify()
        root.update()
        for name in ('track_dl_btn', 'missing_scan_btn', 'operation_controls',
                     'clear_btn', 'delete_reference_btn'):
            widget = ctx[name]
            assert widget.winfo_width() >= widget.winfo_reqwidth(), name + ' clipped'
            assert widget.winfo_x() + widget.winfo_width() <= widget.master.winfo_width(), name
        original_theme = ctx['config'].get('theme', 'dark')
        try:
            for theme in ('light', 'dark'):
                ctx['apply_theme'](theme)
                assert ctx['delete_reference_btn'].cget('bg') == ctx['reference_btn'].cget('bg')
        finally:
            ctx['apply_theme'](original_theme)
        with patch.object(ctx['subprocess'], 'Popen') as launch:
            launch.return_value.poll.return_value = None
            ctx['open_builder_window']()
            assert launch.call_count == 1
            assert launch.call_args.args[0][1].endswith('Triplenine_Hybrid_Sessions_Lyrics_Builder.py')
            with patch.object(ctx['messagebox'], 'showinfo'):
                ctx['open_builder_window']()
            assert launch.call_count == 1
        print('PASS: GUI, layout at 1180x800, both themes, Builder launch, duplicate protection')
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            direct = library / 'Direct.mp3'
            direct.touch()
            (library / 'Lyrics').mkdir()
            (library / 'Lyrics' / 'Direct.txt').write_text('Finished text', encoding='utf-8')
            namespace = ctx['load_artists'].__globals__
            with patch.dict(namespace, MUSIC_DIR=str(library)):
                ctx['song_search_var'].set('')
                ctx['load_artists']()
                settle_status()
                root.update()
                assert set(ctx['TRACK_ROW_PATHS'].values()) == {str(direct)}
                assert not ctx['album_card'].winfo_manager(), 'Lyrics folder should not enable Albums'
                assert ctx['artist_list'].size() == 1
                assert ctx['artist_list'].get(0) == library.name
                ctx['update_action_context']()
                assert ctx['track_dl_btn'].cget('state') == 'disabled'
                assert ctx['scope_var'].get() == 'Selected tracks: 0'
                ctx['artist_list'].selection_set(0)
                ctx['on_artist_select']()
                assert set(ctx['TRACK_ROW_PATHS'].values()) == {str(direct)}
                ctx['track_list'].selection_set(0)
                ctx['update_action_context']()
                assert ctx['track_dl_btn'].cget('state') == 'normal'
                assert str(direct) == ctx['selected_path_var'].get()
                assert 'Selected tracks: 1' in ctx['scope_var'].get()
                assert 'Lyrics downloaded' in ctx['scope_var'].get()
                from mutagen.id3 import ID3, USLT
                tags = ID3()
                tags.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]Test line'))
                tags.save(direct)
                ctx['refresh_tracks_from_disk'](selected_paths=[str(direct)])
                settle_status()
                ctx['update_action_context']()
                assert 'Synced lyrics embedded in audio' in ctx['scope_var'].get()
                with patch.dict(namespace, require_genius_token=lambda: (_ for _ in ()).throw(AssertionError('Local removal needs no API'))):
                    ctx['remove_selected_embedded_lyrics']()
                    deadline = time.monotonic() + 10
                    while namespace['downloading'] and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                    assert not namespace['downloading']
                    settle_status()
                ctx['update_action_context']()
                assert 'Lyrics downloaded' in ctx['scope_var'].get()
                assert 'Synced lyrics embedded in audio' not in ctx['scope_var'].get()
                assert not ctx['track_list'].get(0).startswith('🎶')
                old_file = namespace['__file__']
                namespace['__file__'] = str(library / 'app' / 'downloader.py')
                try:
                    ctx['delete_lyrics_for_selected']()
                finally:
                    namespace['__file__'] = old_file
                settle_status()
                assert direct.is_file()
                assert not (library / 'Lyrics' / 'Direct.txt').exists()
                assert list((library / 'BACKUPS').rglob('Direct.txt'))
                assert ctx['track_list'].get(0) == 'Direct.mp3'
                ctx['update_action_context']()
                assert ctx['undo_lyrics_btn'].cget('state') == 'normal'
                destination = library / 'Lyrics' / 'Direct.txt'
                destination.write_text('New text', encoding='utf-8')
                with patch.object(ctx['messagebox'], 'showwarning'):
                    ctx['undo_delete_lyrics']()
                assert destination.read_text() == 'New text'
                destination.unlink()
                ctx['undo_delete_lyrics']()
                settle_status()
                assert destination.read_text() == 'Finished text'
                ctx['update_action_context']()
                assert ctx['undo_lyrics_btn'].cget('state') == 'disabled'
                ctx['artist_list'].selection_clear(0, 'end')
                nested = library / 'Album'
                nested.mkdir()
                track = nested / 'Nested.flac'
                track.touch()
                ctx['refresh_tracks_from_disk']()
                root.update()
                assert set(ctx['TRACK_ROW_PATHS'].values()) == {str(direct), str(track)}
                assert ctx['album_card'].winfo_manager() == 'pack'
                album_row = next(i for i, name in ctx['ALBUM_ROW_DIRS'].items() if name == 'Album')
                ctx['album_list'].selection_set(album_row)
                ctx['on_album_select']()
                assert set(ctx['TRACK_ROW_PATHS'].values()) == {str(track)}
        print('PASS: direct folder, automatic tracks, nested Albums, album filtering, refresh')
        with patch.dict(ctx['open_syncalong_window'].__globals__,
                        _syncalong_gpu_info=lambda: dict(cuda=True, gpu='NVIDIA Graphics card', torch=True,
                                                         vram_gb=12, detail='CUDA available')):
            ctx['open_syncalong_window']()
            root.update()
            sync_window = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel)
                               and w.title() == 'Triple 9 Studio - Lyrics Sync')
            assert sync_window.state() == 'normal', 'Lyrics Sync should open in a normal window'
            sync_window.geometry('960x680+30000+30000')
            root.update()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            buttons = [w for w in descendants(root) if isinstance(w, tk.Button)
                       and w.cget('text') == 'Check / Setup Lyrics Sync']
            assert len(buttons) == 1
            sync_controls = [w for w in descendants(sync_window)
                             if isinstance(w, tk.Text) or
                             (isinstance(w, tk.Button) and ('START SYNC' in w.cget('text') or 'Setup' in w.cget('text')))]
            assert len(sync_controls) == 3
            for widget in sync_controls:
                assert widget.winfo_ismapped(), str(widget)
                assert widget.winfo_height() > 15, str(widget)
                assert widget.winfo_rooty() + widget.winfo_height() <= sync_window.winfo_rooty() + sync_window.winfo_height()
        print('PASS: Lyrics Sync window and setup control')
        namespace = ctx['require_genius_token'].__globals__
        with patch.dict(namespace, get_genius_api_token=lambda: ''):
            def token_dialog_action(save):
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel)
                              and w.title() == 'Genius API token required')
                if not save:
                    dialog.destroy()
                    return
                field = next(w for w in descendants(dialog) if isinstance(w, tk.Entry))
                field.insert(0, 'dummy-test-token')
                next(w for w in descendants(dialog) if isinstance(w, tk.Button)
                     and w.cget('text') == 'Save and Continue').invoke()
            root.after(100, lambda: token_dialog_action(False))
            assert ctx['require_genius_token']() is False
            with patch.dict(namespace, save_genius_api_token=lambda value: None):
                root.after(100, lambda: token_dialog_action(True))
                assert ctx['require_genius_token']() is True
        print('PASS: missing token dialog, cancellation and save/continue without writing credentials')
    finally:
        for root in roots:
            root.destroy()


if __name__ == '__main__':
    main()
