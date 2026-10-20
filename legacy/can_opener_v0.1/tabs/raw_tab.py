"""
Raw / Log tab — live frame stream with node filter and logging controls.
Uses ttk.Treeview for the frame table (far cheaper than canvas+Label rows).
Frames are batched and throttled so the Pi 3B+ stays responsive.
"""
import tkinter as tk
from tkinter import ttk
import time
import queue
import shutil
from datetime import datetime
from pathlib import Path
from collections import OrderedDict
from typing import Set

from core.theme import *
from core.theme import touch_alert
from core.j1939_defs import pgn_name, sa_name

MAX_ROWS    = 200    # rows kept in Treeview
BATCH_MS    = 120    # UI drain interval — don't go below 100 on Pi 3B+
MAX_BATCH   = 30     # max rows inserted per drain cycle


class RawLogTab(tk.Frame):
    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.app = app
        self._nodes: OrderedDict       = OrderedDict()
        self._selected_nodes: Set[int] = set()
        self._row_queue: queue.Queue   = queue.Queue(maxsize=500)
        self._auto_scroll              = True
        self._row_parity               = 0
        self._build()
        self._schedule_drain()

    def _build(self):
        # ── Top bar ────────────────────────────────────────────────────────────
        top = tk.Frame(self, bg=PANEL, height=48)
        top.pack(fill="x")
        top.pack_propagate(False)

        self._log_btn = FlatButton(
            top, text="▶  Start Log",
            bg=GREEN, fg="white", font=FONT_BOLD_S,
            pad_x=14, pad_y=6, command=self._toggle_log)
        self._log_btn.pack(side="left", padx=(10,4), pady=8)

        FlatButton(top, text="New Log",
                   bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
                   pad_x=12, pad_y=6,
                   command=self._new_log).pack(side="left", padx=(0,12), pady=8)

        self._log_path_lbl = tk.Label(
            top, text="—", bg=PANEL, fg=TEXT_DIM,
            font=FONT_UI_XS, anchor="w")
        self._log_path_lbl.pack(side="left", fill="x", expand=True)

        FlatButton(top, text="⏏ Dump to USB",
                   bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
                   pad_x=12, pad_y=6,
                   command=self._dump_to_usb).pack(side="right", padx=10, pady=8)

        self._scroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Auto-scroll",
                       variable=self._scroll_var,
                       bg=PANEL, fg=TEXT, activebackground=PANEL,
                       font=FONT_UI_XS,
                       command=lambda: setattr(
                           self, "_auto_scroll", self._scroll_var.get())
                       ).pack(side="right", padx=(0,4))

        # ── Body ───────────────────────────────────────────────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        # Node column
        node_col = tk.Frame(body, bg=PANEL, width=130)
        node_col.pack(side="left", fill="y")
        node_col.pack_propagate(False)

        tk.Label(node_col, text="NODES", bg=PANEL, fg=TEXT_DIM,
                 font=FONT_UI_XS, pady=6).pack(fill="x")
        Divider(node_col).pack(fill="x")

        self._node_list_frame = tk.Frame(node_col, bg=PANEL)
        self._node_list_frame.pack(fill="both", expand=True)

        FlatButton(node_col, text="Clear Filter",
                   bg=PANEL_DARK, fg=TEXT_DIM, font=FONT_UI_XS,
                   pad_x=8, pad_y=4,
                   command=self._clear_node_filter).pack(
                       fill="x", padx=4, pady=4)

        Divider(body, orient="v").pack(side="left", fill="y")

        # ── Treeview ───────────────────────────────────────────────────────────
        table_frame = tk.Frame(body, bg=BG)
        table_frame.pack(side="left", fill="both", expand=True)

        cols = ("time", "pgn", "name", "source", "len", "data")
        self._tree = ttk.Treeview(
            table_frame, columns=cols,
            show="headings", selectmode="browse")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("CAN.Treeview",
                        background=ROW_EVEN,
                        foreground=TEXT,
                        fieldbackground=ROW_EVEN,
                        font=FONT_MONO_S,
                        rowheight=22)
        style.configure("CAN.Treeview.Heading",
                        background=PANEL_DARK,
                        foreground=TEXT_DIM,
                        font=FONT_UI_XS,
                        relief="flat")
        style.map("CAN.Treeview",
                  background=[("selected", ACCENT_LT)],
                  foreground=[("selected", TEXT)])
        self._tree.configure(style="CAN.Treeview")
        self._tree.tag_configure("odd",  background=ROW_ODD)
        self._tree.tag_configure("even", background=ROW_EVEN)

        col_cfg = {
            "time":   ("Timestamp",  90, "w"),
            "pgn":    ("PGN",        68, "w"),
            "name":   ("Name",      115, "w"),
            "source": ("Source",     95, "w"),
            "len":    ("Len",        36, "center"),
            "data":   ("Data",      260, "w"),
        }
        for col, (heading, width, anchor) in col_cfg.items():
            self._tree.heading(col, text=heading, anchor="w")
            self._tree.column(col, width=width, minwidth=width,
                              anchor=anchor, stretch=(col == "data"))

        vsb = ttk.Scrollbar(table_frame, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

    # ── Frame ingestion ──────────────────────────────────────────────────────

    def ingest_frame(self, frame, pgn: int, sa: int, da: int,
                     priority: int, name: str):
        self._update_node(sa)
        self.app.log_mgr.write_frame(frame, pgn, sa, da, priority, name)
        if self._selected_nodes and sa not in self._selected_nodes:
            return
        ts       = datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3]
        data_hex = " ".join(f"{b:02X}" for b in frame.data)
        try:
            self._row_queue.put_nowait(
                (ts, f"0x{pgn:04X}", name, sa_name(sa),
                 len(frame.data), data_hex))
        except queue.Full:
            pass  # drop rather than block the CAN thread

    def _update_node(self, sa: int):
        if sa not in self._nodes:
            self._nodes[sa] = {"sa": sa, "count": 0}
            self.after(0, self._refresh_node_list)
        self._nodes[sa]["count"] += 1

    def _refresh_node_list(self):
        for w in self._node_list_frame.winfo_children():
            w.destroy()
        for sa in self._nodes:
            selected = sa in self._selected_nodes
            bg = ACCENT_LT if selected else PANEL
            fg = ACCENT    if selected else TEXT
            lbl = tk.Label(
                self._node_list_frame,
                text=f"0x{sa:02X}  {sa_name(sa)[:9]}",
                bg=bg, fg=fg, font=FONT_MONO_S,
                padx=6, pady=6, anchor="w", cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, s=sa: self._toggle_node(s))

    def _toggle_node(self, sa: int):
        if sa in self._selected_nodes:
            self._selected_nodes.discard(sa)
        else:
            self._selected_nodes.add(sa)
        self._refresh_node_list()

    def _clear_node_filter(self):
        self._selected_nodes.clear()
        self._refresh_node_list()

    # ── Batched drain ────────────────────────────────────────────────────────

    def _schedule_drain(self):
        self._drain_queue()
        self.after(BATCH_MS, self._schedule_drain)

    def _drain_queue(self):
        items = []
        try:
            while len(items) < MAX_BATCH:
                items.append(self._row_queue.get_nowait())
        except queue.Empty:
            pass
        if not items:
            return

        for row in items:
            tag = "even" if self._row_parity % 2 == 0 else "odd"
            self._tree.insert("", "end", values=row, tags=(tag,))
            self._row_parity += 1

        # Prune oldest rows to cap memory
        children = self._tree.get_children()
        excess   = len(children) - MAX_ROWS
        if excess > 0:
            for iid in children[:excess]:
                self._tree.delete(iid)

        if self._auto_scroll:
            self._tree.yview_moveto(1.0)

    # ── Log controls ─────────────────────────────────────────────────────────

    def _toggle_log(self):
        if self.app.log_mgr.logging:
            self.app.log_mgr.stop()
            self._log_btn.configure(text="▶  Start Log")
            self._log_btn.set_bg(GREEN)
            self.app.update_status_log(False)
        else:
            self.app.log_mgr.start()
            self._log_btn.configure(text="⏹  Stop Log")
            self._log_btn.set_bg(RED)
            self.app.update_status_log(True)
        self._log_path_lbl.configure(text=self.app.log_mgr.current_path)

    def _new_log(self):
        self.app.log_mgr.new_log()
        if not self.app.log_mgr.logging:
            self.app.log_mgr.start(new_file=True)
            self._log_btn.configure(text="⏹  Stop Log")
            self._log_btn.set_bg(RED)
            self.app.update_status_log(True)
        self._log_path_lbl.configure(text=self.app.log_mgr.current_path)

    def _dump_to_usb(self):
        from core.settings import list_usb_drives
        drives = list_usb_drives()
        if not drives:
            touch_alert(self.winfo_toplevel(),
                        "No USB drives detected.\nInsert a drive and try again.",
                        title="USB Dump")
            return

        top = modal_overlay(self.winfo_toplevel(), "Dump Logs to USB", 420, 260)
        tk.Label(top, text="Select destination drive:",
                 bg=BG, fg=TEXT, font=FONT_BOLD).pack(pady=(16,8))

        drive_var = tk.StringVar()
        for d in drives:
            tk.Radiobutton(
                top, text=f"{d['label']}  ({d['path']})",
                variable=drive_var, value=d["path"],
                bg=BG, fg=TEXT, activebackground=BG,
                font=FONT_UI_S).pack(anchor="w", padx=20)
        if drives:
            drive_var.set(drives[0]["path"])

        tk.Label(top, text="Copy:", bg=BG, fg=TEXT_DIM,
                 font=FONT_UI_XS).pack(pady=(12,4))
        scope_var = tk.StringVar(value="session")
        fr = tk.Frame(top, bg=BG)
        fr.pack()
        for val, lbl in [("session", "This Session"), ("all", "All Logs")]:
            tk.Radiobutton(fr, text=lbl, variable=scope_var, value=val,
                           bg=BG, fg=TEXT, activebackground=BG,
                           font=FONT_UI_S).pack(side="left", padx=12)

        def _do_copy():
            dst_root = drive_var.get()
            if not dst_root:
                return
            src = (self.app.log_mgr._session_dir
                   if scope_var.get() == "session"
                   else Path(self.app.settings.get(
                       "log_path", "~/can_logs")).expanduser())
            dst = Path(dst_root) / "can_logs"
            try:
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
                top.destroy()
                touch_alert(self.winfo_toplevel(), f"Copied to {dst}", title="USB Dump")
            except Exception as e:
                top.destroy()
                touch_alert(self.winfo_toplevel(), str(e), title="USB Dump Error")

        FlatButton(top, text="Copy", bg=ACCENT, fg="white",
                   font=FONT_BOLD, command=_do_copy).pack(pady=12)
