"""Make Installed options real: the Pi's boot settings and drivers for the parts marked fitted.

A part on the Pi's own pins (a CAN stack on SPI, the ADS1115 on I2C, the sync input, the
buttons, the ESP32's UART) only works once its lines are in ``/boot/firmware/config.txt``. This
works out which lines the options call for (``plan``), and has the root helper
``deploy/raspberry-pi/canopener-boot-config`` write them into the one block it manages
(``apply``). Where the kernel allows it the drivers are loaded straight away; otherwise, and
whenever something is taken out, a restart finishes the job (``restart_needed``).

The helper is the only thing that runs as root, through ``core.interfaces.run_privileged``
(the sudoers rule the Pi installer adds). It knows its own fixed table of lines and
checks every argument against it, so nothing the app passes can put anything else into the boot
config. ``LINES`` here is the app's copy, for showing what will change; a test keeps the two equal.
"""

import os
import time
from dataclasses import dataclass, field

from . import hardware, interfaces

HELPER = "/usr/local/sbin/canopener-boot-config"
CONFIGS = ("/boot/firmware/config.txt", "/boot/config.txt")
BEGIN = "# >>> CANopener installed options (written by the app: System > Installed options) >>>"
END = "# <<< CANopener installed options <<<"
TOKENS_PREFIX = "# options:"
SETUP = "~/CANopener/deploy/raspberry-pi/install.sh"

OSCILLATORS = [("20 MHz (prototype module)", 20000000), ("40 MHz (custom board)", 40000000),
               ("4 MHz", 4000000)]
SPI_SPEEDS = [("Full speed (8.5 MHz)", 0), ("4 MHz", 4000000), ("1 MHz (breadboard wiring)", 1000000)]
GPIO_KEYS = [(5, 59, "F1"), (6, 60, "F2"), (12, 61, "F3"), (13, 62, "F4"), (16, 63, "F5"), (17, 103, "UP"),
             (27, 108, "DOWN"), (22, 105, "LEFT"), (23, 106, "RIGHT"), (26, 28, "SELECT")]
PARTS = ("controls", "can_a", "can_b", "sync", "adc_pi", "esp32")
LIVE = ("can_a", "can_b", "sync", "adc_pi")
CAN = ("can_a", "can_b")


def lines(option: str, osc: int = 20000000, spi: int = 0, pi5: bool = True) -> list[str]:
    """The boot-config lines for one option (the helper's table, mirrored)."""
    if option == "controls":
        out = [f'dtoverlay=gpio-key,gpio={g},active_low=1,gpio_pull=up,keycode={k},label="{n}"'
               for g, k, n in GPIO_KEYS[:9]]
        return out + ["gpio=24,4=ip,pu", "dtoverlay=rotary-encoder,pin_a=24,pin_b=4,relative_axis=1,steps-per-period=1",
                      'dtoverlay=gpio-key,gpio=26,active_low=1,gpio_pull=up,keycode=28,label="SELECT"']
    if option in CAN:
        cs, irq = (0, 25) if option == "can_a" else (1, 20)
        extra = f",speed={spi}" if spi else ""
        return ["dtparam=spi=on", f"dtoverlay=mcp251xfd,spi0-{cs},oscillator={osc},interrupt={irq}{extra}"]
    if option == "sync":
        return ["dtoverlay=pps-gpio,gpiopin=21"]
    if option == "adc_pi":
        return ["dtparam=i2c_arm=on",
                "dtoverlay=ads1115,addr=0x48,cha_enable,cha_cfg=4,chb_enable,chb_cfg=5,chc_enable,chc_cfg=6,"
                "chd_enable,chd_cfg=7"]
    if option == "esp32":
        return ["dtparam=uart0=on"] if pi5 else ["enable_uart=1", "dtoverlay=disable-bt"]
    raise KeyError(option)


def token(option: str, settings) -> str:
    """The helper's name for an option with its settings: 'can_a:osc=20000000:spi=1000000'."""
    if option not in CAN:
        return option
    osc = int(settings.get(f"hardware.{option}.oscillator") or 20000000)
    spi = int(settings.get(f"hardware.{option}.spi_hz") or 0)
    return f"{option}:osc={osc}" + (f":spi={spi}" if spi else "")


def _parse(tok: str) -> tuple[str, int, int]:
    name, *params = tok.split(":")
    osc, spi = 20000000, 0
    for p in params:
        key, _, value = p.partition("=")
        if key == "osc" and value.isdigit():
            osc = int(value)
        elif key == "spi" and value.isdigit():
            spi = int(value)
    return name, osc, spi


def config_path() -> str:
    return next((p for p in CONFIGS if os.path.exists(p)), CONFIGS[0])


def is_pi5() -> bool:
    try:
        with open("/proc/device-tree/model", errors="replace") as f:
            return "Pi 5" in f.read()
    except OSError:
        return True


def current(path: str | None = None) -> list[str]:
    """The options in the managed block now (reading config.txt needs no root)."""
    try:
        with open(path or config_path(), errors="replace") as f:
            rows = f.read().splitlines()
    except OSError:
        return []
    if BEGIN not in rows:
        return []
    for row in rows[rows.index(BEGIN):]:
        if row.startswith(TOKENS_PREFIX):
            return row[len(TOKENS_PREFIX):].split()
        if row == END:
            break
    return []


def wanted(hw: "hardware.Hardware") -> list[str]:
    """Tokens for every part on the Pi's pins that's set to Installed or Use if found."""
    return [token(o, hw.settings) for o in PARTS if hw.state(o) in (hardware.YES, hardware.AUTO)]


@dataclass
class Plan:
    tokens: list                        # what the block should hold
    add: list = field(default_factory=list)       # tokens new or changed
    remove: list = field(default_factory=list)    # option names taken out
    old: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.add and not self.remove

    def describe(self, pi5: bool = True) -> list[str]:
        """What will change, in lines a person can check."""
        out = []
        for tok in self.add:
            name, osc, spi = _parse(tok)
            out.append(f"{hardware.BY_ID[name].name}: adds")
            out += [f"  {line}" for line in lines(name, osc, spi, pi5)]
        for name in self.remove:
            out.append(f"{hardware.BY_ID[name].name}: removes its lines")
        live = [_parse(t)[0] for t in self.add if _parse(t)[0] in LIVE]
        later = [_parse(t)[0] for t in self.add if _parse(t)[0] not in LIVE]
        if live:
            out.append("Loads now: " + ", ".join(hardware.BY_ID[n].name for n in live) + ".")
        if later or self.remove:
            out.append("A restart finishes it" + (" (" + ", ".join(hardware.BY_ID[n].name for n in later + self.remove)
                                                  + ")." if later or self.remove else "."))
        return out


def plan(hw: "hardware.Hardware", path: str | None = None) -> Plan:
    tokens, old = wanted(hw), current(path)
    old_by = {_parse(t)[0]: t for t in old}
    new_by = {_parse(t)[0]: t for t in tokens}
    add = [t for n, t in new_by.items() if old_by.get(n) != t]
    remove = [n for n in old_by if n not in new_by]
    return Plan(tokens, add, remove, old)


def helper_installed() -> bool:
    return os.access(HELPER, os.X_OK)


@dataclass
class Result:
    ok: bool
    message: str
    restart: bool = False               # a restart is needed to finish
    loaded: list = field(default_factory=list)


def apply(p: Plan, settings=None, run=interfaces.run_privileged) -> Result:
    """Write the block, then load what can be loaded now. Asks nothing: the front-end confirms."""
    if not helper_installed():
        return Result(False, f"This unit can't change its boot settings yet. Run once, over SSH or a keyboard:\n"
                             f"  {SETUP}\nthen try again.")
    ok, msg = run([[HELPER, "set", *p.tokens]], timeout=60)
    if not ok:
        return Result(False, f"Couldn't write the boot settings: {msg}")
    restart = bool(p.remove)
    loaded = []
    for tok in p.add:
        name = _parse(tok)[0]
        if name not in LIVE:
            restart = True
            continue
        live_ok, _ = run([[HELPER, "live", tok]], timeout=60)
        if live_ok:
            loaded.append(name)
        else:
            restart = True
    if restart and settings is not None:
        settings.set("hardware.restart_after", time.time())
    names = ", ".join(hardware.BY_ID[n].name for n in loaded)
    text = "Boot settings saved." + (f" Working now: {names}." if names else "")
    if restart:
        text += " Restart to finish."
    return Result(True, text, restart, loaded)


def restart_needed(settings) -> bool:
    """Changes were saved after this boot that only a restart brings in."""
    t = settings.get("hardware.restart_after") or 0
    try:
        with open("/proc/uptime") as f:
            booted = time.time() - float(f.read().split()[0])
    except (OSError, ValueError):
        return False
    return bool(t) and t > booted
