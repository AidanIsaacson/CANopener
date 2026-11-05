"""Retake the handheld screenshots in docs/images/ (device-*.png) from the real Pygame app,
headless, against the simulators:

    python scripts/device_screenshots.py                    # Element Zero, 1280x720 -> docs/images
    python scripts/device_screenshots.py OUT --theme graphite --size 800x480

scripts/readme_screenshots.py runs this too.
"""
import argparse
import os
import sys
import tempfile
import time

# A throwaway, headless environment *before* importing Pygame / the app (hence the E402s).
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["HOME"] = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()

import pygame  # noqa: E402

from canopener.core.settings import Settings  # noqa: E402
from canopener.device.app import DeviceApp  # noqa: E402
from canopener.device.context import DeviceContext  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("out", nargs="?", default=os.path.join(os.path.dirname(__file__), "..", "docs", "images"))
ap.add_argument("--theme", default="element-zero")
ap.add_argument("--size", default="1280x720")
ap.add_argument("--prefix", default="device-")
a = ap.parse_args()
size = tuple(int(v) for v in a.size.lower().split("x"))

pygame.display.init()
pygame.font.init()
s = Settings()
s.set("device.theme", a.theme)
s.set("meters.sim", True)
s.set("show_simulators", True)
ctx = DeviceContext(s)
app = DeviceApp(ctx, size, encoder=False)


def run(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.step([])
        time.sleep(0.03)


def save(name: str):
    app.step([])
    path = os.path.join(a.out, a.prefix + name)
    app.screenshot(path)
    print("saved", path)


def page(title: str):
    can = app.top
    for i, p in enumerate(can.pages):
        if p.title == title:
            can._go(i)


def no_toast():
    """Let the last message fade (they'd cover the bottom of the shot)."""
    end = time.time() + 8
    while ctx.current_toast() and time.time() < end:
        run(0.2)


ctx.connect("sim")
ctx.wait_idle()
run(1)
no_toast()
save("home.png")

app.open_mode("can")
page("Codes")
ctx.read_codes("active")
ctx.wait_idle(10)
run(0.3)
no_toast()
app.top.page.view.focus = 0
save("codes.png")

page("Live")
run(4)                                       # let the traces fill
live = app.top.page
for i in range(3):                          # engine speed, torque, pedal: picked, shown big
    live.view.focus = i
    app.action("select")
live.big = True
run(8)
save("live-grid.png")

app.open_mode("meter")
run(4)
save("meter.png")

app.open_mode("log")
ctx.start_log()
run(3)
ctx.tag()
run(2)
ctx.tag()
run(2)
no_toast()
save("log.png")
ctx.stop_log()
ctx.wait_idle()
ctx.discard_log()
no_toast()

app.open_mode("settings")
run(0.3)
save("system.png")

ctx.shutdown()
pygame.quit()
sys.exit(0)
