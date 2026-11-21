# The Zapi console mode

A mode for working with a Zapi controller's service console port. **It's groundwork**: today it
listens to a Zapi console talking to its controller, shows each message, records it with the
data log and works out what it can about the bytes. Nothing is sent to the controller. Once we
know the protocol from our own recordings, the same mode can ask the controller itself, with the
usual confirmation (default No) before anything that changes it.

Everything on this page comes from our own recordings. How we keep it that way, and why it
matters, is in [SERIAL-SNIFFING.md](SERIAL-SNIFFING.md#4-keeping-it-clean-what-can-go-in-this-repo).

## Switching it on

It's a built-in plugin, off until you switch it on:

* **Handheld:** **System > Software > Plugins > Built in > Zapi console**, then restart. It's a
  tile on the home screen.
* **Desktop:** **Settings > Built-in plugins > Zapi console**, then restart. It's a tab.
* **Command line:** `canopener-cli plugins enable zapi_console`.

The serial adapters need pyserial (the installers include it) and, on a Linux laptop, the
`dialout` group (the laptop installer adds you).

## Using it

1. **Tap the link** between the console and the controller, one USB-serial adapter per wire:
   [SERIAL-SNIFFING.md](SERIAL-SNIFFING.md#1-tapping-the-line). Measure the pins first and write
   the numbers in the findings below.
2. **Setup:** pick the adapter on the console's transmit wire and the one on the controller's,
   and the framing. 9600 8N1 is only a starting guess: measure it with the logic Pico
   (`canopener-cli serial baud`).
3. **Listen.** Each message shows who sent it, its bytes and any text in it. On the handheld,
   **Setup** / **Messages** switches between the two pages.
4. **Record** with the data log (button 5 on the handheld, the Log tool on the desktop) and
   **Mark** each thing you do on the console: open a menu, change a value, run the tester.
   While the mode listens, the log records the console lines beside the CAN bus and the meters
   (`serial.log`). Start listening first: a log that's already running carries on without them.
5. **Analyze** shows message kinds, the checksum, who answers whom and any text
   ([what each part means](SERIAL-SNIFFING.md#3-what-the-analysis-shows)). For a saved log:
   `canopener-cli serial analyze CAPTURE_FOLDER`.

Newer Zapi controllers can talk to a console over CAN as well. If yours does, the console's
traffic is already in an ordinary CAN log; record that too. Each finding says which link it was.

## Test machines

What we have, or hope to get, to record on. Add a row when one is recorded.

| Machine / controller | Console link | Recorded | Notes |
|---|---|---|---|
| Zapi ACE2 (hoped for) | ? | not yet | |
| Zapi Smart Display (hoped for) | ? | not yet | a display, not a console: record its CAN traffic as well |

## Findings

One row per thing we've worked out. **Evidence** is the capture folder (keep it), **How** what
was done when it showed. A finding stays `?` until a second recording, or the machine, confirms
it. A decoder in `canopener/addons/zapi_console/protocol.py` names its row number here.

| # | Finding | Link | Evidence | How | Confirmed |
|---|---|---|---|---|---|
| | *Nothing yet.* | | | | |

## Open work

* **The protocol.** Everything above: framing, message layout, checksum, the request / reply
  pattern, then what each message means.
* **Asking, not just listening.** Once a request is understood, a front-end can send it through
  a tap opened to write (`serialline.port.Tap(allow_write=True)`), listed in **What it sends**
  with its finding, confirmed before each send when it changes anything.
* **The Log tool's own serial lines.** Today a log picks the console lines up only while this
  mode listens; the Log tool could offer serial lines next to the meters.
