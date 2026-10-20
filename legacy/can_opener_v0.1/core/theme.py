"""
Shared theme constants and reusable widget helpers.
"""
import tkinter as tk
from tkinter import ttk

# ── Palette ────────────────────────────────────────────────────────────────────
BG          = "#EDEAE4"
PANEL       = "#E0DDD6"
PANEL_DARK  = "#D0CCC4"
CARD        = "#F5F3EF"
BORDER      = "#C0BBB2"
TEXT        = "#1C1914"
TEXT_DIM    = "#7A756D"
TEXT_MED    = "#44403A"

ACCENT      = "#2060A8"
ACCENT_LT   = "#D4E5F7"
ACCENT_DIM  = "#174E8C"

GREEN       = "#1A7A3C"
GREEN_LT    = "#D0EFD8"
RED         = "#B83232"
RED_LT      = "#F5D6D6"
YELLOW      = "#8A6A00"
YELLOW_LT   = "#FFF0C0"
ORANGE      = "#A04800"
ORANGE_LT   = "#FFE8D0"

TAB_BG      = "#2A2724"
TAB_FG      = "#A09890"
TAB_ACT     = "#F5F3EF"
TAB_ACT_FG  = "#1C1914"
TAB_HOVER   = "#3A3530"

STATUS_BG   = "#1C1914"
STATUS_FG   = "#E0DDD6"

ROW_EVEN    = "#F5F3EF"
ROW_ODD     = "#EDEAE4"
ROW_SEL     = "#D4E5F7"

# ── Fonts ──────────────────────────────────────────────────────────────────────
FONT_MONO   = ("Courier New", 10)
FONT_MONO_S = ("Courier New", 9)
FONT_UI     = ("TkDefaultFont", 11)
FONT_UI_S   = ("TkDefaultFont", 10)
FONT_UI_XS  = ("TkDefaultFont", 9)
FONT_BOLD   = ("TkDefaultFont", 11, "bold")
FONT_BOLD_S = ("TkDefaultFont", 10, "bold")
FONT_BIG    = ("TkDefaultFont", 13, "bold")
FONT_STATUS = ("TkDefaultFont", 10)

# ── Tab definitions ────────────────────────────────────────────────────────────
TABS = [
    ("config",  "⚙",  "Config"),
    ("raw",     "📡", "Raw / Log"),
    ("codes",   "⚠",  "Codes"),
    ("pids",    "📊", "PIDs"),
]

TAB_W = 72   # px wide
TAB_H = 90   # px tall per tab


# ── Helper widgets ─────────────────────────────────────────────────────────────

class FlatButton(tk.Label):
    """Touch-friendly flat button."""
    def __init__(self, parent, text="", command=None,
                 bg=ACCENT, fg="white", font=FONT_BOLD,
                 pad_x=14, pad_y=8, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg,
                         font=font, padx=pad_x, pady=pad_y,
                         cursor="hand2", relief="flat", **kw)
        self._bg  = bg
        self._cmd = command
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>",    self._on_enter)
        self.bind("<Leave>",    self._on_leave)

    def _on_click(self, _=None):
        if self._cmd:
            self._cmd()

    def _on_enter(self, _=None):
        self.configure(bg=_darken(self._bg))

    def _on_leave(self, _=None):
        self.configure(bg=self._bg)

    def set_bg(self, color: str):
        self._bg = color
        self.configure(bg=color)


class Pill(tk.Label):
    """Small status pill label."""
    def __init__(self, parent, text="", color=BORDER, fg=TEXT_DIM,
                 font=FONT_UI_XS, **kw):
        super().__init__(parent, text=text, bg=color, fg=fg,
                         font=font, padx=8, pady=3, relief="flat", **kw)

    def update(self, text: str, color: str, fg: str = TEXT):
        self.configure(text=text, bg=color, fg=fg)


class SectionLabel(tk.Label):
    """Small all-caps section header."""
    def __init__(self, parent, text="", **kw):
        super().__init__(parent, text=text.upper(),
                         bg=kw.pop("bg", BG), fg=TEXT_DIM,
                         font=FONT_UI_XS, **kw)


class Divider(tk.Frame):
    def __init__(self, parent, orient="h", **kw):
        if orient == "h":
            super().__init__(parent, bg=BORDER, height=1, **kw)
        else:
            super().__init__(parent, bg=BORDER, width=1, **kw)


def _darken(hex_color: str, amount: int = 20) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2],16), int(hex_color[2:4],16), int(hex_color[4:6],16)
    r = max(0, r - amount)
    g = max(0, g - amount)
    b = max(0, b - amount)
    return f"#{r:02X}{g:02X}{b:02X}"


def modal_overlay(root, title: str,
                  width: int = 600, height: int = 400) -> tk.Toplevel:
    """Create a centered modal toplevel — no grab_set."""
    top = tk.Toplevel(root)
    top.title(title)
    top.configure(bg=BG)
    top.resizable(False, False)
    top.transient(root)
    rx = root.winfo_x() + root.winfo_width()  // 2 - width  // 2
    ry = root.winfo_y() + root.winfo_height() // 2 - height // 2
    top.geometry(f"{width}x{height}+{rx}+{ry}")
    top.lift()
    return top


def touch_confirm(root, message: str,
                  confirm_text: str = "Yes",
                  cancel_text:  str = "Cancel",
                  confirm_bg:   str = RED,
                  on_confirm=None):
    """
    Touch-friendly yes/no dialog.
    No grab_set — avoids conflicts with other toplevels.
    Calls on_confirm() if user taps the confirm button.
    """
    W, H = 340, 190
    rx = root.winfo_rootx() + root.winfo_width()  // 2 - W // 2
    ry = root.winfo_rooty() + root.winfo_height() // 2 - H // 2

    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.configure(bg=BG, bd=2, relief="solid")
    top.geometry(f"{W}x{H}+{rx}+{ry}")
    top.lift()

    tk.Label(top, text=message, bg=BG, fg=TEXT,
             font=FONT_BOLD, wraplength=300,
             justify="center").pack(pady=(30, 20))

    btn_row = tk.Frame(top, bg=BG)
    btn_row.pack()

    def _confirm():
        top.destroy()
        if on_confirm:
            on_confirm()

    FlatButton(btn_row, text=cancel_text,
               bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD,
               pad_x=24, pad_y=10,
               command=top.destroy).pack(side="left", padx=(0, 12))

    FlatButton(btn_row, text=confirm_text,
               bg=confirm_bg, fg="white", font=FONT_BOLD,
               pad_x=24, pad_y=10,
               command=_confirm).pack(side="left")


def touch_alert(root, message: str, title: str = ""):
    """Touch-friendly info/error alert — no grab_set."""
    W, H = 340, 170
    rx = root.winfo_rootx() + root.winfo_width()  // 2 - W // 2
    ry = root.winfo_rooty() + root.winfo_height() // 2 - H // 2

    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.configure(bg=BG, bd=2, relief="solid")
    top.geometry(f"{W}x{H}+{rx}+{ry}")
    top.lift()

    if title:
        tk.Label(top, text=title, bg=BG, fg=TEXT_DIM,
                 font=FONT_BOLD_S).pack(pady=(16, 2))

    tk.Label(top, text=message, bg=BG, fg=TEXT,
             font=FONT_UI_S, wraplength=300,
             justify="center").pack(pady=(8, 16) if title else (28, 16))

    FlatButton(top, text="OK",
               bg=ACCENT, fg="white", font=FONT_BOLD,
               pad_x=32, pad_y=10,
               command=top.destroy).pack()
