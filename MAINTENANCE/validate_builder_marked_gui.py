"""Hidden GUI check for the Builder's Marked Tracks dialog; starts no ASR work."""
import tempfile
import tkinter as tk
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
from unittest.mock import patch

import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def marked_window(root):
    return next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))


def click_load(root):
    win = marked_window(root)
    listing = next(widget for widget in descendants(win) if isinstance(widget, tk.Listbox))
    button = next(widget for widget in descendants(win)
                  if widget.winfo_class() == "TButton" and widget.cget("text") == "Load Marked Track")
    listing.selection_set(0)
    listing.event_generate("<<ListboxSelect>>")
    button.invoke()
    root.update()


def main():
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        audio = folder / "Missing Reference (Sessions).mp3"
        audio.write_bytes(b"audio")
        settings = folder / "settings.json"
        entry = {"audio": str(audio), "status": "missing"}
        root = tk.Tk()
        root.geometry("1120x850+200+100")
        root.attributes("-alpha", 0.0)
        root.update()
        try:
            with (patch.object(builder, "_settings_path", lambda: settings),
                  patch.object(builder.reference_queue, "entries", return_value=[entry]),
                  patch.object(builder.reference_queue, "is_ready_for_sync", side_effect=lambda item: item.get("status") == "ready_for_lyric_sync"),
                  patch.object(builder.reference_queue, "ready", side_effect=lambda item: bool(item.get("reference")))):
                app = builder.App(root)
                root.update()
                assert root.state() == 'normal', 'Builder should open in a normal window'
                root.geometry('1000x640')
                root.update()
                for widget in (app.start_btn, app.stop_btn, app.log, app.reference, app.result):
                    assert widget.winfo_ismapped(), str(widget)
                    assert widget.winfo_height() > 30 if widget in (app.log, app.reference, app.result) else widget.winfo_height() > 10
                    assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
                app.open_marked_tracks()
                root.update()
                expected_icon = str((Path(builder.__file__).resolve().parent / 'triplenine.ico')).replace('\\', '/')
                assert marked_window(root)._t9_icon_path.replace('\\', '/') == expected_icon
                click_load(root)
                assert app.audio_var.get() == str(audio)
                assert app.reference.get("1.0", "end").strip() == ""
                assert app.active_marked_entry["audio"] == str(audio)

                reference = folder / "reference.txt"
                reference.write_text("[Chorus]\nReference lyrics", encoding="utf-8")
                entry["reference"] = str(reference)
                app.open_marked_tracks()
                root.update()
                click_load(root)
                assert app.reference.get("1.0", "end").strip() == "[Chorus]\nReference lyrics"

                app.open_marked_tracks()
                root.update()
                win = marked_window(root)
                win.geometry("720x410+120+130")
                root.update()
                app._close_marked_tracks(win)
                app.open_marked_tracks()
                root.update()
                restored = marked_window(root).geometry()
                assert restored == "720x410+120+130", restored
                app._close_marked_tracks(marked_window(root))
                assert "marked_tracks_geometry" in settings.read_text(encoding="utf-8")
                entry["status"] = "ready_for_lyric_sync"
                app.open_marked_tracks()
                root.update()
                listing = next(widget for widget in descendants(marked_window(root))
                               if isinstance(widget, tk.Listbox))
                assert listing.size() == 0, "Completed track must be hidden"
                app._close_marked_tracks(marked_window(root))
            print("PASS: ready reference, audio-only missing entry, button label and dialog geometry persistence")
        finally:
            root.destroy()


if __name__ == "__main__":
    main()
