"""
System settings overlay — shown when the status bar is tapped.
Covers: Wi-Fi status, brightness, shutdown/reboot, about, log folder.
"""
import tkinter as tk
import subprocess
import os
import time
from pathlib import Path
from datetime import datetime, timedelta

from core.theme import *
from core.theme import touch_confirm, touch_alert


class SystemOverlay(tk.Toplevel):
    def __init__(self, root, app):
        super().__init__(root)
        self.app = app
        self.overrideredirect(True)
        self.configure(bg=BG)
        # Position full-width at top of root
        rw = root.winfo_width()
        rx = root.winfo_rootx()
        ry = root.winfo_rooty() + 32   # below status bar
        h  = 320
        self.geometry(f"{rw}x{h}+{rx}+{ry}")
        self.lift()
        self._build()

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=STATUS_BG, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="System", bg=STATUS_BG, fg=STATUS_FG,
                 font=FONT_BOLD).pack(side="left", padx=16, pady=8)

        close_x = tk.Label(hdr, text="  ✕  ", bg=STATUS_BG, fg=STATUS_FG,
                            font=FONT_BOLD, cursor="hand2")
        close_x.pack(side="right")
        close_x.bind("<Button-1>", lambda e: self._close())

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=12)

        # ── Left: system info ──────────────────────────────────────────────────
        left = tk.Frame(body, bg=BG, width=240)
        left.pack(side="left", fill="y", padx=(0,16))
        left.pack_propagate(False)

        SectionLabel(left, text="Status", bg=BG).pack(anchor="w", pady=(0,6))
        self._info_lbl = tk.Label(
            left, text=self._get_system_info(),
            bg=CARD, fg=TEXT, font=FONT_UI_XS,
            justify="left", padx=10, pady=8, anchor="nw",
            wraplength=220)
        self._info_lbl.pack(fill="x")

        # ── Middle: brightness + logs ──────────────────────────────────────────
        mid = tk.Frame(body, bg=BG, width=190)
        mid.pack(side="left", fill="y", padx=(0,16))
        mid.pack_propagate(False)

        SectionLabel(mid, text="Brightness", bg=BG).pack(anchor="w", pady=(0,4))
        self._bright_var = tk.IntVar(value=100)
        tk.Scale(mid, from_=10, to=100, orient="horizontal",
                 variable=self._bright_var,
                 bg=BG, fg=TEXT, troughcolor=BORDER,
                 highlightthickness=0, bd=0,
                 command=self._set_brightness).pack(fill="x")

        SectionLabel(mid, text="Units", bg=BG).pack(anchor="w", pady=(12,4))
        self._imperial_var = tk.BooleanVar(
            value=self.app.settings.get("imperial", False))
        units_row = tk.Frame(mid, bg=BG)
        units_row.pack(fill="x")
        for label, val in [("Metric", False), ("Imperial", True)]:
            rb = tk.Radiobutton(
                units_row, text=label, variable=self._imperial_var,
                value=val, bg=BG, fg=TEXT, activebackground=BG,
                selectcolor=CARD, font=FONT_UI_S,
                command=self._apply_units)
            rb.pack(side="left", padx=(0,8))

        SectionLabel(mid, text="Log Folder", bg=BG).pack(anchor="w", pady=(12,4))
        FlatButton(mid, text="Open in Files",
                   bg=PANEL_DARK, fg=TEXT, font=FONT_UI_XS,
                   pad_x=10, pad_y=6,
                   command=self._open_logs).pack(anchor="w")

        # ── Right: app + power ─────────────────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="y")

        SectionLabel(right, text="App", bg=BG).pack(anchor="w", pady=(0,6))
        FlatButton(right, text="✕  Close App",
                   bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD,
                   pad_x=16, pad_y=10,
                   command=self._close_app).pack(fill="x", pady=(0,12))

        SectionLabel(right, text="Power", bg=BG).pack(anchor="w", pady=(0,6))
        FlatButton(right, text="⟳  Reboot",
                   bg=YELLOW_LT, fg=YELLOW, font=FONT_BOLD,
                   pad_x=16, pad_y=10,
                   command=self._reboot).pack(fill="x", pady=(0,6))
        FlatButton(right, text="⏻  Shutdown",
                   bg=RED_LT, fg=RED, font=FONT_BOLD,
                   pad_x=16, pad_y=10,
                   command=self._shutdown).pack(fill="x")

        tk.Label(self,
                 text="CAN Opener v0.1  —  Long-press clock for something fun",
                 bg=BG, fg=TEXT_DIM, font=FONT_UI_XS).pack(pady=(0,6))

    def _get_system_info(self) -> str:
        lines = []
        # Uptime
        try:
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            td = timedelta(seconds=int(up))
            h, rem = divmod(td.seconds, 3600)
            m = rem // 60
            lines.append(f"Uptime:  {td.days}d {h:02d}h {m:02d}m")
        except Exception:
            lines.append("Uptime:  —")

        # CPU temp
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read()) / 1000
            lines.append(f"CPU Temp: {temp:.1f}°C")
        except Exception:
            pass

        # Log disk usage
        try:
            log_path = Path(self.app.settings.get(
                "log_path","~/can_logs")).expanduser()
            if log_path.exists():
                total = sum(f.stat().st_size for f in log_path.rglob("*")
                            if f.is_file())
                lines.append(f"Logs: {total/1024:.0f} KB")
        except Exception:
            pass

        # Wi-Fi
        try:
            result = subprocess.run(
                ["iwgetid", "-r"], capture_output=True, text=True, timeout=2)
            ssid = result.stdout.strip()
            lines.append(f"Wi-Fi: {ssid if ssid else 'Not connected'}")
        except Exception:
            lines.append("Wi-Fi: —")

        return "\n".join(lines)

    def _apply_units(self):
        imperial = self._imperial_var.get()
        self.app.settings["imperial"] = imperial
        from core.j1939_defs import set_units
        from core.settings import save_settings
        set_units(imperial)
        save_settings(self.app.settings)

    def _set_brightness(self, val):
        # Raspberry Pi DSI backlight
        try:
            bl = int(int(val) * 255 / 100)
            with open("/sys/class/backlight/rpi_backlight/brightness", "w") as f:
                f.write(str(bl))
        except Exception:
            pass

    def _open_logs(self):
        path = self.app.settings.get("log_path","~/can_logs")
        subprocess.Popen(["xdg-open", os.path.expanduser(path)])
        self._close()

    def _reboot(self):
        touch_confirm(
            self.master,
            "Reboot now?",
            confirm_text="Reboot",
            confirm_bg=YELLOW,
            on_confirm=lambda: subprocess.Popen(["sudo", "reboot"]))
        self._close()

    def _shutdown(self):
        touch_confirm(
            self.master,
            "Shut down now?",
            confirm_text="Shut Down",
            confirm_bg=RED,
            on_confirm=lambda: subprocess.Popen(["sudo", "shutdown", "-h", "now"]))
        self._close()

    def _close_app(self):
        touch_confirm(
            self.master,
            "Close CAN Opener?",
            confirm_text="Close",
            confirm_bg=PANEL_DARK,
            on_confirm=self.app._on_close)
        self._close()

    def _close(self):
        self.destroy()
