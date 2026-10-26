"""Compact, themed action categories for the Studio library window."""
import tkinter as tk


class ActionPanel(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.tabs = tk.Frame(self)
        self.tabs.pack(fill='x', pady=(0, 8))
        self.content = tk.Frame(self)
        self.content.pack(fill='x')
        self.pages = {}
        self.buttons = {}
        self.actions = {}
        self.current = None
        self.colors = None
        self.bind('<Configure>', self._resize)

    def add(self, key, title):
        page = tk.Frame(self.content)
        tab = tk.Button(self.tabs, text=title, command=lambda: self.select(key),
                        relief='flat', bd=0, padx=14, pady=7, cursor='hand2', font=('Segoe UI', 9))
        tab.pack(side='left', padx=(0, 4))
        self.pages[key], self.buttons[key] = page, tab
        self.actions[key] = []
        if self.current is None:
            self.select(key)
        return page

    def set_actions(self, key, buttons):
        self.actions[key] = list(buttons)
        self._layout()

    def select(self, key):
        if self.current:
            self.pages[self.current].pack_forget()
        self.current = key
        self.pages[key].pack(fill='x')
        self._layout()
        if self.colors:
            self.apply_theme(self.colors)

    def _resize(self, event):
        if event.widget is self:
            self._layout()

    def _layout(self):
        width = max(self.winfo_width(), 1)
        columns = 3 if width >= 900 else 2
        for key, buttons in self.actions.items():
            page = self.pages[key]
            for column in range(4):
                page.columnconfigure(column, weight=1 if column < columns else 0,
                                     uniform='actions' if column < columns else '')
            for index, button in enumerate(buttons):
                button.grid(row=index // columns, column=index % columns, sticky='ew',
                            padx=(0, 6), pady=(0, 6))
                button.configure(wraplength=max(160, width // columns - 35), anchor='center')

    def apply_theme(self, colors):
        self.colors = colors
        for widget in (self, self.tabs, self.content, *self.pages.values()):
            widget.configure(bg=colors['bg'])
        for key, tab in self.buttons.items():
            selected = key == self.current
            tab.configure(bg=colors['sel_bg'] if selected else colors['panel'],
                          fg=colors['sel_fg'] if selected else colors['fg'],
                          activebackground=colors['sel_bg'], activeforeground=colors['sel_fg'],
                          font=('Segoe UI', 9, 'bold' if selected else 'normal'))
