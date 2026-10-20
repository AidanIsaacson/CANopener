#!/usr/bin/env python3
"""
CAN Opener — J1939 Field Diagnostic Tool
Main entry point: app shell, status bar, tab bar, frame dispatch.
"""
import tkinter as tk
from tkinter import ttk
import time
import threading
from datetime import datetime

from core.settings  import load_settings, save_settings
from core.can_manager import CANManager
from core.logger    import LogManager
from core.j1939_defs import decode_j1939_id, pgn_name
from core.theme     import *

from tabs.config_tab    import ConfigTab
from tabs.raw_tab       import RawLogTab
from tabs.codes_tab     import CodesTab
from tabs.pids_tab      import PIDsTab
from tabs.system_overlay import SystemOverlay
from tabs.tetris        import launch_tetris

STATUS_H = 32
TAB_W    = 72
CLOCK_LONG_PRESS_MS = 2000


class CANOpenerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CAN Opener")
        self.geometry("800x480")
        self.resizable(False, False)
        self.configure(bg=BG)
        # Fullscreen on Pi, windowed on desktop for dev
        import os
        if os.environ.get("DISPLAY") == ":0" or os.path.exists("/sys/class/backlight/rpi_backlight"):
            self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        # Core services
        self.settings = load_settings()
        self.can_mgr  = CANManager()
        self.log_mgr  = LogManager(self.settings)

        # Apply saved units preference
        from core.j1939_defs import set_units
        set_units(self.settings.get("imperial", False))

        # Wire CAN callbacks
        self.can_mgr.register_frame_callback(self._on_frame)
        self.can_mgr.register_error_callback(self._on_can_error)
        self.can_mgr.register_status_callback(self._on_can_status)

        self._active_tab  = "config"
        self._tabs: dict  = {}
        self._clock_press_time = 0

        self._build_shell()
        self._build_tabs()
        self._switch_tab("config")

        self._update_clock()
        self._update_status_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Shell construction ───────────────────────────────────────────────────

    def _build_shell(self):
        # Status bar
        self._status_bar = tk.Frame(self, bg=STATUS_BG, height=STATUS_H)
        self._status_bar.pack(fill="x", side="top")
        self._status_bar.pack_propagate(False)

        # Clock (easter egg trigger)
        self._clock_lbl = tk.Label(
            self._status_bar, text="00:00:00",
            bg=STATUS_BG, fg=STATUS_FG,
            font=FONT_STATUS, padx=10)
        self._clock_lbl.pack(side="left")
        self._clock_lbl.bind("<ButtonPress-1>",   self._clock_press)
        self._clock_lbl.bind("<ButtonRelease-1>",  self._clock_release)

        # CAN status pill
        self._can_pill = tk.Label(
            self._status_bar, text="● Disconnected",
            bg=STATUS_BG, fg="#666056",
            font=FONT_STATUS, padx=8)
        self._can_pill.pack(side="left", padx=4)

        # Log indicator
        self._log_pill = tk.Label(
            self._status_bar, text="",
            bg=STATUS_BG, fg=GREEN,
            font=FONT_STATUS, padx=6)
        self._log_pill.pack(side="left")

        # FPS / frame rate
        self._fps_lbl = tk.Label(
            self._status_bar, text="",
            bg=STATUS_BG, fg="#666056",
            font=FONT_STATUS, padx=8)
        self._fps_lbl.pack(side="right", padx=4)

        # Tap anywhere on status bar for system settings
        for w in [self._status_bar, self._fps_lbl]:
            w.bind("<Button-1>", self._open_system_overlay)

        # Body: tab bar left + content right
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        # Tab bar
        self._tab_bar = tk.Frame(body, bg=TAB_BG, width=TAB_W)
        self._tab_bar.pack(side="left", fill="y")
        self._tab_bar.pack_propagate(False)
        self._tab_buttons: dict[str, tk.Frame] = {}
        self._build_tab_buttons()

        Divider(body, orient="v").pack(side="left", fill="y")

        # Content area
        self._content = tk.Frame(body, bg=BG)
        self._content.pack(side="left", fill="both", expand=True)

    def _build_tab_buttons(self):
        for (tab_id, icon, label) in TABS:
            btn = tk.Frame(self._tab_bar, bg=TAB_BG,
                           width=TAB_W, height=TAB_H,
                           cursor="hand2")
            btn.pack(fill="x")
            btn.pack_propagate(False)

            icon_lbl = tk.Label(btn, text=icon, bg=TAB_BG, fg=TAB_FG,
                                font=("TkDefaultFont", 18))
            icon_lbl.pack(pady=(14, 2))
            text_lbl = tk.Label(btn, text=label, bg=TAB_BG, fg=TAB_FG,
                                font=FONT_UI_XS, wraplength=64,
                                justify="center")
            text_lbl.pack()

            # Active indicator bar (left edge)
            bar = tk.Frame(btn, bg=TAB_BG, width=3)
            bar.place(x=0, y=0, relheight=1)

            self._tab_buttons[tab_id] = {
                "frame": btn, "icon": icon_lbl,
                "text": text_lbl, "bar": bar
            }
            for w in [btn, icon_lbl, text_lbl]:
                w.bind("<Button-1>", lambda e, t=tab_id: self._switch_tab(t))

    def _build_tabs(self):
        self._tabs["config"] = ConfigTab(self._content, self)
        self._tabs["raw"]    = RawLogTab(self._content, self)
        self._tabs["codes"]  = CodesTab(self._content, self)
        self._tabs["pids"]   = PIDsTab(self._content, self)

    def _switch_tab(self, tab_id: str):
        # Hide all
        for t in self._tabs.values():
            t.place_forget()

        # Show active
        self._tabs[tab_id].place(x=0, y=0, relwidth=1, relheight=1)
        self._active_tab = tab_id

        # Update tab bar highlight
        for tid, widgets in self._tab_buttons.items():
            active = tid == tab_id
            bg = TAB_BG if not active else "#363330"
            fg = TAB_ACT_FG if active else TAB_FG
            bar_color = ACCENT if active else TAB_BG
            widgets["frame"].configure(bg=bg)
            widgets["icon"].configure(bg=bg, fg=fg)
            widgets["text"].configure(bg=bg, fg=fg)
            widgets["bar"].configure(bg=bar_color)

    # ── Frame dispatch ───────────────────────────────────────────────────────

    def _on_frame(self, frame):
        """Called from CAN reader thread — dispatch to all tabs."""
        try:
            if frame.is_extended:
                priority, pgn, sa, da = decode_j1939_id(frame.arb_id)
                name = pgn_name(pgn)
            else:
                # Raw CAN — minimal decode
                pgn, sa, da, priority = frame.arb_id, 0, 0xFF, 0
                name = f"ID 0x{frame.arb_id:03X}"

            self._tabs["raw"].ingest_frame(frame, pgn, sa, da, priority, name)
            self._tabs["codes"].ingest_frame(frame, pgn, sa, da=da, priority=priority)
            self._tabs["pids"].ingest_frame(frame, pgn, sa, da=da, priority=priority)
        except Exception:
            pass

    def _on_can_error(self, msg: str):
        self.after(0, lambda: self._can_pill.configure(
            text=f"● {msg}", fg=RED))

    def _on_can_status(self, msg: str):
        self.after(0, lambda: self._update_can_pill())

    # ── Status bar updates ───────────────────────────────────────────────────

    def _update_clock(self):
        self._clock_lbl.configure(
            text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._update_clock)

    def _update_status_bar(self):
        self._update_can_pill()
        if self.can_mgr.fps > 0:
            self._fps_lbl.configure(
                text=f"{self.can_mgr.fps:.0f} fps  {self.can_mgr.frame_count} frames")
        else:
            self._fps_lbl.configure(text="")
        self.after(500, self._update_status_bar)

    def _update_can_pill(self):
        if self.can_mgr.connected:
            br  = self.can_mgr.bitrate // 1000
            txt = f"● {self.can_mgr.interface}  {br}k"
            self._can_pill.configure(text=txt, fg=GREEN)
        else:
            self._can_pill.configure(
                text="● Disconnected", fg="#666056")

    def update_status_log(self, logging: bool):
        if logging:
            self._log_pill.configure(text="⏺ REC", fg=RED)
        else:
            self._log_pill.configure(text="")

    # ── Easter egg: clock long-press ─────────────────────────────────────────

    def _clock_press(self, _=None):
        self._clock_press_time = time.time()

    def _clock_release(self, _=None):
        if time.time() - self._clock_press_time >= 2.0:
            launch_tetris(self, self)

    # ── System overlay ───────────────────────────────────────────────────────

    def _open_system_overlay(self, _=None):
        SystemOverlay(self, self)

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def _on_close(self):
        if self.log_mgr.logging:
            self.log_mgr.stop()
        self.can_mgr.disconnect()
        save_settings(self.settings)
        self.destroy()


if __name__ == "__main__":
    app = CANOpenerApp()
    app.mainloop()
