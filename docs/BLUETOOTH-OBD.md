# Bluetooth OBD adapters: testing notes and gaps

How the Bluetooth OBD adapter support was built, what has and hasn't been checked against real
hardware, and how to test each piece yourself. **[Gaps to fill](#gaps-to-fill)** is the list to work
through as data comes in; **[Adapters tried](#adapters-tried)** records what each adapter does.

Using it (pairing, connecting, what works): [USAGE.md](USAGE.md#bluetooth-obd-adapters). How the
code fits together: [ARCHITECTURE.md](ARCHITECTURE.md#bluetooth-obd-adapters-elm327).

## Gaps to fill

Everything below was built from the ELM327 data sheet and SAE J1979 as we read them, and tested
against CANopener's own simulated adapter (`sim/elm.py`), **not** against a real adapter or
vehicle yet. When you check one, update its row: what you found, the date, and the adapter /
vehicle. Fix the code (and the simulator, so the tests keep it fixed) when it's wrong.

| # | Gap | How to check / fill it | Status |
|---|---|---|---|
| 1 | **A real adapter's replies** match what the backend expects: headers on show the PCI byte on CAN (`7E8 06 41 00 ...`), multi-frame answers come as raw frames, `ATAR` / `ATCRA` / `ATCP` are accepted | `canopener-cli bluetooth probe` and `canopener-cli obd --elm` on any CAN car. Paste the probe into [Adapters tried](#adapters-tried). | not checked |
| 2 | **Timing** on a real adapter (round trip, values per second) | `canopener-cli obd --elm --bench 10` on the F-550 and a car. Record it in Adapters tried. | not checked |
| 3 | **Response pending** (7F xx 78) after clearing codes: does the adapter keep listening (we raise its timeout with `ATSTFF` for clears / tests / resets)? | Clear codes on a car that's OK to clear, with the Log tool recording; check the clear result says *cleared* for each module. | **partly, 2026-11-13** (vLinker MC+, ISO 9141-2 forklift): Mode 04 answered `44` straight away and the lamp went out; no response-pending seen, so the long-wait path is still unchecked |
| 4 | **Non-CAN cars** (J1850 PWM / VPW, ISO 9141-2, KWP2000): the reply shapes in `obd/noncan.py` (header + check byte shown with headers on; 3 codes per message; VIN in 5 numbered messages; the sequence byte on Mode 09 PID 00) | A 1996-2007 non-CAN car: `CANOPENER_ELM_TRACE=~/elm-trace.txt canopener-cli obd --elm --all` keeps the adapter's raw text (see [below](#raw-adapter-trace)). Compare VIN, codes and live data with another scan tool, and the raw lines with `obd/noncan.py`'s docstring. | **partly, 2026-11-13** (vLinker MC+, ISO 9141-2 forklift, engine at 0x10): header + check byte as expected (`486B104100...`), Mode 03 as three codes per message, Mode 04 `486B104407`. VIN / Mode 09, live data and J1850 / KWP still unchecked (that machine answers none of them: see "Thin K-line computers") |
| 5 | **Mode 06 on non-CAN cars** uses an older layout CANopener doesn't decode: left out (the On-Board Tests page stays empty there) | Needs a decoder for the J1979 non-CAN Mode 06 format, and a car to check it on. | not done |
| 6 | **Freeze frame, Mode 08 (EVAP test) on non-CAN cars**: passed through unchanged, unverified | A non-CAN car with a stored code. | not checked |
| 7 | **J1939 through the adapter** (protocol A): monitoring with `ATMA` keeps up or reports BUFFER FULL; `ATJHF0` / `ATCSM1` accepted; requests sent raw (`ATCAF0`) reach the modules and their answers arrive | A truck or machine with a 16-to-9-pin Deutsch cable: `canopener-cli bluetooth probe --listen 5 --j1939`, then `canopener-cli scan --elm --listen-only` and `canopener-cli scan --elm --full`. Watch `monitor_restarts` (frames lost to BUFFER FULL). | not checked |
| 8 | **J1939 multi-packet answers to us** (RTS/CTS): our CTS goes out between monitoring sessions; may be too slow on a real adapter | A Full Scan through the adapter; compare Component ID / VIN with a wired scan of the same machine. | not checked |
| 9 | **User protocols B / C** (`ATPB`): the backend assumes the data sheet defaults (11-bit, 125k / 50k), whatever ATPB sets | Only matters for plugin bus setups (Ford MS-CAN). Check with the adapter's own `ATDP` after switching. | not checked |
| 10 | **Battery voltage accuracy** (`ATRV`): adapters are often +/- 0.1-0.2 V | Compare with a meter at the battery, key on and engine running. | not checked |
| 11 | **Python's Bluetooth sockets on the Pi** (`socket.AF_BLUETOOTH`): Pi OS's python3 should have them; the fallback is `/dev/rfcomm0` | `python3 -c "import socket; print(socket.AF_BLUETOOTH)"` in the Pi's venv. | not checked |
| 12 | **Pairing through `bluetoothctl`** on a real Pi 5 and laptop (the PIN prompt, a dual-mode adapter listed twice) | Pair / Manage Adapters → Scan → Pair. | not checked |
| 13 | **The vLinker MC+'s own commands** (Ford MS-CAN switching, the STN commands its manual mentions) | Private plugin: its `docs/ford-ms-can.md`, "Gaps to fill". | not started |
| 14 | **Multi-PID requests** (up to 6 values per request on CAN) would roughly quadruple live data over an adapter, and help wired too | Needs `obd.decode` and the passive decoder to take multi-PID answers. Decide after gap 2's numbers. | not done |

## Thin K-line computers: the deep probe

Some engine computers on ISO 9141-2 / KWP2000 (off-road and industrial engines especially) answer
generic OBD-II only as far as they had to. The first real one we met (2026-11-13, an LPG forklift)
answers `0100` with all zeros (no live data), `03` with no codes **while its lamp was on with a
code its own blink-out reported**, nothing to `07` / `0A` / `09`, and accepts `04` (which did put
the lamp out). So "no stored codes" from such a machine doesn't mean there are none.

The **deep probe** finds out what else the computer answers, without changing anything
(`can/kline_probe.py`; handheld **Codes > More... > Deep probe**, or
`canopener-cli bluetooth kline --out probe.txt`):

1. the generic OBD-II reads, asked whether or not they're listed as supported;
2. each K-line wake-up (ISO 9141-2, KWP2000 5-baud, KWP2000 fast) with the standard request;
3. KWP2000 (ISO 14230-3) reads addressed to the module itself, from tester addresses 0xF1 and
   0xFC: tester present (`3E`), identification (`1A 80 / 90 / 91 / 94`), codes by status
   (`18 00 FF 00`, `18 02 FF 00`, `18 02 FF FF`), the older code read (`13`), data blocks
   (`21 01`, `21 00`). If a read is refused, it starts the default diagnostic session (`10 81`)
   and asks once more.

4. where step 3 shows the data-block read exists (it answered, or refused only the block
   number): every block number (`21 00` - `21 FF`) and identification option (`1A 80` - `1A 9F`),
   and whether `12`, `17` and `22` exist.

**First result (2026-11-13, the same forklift, vLinker MC+):** generic OBD-II stays empty however
it's asked (`0120`, `0200`, `0900` answer with zeros; everything else NO DATA). KWP2000 with the
OBD header doesn't wake it (5-baud or fast). **KWP2000 fast init to module 0x10 from tester 0xFC
does** (key bytes `5D 8F`), and not from 0xF1. In that session: `3E` answers, `1A 80` answers
`5A 00 00`, service `21` exists (blocks 00 and 01 refused as numbers, `7F 21 12`), while `10`,
`13`, `18` and the OBD modes are "service not supported". 5-baud init at address 0x10 gets nothing.

**Second result (2026-11-13, 0.3.5):** all 256 data-block numbers are refused the same way
(`7F 21 12`), so service `21` wants something else or has nothing in this session. Services `12`
and `22` exist (`7F 12 12`, `7F 22 12`); `17` does not. Identification: `1A 81` and `1A 82` give
short ASCII strings, `1A 83` four bytes after about 5 s of "response pending" with the key on and
engine off, and a refusal (`7F 1A 94`) with the engine running. Replies don't echo the option byte.

5. where service `22` exists: every two-byte page sampled (`22 xx 00` / `22 xx 01`), then a full
   scan of the pages that answered and of pages 11-15 (0.3.6; up to ten minutes in all).

**Third result (2026-11-13, 0.3.6):** no common identifier answers in the plain shape either:
all 512 samples and all of pages 11-15 are refused `7F 22 12`. From 0.3.7 the probe first tries
a few numbers with extra bytes after them (`01`, `04`, `04 01`, `01 01`), and the full scans are
optional (handheld: the second menu entry; `--scan`).

It leaves 5.5 s of silence before each new wake-up (a module ignores one until its last session
has timed out), so it takes two to four minutes, then sets the adapter up again from scratch. The
transcript (`kline_probe.txt`) has every command, how long it took, and each reply line as the
adapter printed it, 'BUS INIT' lines included. What a maker's identification, codes or data blocks
mean is not in this repository: that goes in a profile or the plugin, from our own captures.

## Adapters tried

Paste `canopener-cli bluetooth probe --protocols` output (or **Test** in Pair / Manage Adapters,
which has a Copy button) and the bench numbers here, one section per adapter.

| Adapter | Firmware (ATI) | Probe date | Notes |
|---|---|---|---|
| (simulated) | ELM327 v2.1 | | `sim/elm.py`: everything answers; not a real reference |

## Testing it yourself

**Without an adapter** (the simulated adapter speaks the real text protocol, so this runs the
real backend code):

```bash
canopener-cli obd --elm sim --all                 # a CAN car
canopener-cli obd --elm sim:vpw --all             # an older GM (J1850 VPW); also sim:pwm, sim:iso9141,
                                                  #   sim:kwp, sim:kwp-slow, sim:29
canopener-cli scan --elm sim --full               # a J1939 machine through the adapter
canopener-cli bluetooth probe sim --protocols --listen 2
canopener-cli bluetooth console sim               # type AT commands; 'quit' leaves
canopener-cli uds --elm sim --find                # module finder
```

In the desktop app: **Connection → Show simulators**, **Source: Bluetooth OBD simulator (car)**,
and pick the car type under **Connection → Bluetooth OBD simulator plays** (handheld: **System →
Simulators**). **Vehicle: Truck or equipment (J1939)** makes it a J1939 machine instead.

**With the vLinker**, in this order:

1. Pair: **Connection → Pair / manage Bluetooth OBD adapters…**, or `canopener-cli bluetooth scan`
   then `canopener-cli bluetooth pair ADDRESS --name "vLinker (truck)"`.
2. `canopener-cli bluetooth probe --protocols` with the key on, then paste it into
   [Adapters tried](#adapters-tried).
3. `canopener-cli obd --elm --bench 10` (gap 2), then `canopener-cli obd --elm --all` (gap 1).
4. The desktop app: **Source: Bluetooth OBD**, Connect, check codes, stream live data, record a
   short drive with the Log tool (the battery voltage goes into the capture as a meter channel).
5. A non-CAN car if you can borrow one (gaps 4-6), a truck for J1939 (gaps 7-8).

### Raw adapter trace

Set `CANOPENER_ELM_TRACE` to a file and every command CANopener sends the adapter, and every line it
answers, goes into it with a timestamp (desktop app, handheld or CLI):

```bash
CANOPENER_ELM_TRACE=~/elm-trace.txt canopener-cli obd --elm --all
CANOPENER_ELM_TRACE=~/elm-trace.txt canopener          # the desktop app
```

A capture recorded through an adapter keeps the same trace for its own stretch as
`adapter_trace.txt` in the capture folder, with no setting needed, so a log from a non-CAN vehicle
carries the raw replies the frames in `capture.log` were rebuilt from.

That's the evidence for most of the gaps above: attach the relevant part when you update a row.
J1939 monitoring (`ATMA`) isn't traced line by line (it would be huge); the commands around it are.

### The console

`canopener-cli bluetooth console` only sends adapter commands (AT, ST): requests to the vehicle
go through `obd` / `uds`, which ask before anything that changes it, and it refuses commands that
rewrite the adapter's stored settings or serial speed (`ATPP`, `ATBRD` ...) or transmit frames
(`STPX`).
