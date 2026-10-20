"""
Easter egg Tetris — pure Tkinter, touch-playable.
Pauses the CAN sim while open, resumes on close.
Long-press clock in status bar to launch.
"""
import tkinter as tk
import random
import time

COLS, ROWS = 10, 20
CELL = 21          # 20×21 = 420px board height — fits 480px screen
W    = COLS * CELL
H    = ROWS * CELL

SHAPES = {
    'I': [(0,1),(1,1),(2,1),(3,1)],
    'O': [(0,0),(1,0),(0,1),(1,1)],
    'T': [(1,0),(0,1),(1,1),(2,1)],
    'S': [(1,0),(2,0),(0,1),(1,1)],
    'Z': [(0,0),(1,0),(1,1),(2,1)],
    'J': [(0,0),(0,1),(1,1),(2,1)],
    'L': [(2,0),(0,1),(1,1),(2,1)],
}
COLORS = {
    'I': '#00BFFF', 'O': '#FFD700', 'T': '#DA70D6',
    'S': '#7CFC00', 'Z': '#FF6347', 'J': '#4169E1', 'L': '#FF8C00',
}
BG_CLR  = '#1C1914'
GRID_CL = '#2A2724'
BTN_BG  = '#2A2724'
BTN_FG  = '#E0DDD6'
BTN_H   = '#3A3530'


def rotate(piece):
    return [(-y, x) for x, y in piece]


def normalize(piece):
    mx = min(x for x,y in piece)
    my = min(y for x,y in piece)
    return [(x-mx, y-my) for x,y in piece]


class TetrisWindow(tk.Toplevel):
    def __init__(self, root, app=None):
        super().__init__(root)
        self.app = app
        self.title("Tetris")
        self.configure(bg=BG_CLR)
        self.resizable(False, False)
        self.transient(root)

        # Pause sim if running
        self._sim_was_running = False
        if app:
            sim = getattr(app.can_mgr, "_sim", None)
            if sim and sim._running:
                sim._running = False
                self._sim_was_running = True

        # Window: board width + right panel, height = board only
        tw = W + 130
        th = H + 16   # small vertical padding
        rw, rh = root.winfo_width(), root.winfo_height()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        cx = rx + rw // 2 - tw // 2
        cy = max(ry + 32, ry + rh // 2 - th // 2)
        self.geometry(f"{tw}x{th}+{cx}+{cy}")

        self._board     = [[None]*COLS for _ in range(ROWS)]
        self._score     = 0
        self._level     = 1
        self._lines     = 0
        self._running   = False
        self._game_over = False
        self._piece     = None
        self._piece_x   = 0
        self._piece_y   = 0
        self._piece_type  = None
        self._next_type   = None
        self._drop_interval = 500
        self._last_drop   = 0

        self._build()
        self._new_piece()
        self._running = True
        self.bind("<KeyPress>", self._key)
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._loop()

    def _build(self):
        # Main area: board left, info+controls right
        main = tk.Frame(self, bg=BG_CLR)
        main.pack(fill="both", expand=True)

        # Board canvas
        left = tk.Frame(main, bg=BG_CLR)
        left.pack(side="left", padx=(8,4), pady=8)
        self._canvas = tk.Canvas(left, width=W, height=H,
                                 bg=BG_CLR, highlightthickness=1,
                                 highlightbackground='#3A3530')
        self._canvas.pack()

        # Info + controls panel (right side)
        right = tk.Frame(main, bg=BG_CLR, width=114)
        right.pack(side="left", fill="y", pady=8, padx=(0,4))
        right.pack_propagate(False)

        for label, attr in [("SCORE","_score_var"),
                             ("LEVEL","_level_var"),
                             ("LINES","_lines_var")]:
            tk.Label(right, text=label, bg=BG_CLR, fg='#7A756D',
                     font=('Courier New',8,'bold')).pack(anchor='w', pady=(4,0))
            var = tk.StringVar(value="0")
            setattr(self, attr, var)
            tk.Label(right, textvariable=var, bg=BG_CLR, fg='#F5F3EF',
                     font=('Courier New',13,'bold')).pack(anchor='w')

        tk.Label(right, text="NEXT", bg=BG_CLR, fg='#7A756D',
                 font=('Courier New',8,'bold')).pack(anchor='w', pady=(8,2))
        self._next_canvas = tk.Canvas(right, width=70, height=70,
                                      bg=BG_CLR, highlightthickness=0)
        self._next_canvas.pack(anchor='w')

        # ── Touch controls — stacked in right panel ────────────────────────────
        tk.Label(right, text="CONTROLS", bg=BG_CLR, fg='#7A756D',
                 font=('Courier New',7,'bold')).pack(anchor='w', pady=(10,2))

        # Layout: rotate on top, then left/right side by side, then drops
        btn_rows = [
            [("↺", self._rotate)],
            [("←", self._left), ("→", self._right)],
            [("▼", self._soft_drop)],
            [("⬇", self._hard_drop)],
        ]
        for btn_row in btn_rows:
            row_frame = tk.Frame(right, bg=BG_CLR)
            row_frame.pack(fill="x", pady=1)
            for symbol, cmd in btn_row:
                b = tk.Label(row_frame, text=symbol, bg=BTN_BG, fg=BTN_FG,
                             font=('Courier New', 16, 'bold'),
                             cursor="hand2")
                b.pack(side="left", fill="both", expand=True,
                       padx=1, ipady=6)
                b.bind("<ButtonPress-1>", lambda e, c=cmd: c())
                b.bind("<Enter>", lambda e, w=b: w.configure(bg=BTN_H))
                b.bind("<Leave>", lambda e, w=b: w.configure(bg=BTN_BG))

        # Quit button at bottom of right panel
        tk.Frame(right, bg='#3A3530', height=1).pack(fill="x", pady=(8,4))
        close_btn = tk.Label(right, text="✕ QUIT", bg=BTN_BG, fg=BTN_FG,
                             font=('Courier New',9,'bold'),
                             padx=8, pady=6, cursor="hand2")
        close_btn.pack(fill="x")
        close_btn.bind("<Button-1>", lambda e: self._on_close())

    # ── Game logic ────────────────────────────────────────────────────────────

    def _rotate(self):
        if not self._running or self._game_over: return
        rot = rotate(self._piece)
        if self._valid(rot, self._piece_x, self._piece_y):
            self._piece = rot
        self._draw()

    def _left(self):
        if not self._running or self._game_over: return
        if self._valid(self._piece, self._piece_x-1, self._piece_y):
            self._piece_x -= 1
        self._draw()

    def _right(self):
        if not self._running or self._game_over: return
        if self._valid(self._piece, self._piece_x+1, self._piece_y):
            self._piece_x += 1
        self._draw()

    def _soft_drop(self):
        if not self._running or self._game_over: return
        if self._valid(self._piece, self._piece_x, self._piece_y+1):
            self._piece_y += 1
        else:
            self._lock()
        self._draw()

    def _hard_drop(self):
        if not self._running or self._game_over: return
        while self._valid(self._piece, self._piece_x, self._piece_y+1):
            self._piece_y += 1
        self._lock()
        self._draw()

    def _new_piece(self):
        if self._next_type is None:
            self._next_type = random.choice(list(SHAPES.keys()))
        self._piece_type  = self._next_type
        self._next_type   = random.choice(list(SHAPES.keys()))
        self._piece       = list(SHAPES[self._piece_type])
        self._piece_x     = COLS // 2 - 2
        self._piece_y     = 0
        if not self._valid(self._piece, self._piece_x, self._piece_y):
            self._game_over = True
            self._running   = False
            self._draw()
            self._canvas.create_text(W//2, H//2, text="GAME OVER",
                                     fill='#FF6347',
                                     font=('Courier New',20,'bold'))

    def _valid(self, piece, px, py):
        for dx, dy in piece:
            x, y = px+dx, py+dy
            if x < 0 or x >= COLS or y >= ROWS: return False
            if y >= 0 and self._board[y][x]:    return False
        return True

    def _lock(self):
        color = COLORS[self._piece_type]
        for dx, dy in self._piece:
            x, y = self._piece_x+dx, self._piece_y+dy
            if 0 <= y < ROWS:
                self._board[y][x] = color
        self._clear_lines()
        self._new_piece()

    def _clear_lines(self):
        full = [r for r in range(ROWS) if all(self._board[r])]
        if not full: return
        for r in full:
            del self._board[r]
            self._board.insert(0, [None]*COLS)
        n = len(full)
        self._lines += n
        self._score += [0,100,300,500,800][n] * self._level
        self._level  = self._lines // 10 + 1
        self._drop_interval = max(80, 500 - (self._level-1)*40)
        self._score_var.set(str(self._score))
        self._level_var.set(str(self._level))
        self._lines_var.set(str(self._lines))

    def _draw(self):
        c = self._canvas
        c.delete("all")
        for col in range(COLS):
            for row in range(ROWS):
                x1, y1 = col*CELL, row*CELL
                c.create_rectangle(x1, y1, x1+CELL, y1+CELL,
                                   fill=BG_CLR, outline=GRID_CL, width=1)
        for row in range(ROWS):
            for col in range(COLS):
                color = self._board[row][col]
                if color:
                    x1, y1 = col*CELL+1, row*CELL+1
                    c.create_rectangle(x1, y1, x1+CELL-2, y1+CELL-2,
                                       fill=color, outline='', width=0)
        if self._piece:
            color = COLORS[self._piece_type]
            # Ghost
            gy = self._piece_y
            while self._valid(self._piece, self._piece_x, gy+1):
                gy += 1
            for dx, dy in self._piece:
                x1 = (self._piece_x+dx)*CELL+1
                y1 = (gy+dy)*CELL+1
                if y1 >= 0:
                    c.create_rectangle(x1, y1, x1+CELL-2, y1+CELL-2,
                                       fill='', outline=color, width=1)
            for dx, dy in self._piece:
                x1 = (self._piece_x+dx)*CELL+1
                y1 = (self._piece_y+dy)*CELL+1
                if y1 >= 0:
                    c.create_rectangle(x1, y1, x1+CELL-2, y1+CELL-2,
                                       fill=color, outline='', width=0)
        # Next piece
        nc = self._next_canvas
        nc.delete("all")
        if self._next_type:
            p = normalize(SHAPES[self._next_type])
            col = COLORS[self._next_type]
            for dx, dy in p:
                x1, y1 = dx*16+8, dy*16+8
                nc.create_rectangle(x1, y1, x1+14, y1+14,
                                    fill=col, outline='', width=0)

    def _key(self, e):
        k = e.keysym
        if self._game_over:
            if k in ('q','Q','Escape'): self._on_close()
            return
        if k == 'Left':       self._left()
        elif k == 'Right':    self._right()
        elif k == 'Down':     self._soft_drop()
        elif k == 'Up':       self._rotate()
        elif k == 'space':    self._hard_drop()
        elif k in ('q','Q','Escape'): self._on_close()

    def _loop(self):
        if not self._running:
            return
        now = int(time.time()*1000)
        if now - self._last_drop >= self._drop_interval:
            self._last_drop = now
            if self._valid(self._piece, self._piece_x, self._piece_y+1):
                self._piece_y += 1
            else:
                self._lock()
        self._draw()
        self.after(33, self._loop)   # ~30fps

    def _on_close(self):
        self._running = False
        # Resume sim if we paused it
        if self._sim_was_running and self.app:
            sim = getattr(self.app.can_mgr, "_sim", None)
            if sim:
                sim._running = True
                import threading
                sim._thread = threading.Thread(
                    target=sim._loop, daemon=True)
                sim._thread.start()
        self.destroy()


def launch_tetris(root, app=None):
    TetrisWindow(root, app)
