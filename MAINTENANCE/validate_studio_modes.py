"""GUI regression checks for help, developer visibility and preserved values."""
from pathlib import Path
import runpy
import sys
import tempfile
import tkinter as tk
from unittest.mock import patch

APP = Path(__file__).resolve().parent.parent / 'SYNCED-LYRICS-DOWNLOADER-MAIN'
sys.path.insert(0, str(APP))
import studio_preferences as prefs
import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def named(widget, text):
    return next(w for w in descendants(widget)
                if 'text' in w.keys() and w.cget('text') == text)


def main():
    roots = []
    original = tk.Tk

    def hidden(*args, **kwargs):
        root = original(*args, **kwargs)
        root.geometry('1180x800+30000+30000')
        roots.append(root)
        return root

    try:
        with tempfile.TemporaryDirectory() as temp, patch.object(prefs, 'PATH', Path(temp) / 'prefs.json'), \
                patch.object(tk, 'Tk', hidden), patch.object(tk.Misc, 'mainloop', lambda self: None):
            ctx = runpy.run_path(str(APP / 'triplenine_lyrics_downloader.py'), run_name='studio_modes')
            root = ctx['root']
            assert ctx['clear_btn'].master is ctx['search_entry'].master
            ctx['about']()
            help_win = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            expected_icon = str((APP / 'triplenine.ico').resolve()).replace('\\', '/')
            assert help_win._t9_icon_path.replace('\\', '/') == expected_icon
            notebook = next(w for w in descendants(help_win) if w.winfo_class() == 'TNotebook')
            style = ctx['ttk'].Style(help_win)
            assert style.lookup('Help.TNotebook.Tab', 'foreground', ('selected',)) == '#000000'
            assert style.lookup('Help.TNotebook.Tab', 'background', ('selected',)) == '#ffffff'
            assert [notebook.tab(tab, 'text') for tab in notebook.tabs()] == ['T9 Downloader', 'T9 Lyrics Sync', 'T9 Lyric Builder']
            for geometry in ('1060x820+30000+30000', '640x480+30000+30000'):
                help_win.geometry(geometry)
                for tab in notebook.tabs():
                    notebook.select(tab)
                    help_win.update()
                    page_text = next(w for w in descendants(notebook.nametowidget(tab)) if isinstance(w, tk.Text))
                    content = page_text.get('1.0', 'end')
                    assert '**' not in content and '## ' not in content
                    assert '\u00e2\u20ac' not in content and '\u00e2\u2020' not in content
                    assert 'Testjuice' not in content and 'Juice WRLD' not in content
                    assert 'YOURMUSICFOLDER' in content and 'ARTIST NAME' in content and 'ALBUM NAME' in content
                    assert page_text.tag_ranges('bold') and page_text.tag_ranges('heading')
                    assert page_text.cget('state') == 'disabled'
                    jumps = [w for w in descendants(notebook.nametowidget(tab)) if isinstance(w, tk.Button)]
                    assert len(jumps) == 3
                    for button in jumps:
                        button.invoke()
                        help_win.update()
                        assert page_text.yview()[0] > 0, 'Section jump did not scroll'
                    page_text.yview_moveto(1.0)
                    help_win.update()
                    assert page_text.yview()[0] > 0 and page_text.yview()[1] >= 0.999
                    page_text.yview_moveto(0)
                last_tab = None
                for x in range(notebook.winfo_width()):
                    try:
                        last_tab = notebook.index(f'@{x},20')
                    except tk.TclError:
                        pass
                assert last_tab == 2, 'Help tabs clipped'
            help_win.destroy()
            ctx['open_options_window']()
            options = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            assert options._t9_icon_path.replace('\\', '/') == expected_icon
            legacy = next(w for w in descendants(options) if 'text' in w.keys() and str(w.cget('text')).startswith('Legacy / Advanced Options'))
            toggle = named(options, 'Developer mode — show technical and testing controls')
            assert not legacy.winfo_manager()
            toggle.invoke()
            assert legacy.winfo_manager() == 'pack'
            named(options, 'Cancel').invoke()
            assert not prefs.developer_mode()
            ctx['open_syncalong_window']()
            sync = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            assert sync._t9_icon_path.replace('\\', '/') == expected_icon
            sync.geometry('980x860+30000+30000')
            sync_threshold = named(sync, 'Match threshold:')
            sync_prompt = named(sync, 'Use lyrics as Whisper prompt (recommended)')
            assert named(sync, 'Need help or not sure what to do? In T9 Downloader, open Help > How To Use.')
            assert not sync_threshold.winfo_manager() and not sync_prompt.winfo_manager()
            with patch.object(builder.App, 'load_settings'), patch.object(builder.reference_queue, 'reconcile_completed', return_value=[]):
                app = builder.App(tk.Tk())
            assert app.engine_var.get() == 'TripleNine Engine'
            notice = "I'm still working on Builder and trying to improve it. It won't always be 100% perfect."
            assert sum(1 for w in descendants(app.root) if 'text' in w.keys() and w.cget('text') == notice) == 2
            assert builder.ICON_FILE and Path(builder.ICON_FILE).resolve() == (APP / 'triplenine.ico').resolve()
            assert named(app.root, 'Need help or not sure what to do? In T9 Downloader, open Help > How To Use.')
            app.dark_theme_var.set(True)
            app.apply_theme()
            style = builder.ttk.Style(app.root)
            assert style.lookup('TFrame', 'background') == '#141414'
            assert style.lookup('TEntry', 'fieldbackground') == '#1c1c1c'
            assert style.lookup('TButton', 'background') == '#2a2a2a'
            assert not any('values' in w.keys() and 'Current V5' in str(w.cget('values')) for w in descendants(app.root))
            assert named(app.root, 'TripleNine Engine:')
            # Saved geometry must not put this test window on the user's screen.
            app.root.geometry('1120x850+30000+30000')
            threshold = named(app.root, 'Reference match threshold:').master
            testing = named(app.root, 'Export Result + Log for Testing').master
            original_value = app.threshold_var.get()
            app.log.insert('end', 'retained diagnostic')
            assert not threshold.winfo_manager() and not testing.winfo_manager()
            def tick():
                flag = tk.BooleanVar(root, False)
                root.after(1100, lambda: flag.set(True))
                root.wait_variable(flag)
            prefs.set_developer_mode(True)
            tick()
            assert threshold.winfo_manager() == testing.winfo_manager() == 'pack'
            assert sync_threshold.winfo_manager() == sync_prompt.winfo_manager() == 'grid'
            prefs.set_developer_mode(False)
            tick()
            assert not threshold.winfo_manager() and not testing.winfo_manager()
            assert not sync_threshold.winfo_manager() and not sync_prompt.winfo_manager()
            assert app.threshold_var.get() == original_value
            assert 'retained diagnostic' in app.log.get('1.0', 'end')
            print('PASS: three help tabs; Clear button location; Options preview/cancel; live Builder and Sync toggle; values/log preserved')
    finally:
        for root in reversed(roots):
            root.destroy()


if __name__ == '__main__':
    main()
