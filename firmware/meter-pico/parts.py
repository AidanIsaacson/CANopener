# Small drivers for the meter Pico's I2C and SPI parts. MicroPython.
# Register details are from the datasheets; each one marked [verify] wants a bench check.

import time

from machine import Pin


class MCP4728:
    """Quad 12-bit DAC: the four comparator thresholds. VDD reference, gain 1."""

    def __init__(self, i2c, addr, vref):
        self.i2c, self.addr, self.vref = i2c, addr, vref

    def set_volts(self, channel, volts):
        code = max(0, min(4095, int(volts / self.vref * 4095 + 0.5)))
        # Multi-write: 0b01000 CH1 CH0 UDAC=0, then VREF=0 PD=00 GAIN=0 D11-D8, D7-D0
        self.i2c.writeto(self.addr, bytes([0x40 | (channel << 1), (code >> 8) & 0x0F, code & 0xFF]))
        return code * self.vref / 4095


class ADS1115:
    """16-bit ADC, single-shot, AIN0 vs GND, +/-4.096 V: the ohms node."""

    CONFIG = 0xC383             # OS=1, MUX=100 (AIN0), PGA=001 (4.096 V), single-shot, 128 SPS, comp off

    def __init__(self, i2c, addr):
        self.i2c, self.addr = i2c, addr

    def read_volts(self):
        self.i2c.writeto_mem(self.addr, 0x01, self.CONFIG.to_bytes(2, "big"))
        for _ in range(20):                         # 128 SPS: ~8 ms
            time.sleep_ms(1)
            if self.i2c.readfrom_mem(self.addr, 0x01, 2)[0] & 0x80:
                break
        raw = int.from_bytes(self.i2c.readfrom_mem(self.addr, 0x00, 2), "big")
        if raw & 0x8000:
            raw -= 0x10000
        return raw * 4.096 / 32768


class INA226:
    """Current and bus voltage of the test source (shunt 2.5 uV/LSB, bus 1.25 mV/LSB)."""

    def __init__(self, i2c, addr, shunt_ohms):
        self.i2c, self.addr, self.shunt = i2c, addr, shunt_ohms
        # 16 averages, 1.1 ms conversions, shunt + bus continuous [verify against the datasheet]
        self.i2c.writeto_mem(addr, 0x00, (0x4527).to_bytes(2, "big"))

    def _reg(self, r, signed):
        v = int.from_bytes(self.i2c.readfrom_mem(self.addr, r, 2), "big")
        return v - 0x10000 if signed and v & 0x8000 else v

    def current_a(self):
        return self._reg(0x01, True) * 2.5e-6 / self.shunt

    def bus_volts(self):
        return self._reg(0x02, False) * 1.25e-3


class ADS8584S:
    """4-channel simultaneous 16-bit ADC, serial mode, read by bit-banging.

    Bit-banged reads manage a few thousand conversions a second: plenty for meter mode
    (oversampled readings at 10-50 Hz). Waveform streaming at 100-330 kSPS needs PIO + DMA and
    most likely the C SDK (firmware/meter-pico/README.md). Timing and bit order [verify]
    against the datasheet before trusting a reading."""

    FULL_SCALE = 10.0           # RANGE pin at +/-10 V (docs/ELECTRONICS.md 6.1)

    def __init__(self, sclk, cs, douta, doutb, convst, busy, reset):
        self.sclk = Pin(sclk, Pin.OUT, value=0)
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.douta = Pin(douta, Pin.IN)
        self.doutb = Pin(doutb, Pin.IN)
        self.convst = Pin(convst, Pin.OUT, value=0)
        self.busy = Pin(busy, Pin.IN)
        self.reset = Pin(reset, Pin.OUT, value=0)

    def present(self):
        """Reset, start one conversion and watch BUSY go high then low (about 2 us)."""
        self.reset.value(1)
        time.sleep_us(1)
        self.reset.value(0)
        time.sleep_ms(1)
        self.convst.value(1)
        seen = any(self.busy.value() for _ in range(50))
        self.convst.value(0)
        t0 = time.ticks_us()
        while self.busy.value():
            if time.ticks_diff(time.ticks_us(), t0) > 100:
                return False
        return seen

    def convert(self):
        """One simultaneous conversion -> [v1, v2, v3, v4] at the ADC pins (volts)."""
        self.convst.value(1)
        self.convst.value(0)
        t0 = time.ticks_us()
        while self.busy.value():
            if time.ticks_diff(time.ticks_us(), t0) > 50:
                return None
        a = b = 0
        self.cs.value(0)
        for _ in range(32):                 # DOUTA: ch1 then ch2; DOUTB: ch3 then ch4
            self.sclk.value(1)
            self.sclk.value(0)
            a = (a << 1) | self.douta.value()
            b = (b << 1) | self.doutb.value()
        self.cs.value(1)
        out = []
        for word in (a >> 16, a & 0xFFFF, b >> 16, b & 0xFFFF):
            if word & 0x8000:
                word -= 0x10000
            out.append(word * self.FULL_SCALE / 32768)
        return out
