"""Draw docs/images/db9-cables.svg: the handheld's DB9 face, the OBD-II socket face, and the three
adapter cables (docs/ELECTRONICS.md, 5.5). Keep it in step with that section's tables.

    python3 scripts/db9_cables_diagram.py            # rewrites docs/images/db9-cables.svg
"""
import os
import sys

W = 1000
COL = {"ah": "#d4a000", "al": "#2e8b3a", "bh": "#7b3fbf", "bl": "#2b6cd4", "gnd": "#222222",
       "bat": "#d32f2f", "k": "#00897b", "sh": "#a8a8a8", "cg": "#6d4c41"}
SIG = {7: ("CAN A high", "ah"), 2: ("CAN A low", "al"), 3: ("CAN ground", "gnd"), 6: ("chassis ground", "cg"),
       9: ("battery + (input)", "bat"), 5: ("shield", "sh"), 8: ("CAN B high", "bh"), 4: ("CAN B low", "bl"),
       1: ("K-line", "k")}
out = []
add = out.append


def text(x, y, s, size=13, weight="normal", anchor="start", fill="#111", italic=False):
    st = ' font-style="italic"' if italic else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'fill="{fill}"{st}>{s}</text>')


def db9_face(cx, cy, title, sub):
    """A male DB9 seen from the front: 1-5 on the wide row, 6-9 below, left to right."""
    add(f'<path d="M{cx-150},{cy-48} L{cx+150},{cy-48} L{cx+128},{cy+48} L{cx-128},{cy+48} Z" '
        'fill="#f4f4f4" stroke="#555" stroke-width="2"/>')
    for i, pin in enumerate(range(1, 6)):
        x = cx - 104 + i * 52
        pin_dot(x, cy - 20, pin)
    for i, pin in enumerate(range(6, 10)):
        x = cx - 78 + i * 52
        pin_dot(x, cy + 22, pin)
    text(cx, cy - 66, title, 15, "bold", "middle")
    text(cx, cy + 72, sub, 12, anchor="middle", fill="#444", italic=True)


def pin_dot(x, y, pin):
    name, c = SIG[pin]
    add(f'<circle cx="{x}" cy="{y}" r="13" fill="{COL[c]}" stroke="#333" stroke-width="1"/>')
    text(x, y + 5, str(pin), 13, "bold", "middle", fill="#fff" if c != "sh" else "#111")


def legend(x, y):
    items = [(7, "7 CAN A high"), (2, "2 CAN A low"), (8, "8 CAN B high"), (4, "4 CAN B low"),
             (3, "3 CAN ground"), (6, "6 chassis ground"), (9, "9 battery + (sense input)"), (1, "1 K-line"),
             (5, "5 shield")]
    for i, (pin, label) in enumerate(items):
        yy = y + i * 19
        add(f'<rect x="{x}" y="{yy-11}" width="14" height="14" fill="{COL[SIG[pin][1]]}" stroke="#333"/>')
        text(x + 22, yy + 1, label, 13)


def obd_face(x0, y0):
    """The vehicle's OBD-II socket, looking into it: 1-8 on top, 9-16 below, left to right."""
    used = {6: "ah", 14: "al", 3: "bh", 11: "bl", 4: "cg", 5: "gnd", 16: "bat", 7: "k"}
    add(f'<path d="M{x0},{y0} L{x0+300},{y0} L{x0+284},{y0+80} L{x0+16},{y0+80} Z" '
        'fill="#f4f4f4" stroke="#555" stroke-width="2"/>')
    for i in range(8):
        for row, pin in ((0, i + 1), (1, i + 9)):
            x = x0 + 32 + i * 34 + (6 if row else 0)
            y = y0 + 24 + row * 34
            c = used.get(pin)
            fill = COL[c] if c else "#ffffff"
            add(f'<rect x="{x-11}" y="{y-9}" width="22" height="18" rx="3" fill="{fill}" stroke="#333"/>')
            text(x, y + 5, str(pin), 11, "bold", "middle", fill="#fff" if c else "#111")
    text(x0 + 150, y0 - 14, "The vehicle's OBD-II socket", 15, "bold", "middle")
    text(x0 + 150, y0 + 104, "looking into the socket (the cable's plug is the mirror image)", 12,
         anchor="middle", fill="#444", italic=True)


def cable(y, title, left_title, rows, notes):
    """rows: (left label, DB9 pin, twist group or '')"""
    add(f'<rect x="20" y="{y}" width="{W-40}" height="{72 + 30 * len(rows) + 18 * len(notes)}" rx="10" '
        'fill="#fafafa" stroke="#bbb"/>')
    text(40, y + 30, title, 17, "bold")
    lx, rx = 80, 640
    text(lx, y + 56, left_title, 13, "bold")
    text(rx, y + 56, "DB9 female (to the handheld)", 13, "bold")
    for i, (left, pin, twist) in enumerate(rows):
        yy = y + 82 + i * 30
        name, c = SIG[pin]
        add(f'<rect x="{lx}" y="{yy-14}" width="150" height="24" rx="4" fill="#fff" stroke="#888"/>')
        text(lx + 10, yy + 3, left, 13, "bold")
        dash = ' stroke-dasharray="6,5"' if c == "sh" else ""
        add(f'<line x1="{lx+150}" y1="{yy-2}" x2="{rx}" y2="{yy-2}" stroke="{COL[c]}" stroke-width="5"{dash}/>')
        text((lx + 150 + rx) / 2, yy - 8, name + (f"   (twist with {twist})" if twist else ""), 12, anchor="middle",
             fill="#333")
        add(f'<rect x="{rx}" y="{yy-14}" width="70" height="24" rx="4" fill="{COL[c]}" stroke="#333"/>')
        text(rx + 35, yy + 3, f"pin {pin}", 13, "bold", "middle", fill="#fff" if c != "sh" else "#111")
    for j, n in enumerate(notes):
        text(40, y + 82 + len(rows) * 30 + j * 18, n, 12, fill="#444")
    return y + 72 + 30 * len(rows) + 18 * len(notes) + 22


H_TOP = 260
y = H_TOP
body = []
y = cable(y, "1. OBD-II cable (cars and light trucks; CAN B wired for Ford's body bus, e.g. the F-550)",
          "OBD-II plug pin", [("6  CAN high", 7, "14"), ("14  CAN low", 2, "6"), ("3  Ford MS-CAN +", 8, "11"),
                              ("11  Ford MS-CAN -", 4, "3"), ("5  signal ground", 3, ""), ("4  chassis ground", 6, ""),
                              ("16  battery +", 9, ""), ("7  K-line", 1, "")],
          ["Other makes put their second bus on other OBD pins (1/9, 12/13 ...): "
           "wire that pair to DB9 pins 8 / 4 instead.",
           "OBD pins 2, 10 (J1850) and 15 (L-line) stay unconnected: the handheld has no transceiver for them."])
y = cable(y, "2. J1939 cable (trucks and equipment, 9-pin Deutsch)", "Deutsch plug pin (letter)",
          [("C  CAN high", 7, "D"), ("D  CAN low", 2, "C"), ("H  OEM CAN high", 8, "J"), ("J  OEM CAN low", 4, "H"),
           ("A  ground", 3, ""), ("B  battery +", 9, ""), ("E  shield", 5, "")],
          ["H / J are usually the maker's own second CAN; F / G vary (J1708 or another CAN): "
           "check the truck before using them.",
           "Shield: connect at one end only (the handheld's end) to avoid a ground loop."])
y = cable(y, "3. Bench / CANopen cable (CiA 303-1, straight through: CAN A only)", "DB9 / bench lead",
          [("7  CAN_H", 7, "2"), ("2  CAN_L", 2, "7"), ("3  CAN_GND", 3, ""), ("5  shield", 5, "")],
          ["Any off-the-shelf CiA DB9 CAN cable works here. "
           "Leave pins 1, 4, 8 unconnected on cables shared with other CiA gear.",
           "A bus with one module on the bench needs termination: the handheld's switch, plus 120 ohm at the far end."])
H = y + 10

svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="DejaVu Sans, Arial, Helvetica, sans-serif">',
       f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
head = out[:]
out.clear()
text(W / 2, 32, "CANopener handheld: DB9 pinout and adapter cables", 20, "bold", "middle")
text(W / 2, 54, "All cables are CiA 303-1 compatible on CAN A. Wire colours here are for the diagram only.", 12,
     anchor="middle", fill="#444", italic=True)
db9_face(200, 160, "The handheld's DB9 (male)", "as you look at the connector on the device")
obd_face(400, 108)
legend(760, 100)
svg += out + head + ["</svg>"]
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "docs", "images",
                                                            "db9-cables.svg")
with open(path, "w") as f:
    f.write("\n".join(svg) + "\n")
print(f"wrote {path}")
