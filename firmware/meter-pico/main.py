# CANopener meter firmware for the Pico 2 (RP2350). MicroPython sketch for bring-up (stage 2b on).
#
# Talks the meter line protocol over USB serial (canopener/core/meters.py): a hello listing its
# channels and the parts it found, readings with the Pico's microsecond clock, a line per SYNC
# pulse, and commands back from the Pi. The Pi maps t_us to wall time with the SYNC pulses
# (canopener/core/sync.py).
#
# Protection lives in the hardware (docs/ELECTRONICS.md section 2). What's here is the user-facing
# part: refuse ohms or the test source on a powered circuit, switch the source off on a fault.
#
# Copy config.py, parts.py and main.py to the Pico (mpremote cp *.py :) and reset it.

import json
import select
import sys
import time

import config as C
from machine import ADC, I2C, Pin, Timer
from parts import ADS1115, ADS8584S, INA226, MCP4728

VERSION = "0.1-sketch"


# ---------------------------------------------------------------------------
# Clock: ticks_us wraps every ~18 minutes, so extend it to 64 bits
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.last = time.ticks_us()
        self.us = 0

    def extend(self, raw):
        self.us += time.ticks_diff(raw, self.last)
        self.last = raw
        return self.us

    def now(self):
        return self.extend(time.ticks_us())


clock = Clock()


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


def status(level, text):
    send({"status": level, "text": text})


# ---------------------------------------------------------------------------
# SYNC: a hardware timer raises the pin and records the time; the main loop drops it again
# when it reports the pulse (width a few ms; only the rising edge matters).
# ---------------------------------------------------------------------------
sync_pin = Pin(C.SYNC, Pin.OUT, value=0)
sync_raw = []                   # raw ticks_us of edges not reported yet (filled in the IRQ)
sync_count = 0


def _sync_irq(_t):
    if sync_pin.value():        # main loop hasn't dropped the last one: no edge this time
        return
    sync_pin.value(1)
    if len(sync_raw) < 8:
        sync_raw.append(time.ticks_us())


def report_sync():
    global sync_count
    while sync_raw:
        raw = sync_raw.pop(0)
        send({"sync": sync_count, "t_us": clock.extend(raw)})
        sync_count += 1
    sync_pin.value(0)


# ---------------------------------------------------------------------------
# Parts: probe what's fitted, so the Pi knows (core/hardware.py "meter" options)
# ---------------------------------------------------------------------------
i2c = I2C(C.I2C_ID, sda=Pin(C.I2C_SDA), scl=Pin(C.I2C_SCL), freq=C.I2C_HZ)
found = set(i2c.scan())
dac = MCP4728(i2c, C.ADDR_DAC, C.DAC_VREF) if C.ADDR_DAC in found else None
ohms_adc = ADS1115(i2c, C.ADDR_OHMS_ADC) if C.ADDR_OHMS_ADC in found else None
ina = INA226(i2c, C.ADDR_INA226, C.SRC_SHUNT_OHMS) if C.ADDR_INA226 in found else None
ads = ADS8584S(C.ADS_SCLK, C.ADS_CS, C.ADS_DOUTA, C.ADS_DOUTB, C.ADS_CONVST, C.ADS_BUSY, C.ADS_RESET)
if not ads.present():
    ads = None

ohms_en = Pin(C.OHMS_EN, Pin.OUT, value=0)
src_en = Pin(C.SRC_EN, Pin.OUT, value=0)
src_fault = Pin(C.SRC_FAULT, Pin.IN, Pin.PULL_UP)
pullups = {ch: Pin(gp, Pin.OUT, value=0) for ch, gp in C.PULLUP.items()}
can_adc = {ch: ADC(gp) for ch, gp in C.CAN_ADC.items()}
pico_probe = None
if C.PROBE_ON_PICO_ADC:
    pico_probe = (C.PROBE_ON_PICO_ADC[0], ADC(C.PROBE_ON_PICO_ADC[1]))
    can_adc = {k: v for k, v in can_adc.items() if C.CAN_ADC[k] != C.PROBE_ON_PICO_ADC[1]}


def options():
    out = []
    if dac:
        out.append("dac")
    if ohms_adc:
        out.append("ohms")
    if ina:
        out.append("src")
    if ads:
        out.append("ads8584s")
    return out


def hello():
    chans = [{"id": ch, "name": ch.replace("_", " ").upper().replace("CAN ", "CAN-"), "unit": "V",
              "decimals": 2, "kind": "voltage"} for ch in can_adc]
    if ads:
        chans += [{"id": p, "name": f"Probe {p[1]}", "unit": "V", "decimals": 3, "kind": "voltage"}
                  for p in ("p1", "p2", "p3", "p4")]
    elif pico_probe:
        chans.append({"id": pico_probe[0], "name": f"Probe {pico_probe[0][1]}", "unit": "V", "decimals": 2,
                      "kind": "voltage"})
    if ohms_adc:
        chans.append({"id": "r", "name": "Resistance", "unit": "ohm", "decimals": 1, "kind": "resistance"})
    if ina:
        chans.append({"id": "src_i", "name": "Source current", "unit": "mA", "decimals": 2, "kind": "current"})
        chans.append({"id": "src_v", "name": "Source rail", "unit": "V", "decimals": 2, "kind": "voltage"})
    send({"hello": "canopener-meter", "name": "Meter Pico", "version": VERSION, "options": options(),
          "channels": chans})


# ---------------------------------------------------------------------------
# State and readings
# ---------------------------------------------------------------------------
state = {"rate": C.DEFAULT_RATE_HZ, "ohms": False, "src": False,
         "thresholds": {}, "auto": set()}      # probe -> tip volts; probes on auto-threshold
probe_v = {}                                   # latest probe volts, for pre-checks
span = {}                                      # probe -> [min, max] since the last auto update


def read_probes():
    if ads:
        acc = [0.0] * 4
        n = 0
        for _ in range(C.OVERSAMPLE):
            v = ads.convert()
            if v:
                acc = [a + b for a, b in zip(acc, v)]
                n += 1
        if not n:
            return {}
        return {p: acc[i] / n * C.PROBE_RATIO[p] for i, p in enumerate(("p1", "p2", "p3", "p4"))}
    if pico_probe:
        ch, adc = pico_probe
        raw = sum(adc.read_u16() for _ in range(C.OVERSAMPLE)) / C.OVERSAMPLE
        return {ch: raw * 3.3 / 65535 * C.PROBE_RATIO[ch]}
    return {}


def read_all(t_us):
    for ch, adc in can_adc.items():
        raw = sum(adc.read_u16() for _ in range(C.OVERSAMPLE)) / C.OVERSAMPLE
        send({"ch": ch, "v": round(raw * 3.3 / 65535 * C.CAN_DIVIDER + C.CAN_OFFSET, 3), "t_us": t_us})
    for p, v in read_probes().items():
        probe_v[p] = v
        lo_hi = span.setdefault(p, [v, v])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], v), max(lo_hi[1], v)
        send({"ch": p, "v": round(v, 4), "t_us": t_us})
    if ohms_adc:
        if state["ohms"]:
            v = ohms_adc.read_volts()
            if v >= C.OHMS_REF_V * 0.98:
                send({"ch": "r", "v": None, "flag": "OL", "t_us": t_us})
            else:
                send({"ch": "r", "v": round(C.OHMS_SERIES * v / (C.OHMS_REF_V - v), 1), "t_us": t_us})
        else:
            send({"ch": "r", "v": None, "flag": "off", "t_us": t_us})
    if ina:
        send({"ch": "src_i", "v": round(ina.current_a() * 1000, 3), "t_us": t_us})
        send({"ch": "src_v", "v": round(ina.bus_volts(), 3), "t_us": t_us})


def auto_thresholds():
    """Threshold halfway between what each auto probe has seen (docs/ELECTRONICS.md 6.2)."""
    for p in state["auto"]:
        lo_hi = span.get(p)
        if lo_hi and lo_hi[1] - lo_hi[0] > 0.5:
            set_threshold(p, (lo_hi[0] + lo_hi[1]) / 2)
    span.clear()


def set_threshold(p, tip_v):
    if not dac:
        return False
    ch = int(p[1]) - 1
    state["thresholds"][p] = tip_v
    dac.set_volts(ch, tip_v / C.THRESHOLD_RATIO)
    return True


def src_off(why=""):
    src_en.value(0)
    for pin in pullups.values():
        pin.value(0)
    state["src"] = False
    if why:
        status("error", why)


# ---------------------------------------------------------------------------
# Commands from the Pi (one JSON object per line)
# ---------------------------------------------------------------------------
def command(cmd):
    c = cmd.get("cmd")
    if c == "hello":
        hello()
    elif c == "rate":
        state["rate"] = max(1, min(50, int(cmd.get("hz", C.DEFAULT_RATE_HZ))))
        status("ok", f"rate {state['rate']} Hz")
    elif c == "threshold":
        p, v = cmd.get("ch"), cmd.get("v")
        if p not in ("p1", "p2", "p3", "p4") or not dac:
            return status("refused", "no threshold DAC" if not dac else f"no probe {p}")
        if v == "auto":
            state["auto"].add(p)
            return status("ok", f"{p} threshold: auto")
        state["auto"].discard(p)
        set_threshold(p, float(v))
        status("ok", f"{p} threshold {float(v):.2f} V")
    elif c == "mode":
        if cmd.get("ch") != C.OHMS_PROBE or not ohms_adc:
            return status("refused", f"ohms is only on {C.OHMS_PROBE}" if ohms_adc else "no ohms channel")
        on = cmd.get("mode") == "ohms"
        if on:
            ohms_en.value(0)
            if abs(ohms_adc.read_volts()) > C.OHMS_PRECHECK_V:
                return status("refused", "circuit is powered: measure ohms with the power off")
        ohms_en.value(1 if on else 0)
        state["ohms"] = on
        status("ok", "ohms on" if on else "volts")
    elif c == "src":
        if not ina:
            return status("refused", "no test source fitted")
        if not cmd.get("on"):
            src_off()
            return status("ok", "source off")
        if "p4" not in probe_v:
            return status("refused", "can't check probe 4 for an outside voltage (no probe ADC)")
        if abs(probe_v["p4"]) > C.SRC_PRECHECK_V:
            return status("refused", f"circuit is powered ({probe_v['p4']:.1f} V on probe 4)")
        src_en.value(1)
        state["src"] = True
        status("ok", "source on")
    elif c == "pullup":
        pin = pullups.get(cmd.get("ch"))
        if pin is None:
            return status("refused", "pull-ups are on p1 and p2")
        if cmd.get("on") and not state["src"]:
            return status("refused", "switch the source on first (pull-ups run from its rail)")
        pin.value(1 if cmd.get("on") else 0)
        status("ok", f"pull-up {cmd.get('ch')} {'on' if cmd.get('on') else 'off'}")
    else:
        status("refused", f"unknown command {c}")


poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
line_buf = ""


def read_commands():
    global line_buf
    while poll.poll(0):
        ch = sys.stdin.read(1)
        if ch == "\n":
            try:
                command(json.loads(line_buf))
            except (ValueError, TypeError, KeyError) as e:
                status("refused", f"bad command: {e}")
            line_buf = ""
        elif len(line_buf) < 256:
            line_buf += ch


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    hello()
    Timer(period=C.SYNC_PERIOD_MS, mode=Timer.PERIODIC, callback=_sync_irq)
    next_read = time.ticks_ms()
    next_auto = time.ticks_add(next_read, 1000)
    while True:
        report_sync()
        read_commands()
        if state["src"] and not src_fault.value():
            src_off("test source tripped: short or overload on probe 4")
        now = time.ticks_ms()
        if time.ticks_diff(now, next_read) >= 0:
            next_read = time.ticks_add(next_read, 1000 // state["rate"])
            read_all(clock.now())
        if time.ticks_diff(now, next_auto) >= 0:
            next_auto = time.ticks_add(now, 1000)
            auto_thresholds()
        time.sleep_ms(1)


main()
