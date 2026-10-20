"""
PIDs tab — live SPN sensor data, grouped by SA.
Only shows SPNs seen on the bus. Static labels, live values.
Select mode filters to chosen SPNs before starting live view.
"""
import tkinter as tk
from tkinter import ttk
import time
from collections import defaultdict

from core.theme import *
from core.theme import touch_alert
from core.j1939_defs import (extract_spn_readings, spn_info, sa_name,
                              PGN_SPN_MAP, SPN_DEFS)

# How often to update the display (ms)
UPDATE_MS = 250


class PIDsTab(tk.Frame):
    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.app = app
        # (sa, spn) -> {value_str, raw, last_seen, label_widget, value_widget}
        self._live: dict[tuple, dict] = {}
        self._selected: set[tuple]    = set()   # (sa, spn) filter; empty=all
        self._running  = False
        self._build()
        self._schedule_update()

    def _build(self):
        # Top bar
        top = tk.Frame(self, bg=PANEL, height=48)
        top.pack(fill="x")
        top.pack_propagate(False)

        self._select_btn = FlatButton(
            top, text="☰ Select PIDs",
            bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
            pad_x=14, pad_y=6,
            command=self._open_selector)
        self._select_btn.pack(side="left", padx=(10,4), pady=8)

        self._start_btn = FlatButton(
            top, text="▶  Start",
            bg=GREEN, fg="white", font=FONT_BOLD_S,
            pad_x=14, pad_y=6,
            command=self._toggle_running)
        self._start_btn.pack(side="left", padx=(0,12), pady=8)

        self._filter_lbl = tk.Label(
            top, text="Showing all live SPNs",
            bg=PANEL, fg=TEXT_DIM, font=FONT_UI_XS)
        self._filter_lbl.pack(side="left")

        self._spn_count_lbl = tk.Label(
            top, text="0 SPNs", bg=PANEL, fg=TEXT_DIM, font=FONT_UI_XS)
        self._spn_count_lbl.pack(side="right", padx=12)

        # Scrollable PID grid
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._grid  = tk.Frame(self._canvas, bg=BG)
        win = self._canvas.create_window((0,0), window=self._grid, anchor="nw")
        self._grid.bind("<Configure>",
                        lambda e: self._canvas.configure(
                            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(win, width=e.width))

        # Touch scroll — track drag across the whole canvas including children
        self._touch_y = [0]
        self._touch_dragging = [False]

        def _touch_start(e):
            self._touch_y[0] = e.y_root
            self._touch_dragging[0] = False

        def _touch_drag(e):
            dy = self._touch_y[0] - e.y_root
            self._touch_y[0] = e.y_root
            if abs(dy) > 1:
                self._touch_dragging[0] = True
                self._canvas.yview_scroll(int(dy / 6), "units")

        self._canvas.bind("<ButtonPress-1>",  _touch_start)
        self._canvas.bind("<B1-Motion>",       _touch_drag)

        # Store callbacks so _bind_scroll_to_children can use them
        self._scroll_start_cb = _touch_start
        self._scroll_drag_cb  = _touch_drag

    # ── Frame ingestion ──────────────────────────────────────────────────────

    def ingest_frame(self, frame, pgn: int, sa: int, **_):
        # Always collect SPNs passively so Select PIDs has data to show.
        # Value updates only pushed to the display when running.
        readings = extract_spn_readings(pgn, frame.data)
        for (spn, raw, display) in readings:
            key = (sa, spn)
            if key not in self._live:
                info = spn_info(spn)
                name = info[0] if info else f"SPN {spn}"
                unit = info[1] if info else ""
                self._live[key] = {
                    "sa":    sa,
                    "spn":   spn,
                    "name":  name,
                    "unit":  unit,
                    "value": display,
                    "raw":   raw,
                    "last":  time.time(),
                    "lbl_w": None,
                    "val_w": None,
                    "age_w": None,
                }
                if self._running:
                    self.after(0, self._rebuild_grid)
            elif self._running:
                self._live[key]["value"] = display
                self._live[key]["raw"]   = raw
                self._live[key]["last"]  = time.time()

    # ── Grid rendering ───────────────────────────────────────────────────────

    def _rebuild_grid(self):
        for w in self._grid.winfo_children():
            w.destroy()

        # Filter
        keys = list(self._live.keys())
        if self._selected:
            keys = [k for k in keys if k in self._selected]

        if not keys:
            tk.Label(self._grid, text="No live SPNs detected",
                     bg=BG, fg=TEXT_DIM, font=FONT_UI_S,
                     pady=30).pack()
            self._spn_count_lbl.configure(text="0 SPNs")
            return

        # Group by SA
        by_sa = defaultdict(list)
        for k in keys:
            by_sa[k[0]].append(k)

        for sa in sorted(by_sa.keys()):
            # Node header
            hdr = tk.Frame(self._grid, bg=PANEL_DARK)
            hdr.pack(fill="x", pady=(8,0))
            tk.Label(hdr,
                     text=f"0x{sa:02X}  {sa_name(sa)}",
                     bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
                     padx=10, pady=4).pack(side="left")

            # SPN rows — 2-column grid
            inner = tk.Frame(self._grid, bg=BG)
            inner.pack(fill="x", padx=4)

            col = 0
            for k in sorted(by_sa[sa], key=lambda x: x[1]):
                entry = self._live[k]
                cell_bg = ROW_EVEN if col % 4 < 2 else ROW_ODD
                cell = tk.Frame(inner, bg=cell_bg,
                                relief="flat", bd=0,
                                width=360, height=52)
                cell.grid(row=col//2, column=col%2,
                          sticky="ew", padx=2, pady=2)
                cell.pack_propagate(False)
                inner.columnconfigure(0, weight=1)
                inner.columnconfigure(1, weight=1)

                name_lbl = tk.Label(cell,
                                    text=f"{entry['name']}",
                                    bg=cell_bg, fg=TEXT_DIM,
                                    font=FONT_UI_XS, anchor="w",
                                    padx=8, pady=2)
                name_lbl.pack(fill="x")

                val_lbl = tk.Label(cell,
                                   text=entry["value"],
                                   bg=cell_bg, fg=ACCENT,
                                   font=("Courier New", 15, "bold"),
                                   anchor="w", padx=8)
                val_lbl.pack(fill="x")

                age_lbl = tk.Label(cell, text="",
                                   bg=cell_bg, fg=TEXT_DIM,
                                   font=FONT_UI_XS, anchor="e", padx=6)
                age_lbl.pack(fill="x")

                entry["lbl_w"] = name_lbl
                entry["val_w"] = val_lbl
                entry["age_w"] = age_lbl
                col += 1

        self._spn_count_lbl.configure(text=f"{len(keys)} SPN{'s' if len(keys)!=1 else ''}")
        # Re-bind scroll to all newly created child widgets
        self._bind_scroll_to_children(self._grid)

    def _bind_scroll_to_children(self, widget):
        """Recursively bind touch-scroll to every widget in the grid."""
        widget.bind("<ButtonPress-1>", self._scroll_start_cb, add="+")
        widget.bind("<B1-Motion>",      self._scroll_drag_cb,  add="+")
        for child in widget.winfo_children():
            self._bind_scroll_to_children(child)

    def _schedule_update(self):
        self._update_values()
        self.after(UPDATE_MS, self._schedule_update)

    def _update_values(self):
        if not self._running:
            return
        now = time.time()
        for key, entry in self._live.items():
            if self._selected and key not in self._selected:
                continue
            vw = entry.get("val_w")
            aw = entry.get("age_w")
            if vw:
                try:
                    vw.configure(text=entry["value"])
                except tk.TclError:
                    entry["val_w"] = None
            if aw:
                age = now - entry["last"]
                try:
                    if age < 2:
                        aw.configure(text="", fg=TEXT_DIM)
                    elif age < 5:
                        aw.configure(text=f"  {age:.0f}s ago", fg=YELLOW)
                    else:
                        aw.configure(text=f"  {age:.0f}s ago  STALE", fg=RED)
                except tk.TclError:
                    entry["age_w"] = None

    # ── Controls ─────────────────────────────────────────────────────────────

    def _toggle_running(self):
        self._running = not self._running
        if self._running:
            self._start_btn.configure(text="⏹  Stop")
            self._start_btn.set_bg(RED)
            self._rebuild_grid()
        else:
            self._start_btn.configure(text="▶  Start")
            self._start_btn.set_bg(GREEN)

    def _open_selector(self):
        if not self._live:
            touch_alert(self.winfo_toplevel(),
                        "No live SPNs detected yet.\nConnect to see available SPNs.",
                        title="Select PIDs")
            return

        root = self.winfo_toplevel()
        top  = modal_overlay(root, "Select PIDs", 500, 420)

        # Header with close button
        hdr = tk.Frame(top, bg=PANEL_DARK, height=36)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Select SPNs to show",
                 bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
                 padx=12).pack(side="left", pady=6)
        close_lbl = tk.Label(hdr, text="  ✕  ", bg=PANEL_DARK, fg=TEXT_DIM,
                             font=FONT_BOLD, cursor="hand2")
        close_lbl.pack(side="right")
        close_lbl.bind("<Button-1>", lambda e: top.destroy())

        # Scrollable checklist
        frame = tk.Frame(top, bg=BG)
        frame.pack(fill="both", expand=True, padx=16, pady=(8,0))
        canvas2 = tk.Canvas(frame, bg=BG, highlightthickness=0)
        vsb2 = ttk.Scrollbar(frame, orient="vertical", command=canvas2.yview)
        canvas2.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        canvas2.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas2, bg=BG)
        canvas2.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas2.configure(
                       scrollregion=canvas2.bbox("all")))

        check_vars = {}
        for key in sorted(self._live.keys(), key=lambda x: (x[0], x[1])):
            sa, spn = key
            entry = self._live[key]
            var = tk.BooleanVar(
                value=(key in self._selected) if self._selected else True)
            check_vars[key] = var
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(
                row, variable=var, bg=BG, activebackground=BG,
                font=FONT_UI_S,
                text=f"0x{sa:02X}  SPN {spn:5d}  {entry['name']}  [{entry['value']}]",
                fg=TEXT).pack(side="left", padx=4)

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill="x", padx=16, pady=8)

        def _apply():
            selected = {k for k, v in check_vars.items() if v.get()}
            self._selected = selected if len(selected) < len(check_vars) else set()
            n = len(selected)
            self._filter_lbl.configure(
                text=f"Filtered: {n} SPN{'s' if n!=1 else ''}"
                if self._selected else "Showing all live SPNs")
            self._rebuild_grid()
            top.destroy()

        def _all():
            for v in check_vars.values(): v.set(True)
        def _none():
            for v in check_vars.values(): v.set(False)

        FlatButton(btns, text="All",  bg=PANEL_DARK, fg=TEXT,
                   font=FONT_UI_S, pad_x=10, pad_y=4,
                   command=_all).pack(side="left", padx=(0,4))
        FlatButton(btns, text="None", bg=PANEL_DARK, fg=TEXT,
                   font=FONT_UI_S, pad_x=10, pad_y=4,
                   command=_none).pack(side="left", padx=(0,12))
        FlatButton(btns, text="Apply", bg=ACCENT, fg="white",
                   font=FONT_BOLD, pad_x=14, pad_y=6,
                   command=_apply).pack(side="right")
