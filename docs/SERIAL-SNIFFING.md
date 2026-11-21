# Serial lines: tapping, recording and working out a console port

Some controllers talk to their service console over a serial line instead of CAN (older Zapi
controllers' current-loop console port, for one). CANopener can record such a line beside the CAN
bus, with timestamps, and help work out what the bytes mean. This page is the working notes: how to
tap a line without breaking it, how to record it, what the analysis shows, and how we keep what
we learn clean enough to publish.

What CANopener has today: recording and analysis from the [command line](USAGE.md#serial-lines)
(`canopener-cli serial`), and serial lines in a capture (`serial.log`, see
[Recording a job](USAGE.md#recording-a-job-the-log-tool)). The console mode built on top is
[ZAPI-CONSOLE.md](ZAPI-CONSOLE.md).

## 1. Tapping the line

**Measure before you connect anything.** With the key on, use the meter on each console pin
against the controller's ground, and across each pair. Write the numbers down: they decide the
wiring, and they go in your notes.

Sniffing a two-way link takes **one input per direction**: what the console sends and what the
controller sends. Two USB-serial adapters do it, or one dual-port adapter (FT2232-type). Each
adapter's **RX** and **GND** are all you connect: never its TX while listening.

| The line is | Tap it with |
|---|---|
| 3.3 V or 5 V logic (TTL UART) | A USB-TTL adapter's RX (and GND) on each wire. A 5 V line needs a 5 V-tolerant adapter, or a 2.2 kohm series resistor. |
| RS-232 (about +/-5 to 12 V) | A USB-RS232 adapter's RX (DB9 pin 2) and GND (pin 5) on each wire. |
| Current loop (pin pairs like `+RX` / `-RX`, `+TX` / `-TX`) | An optocoupler's LED **in series** in each loop, its output into a USB-TTL adapter's RX. See below. |

### Current loop

In a current loop, the sender switches a current (often around 20 mA) on and off through a pair
of wires, and the receiver is usually an optocoupler's LED. To listen without taking part, put a
second optocoupler's LED in series in the same loop:

```
 sender +  ──────►|────── receiver +          (LED of the tap's optocoupler, in series:
                 tap LED                        cut the wire and put it in between)
 sender -  ─────────────── receiver -

 tap output (emitter follower, current = high = UART idle):
   3.3 V ── collector ┐
                      ├─ emitter ──┬── adapter RX
                                   └── 4.7 kohm ── adapter GND
```

* Do this on a **Y-cable** between the console and the controller, so nothing on the machine is cut.
* The LED drops about 1.2 V. Most loops have that much to spare; if the console stops talking
  with the tap in place, this loop doesn't.
* The optocoupler keeps the truck's wiring electrically apart from the laptop or handheld, which
  matters on a traction controller. Power its output side from the USB adapter, never from the
  console port's supply pin.
* A PC817-type part is fine to about 9600 baud with the output resistor at 1-4.7 kohm; use a
  faster one (6N137-type) beyond that.
* Bytes that are junk at the right baud rate (see [Analysis](#3-what-the-analysis-shows)) can
  mean the line idles the other way: move the resistor to the collector side (output inverted),
  or invert RX in the adapter's settings.
* An LED in the wrong way round passes no current and breaks the loop: if the console goes quiet,
  turn it round.

### The baud rate

If you don't know it, record the line with the logic Pico (the tap's output on a logic channel)
and let CANopener measure the narrowest pulses:

```bash
canopener-cli serial baud capture.sr --channel D3
```

USB adapters bunch bytes together before passing them on, which blurs the gaps between messages.
On Linux, `canopener-cli serial ports` shows an FTDI adapter's latency timer; 1 ms keeps
messages apart (`echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer`).

## 2. Recording

```bash
canopener-cli serial ports                       # which adapter is which
canopener-cli serial sniff --line console=/dev/ttyUSB0 --line controller=/dev/ttyUSB1 \
    --framing "9600 8N1" --plan "connect,main menu,tester,throttle,key off" --show
```

Press **Enter** to tag the next timestamp, type a label and Enter to add your own, **q** to stop.
It's saved as an ordinary capture (in `~/canopener/logs/`), with `serial.log`, a
`serial_analysis.txt` and the usual `README.md`. Do **one thing per timestamp**, as with
[working out a CAN bus](USAGE.md#working-out-a-machine-from-what-its-bus-does): open one menu,
move one control, hold it a few seconds.

## 3. What the analysis shows

`canopener-cli serial analyze CAPTURE_FOLDER` (it's also written to `serial_analysis.txt` when a
capture is saved):

* **Messages** are rebuilt from the chunks the adapters delivered, ending at 10 ms of quiet
  (`--gap` to change it) or when the other side starts talking.
* **Message kinds** group messages by line, length and first byte (`--head 2` for two), and say
  what each byte does: constant, a counter, a switch, a value, or noise (often a checksum).
* **Checksums that fit**: the usual sums, XORs and CRC-8 / CRC-16 variants, tested at the end of
  every message. A rule that fits nearly all of them is almost certainly the real one.
* **Exchanges**: which message is answered by which, how fast, and which line asks (usually the
  console).
* **Text**: menu screens are often plain characters. Text that only appears with the top bit
  cleared means the line is probably 7 data bits plus parity: record again at 7E1 or 7O1.
* **By timestamp**: kinds that started or stopped, and bytes whose usual value moved, at each
  timestamp.
* **Framing**: mostly 00 / 80 / F0 / FF bytes is how a wrong baud rate looks.

## 4. Keeping it clean (what can go in this repo)

What we work out ourselves, from our own recordings of our own machines and tools, can go in this
public repo: message layouts, checksums, commands, menu texts. For that to hold up if anyone asks
where it came from:

* **Keep the evidence.** Every finding names the capture it came from (the folder name), the
  machine and the action that showed it, in the findings table of the mode's page
  ([ZAPI-CONSOLE.md](ZAPI-CONSOLE.md#findings)). Keep those capture folders.
* **Our own words.** Names and descriptions are ours, written from what we saw. Menu text the
  controller itself sends is data on the wire; quote it only as far as needed to recognise a screen.
* **Never from the maker's tools or paperwork.** No decompiled or disassembled console or PC
  software, no files extracted from them, no dealer or service manuals under licence or NDA, no
  leaked documents, however they're reworded. Anything from those sources stays out of this
  repo (CLAUDE.md), and a finding that leans on one isn't "ours" any more. A maker's freely published manual (connector pinouts, alarm lists) is
  fine to cite by name.
* **Unconfirmed stays marked.** A guess is `?` until a recording confirms it.

This is how we keep the work defensible, not legal advice: for anything commercial, ask a lawyer.
