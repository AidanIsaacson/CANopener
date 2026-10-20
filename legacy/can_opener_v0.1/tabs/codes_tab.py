"""
Codes tab — passive DM1 collection + active poll, SPN/FMI decode.
"""
import tkinter as tk
from tkinter import ttk
from collections import defaultdict

from core.theme import *
from core.j1939_defs import (decode_dm1, sa_name, spn_info, fmi_desc,
                              PGN_NAMES)

# DM1 PGN
DM1_PGN = 0xFECA

# Lamp bit labels
LAMP_BITS = {
    7: ("MIL", RED),
    5: ("Red Stop", RED),
    3: ("Amber Warn", YELLOW),
    1: ("Protect", ORANGE),
}


class CodesTab(tk.Frame):
    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.app = app
        # fault_key → {spn, fmi, sa, oc, first_seen, last_seen, lamp, active}
        self._faults: dict[tuple, dict] = {}
        # sa → "no_faults" flag
        self._no_fault_nodes: dict[int, float] = {}
        # node filter
        self._filter_sa: set[int] = set()
        self._all_seen_sa: set[int] = set()
        self._build()

    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self):
        # Top bar
        top = tk.Frame(self, bg=PANEL, height=48)
        top.pack(fill="x")
        top.pack_propagate(False)

        FlatButton(top, text="⟳ Poll All Nodes",
                   bg=ACCENT, fg="white", font=FONT_BOLD_S,
                   pad_x=14, pad_y=6,
                   command=self._poll_all).pack(side="left", padx=(10,4), pady=8)

        self._poll_node_btn = FlatButton(
            top, text="Poll Selected",
            bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
            pad_x=12, pad_y=6,
            command=self._poll_selected)
        self._poll_node_btn.pack(side="left", padx=(0,12), pady=8)

        FlatButton(top, text="Clear List",
                   bg=PANEL_DARK, fg=TEXT, font=FONT_BOLD_S,
                   pad_x=12, pad_y=6,
                   command=self._clear_faults).pack(side="left", pady=8)

        self._count_lbl = tk.Label(top, text="0 faults", bg=PANEL,
                                   fg=TEXT_DIM, font=FONT_UI_S)
        self._count_lbl.pack(side="left", padx=12)

        # Body
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        # Node filter column
        node_col = tk.Frame(body, bg=PANEL, width=130)
        node_col.pack(side="left", fill="y")
        node_col.pack_propagate(False)

        tk.Label(node_col, text="NODE FILTER", bg=PANEL, fg=TEXT_DIM,
                 font=FONT_UI_XS, pady=6).pack(fill="x")
        Divider(node_col).pack(fill="x")

        self._node_list = tk.Frame(node_col, bg=PANEL)
        self._node_list.pack(fill="both", expand=True)

        FlatButton(node_col, text="Show All",
                   bg=PANEL_DARK, fg=TEXT_DIM, font=FONT_UI_XS,
                   pad_x=8, pad_y=4,
                   command=self._clear_filter).pack(
                       fill="x", padx=4, pady=4)

        Divider(body, orient="v").pack(side="left", fill="y")

        # Fault table
        tbl = tk.Frame(body, bg=BG)
        tbl.pack(side="left", fill="both", expand=True)

        # Column headers
        hdr_cols = [
            ("Node",    90),
            ("SPN",     70),
            ("FMI",     40),
            ("OC",      36),
            ("Lamps",   80),
            ("SPN Description",     200),
            ("FMI Description",     220),
        ]
        hdr = tk.Frame(tbl, bg=PANEL_DARK, height=26)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        x = 4
        for label, w in hdr_cols:
            tk.Label(hdr, text=label, bg=PANEL_DARK, fg=TEXT_DIM,
                     font=FONT_UI_XS, anchor="w").place(x=x, y=4)
            x += w
        self._hdr_cols = hdr_cols

        # Scrollable list
        wrap = tk.Frame(tbl, bg=BG)
        wrap.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._fault_frame = tk.Frame(self._canvas, bg=BG)
        win = self._canvas.create_window((0,0), window=self._fault_frame,
                                         anchor="nw")
        self._fault_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(win, width=e.width))

    # ── Frame ingestion ──────────────────────────────────────────────────────

    def ingest_frame(self, frame, pgn: int, sa: int, **_):
        if pgn != DM1_PGN:
            return
        self._all_seen_sa.add(sa)
        faults = decode_dm1(frame.data)

        if faults is None:
            return

        if not faults:
            # No active faults from this node
            self._no_fault_nodes[sa] = time.time()
            # Remove any previously active faults from this SA
            to_remove = [k for k in self._faults if k[0] == sa]
            for k in to_remove:
                del self._faults[k]
            self.after(0, self._refresh_table)
            return

        # Remove "no faults" marker for this SA
        self._no_fault_nodes.pop(sa, None)
        changed = False
        for f in faults:
            key = (sa, f["spn"], f["fmi"])
            if key not in self._faults:
                self._faults[key] = {
                    "sa":         sa,
                    "spn":        f["spn"],
                    "fmi":        f["fmi"],
                    "oc":         f["oc"],
                    "lamp":       f["lamp"],
                    "first_seen": time.time(),
                    "last_seen":  time.time(),
                }
                changed = True
            else:
                self._faults[key]["oc"]        = f["oc"]
                self._faults[key]["last_seen"]  = time.time()

        if changed:
            self.after(0, self._refresh_table)
            self.after(0, self._refresh_node_list)

    def _refresh_table(self):
        for w in self._fault_frame.winfo_children():
            w.destroy()

        rows = []

        # "No active faults" entries
        for sa, ts in self._no_fault_nodes.items():
            if self._filter_sa and sa not in self._filter_sa:
                continue
            rows.append(("no_fault", sa, ts))

        # Active faults
        for key, f in self._faults.items():
            sa = f["sa"]
            if self._filter_sa and sa not in self._filter_sa:
                continue
            rows.append(("fault", f))

        if not rows:
            tk.Label(self._fault_frame,
                     text="No faults recorded",
                     bg=BG, fg=TEXT_DIM, font=FONT_UI_S,
                     pady=20).pack()
            self._count_lbl.configure(text="0 faults")
            return

        fault_count = sum(1 for r in rows if r[0] == "fault")
        self._count_lbl.configure(
            text=f"{fault_count} active fault{'s' if fault_count != 1 else ''}")

        for i, row in enumerate(rows):
            bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
            fr = tk.Frame(self._fault_frame, bg=bg, height=28)
            fr.pack(fill="x")
            fr.pack_propagate(False)

            if row[0] == "no_fault":
                _, sa, _ = row
                tk.Label(fr,
                         text=f"0x{sa:02X} {sa_name(sa)[:14]}",
                         bg=bg, fg=TEXT_DIM, font=FONT_MONO_S,
                         padx=6, anchor="w", width=12).place(x=4, y=4)
                tk.Label(fr, text="— No Active Faults —",
                         bg=GREEN_LT, fg=GREEN, font=FONT_BOLD_S,
                         padx=8, pady=2).place(x=98, y=3)
                continue

            f = row[1]
            spn_i = spn_info(f["spn"])
            spn_name_str = spn_i[0] if spn_i else f"SPN {f['spn']}"
            fmi_str      = fmi_desc(f["fmi"])
            lamp_str     = _lamp_label(f["lamp"])

            # Row highlight color based on lamp severity
            if f["lamp"] & 0x80:   row_hl = RED_LT
            elif f["lamp"] & 0x20: row_hl = RED_LT
            elif f["lamp"] & 0x08: row_hl = YELLOW_LT
            else:                  row_hl = bg

            fr.configure(bg=row_hl)

            vals = [
                (f"0x{f['sa']:02X} {sa_name(f['sa'])[:10]}",  90),
                (str(f["spn"]),                                  70),
                (str(f["fmi"]),                                  40),
                (str(f["oc"]),                                   36),
                (lamp_str,                                       80),
                (spn_name_str,                                  200),
                (fmi_str,                                       220),
            ]
            x = 4
            for text, w in vals:
                tk.Label(fr, text=text, bg=row_hl, fg=TEXT,
                         font=FONT_MONO_S, anchor="w").place(x=x, y=4)
                x += w

    def _refresh_node_list(self):
        for w in self._node_list.winfo_children():
            w.destroy()
        for sa in sorted(self._all_seen_sa):
            selected = sa in self._filter_sa
            bg = ACCENT_LT if selected else PANEL
            fg = ACCENT    if selected else TEXT
            lbl = tk.Label(self._node_list,
                           text=f"0x{sa:02X}  {sa_name(sa)[:8]}",
                           bg=bg, fg=fg, font=FONT_MONO_S,
                           padx=6, pady=5, anchor="w", cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, s=sa: self._toggle_filter(s))

    def _toggle_filter(self, sa: int):
        if sa in self._filter_sa:
            self._filter_sa.discard(sa)
        else:
            self._filter_sa.add(sa)
        self._refresh_node_list()
        self._refresh_table()

    def _clear_filter(self):
        self._filter_sa.clear()
        self._refresh_node_list()
        self._refresh_table()

    def _poll_all(self):
        ok = self.app.can_mgr.send_request(DM1_PGN)
        if not ok:
            self._count_lbl.configure(text="Not connected", fg=RED)

    def _poll_selected(self):
        if not self._filter_sa:
            self._poll_all()
            return
        for sa in self._filter_sa:
            self.app.can_mgr.send_request(DM1_PGN, da=sa)

    def _clear_faults(self):
        self._faults.clear()
        self._no_fault_nodes.clear()
        self._refresh_table()


def _lamp_label(lamp_byte: int) -> str:
    active = []
    for bit, (name, _) in LAMP_BITS.items():
        if (lamp_byte >> (bit-1)) & 0x03 in (1, 2, 3):
            active.append(name)
    return ", ".join(active) if active else "—"
