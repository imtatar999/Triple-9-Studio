"""Shared presentation preferences; never changes transcription parameters."""
import json
from pathlib import Path

PATH = Path(__file__).resolve().with_name('studio_preferences.json')


def developer_mode():
    try:
        return json.loads(PATH.read_text(encoding='utf-8')).get('developer_mode') is True
    except (OSError, ValueError):
        return False


def set_developer_mode(enabled):
    temporary = PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps({'developer_mode': bool(enabled)}, indent=2), encoding='utf-8')
    temporary.replace(PATH)


def watch_visibility(root, widgets):
    """Remember pack/grid positions and update live across Studio processes."""
    placements = [(widget, widget.winfo_manager(),
                   widget.pack_info() if widget.winfo_manager() == 'pack' else widget.grid_info())
                  for widget in widgets]
    for widget, manager, options in placements:
        if manager == 'pack':
            siblings = widget.master.pack_slaves()
            index = siblings.index(widget)
            if index + 1 < len(siblings):
                options['before'] = siblings[index + 1]
    previous = None

    def refresh():
        nonlocal previous
        enabled = developer_mode()
        if enabled != previous:
            for widget, manager, options in placements:
                if enabled:
                    if manager == 'pack':
                        widget.pack(**options)
                    else:
                        widget.grid(**options)
                elif manager == 'pack':
                    widget.pack_forget()
                else:
                    widget.grid_remove()
            previous = enabled
        root.after(1000, refresh)

    refresh()
