# Logic Pico (original Pico, RP2040): stock sigrok-pico

Nothing custom runs here (docs/ELECTRONICS.md, D2). The Pi drives it through `sigrok-cli`
(`canopener/core/logic.py`), so sigrok's protocol decoders (CAN, LIN, UART, SPI, I2C, PWM ...)
work unchanged.

## Flash

Download the sigrok-pico UF2 from <https://github.com/pico-coder/sigrok-pico> (releases), hold
BOOTSEL, plug in, copy it onto the drive. Then:

```bash
canopener-cli hardware --set logic_pico=yes -v
```

If it isn't recognised, look in `/dev/serial/by-id/` and set it:
`canopener-cli hardware --port logic_pico=/dev/serial/by-id/usb-...`.

## sigrok-cli on the Pi

The `raspberrypi-pico` driver has been in mainline libsigrok since 2023, but the sigrok-cli in
Debian / Pi OS (0.7.2) predates it. Build libsigrok and sigrok-cli from source:

```bash
sudo apt install git build-essential autoconf autoconf-archive automake libtool pkg-config \
  libglib2.0-dev libzip-dev libusb-1.0-0-dev libftdi1-dev libserialport-dev python3-dev
git clone git://sigrok.org/libsigrok && cd libsigrok && ./autogen.sh && ./configure && make -j4 && sudo make install && cd ..
git clone git://sigrok.org/libsigrokdecode && cd libsigrokdecode && ./autogen.sh && ./configure && make -j4 && sudo make install && cd ..
git clone git://sigrok.org/sigrok-cli && cd sigrok-cli && ./autogen.sh && ./configure && make -j4 && sudo make install
sudo ldconfig
sigrok-cli -L | grep raspberrypi-pico      # should list the driver
```

[TBD: confirm the package list on the Pi OS release in use; libserialport must be found by
libsigrok's configure, or the serial drivers are left out.]

## Channels

| sigrok | Wired to | Notes |
|---|---|---|
| D2 | SYNC from the meter Pico GP15 | Always recorded: it places the capture on the CAN timeline |
| D3-D6 | Probe 1-4 comparator outputs | Thresholds set by the meter Pico's DAC (D11) |
| D7-D14 | Low-voltage logic header | 2.2 k series + clamp per pin, 3.3/5 V only |
| D15-D22 | Spare | |

Check that sigrok's D-numbers are the Pico's GPIO numbers before wiring (ELECTRONICS.md 5.6). The
names in `core/logic.CHANNELS` are what front-ends show.

Speed (per the sigrok-pico guide): up to 120 Msps with 1-4 channels, 60 Msps or less for 8+;
long captures fall back to about 500 ksps with RLE. The SYNC pulse is a few ms wide, so any
sample rate above 1 kHz sees it.
