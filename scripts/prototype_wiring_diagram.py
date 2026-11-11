"""Draw docs/images/prototype-wiring.svg: how the prototype's modules hang off the Pi 5 (the block
diagram) and the breadboard's power rails. Keep it in step with docs/install/handheld-prototype.md
and docs/ELECTRONICS.md section 5.1.

    python3 scripts/prototype_wiring_diagram.py      # rewrites docs/images/prototype-wiring.svg
"""
import os
import sys

W, H = 1000, 940
COL = {"5v": "#d32f2f", "3v3": "#e37b00", "gnd": "#222222", "spi": "#2b6cd4", "i2c": "#2e8b3a",
       "ctl": "#b38f00", "sync": "#7b3fbf", "usb": "#a8a8a8", "ink": "#111111", "dim": "#555555",
       "box": "#f4f4f4", "pi": "#e8f2ec", "accent": "#0b6e4f"}
out = []
add = out.append


def text(x, y, s, size=13, weight="normal", anchor="start", fill="#111", italic=False):
    st = ' font-style="italic"' if italic else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'fill="{fill}"{st}>{s}</text>')


def box(x, y, w, h, title, lines=(), fill=COL["box"], stroke="#555"):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    text(x + w / 2, y + 30, title, 16, "bold", "middle")
    for i, ln in enumerate(lines):
        text(x + w / 2, y + 52 + i * 18, ln, 12.5, anchor="middle", fill=COL["dim"])


def wire(d, col, width=4):
    add(f'<path d="{d}" fill="none" stroke="{COL[col]}" stroke-width="{width}" stroke-linecap="round" '
        'stroke-linejoin="round"/>')


def cap(x, y_top, y_bot, label):
    """A capacitor hanging from a rail at y_top down to ground at y_bot."""
    wire(f"M{x} {y_top} V{y_top + 22} M{x - 12} {y_top + 22} H{x + 12} M{x - 12} {y_top + 30} H{x + 12} "
         f"M{x} {y_top + 30} V{y_bot}", "ink", 2)
    text(x + 18, y_top + 30, label, 12, fill=COL["dim"])
    for y in (y_top, y_bot):
        add(f'<circle cx="{x}" cy="{y}" r="4" fill="{COL["ink"]}"/>')


def draw() -> str:
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        'font-family="DejaVu Sans, Arial, Helvetica, sans-serif">')
    add(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    text(W / 2, 32, "CANopener handheld prototype: wiring", 20, "bold", "middle")
    text(W / 2, 54, "Pins per docs/ELECTRONICS.md 5.1. Wire colours are for the diagram only.", 12,
         anchor="middle", fill="#444", italic=True)

    # legend
    items = [("5v", "5 V"), ("3v3", "3.3 V"), ("gnd", "ground"), ("spi", "SPI + CAN INT"), ("i2c", "I2C"),
             ("ctl", "buttons / encoder"), ("sync", "SYNC pulse"), ("usb", "USB / ribbon")]
    x = 60
    for key, label in items:
        wire(f"M{x} 82 H{x + 24}", key)
        text(x + 30, 86, label, 12, fill=COL["dim"])
        x += 30 + len(label) * 7 + 26

    o = 70      # vertical offset of the block diagram
    # display
    box(390, 30 + o, 220, 80, "Touch Display 2", ["DSI ribbon + its power lead"])
    wire(f"M460 {110 + o} V{190 + o}", "usb")
    wire(f"M540 {110 + o} V{190 + o}", "5v")
    text(452, 155 + o, "DSI", 12, anchor="end")
    text(550, 155 + o, "5V · GND (pins 4, 6)", 12)
    # Pi
    box(390, 190 + o, 220, 260, "Raspberry Pi 5", ["40-pin header"], fill=COL["pi"], stroke=COL["accent"])
    for i, ln in enumerate(["SPI0 CE0 · GPIO25", "I2C1 · GPIO2/3", "gpio-key · rotary-encoder",
                            "pps-gpio · GPIO21", "USB × 2 to the Picos"]):
        text(500, 300 + o + i * 20, ln, 12.5, anchor="middle", fill=COL["dim"])
    # CAN module
    box(40, 160 + o, 230, 110, "MCP2518FD module", ["ATA6563 transceiver", "20 MHz crystal · 120 Ω jumper"])
    wire(f"M270 {200 + o} H390", "spi")
    wire(f"M270 {245 + o} H390", "5v")
    text(330, 191 + o, "SPI0 CE0 + INT", 12, anchor="middle")
    text(330, 236 + o, "fused 5V · GND", 12, anchor="middle")
    # DB9
    box(40, 330 + o, 230, 60, "DB9 to the machine", ["pin 7 H · pin 2 L · pin 3 G"])
    wire(f"M110 {270 + o} V{330 + o}", "ink")
    wire(f"M155 {270 + o} V{330 + o}", "dim")
    wire(f"M200 {270 + o} V{330 + o}", "gnd")
    text(102, 306 + o, "H", 12, anchor="end")
    text(163, 306 + o, "L", 12)
    text(208, 306 + o, "G", 12)
    # ADS1115
    box(40, 440 + o, 230, 110, "ADS1115 · 0x48", ["4 × 1 MΩ / 49.9 kΩ + clamp", "in front of A0-A3"])
    wire(f"M270 {480 + o} H330 V{410 + o} H390", "i2c")
    text(338, 470 + o, "I2C1", 12)
    text(338, 486 + o, "3V3 · GND", 12)
    text(155, 576 + o, "probes 1-4 + COM come in here", 12, anchor="middle", fill=COL["dim"])
    # controls
    box(730, 160 + o, 230, 110, "5 buttons + encoder", ["switches to GND", "Pi pull-ups, no resistors"])
    wire(f"M730 {215 + o} H610", "ctl")
    text(670, 206 + o, "8 GPIO + GND", 12, anchor="middle")
    # Pico 2
    box(730, 320 + o, 230, 90, "Pico 2 · meter", ["firmware/meter-pico"])
    wire(f"M730 {345 + o} H610", "usb")
    wire(f"M730 {390 + o} H610", "sync")
    text(670, 337 + o, "USB", 12, anchor="middle")
    text(670, 382 + o, "SYNC → GPIO21", 12, anchor="middle")
    # logic Pico
    box(730, 470 + o, 230, 90, "Pico · logic analyser", ["sigrok-pico"])
    wire(f"M730 {515 + o} H660 V{430 + o} H610", "usb")
    text(652, 480 + o, "USB", 12, anchor="end")
    wire(f"M845 {410 + o} V{470 + o}", "sync")
    text(855, 445 + o, "SYNC → GP2", 12)

    # power rails
    y0 = 690
    add(f'<line x1="40" y1="{y0 - 20}" x2="960" y2="{y0 - 20}" stroke="#ccc" stroke-width="1"/>')
    text(40, y0 + 6, "Breadboard power rails", 16, "bold")
    r5, r3, rg = y0 + 60, y0 + 140, y0 + 220
    text(40, r5 - 12, "Pi pin 2 (5V)", 13)
    wire(f"M40 {r5} H260", "5v", 3)
    add(f'<rect x="260" y="{r5 - 12}" width="60" height="24" rx="3" fill="#fff" '
        f'stroke="{COL["ink"]}" stroke-width="2"/>')
    wire(f"M266 {r5 + 10} L314 {r5 - 10}", "ink", 2)
    text(290, r5 + 32, "PTC 0.5 A hold (MF-MSMF050-2)", 12, anchor="middle", fill=COL["dim"])
    wire(f"M320 {r5} H900", "5v", 3)
    text(900, r5 - 12, "fused 5 V rail: CAN module 5V", 13, anchor="end")
    text(40, r3 - 12, "Pi pin 1 (3V3)", 13)
    wire(f"M40 {r3} H900", "3v3", 3)
    text(900, r3 - 12, "3.3 V rail: ADS1115, encoder +, CAN 3V3 if needed", 13, anchor="end")
    text(40, rg - 12, "Pi pin 6 or 20 (GND)", 13)
    wire(f"M40 {rg} H900", "gnd", 3)
    text(900, rg + 22, "ground rail: every module, the buttons, the encoder", 13, anchor="end")
    cap(450, r5, rg, "10 µF")
    cap(540, r5, rg, "100 nF")
    cap(660, r3, rg, "10 µF")
    cap(750, r3, rg, "100 nF")
    add("</svg>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "images", "prototype-wiring.svg")
    with open(path, "w") as f:
        f.write(draw())
    print(os.path.normpath(path), file=sys.stderr)
