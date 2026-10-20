"""
Config tab — CAN interface configuration and saved profiles.
Selectors are inline expanding panels — no Toplevel popups at all.
"""
import tkinter as tk
import threading
import subprocess
import os

from core.theme import *
from core.theme import modal_overlay

BITRATE_OPTIONS = ["Auto", "125000", "250000", "500000", "1000000"]
BITRATE_LABELS  = ["Auto", "125k",   "250k",   "500k",   "1000k"]
PROTOCOL_OPTIONS = ["Auto", "J1939", "Raw CAN"]


def _get_available_interfaces() -> list:
    """Poll /proc/net/dev for real can* interfaces, always append sim."""
    ifaces = []
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                name = line.split(":")[0].strip()
                if name.startswith("can") or name.startswith("vcan"):
                    ifaces.append(name)
    except Exception:
        pass
    if not ifaces:
        ifaces = ["can0"]   # sensible default if nothing found
    ifaces.append("sim")
    return ifaces


class InlineSelector(tk.Frame):
    """
    Touch-friendly inline selector — tap the value label to expand/collapse
    a list of options directly in the UI. Zero Toplevel windows.
    """
    def __init__(self, parent, label: str, options: list,
                 labels: list, var: tk.StringVar,
                 on_change=None, bg=BG, **kw):
        super().__init__(parent, bg=bg, **kw)
        self._opts   = options
        self._labels    = labels
        self._var       = var
        self._on_change = on_change
        self._open      = False
        self._bg        = bg

        # Header row: label + current value button
        hdr = tk.Frame(self, bg=bg)
        hdr.pack(fill="x")

        tk.Label(hdr, text=label, bg=bg, fg=TEXT_MED,
                 font=FONT_UI_S, width=10, anchor="w").pack(side="left")

        self._val_btn = tk.Label(
            hdr, textvariable=var,
            bg=CARD, fg=TEXT, font=FONT_BOLD,
            padx=12, pady=8, cursor="hand2",
            relief="flat", anchor="w")
        self._val_btn.pack(side="left", fill="x", expand=True)

        self._arrow = tk.Label(hdr, text="▾", bg=CARD, fg=TEXT_DIM,
                               font=FONT_BOLD, padx=8, pady=8)
        self._arrow.pack(side="left")

        self._val_btn.bind("<Button-1>", lambda e: self._toggle())
        self._arrow.bind("<Button-1>",   lambda e: self._toggle())

        # Option list — hidden until expanded
        self._list_frame = tk.Frame(self, bg=CARD,
                                    relief="flat", bd=0)
        # Not packed yet — shown on expand

        self._rebuild_options()
        var.trace_add("write", self._on_var_change)

    def _rebuild_options(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        for val, lbl in zip(self._opts, self._labels):
            selected = (self._var.get() == val)
            bg = ACCENT_LT if selected else CARD
            fg = ACCENT    if selected else TEXT
            row = tk.Label(
                self._list_frame, text=lbl,
                bg=bg, fg=fg, font=FONT_BOLD,
                padx=28, pady=10, anchor="w",
                cursor="hand2")
            row.pack(fill="x")
            row.bind("<Button-1>", lambda e, v=val: self._select(v))

    def _select(self, val: str):
        self._var.set(val)
        self._collapse()
        if self._on_change:
            self._on_change(val)

    def _toggle(self):
        if self._open:
            self._collapse()
        else:
            self._expand()

    def _expand(self):
        self._rebuild_options()
        self._list_frame.pack(fill="x", padx=(80, 0))
        self._arrow.configure(text="▴")
        self._open = True

    def _collapse(self):
        self._list_frame.pack_forget()
        self._arrow.configure(text="▾")
        self._open = False

    def _on_var_change(self, *_):
        self._rebuild_options()

    def refresh_options(self, options: list, labels: list):
        """Hot-reload the option list (used by interface selector to re-poll)."""
        self._opts = options
        self._labels  = labels
        # If current value no longer in list, reset to first option
        if self._var.get() not in options and options:
            self._var.set(options[0])
        self._rebuild_options()

    def close(self):
        self._collapse()


class ConfigTab(tk.Frame):
    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.app = app
        self._selectors: list[InlineSelector] = []
        self._build()

    def _build(self):
        # ── Left column ────────────────────────────────────────────────────────
        left = tk.Frame(self, bg=BG, width=340)
        left.pack(side="left", fill="y", padx=(20,10), pady=20)
        left.pack_propagate(False)

        SectionLabel(left, text="CAN Interface", bg=BG).pack(
            anchor="w", pady=(0,8))

        # Interface
        self._iface_var = tk.StringVar(
            value=self.app.settings.get("interface", "can0"))
        self._iface_sel = InlineSelector(
            left, "Interface",
            _get_available_interfaces(),
            _get_available_interfaces(),
            self._iface_var,
            on_change=self._collapse_others,
            bg=BG)
        self._iface_sel.pack(fill="x", pady=(0,6))
        self._selectors.append(self._iface_sel)

        # Refresh interfaces button (re-polls /proc/net/dev)
        FlatButton(left, text="↺ Refresh Interfaces",
                   bg=PANEL_DARK, fg=TEXT_DIM, font=FONT_UI_XS,
                   pad_x=8, pad_y=3,
                   command=self._refresh_interfaces
                   ).pack(anchor="e", pady=(0,10))

        # Baud rate
        self._bitrate_var = tk.StringVar(
            value=self.app.settings.get("bitrate", "250000"))
        br_sel = InlineSelector(
            left, "Baud Rate",
            BITRATE_OPTIONS, BITRATE_LABELS,
            self._bitrate_var,
            on_change=self._collapse_others,
            bg=BG)
        br_sel.pack(fill="x", pady=(0,6))
        self._selectors.append(br_sel)

        # Protocol
        self._proto_var = tk.StringVar(
            value=self.app.settings.get("protocol", "J1939"))
        proto_sel = InlineSelector(
            left, "Protocol",
            PROTOCOL_OPTIONS, PROTOCOL_OPTIONS,
            self._proto_var,
            on_change=self._collapse_others,
            bg=BG)
        proto_sel.pack(fill="x", pady=(0,6))
        self._selectors.append(proto_sel)

        Divider(left).pack(fill="x", pady=(8,16))

        # Connect button + status
        self._connect_btn = FlatButton(
            left, text="Connect",
            bg=GREEN, fg="white", font=FONT_BIG,
            command=self._on_connect, pad_x=20, pad_y=10)
        self._connect_btn.pack(fill="x", pady=(0,8))

        self._conn_status = tk.Label(
            left, text="Not connected", bg=BG, fg=TEXT_DIM,
            font=FONT_UI_S, wraplength=300, justify="left")
        self._conn_status.pack(anchor="w")

        # Log path
        Divider(left).pack(fill="x", pady=16)
        SectionLabel(left, text="Logging", bg=BG).pack(anchor="w", pady=(0,6))
        lp_row = tk.Frame(left, bg=BG)
        lp_row.pack(fill="x")
        self._logpath_var = tk.StringVar(
            value=self.app.settings.get("log_path", "~/can_logs"))
        tk.Label(lp_row, textvariable=self._logpath_var,
                 bg=CARD, fg=TEXT_DIM, font=FONT_UI_XS,
                 padx=8, pady=4, anchor="w"
                 ).pack(side="left", fill="x", expand=True)
        FlatButton(lp_row, text="Open", bg=PANEL_DARK, fg=TEXT,
                   font=FONT_UI_XS, pad_x=8, pad_y=4,
                   command=self._open_log_folder
                   ).pack(side="left", padx=(4,0))

        # ── Right column: saved configs ────────────────────────────────────────
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True,
                   padx=(10,20), pady=20)

        hdr = tk.Frame(right, bg=BG)
        hdr.pack(fill="x", pady=(0,8))
        SectionLabel(hdr, text="Saved Configurations", bg=BG).pack(side="left")
        FlatButton(hdr, text="+ Save Current",
                   bg=ACCENT, fg="white", font=FONT_UI_XS,
                   pad_x=10, pad_y=4,
                   command=self._save_current).pack(side="right")

        list_frame = tk.Frame(right, bg=CARD, relief="flat")
        list_frame.pack(fill="both", expand=True)
        self._saved_list = tk.Frame(list_frame, bg=CARD)
        self._saved_list.pack(fill="both", expand=True, padx=2, pady=2)
        self._refresh_saved()

    # ── Selector helpers ─────────────────────────────────────────────────────

    def _collapse_others(self, _val=None):
        """When any selector opens, close the others."""
        for sel in self._selectors:
            sel.close()

    def _refresh_interfaces(self):
        ifaces = _get_available_interfaces()
        self._iface_sel.refresh_options(ifaces, ifaces)

    # ── Connect ──────────────────────────────────────────────────────────────

    def _on_connect(self):
        # Close any open selectors first
        for sel in self._selectors:
            sel.close()

        if self.app.can_mgr.connected:
            self.app.can_mgr.disconnect()
            self._connect_btn.configure(text="Connect")
            self._connect_btn.set_bg(GREEN)
            self._conn_status.configure(text="Disconnected", fg=TEXT_DIM)
            self.app.settings["last_connected"] = False
            return

        iface   = self._iface_var.get()
        bitrate = self._bitrate_var.get()
        proto   = self._proto_var.get()
        auto    = bitrate == "Auto"
        br      = 250000 if auto else int(bitrate)

        self._connect_btn.configure(text="Connecting…")
        self._connect_btn.set_bg(BORDER)
        self._conn_status.configure(text="Bringing up interface…", fg=YELLOW)
        self.update()

        def _do_connect():
            ok, msg = self.app.can_mgr.connect(
                iface, br, proto, auto_baud=auto)
            self.after(0, lambda: self._connect_done(
                ok, msg, iface, bitrate, proto))

        threading.Thread(target=_do_connect, daemon=True).start()

    def _connect_done(self, ok: bool, msg: str,
                      iface: str, bitrate: str, proto: str):
        if ok:
            self._connect_btn.configure(text="Disconnect")
            self._connect_btn.set_bg(RED)
            self._conn_status.configure(text=msg, fg=GREEN)
            self.app.settings.update({
                "interface": iface,
                "bitrate":   bitrate,
                "protocol":  proto,
                "last_connected": True,
            })
            from core.settings import save_settings
            save_settings(self.app.settings)
        else:
            self._connect_btn.configure(text="Connect")
            self._connect_btn.set_bg(GREEN)
            self._conn_status.configure(text=f"Failed: {msg}", fg=RED)

    def set_connection_status(self, connected: bool, msg: str = ""):
        if connected:
            self._connect_btn.configure(text="Disconnect")
            self._connect_btn.set_bg(RED)
            self._conn_status.configure(text=msg, fg=GREEN)
        else:
            self._connect_btn.configure(text="Connect")
            self._connect_btn.set_bg(GREEN)
            self._conn_status.configure(
                text=msg or "Not connected", fg=TEXT_DIM)

    # ── Saved configs ─────────────────────────────────────────────────────────

    def _save_current(self):
        top = modal_overlay(self.winfo_toplevel(), "Save Config", 320, 170)

        hdr = tk.Frame(top, bg=PANEL_DARK, height=36)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Save Configuration", bg=PANEL_DARK, fg=TEXT,
                 font=FONT_BOLD_S, padx=12).pack(side="left", pady=6)
        close_l = tk.Label(hdr, text="  ✕  ", bg=PANEL_DARK, fg=TEXT_DIM,
                           font=FONT_BOLD, cursor="hand2")
        close_l.pack(side="right")
        close_l.bind("<Button-1>", lambda e: top.destroy())

        tk.Label(top, text="Config name:", bg=BG, fg=TEXT_MED,
                 font=FONT_UI_S).pack(anchor="w", padx=16, pady=(12,2))
        name_var = tk.StringVar(value=(
            f"{self._iface_var.get()} {self._bitrate_var.get()} "
            f"{self._proto_var.get()}"))
        entry = tk.Entry(top, textvariable=name_var, font=FONT_UI,
                         bg=CARD, fg=TEXT, relief="flat",
                         insertbackground=TEXT)
        entry.pack(fill="x", padx=16, pady=(0,12))
        entry.select_range(0, "end")
        entry.focus_set()

        def _save():
            n = name_var.get().strip()
            if not n:
                return
            cfg = {
                "name":      n,
                "interface": self._iface_var.get(),
                "bitrate":   self._bitrate_var.get(),
                "protocol":  self._proto_var.get(),
            }
            self.app.settings.setdefault("saved_configs", []).append(cfg)
            from core.settings import save_settings
            save_settings(self.app.settings)
            self._refresh_saved()
            top.destroy()

        FlatButton(top, text="Save", bg=ACCENT, fg="white",
                   font=FONT_BOLD, command=_save).pack(pady=(0,8))

    def _refresh_saved(self):
        for w in self._saved_list.winfo_children():
            w.destroy()
        saved = self.app.settings.get("saved_configs", [])
        if not saved:
            tk.Label(self._saved_list, text="No saved configurations",
                     bg=CARD, fg=TEXT_DIM, font=FONT_UI_S).pack(pady=20)
            return
        for i, cfg in enumerate(saved):
            row = tk.Frame(self._saved_list,
                           bg=ROW_EVEN if i % 2 == 0 else ROW_ODD)
            row.pack(fill="x")
            tk.Label(row, text=cfg["name"], bg=row["bg"], fg=TEXT,
                     font=FONT_BOLD_S, padx=10, pady=6,
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(row,
                     text=f"{cfg['interface']}  {cfg['bitrate']}  {cfg['protocol']}",
                     bg=row["bg"], fg=TEXT_DIM,
                     font=FONT_UI_XS).pack(side="left", padx=8)
            FlatButton(row, text="Load", bg=ACCENT, fg="white",
                       font=FONT_UI_XS, pad_x=8, pad_y=3,
                       command=lambda c=cfg: self._load_config(c)
                       ).pack(side="left", padx=2, pady=4)
            FlatButton(row, text="✕", bg=RED, fg="white",
                       font=FONT_UI_XS, pad_x=8, pad_y=3,
                       command=lambda idx=i: self._delete_config(idx)
                       ).pack(side="left", padx=(0,6), pady=4)

    def _load_config(self, cfg: dict):
        self._iface_var.set(cfg["interface"])
        self._bitrate_var.set(cfg["bitrate"])
        self._proto_var.set(cfg["protocol"])

    def _delete_config(self, idx: int):
        self.app.settings["saved_configs"].pop(idx)
        from core.settings import save_settings
        save_settings(self.app.settings)
        self._refresh_saved()

    def _open_log_folder(self):
        import subprocess
        path = self.app.settings.get("log_path", "~/can_logs")
        subprocess.Popen(["xdg-open", os.path.expanduser(path)])
