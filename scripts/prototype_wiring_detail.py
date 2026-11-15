"""Draw the prototype's pin-by-pin wiring: docs/images/prototype-pins.svg (where every pin of the
Pi 5's header goes) and docs/images/prototype-modules.svg (each module's pins, the local wiring
and the power rails). Each wire is labelled at both ends, harness-drawing style. Keep them in step
with docs/install/handheld-prototype.md and docs/ELECTRONICS.md section 5.1.

    python3 scripts/prototype_wiring_detail.py       # rewrites both SVGs
"""
import os
import sys

W, H = 1000, 1300
COL = {"5v": "#d32f2f", "3v3": "#e37b00", "gnd": "#222222", "spi": "#2b6cd4", "i2c": "#2e8b3a",
       "ctl": "#b38f00", "sync": "#7b3fbf", "usb": "#a8a8a8", "free": "#b0b0b0", "ink": "#111111",
       "dim": "#555555", "box": "#f4f4f4", "pi": "#e8f2ec", "accent": "#0b6e4f", "can": "#111111"}


class Svg:
    def __init__(self, title: str, sub: str):
        self.out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
                    'font-family="DejaVu Sans, Arial, Helvetica, sans-serif">',
                    f'<rect width="{W}" height="{H}" fill="#fff"/>']
        self.text(W / 2, 34, title, 20, "bold", "middle")
        self.text(W / 2, 56, sub, 12, anchor="middle", fill="#444", italic=True)

    def add(self, s):
        self.out.append(s)

    def text(self, x, y, s, size=13, weight="normal", anchor="start", fill="#111", italic=False):
        st = ' font-style="italic"' if italic else ""
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
                 f'fill="{fill}"{st}>{s}</text>')

    def wire(self, d, col, width=3, dash=False):
        da = ' stroke-dasharray="6 4"' if dash else ""
        self.add(f'<path d="{d}" fill="none" stroke="{COL[col]}" stroke-width="{width}" stroke-linecap="round" '
                 f'stroke-linejoin="round"{da}/>')

    def rect(self, x, y, w, h, fill=COL["box"], stroke="#555", rx=5, width=2):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{width}"/>')

    def dot(self, x, y, r=4, fill=COL["ink"]):
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"/>')

    def panel(self, x, y, w, h, title, stage=""):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none" stroke="#ccc" '
                 'stroke-width="1.5"/>')
        self.text(x + 14, y + 24, title, 15, "bold")
        if stage:
            self.text(x + w - 14, y + 24, stage, 12, anchor="end", fill=COL["accent"])

    def legend(self, y):
        items = [("5v", "5 V"), ("3v3", "3.3 V"), ("gnd", "ground"), ("spi", "SPI / CAN INT"), ("i2c", "I2C"),
                 ("ctl", "buttons / encoder"), ("sync", "SYNC"), ("free", "kept free / open")]
        x = 60
        for key, label in items:
            self.wire(f"M{x} {y} H{x + 24}", key, 4)
            self.text(x + 30, y + 4, label, 12, fill=COL["dim"])
            x += 30 + len(label) * 7 + 26

    def resistor_h(self, x, y, label, sub=""):
        """A horizontal resistor from x to x+80."""
        self.wire(f"M{x} {y} l5 -8 l10 16 l10 -16 l10 16 l10 -16 l10 16 l10 -16 l10 16 l5 -8", "ink", 2)
        self.text(x + 40, y - 14, label, 12, anchor="middle")
        if sub:
            self.text(x + 40, y + 24, sub, 11, anchor="middle", fill=COL["dim"])

    def resistor_v(self, x, y, label):
        """A vertical resistor from y to y+60."""
        self.wire(f"M{x} {y} l-8 5 l16 10 l-16 10 l16 10 l-16 10 l16 10 l-8 5", "ink", 2)
        self.text(x - 14, y + 34, label, 12, anchor="end")

    def cap_v(self, x, y_top, y_bot, label, at=None):
        m = (y_top + y_bot) / 2 if at is None else y_top + at
        self.wire(f"M{x} {y_top} V{m - 4} M{x - 12} {m - 4} H{x + 12} "
                  f"M{x - 12} {m + 4} H{x + 12} M{x} {m + 4} V{y_bot}", "ink", 2)
        self.text(x + 16, m + 4, label, 12, fill=COL["dim"])

    def switch(self, x, y):
        """A push button from x to x+50 on a horizontal wire."""
        self.dot(x + 8, y, 3.5)
        self.dot(x + 42, y, 3.5)
        self.wire(f"M{x} {y} H{x + 8} M{x + 42} {y} H{x + 50} M{x + 10} {y - 4} L{x + 40} {y - 14}", "ink", 2)
        self.wire(f"M{x + 25} {y - 9} V{y - 20} M{x + 17} {y - 20} H{x + 33}", "ink", 2)

    def done(self) -> str:
        return "\n".join(self.out + ["</svg>"]) + "\n"


# Pi 5 header: pin -> (GPIO / function, where it goes, wire colour). Matches ELECTRONICS.md 5.1.
PINS = {
    1: ("3V3", "3.3 V rail", "3v3"), 2: ("5V", "fuse (PTC), then the 5 V rail", "5v"),
    3: ("GPIO2 SDA", "ADS1115 SDA", "i2c"), 4: ("5V", "Touch Display 2 power", "5v"),
    5: ("GPIO3 SCL", "ADS1115 SCL", "i2c"), 6: ("GND", "Touch Display 2 ground", "gnd"),
    7: ("GPIO4", "encoder B", "ctl"), 8: ("GPIO14 TXD", "kept free: ESP32 (stage 6)", "free"),
    9: ("GND", "ADS1115 GND, probe COM", "gnd"), 10: ("GPIO15 RXD", "kept free: ESP32 (stage 6)", "free"),
    11: ("GPIO17", "d-pad up (later)", "ctl"), 12: ("GPIO18", "buzzer (optional)", "free"),
    13: ("GPIO27", "d-pad down (later)", "ctl"), 14: ("GND", "spare ground", "gnd"),
    15: ("GPIO22", "d-pad left (later)", "ctl"), 16: ("GPIO23", "d-pad right (later)", "ctl"),
    17: ("3V3", "spare 3.3 V", "3v3"), 18: ("GPIO24", "encoder A", "ctl"),
    19: ("GPIO10 MOSI", "CAN module SDI", "spi"), 20: ("GND", "ground rail, CAN module GND", "gnd"),
    21: ("GPIO9 MISO", "CAN module SDO", "spi"), 22: ("GPIO25", "CAN module INT", "spi"),
    23: ("GPIO11 SCLK", "CAN module SCK", "spi"), 24: ("GPIO8 CE0", "CAN module nCS", "spi"),
    25: ("GND", "second ground to the rail", "gnd"), 26: ("GPIO7 CE1", "kept free: CAN stack B", "free"),
    27: ("ID_SD", "don't use (HAT EEPROM)", "free"), 28: ("ID_SC", "don't use (HAT EEPROM)", "free"),
    29: ("GPIO5", "button 1 (F1)", "ctl"), 30: ("GND", "buttons' ground", "gnd"),
    31: ("GPIO6", "button 2 (F2)", "ctl"), 32: ("GPIO12", "button 3 (F3)", "ctl"),
    33: ("GPIO13", "button 4 (F4)", "ctl"), 34: ("GND", "spare ground", "gnd"),
    35: ("GPIO19", "spare", "free"), 36: ("GPIO16", "button 5 (F5, Back)", "ctl"),
    37: ("GPIO26", "encoder push", "ctl"), 38: ("GPIO20", "kept free: CAN stack B INT", "free"),
    39: ("GND", "encoder ground, SYNC ground", "gnd"), 40: ("GPIO21", "SYNC in from Pico 2 GP15", "sync"),
}


def pins_page() -> str:
    s = Svg("Prototype wiring 1 of 2: the Pi 5 header, pin by pin",
            "Seen from above, pin 1 (square pad) at the top, the end farthest from the USB ports. Wire colours "
            "are for the drawing only.")
    s.legend(84)
    top, step = 150, 54
    s.rect(440, top - 30, 120, 20 * step + 8, fill=COL["pi"], stroke=COL["accent"], rx=6)
    s.text(500, top - 40, "Raspberry Pi 5", 15, "bold", "middle")
    for row in range(20):
        y = top + row * step
        for pin, x, side in ((2 * row + 1, 475, -1), (2 * row + 2, 525, 1)):
            func, dest, col = PINS[pin]
            free = col == "free"
            ring = COL["free"] if free else COL[col]
            dash = ' stroke-dasharray="4 3"' if free else ""
            sq = pin == 1
            if sq:
                s.add(f'<rect x="{x - 17}" y="{y - 17}" width="34" height="34" rx="3" fill="#fff" stroke="{ring}" '
                      f'stroke-width="4"/>')
            else:
                s.add(f'<circle cx="{x}" cy="{y}" r="17" fill="#fff" stroke="{ring}" stroke-width="4"{dash}/>')
            s.text(x, y + 5, str(pin), 13, "bold", "middle")
            x0, x1 = x + side * 20, x + side * 92
            s.wire(f"M{x0} {y} H{x1}", col, 4, dash=free)
            tx = x1 + side * 8
            anchor = "end" if side < 0 else "start"
            s.text(tx, y - 3, dest, 14, "normal" if free else "bold", anchor, COL["dim"] if free else COL["ink"])
            s.text(tx, y + 15, func, 12, anchor=anchor, fill=COL["dim"])
    y = top + 20 * step + 20
    s.text(500, y, "Ground: any GND pin works; 6, 20 and 39 feed the rails. 3V3 / 5V pins are joined inside "
           "the Pi.", 12, anchor="middle", fill=COL["dim"])
    s.text(500, y + 20, "GPIO pins are 3.3 V only: nothing at 5 V or more ever goes to one.", 12, "bold", "middle")
    return s.done()


def modules_page() -> str:
    s = Svg("Prototype wiring 2 of 2: the modules, pin by pin",
            "Each wire is labelled with where its other end goes. Pi numbers are header pins (page 1).")
    s.legend(84)

    # --- CAN module ---------------------------------------------------------------------
    x0, y0 = 30, 110
    s.panel(x0, y0, 460, 400, "MCP2518FD module (CAN stack A)", "stage 1")
    s.rect(x0 + 20, y0 + 42, 170, 262)
    s.text(x0 + 105, y0 + 64, "P1 header", 12, anchor="middle", fill=COL["dim"])
    pins = [("5V", "5v", "5 V rail (fused)"), ("GND", "gnd", "Pi 20"), ("3V3", "3v3", "3.3 V rail (if needed)"),
            ("SCK", "spi", "Pi 23 · GPIO11"), ("SDI", "spi", "Pi 19 · GPIO10 MOSI"),
            ("SDO", "spi", "Pi 21 · GPIO9 MISO"), ("nCS", "spi", "Pi 24 · GPIO8 CE0"),
            ("INT", "spi", "Pi 22 · GPIO25"), ("INT0", "free", "open"), ("INT1", "free", "open"),
            ("CLK", "free", "open (crystal test point)")]
    for i, (name, col, dest) in enumerate(pins):
        y = y0 + 82 + i * 19
        s.text(x0 + 180, y + 4, name, 12, "bold", "end")
        s.dot(x0 + 190, y, 4)
        s.wire(f"M{x0 + 194} {y} H{x0 + 250}", col, 3, dash=col == "free")
        s.text(x0 + 258, y + 4, dest, 12, fill=COL["dim"] if col == "free" else COL["ink"])
    s.text(x0 + 105, y0 + 294, "ATA6563 · 20 MHz · 120 Ω", 11, anchor="middle", fill=COL["dim"])
    # screw terminal -> DB9
    for i, (lbl, pin, col) in enumerate((("H", 7, "can"), ("L", 2, "dim"), ("G", 3, "gnd"))):
        x = x0 + 55 + i * 50
        s.rect(x - 12, y0 + 312, 24, 18, fill="#cfe8dc", stroke="#555", rx=2, width=1.5)
        s.text(x, y0 + 326, lbl, 12, "bold", "middle")
        s.wire(f"M{x} {y0 + 330} V{y0 + 356}", col, 3)
        s.text(x, y0 + 374, f"DB9 {pin}", 12, "bold", "middle")
    s.text(x0 + 20, y0 + 392, "Twist H and L. DB9 pin 6 (second ground) optional.", 11, fill=COL["dim"])
    s.text(x0 + 220, y0 + 340, "Termination jumper:", 12, "bold")
    s.text(x0 + 220, y0 + 356, "off on a machine, on for the bench bus", 12, fill=COL["dim"])
    s.text(x0 + 220, y0 + 372, "P2 (4.5-36 V input): leave unconnected", 12, fill=COL["dim"])

    # --- ADS1115 ------------------------------------------------------------------------
    x0, y0 = 510, 110
    s.panel(x0, y0, 460, 400, "ADS1115 and the probe inputs", "stage 3")
    s.rect(x0 + 300, y0 + 42, 140, 238)
    s.text(x0 + 370, y0 + 64, "ADS1115", 13, "bold", "middle")
    s.text(x0 + 370, y0 + 80, "address 0x48", 11, anchor="middle", fill=COL["dim"])
    pins = [("VDD", "3v3", "3.3 V rail"), ("GND", "gnd", "Pi 9 (also probe COM)"), ("SCL", "i2c", "Pi 5 · GPIO3"),
            ("SDA", "i2c", "Pi 3 · GPIO2"), ("ADDR", "gnd", "ground"), ("ALRT", "free", "open"),
            ("A0", "ink", "probe 1 front end"), ("A1", "ink", "probe 2 front end"), ("A2", "ink", "probe 3 front end"),
            ("A3", "ink", "probe 4 front end")]
    for i, (name, col, dest) in enumerate(pins):
        y = y0 + 104 + i * 18.5
        s.text(x0 + 310, y + 4, name, 12, "bold")
        s.dot(x0 + 300, y, 4)
        s.wire(f"M{x0 + 296} {y} H{x0 + 240}", col, 3 if col != "ink" else 2, dash=col == "free")
        s.text(x0 + 232, y + 4, dest, 12, anchor="end", fill=COL["dim"] if col == "free" else COL["ink"])
    # one front end
    y = y0 + 334
    s.text(x0 + 14, y0 + 300, "Front end, one per probe (×4). R1: metal film, measured.", 12, "bold")
    s.text(x0 + 14, y - 4, "probe n", 12)
    s.wire(f"M{x0 + 14} {y} H{x0 + 70}", "ink", 2)
    s.resistor_h(x0 + 70, y, "R1 1 MΩ 1%")
    s.wire(f"M{x0 + 150} {y} H{x0 + 400}", "ink", 2)
    s.text(x0 + 404, y + 4, "→ An", 12, "bold")
    s.dot(x0 + 190, y)
    s.resistor_v(x0 + 190, y + 2, "R2 49.9 kΩ")
    s.dot(x0 + 270, y)
    s.cap_v(x0 + 270, y, y + 62, "100 nF")
    s.dot(x0 + 345, y)
    s.wire(f"M{x0 + 345} {y} V{y + 62}", "free", 2, dash=True)
    s.text(x0 + 352, y + 30, "clamp", 11, fill=COL["dim"])
    s.text(x0 + 352, y + 44, "(fit later)", 11, fill=COL["dim"])
    s.wire(f"M{x0 + 190} {y + 62} H{x0 + 345}", "gnd", 2)
    s.text(x0 + 14, y + 58, "COM = ground", 12)

    # --- buttons and encoder ------------------------------------------------------------
    x0, y0 = 30, 530
    s.panel(x0, y0, 460, 400, "Buttons and encoder", "stage 0b")
    rail = x0 + 410
    s.wire(f"M{rail} {y0 + 50} V{y0 + 370}", "gnd", 5)
    s.text(rail, y0 + 42, "ground rail", 12, "bold", "middle")
    buttons = [("Button 1 (F1)", "Pi 29 · GPIO5"), ("Button 2 (F2)", "Pi 31 · GPIO6"),
               ("Button 3 (F3)", "Pi 32 · GPIO12"), ("Button 4 (F4)", "Pi 33 · GPIO13"),
               ("Button 5 (Back)", "Pi 36 · GPIO16")]
    for i, (name, dest) in enumerate(buttons):
        y = y0 + 70 + i * 34
        s.text(x0 + 16, y - 2, name, 12, "bold")
        s.text(x0 + 16, y + 12, dest, 11, fill=COL["dim"])
        s.wire(f"M{x0 + 150} {y} H{x0 + 300}", "ctl", 3)
        s.switch(x0 + 300, y)
        s.wire(f"M{x0 + 350} {y} H{rail}", "gnd", 3)
        s.dot(rail, y)
    ey = y0 + 250
    s.rect(x0 + 230, ey - 12, 90, 128, fill="#fff")
    s.text(x0 + 275, ey - 18, "EC11 encoder", 11, anchor="middle", fill=COL["dim"])
    enc = [("A", "Pi 18 · GPIO24", "ctl"), ("C", "ground rail", "gnd"), ("B", "Pi 7 · GPIO4", "ctl"),
           ("SW", "Pi 37 · GPIO26", "ctl"), ("SW", "ground rail", "gnd")]
    for i, (name, dest, col) in enumerate(enc):
        y = ey + i * 24
        s.text(x0 + 240, y + 4, name, 12, "bold")
        if col == "gnd":
            s.wire(f"M{x0 + 320} {y} H{rail}", "gnd", 3)
            s.dot(rail, y)
        else:
            s.text(x0 + 16, y + 4, dest, 12)
            s.wire(f"M{x0 + 150} {y} H{x0 + 230}", "ctl", 3)
    s.text(x0 + 16, y0 + 378, "KY-040 module: CLK = A, DT = B, SW = push,", 11, fill=COL["dim"])
    s.text(x0 + 16, y0 + 392, "GND to the rail, + to the 3.3 V rail (never 5 V).", 11, fill=COL["dim"])

    # --- Picos --------------------------------------------------------------------------
    x0, y0 = 510, 530
    s.panel(x0, y0, 460, 400, "The two Picos", "stages 2a, 2b")
    s.rect(x0 + 20, y0 + 44, 160, 186)
    s.text(x0 + 100, y0 + 66, "Pico 2 (meter)", 13, "bold", "middle")
    p2 = [("GP15 · 20", "sync"), ("GND · 18", "gnd"), ("GP26 · 31", "free"), ("GP27 · 32", "free"),
          ("GP28 · 34", "free"), ("AGND · 33", "free"), ("USB", "usb")]
    ys = {}
    for i, (name, _col) in enumerate(p2):
        y = y0 + 90 + i * 20
        ys[name] = y
        s.text(x0 + 170, y + 4, name, 12, "bold", "end")
        s.dot(x0 + 180, y, 4)
    s.rect(x0 + 20, y0 + 262, 160, 104)
    s.text(x0 + 100, y0 + 284, "Pico (logic)", 13, "bold", "middle")
    p1 = [("GP2 · 4", "sync"), ("GND · 3", "gnd"), ("USB", "usb")]
    ys1 = {}
    for i, (name, _col) in enumerate(p1):
        y = y0 + 306 + i * 22
        ys1[name] = y
        s.text(x0 + 170, y + 4, name, 12, "bold", "end")
        s.dot(x0 + 180, y, 4)
    # SYNC: Pico 2 GP15 -> Pi 40 and Pico GP2
    y = ys["GP15 · 20"]
    s.wire(f"M{x0 + 184} {y} H{x0 + 300}", "sync", 3)
    s.text(x0 + 306, y + 4, "Pi 40 · GPIO21 (pps)", 12, "bold")
    s.wire(f"M{x0 + 230} {y} V{ys1['GP2 · 4']} H{x0 + 184}", "sync", 3)
    s.dot(x0 + 230, y)
    y = ys["GND · 18"]
    s.wire(f"M{x0 + 184} {y} H{x0 + 300}", "gnd", 3)
    s.text(x0 + 306, y + 4, "Pi 39 (ground)", 12)
    s.wire(f"M{x0 + 214} {y} V{ys1['GND · 3']} H{x0 + 184}", "gnd", 3)
    s.dot(x0 + 214, y)
    for name, dest in (("GP26 · 31", "CAN-H"), ("GP27 · 32", "CAN-L"), ("GP28 · 34", "CAN GND")):
        y = ys[name]
        s.wire(f"M{x0 + 184} {y} H{x0 + 300}", "free", 3, dash=True)
        s.text(x0 + 306, y + 4, dest, 12, fill=COL["dim"])
    s.text(x0 + 306, ys["AGND · 33"] + 4, "dividers' ground", 12, fill=COL["dim"])
    s.text(x0 + 240, y0 + 386, "CAN monitors (dashed): 100k/10k dividers, only once clamps are fitted.", 11, "bold",
           "middle", COL["dim"])
    for y in (ys["USB"], ys1["USB"]):
        s.wire(f"M{x0 + 184} {y} H{x0 + 300}", "usb", 3)
        s.text(x0 + 306, y + 4, "Pi USB (data + power)", 12)

    # --- power rails --------------------------------------------------------------------
    x0, y0 = 30, 950
    s.panel(x0, y0, 940, 320, "Power rails on the breadboard", "build first")
    r5, r3, rg = y0 + 80, y0 + 160, y0 + 250
    s.text(x0 + 20, r5 - 12, "Pi pin 2 (5V)", 13, "bold")
    s.wire(f"M{x0 + 20} {r5} H{x0 + 230}", "5v", 3)
    s.rect(x0 + 230, r5 - 12, 60, 24, fill="#fff", stroke=COL["ink"], rx=3)
    s.wire(f"M{x0 + 236} {r5 + 10} L{x0 + 284} {r5 - 10}", "ink", 2)
    s.text(x0 + 260, r5 + 32, "PTC 0.5 A hold", 12, anchor="middle", fill=COL["dim"])
    s.text(x0 + 260, r5 + 46, "MF-MSMF050-2", 12, anchor="middle", fill=COL["dim"])
    s.wire(f"M{x0 + 290} {r5} H{x0 + 900}", "5v", 3)
    s.text(x0 + 900, r5 - 12, "5 V rail: CAN module 5V", 13, "bold", "end")
    s.text(x0 + 20, r3 - 12, "Pi pin 1 (3V3)", 13, "bold")
    s.wire(f"M{x0 + 20} {r3} H{x0 + 900}", "3v3", 3)
    s.text(x0 + 900, r3 - 12, "3.3 V rail: ADS1115 VDD, KY-040 +, CAN 3V3 if needed", 13, "bold", "end")
    s.text(x0 + 20, rg - 12, "Pi pin 6 or 20 (GND)", 13, "bold")
    s.wire(f"M{x0 + 20} {rg} H{x0 + 900}", "gnd", 3)
    s.text(x0 + 900, rg + 22, "ground rail: every module, the buttons, the encoder", 13, "bold", "end")
    for x, top, lbl in ((x0 + 400, r5, "10 µF"), (x0 + 490, r5, "100 nF"), (x0 + 620, r3, "10 µF"),
                        (x0 + 710, r3, "100 nF")):
        s.cap_v(x, top, rg, lbl, at=34)
        s.dot(x, top)
        s.dot(x, rg)
    s.text(x0 + 20, y0 + 300, "Not on the rails: the display keeps its own lead to pins 4 and 6; the Picos are on USB. "
           "Electrolytic: stripe to ground.", 12, fill=COL["dim"])
    return s.done()


if __name__ == "__main__":
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "images")
    for name, fn in (("prototype-pins.svg", pins_page), ("prototype-modules.svg", modules_page)):
        path = os.path.normpath(os.path.join(folder, name))
        with open(path, "w") as f:
            f.write(fn())
        print(path, file=sys.stderr)
