# Changelog

What changed in each version, newest first. Versions are git tags (`v0.3.3`): to put a unit back
on an older one, `git checkout v0.3.2` in its checkout.

## Unreleased

- **Team access from a browser.** The upload server has an admin page at `/admin`: paste the
  admin token, then approve the computers that asked, revoke, rename or forget any of them, make
  invite links (with a **Copy** button) and cancel them, and publish a new build of the team
  plugin from a zip. It works on a phone and follows light/dark mode. The token stays in that
  browser tab only, and the page loads nothing from anywhere else ([upload server
  README](deploy/upload-server/README.md#team-access)). The server's version is now 4.
- **Upload server: changes without logging in to it.** `deploy/upload-server/auto-deploy.sh` keeps
  a server in step with `main` (checks every 10 minutes, rebuilds only when that folder changed,
  rolls back if the health check fails, takes a few fixed requests from a folder), and
  `deploy/upload-server/claude-remote/` runs Claude Code with Remote Control beside it, in a full
  or a narrow (no Docker socket) mode ([How changes reach the
  server](deploy/upload-server/README.md#how-changes-reach-the-server)).
- **Profile builder: electric stand-ups.** A new kind of machine, **Electric stand-up**, asks
  **Reach truck** or **Counterbalance**. Its controls are mapped with your feet off the operator
  pedal (the truck can't drive or lift): the operator presence pedal and the brake pedal (or the
  one pedal, when that's all it has), the gate bar if fitted, travel both ways, lift and lower,
  reach out and in (reach trucks), tilt, side shift, the function button and the horn. Then on the pedal: lift,
  lower, reach, tilt, side shift, mast height, the steering angle, drive motor speed (a
  counterbalance asks for the left and right motors separately) and battery current.
- **Profile builder: a control's two directions make one signal.** Lift and lower, tilt back and
  forward, travel both ways and the like are still asked for one at a time, but when the second
  moves the value kept for the first, it's offered ticked and the two become one signal, signed
  when it swings either side of rest (*Lift/lower input (+ lower / - lift)*). Before, the second
  direction found nothing, because that value was already taken.
- **Profile builder: interlock warnings are expected.** Each part of the walk-through says what
  to expect before it starts, including the "release the control" warnings most machines raise
  when a control is worked with nobody aboard. A warning (CANopen EMCY, J1939 DM1) raised while a
  step is held is reported with that step and named after it in the saved profile, and the next
  step waits ("Let go of everything") until it clears. Protocol housekeeping (EMCY, SDO,
  heartbeats, requests) is never offered as a step's value.
- **Profile builder: a better record.** Each step leaves three timestamps in the log (*get
  ready*, the step, *let go*), so **What changed between the timestamps?** compares the exact
  windows, and the log's README gets a **Profile builder** table: what each step offered, what
  was kept and the warnings it raised (also in `capture.json`). A port set to Auto now records the
  bitrate it found instead of 0.
- **Handheld: CANopen codes show on the Codes page.** It listed J1939 codes only, so a CANopen
  machine's stored and active errors were read (and counted) but never shown. They're listed now,
  with the node and the maker's extra bytes; history slots that read 0 are left out (desktop too);
  and **Clear...** on a CANopen machine offers the standard error-history clear, after asking.
- **Connecting: CAN-H and CAN-L first.** Some machines throw bus errors when the adapter's CAN
  GND is connected too; the docs now say to start without it.
- **Values other profiles know.** The **Profile builder** page checks every profile on the unit
  (built-in, plugins, your own) against the messages nothing decodes here, and lists the values
  that fit what the bus is sending (the same message on a sister machine); **Add all** adds them
  to the profile in use, each marked where it came from. The guided builder offers those values
  first, ticked, with the profile's name. From a computer: `canopener-cli suggest CAPTURE`
  ([USAGE.md](docs/USAGE.md#working-out-a-machine-from-what-its-bus-does)).

- **No more bus faults from an idle handheld.** The Pi launcher used to bring `can0` up at 250k in
  normal mode before the app even opened. On a machine at another bitrate the CAN chip then answers
  every frame with error frames, and the machine logs CAN faults (a Yale reach truck did, until
  **Find signal** set the right bitrate). Now `can0` stays down until you connect (or comes up
  listen-only, if `CANOPENER_BITRATE` is set), **Auto** always listens first instead of trusting an
  interface that's already up (desktop too), and **Disconnect** takes the interface down where
  that needs no password, so the unit can't disturb the next machine it's plugged into.
  `canopener-cli setup --down` does it by hand.
- **Auto-Connect.** The Connection panel and the handheld's **CAN > Connect** start with one big
  **Auto-Connect** button: it listens (listen-only) until it hears steady traffic, connects at that
  bitrate (the busier port, with two), and a few seconds later offers the known profile that fits
  the bus and the values other profiles know (**Use it**, **Add them**, or keep what's in use). The
  settings moved under **Manual settings**; with anything set there the button says *Connect
  (250k, listen-only)* and **Back to Auto-Connect** undoes it.
- **Timestamps the bus sets.** While recording, a timestamp can set itself, named after what set
  it: when a fault code appears (CANopen EMCY, or a new J1939 DM1 code), or when a button or switch
  you pick from the profile turns on (map a spare button, drive, press it when the fault happens).
  Desktop: **Log > Auto timestamps…**; handheld: **Data log > Plan > Automatic timestamps**.

## 0.5.2 (2026-11-25)

- **Team access on the handheld:** **System > Software > Team access** shows the unit's serial,
  asks the unit's upload server for access, and checks, installs and leaves once it's approved
  ([DEVICE.md](docs/DEVICE.md#team-access)).
- **Desktop: the Connection panel fits a narrow window.** It used to open a sliver wide and cut
  its contents off until dragged to a third of the screen. Now its text wraps and it never gets
  narrower than its contents need. The Bus Monitor's toolbar and the OBD-II tab's notes wrap too,
  so the window still fits a 1366-pixel laptop screen with both side panels open.

## 0.5.1 (2026-11-25)

- **Team access.** Your team's plugin, handed out by your own upload server to the computers you
  approve, with no GitHub account needed. Every install now has a serial (`CO-7F3K-9QXA`, in
  **Help → Team access** and **Help → About**). Send a colleague an invite link
  (`canopener-cli team-admin invite "Dave's laptop"`), or approve the serial they read out after
  **Ask for access**. They paste the link into **Help → Team access → Join** and the plugin installs
  itself. `team-admin publish` sends a new build of the plugin, and `team-admin list` shows every
  install, its version and when it last checked in. **Revoking** an install removes the plugin
  from it the next time it starts online, without asking. Offline, what's installed keeps working.
  The install's key is made on the computer and only its hash is kept on the server, and a copied
  settings folder doesn't carry access to another computer. How: [Team access](docs/USAGE.md#team-access);
  the server side: [its README](deploy/upload-server/README.md#team-access).
- **Updates are checked at startup by default**, now for the app, plugins installed from GitHub
  and the team plugin together. When there's something new it lists it and asks (**Install now** /
  **Later**); nothing installs without a yes, and offline it says nothing. Untick **Help → Check
  for updates at startup** to stop it.

## 0.5.0 (2026-11-23)

- **Live Data groups by controller.** **By source** on the desktop's Live Data (and the OBD-II
  tab's) puts each value under a heading for the controller that sends it; **By group** uses the
  profile's groups. Click a heading to fold it away, or **Collapse all**. On the handheld,
  **Group** (button 5 on the Live list, in place of Back) picks the same and folds or opens every
  heading, and Select or a tap on a heading folds it. The grouping and which headings are folded
  are remembered.
- **Serial console ports.** Controllers that talk to their service console over a serial line
  (RS-232, logic level, or a current loop through an optocoupler tap) can be recorded:
  `canopener-cli serial sniff` records one adapter per direction into an ordinary capture, with
  timestamps, as `serial.log` on the same clock as the CAN log. `canopener-cli serial analyze`
  (also `serial_analysis.txt` in the capture) rebuilds the messages and reports their kinds, what
  each byte does, the checksum that fits (sums, XORs, CRC-8 / CRC-16), which message answers which,
  any text, and what changed at each timestamp. `canopener-cli serial baud` measures the baud
  rate from a logic capture of the line. How to tap a line without breaking it, and how we keep
  what we learn publishable: [docs/SERIAL-SNIFFING.md](docs/SERIAL-SNIFFING.md).
- **Zapi console mode (groundwork).** A built-in plugin, off until switched on (**System >
  Software > Plugins > Built in** on the handheld, **Settings > Built-in plugins** on the desktop):
  a home tile and a desktop tab that listen to a Zapi console talking to its controller, show each
  message, record it with the data log (beside the CAN bus) and run the analysis. Nothing is sent.
  The findings, and the recording each one came from, go in [docs/ZAPI-CONSOLE.md](docs/ZAPI-CONSOLE.md).
- **Built-in plugins.** Plugins can ship inside CANopener (`canopener/addons/`) and be switched on
  or off (`canopener-cli plugins enable / disable ID` too); they use the same hooks as installed ones.
- **pyserial** is a new optional extra (`serial`), which the installers include; the laptop
  installer also adds you to the `dialout` group so USB-serial adapters open without root.
- **Which module is missing.** When a machine only says "communication error", the **Network** tab
  names the module that isn't talking: one the profile lists that was never heard, or one that was
  talking and went quiet (a CAN control handle going dark, say). **Remember these modules…** (or
  **Recognise this machine**) saves the modules of a machine that works; next time, any that's
  missing is named. The **Fault Codes** tab shows the same line when something's wrong, and a module
  dropping out or coming back shows in the status bar. On the handheld: **CAN → Bus → Modules**
  (with **Remember**), a line on the Codes page, and a message with a new sound. Only what a module
  sends itself counts, so a master still sending to a dead one doesn't hide it. Listens only.
  `canopener-cli modules` does the same (`--watch`, `--remember`, `--log`). Profiles can list the
  modules to check in `"expect"` when it should differ from `"detect"`
  ([USAGE.md](docs/USAGE.md#which-module-is-missing)).
- **New built-in profile: Generic ZAPI/Chinese AC Truck** (unconfirmed). Chinese-built AC
  forklifts on ZAPI controllers: battery (pack voltage and current, remaining Ah, all 16 cell
  voltages and 12 temperatures from the lithium BMS), seat, pedals, direction, park brake, both
  drive motors, travel speed, lift / tilt and the pump, controller temperatures, hour meters and
  the active error code (79 Incorrect start confirmed on the truck). The alarm bytes and EMCY
  codes are named from ZAPI's ACE2 2uC alarm table, in our own words. Worked out from timestamped
  captures of one truck, most of it found by the Profile builder.
- **Soft key 5 does something on every page.** With **Button 5 does** set to **Soft key 5** (Back on
  a button of its own), the fifth button gets a job of the page's own: Codes is **Full scan /
  Active / Stored / Clear... / Live data**, Live and Bus start or stop the log, Connect jumps to
  Codes once connected, the Meter marks a timestamp while recording (otherwise goes to CAN), Home
  opens the Meter, a recording log gets Hold, and any other page gets **Home**. The full list is
  in DEVICE.md.
- **Buttons on the button ADC answer at once.** They took half a second to a second: the Pi's
  firmware cuts config.txt lines at 98 characters, so the ADS1115 line lost its last input's
  settings, leaving A3 at 8 samples a second, and every pass over the stacks waited for it. The
  boot lines are short now (and rewritten on the next **Set up**), the base settings come before the
  overlays, `install.sh --device` adds a rule that runs every ADC input at 860 a second whatever the
  boot lines say, and **Control mapping** shows how often the stacks are read.
- **The battery only counts once it reads like a real pack**, so an input with no divider on it
  yet (floating) can't pass for a flat pack and power the handheld off.
- **Control mapping.** A new home tile: every button (grouped by the button ADC's input, or by
  GPIO) and the encoder, live, with the voltage each stack reads and what each button does. While
  **Testing** is on, presses only light up there, so a miswired button can't run through the
  screens. Select a button to give it another job (F1 and F3 wired the wrong way round), ignore
  it, or take its voltage as its level; switch a stack that isn't wired yet off; reverse the
  encoder. Also from **System > Controls**.
- **Presses nobody made are ignored.** A button stack whose input floats (no pull-up, or not wired)
  reads as press after press. The handheld now spots that, says so, and ignores that stack until
  it reads steady again.
- **Buttons and battery on a second ADC.** With the Waveshare HAT taking GPIO18-21 and 24, every
  button moves to resistor stacks on a second ADS1115 at 0x49 (**System > Installed options >
  Buttons and battery ADC**): the top row on A1, the d-pad and encoder push on A2, the side buttons
  (Back, Hold, Record, Timestamp) on A3. Buttons on different stacks can be held together, and
  holding the push is still Back. Only the encoder stays on GPIO, on 17 / 27, which leaves twelve
  pins free. The probe ADC keeps all four inputs. Run the Pi installer once more first, so the
  boot-settings helper knows the new layout. Wiring in DEVICE.md and the prototype guide; the
  wiring diagram shows it.
- **Battery charge.** The same ADC reads the pack (a DeWalt 20V MAX on the prototype) on A0 through
  a 100k / 15k divider: the charge at the right of the status strip and in **System > Power >
  Battery**, warnings at 20% and 8% with a tone (**Battery low**), and a power-off, saving any log,
  once the pack has sat at 3.0 V a cell for 30 s (packs don't all cut off by themselves; it can be
  turned off).
- **Doom with the buttons on the ADC:** their presses reach it as keys.

- **Two buses: the busier one is the main bus.** With both of the handheld's CAN ports in use (two
  buses on the machine's 9-pin connector), Connect listens to both first, listen-only, and makes
  the busier one the main bus, saying which and how busy each was. Picking a port by hand (the
  interface, or **Make CAN 2 the main bus**) turns this off; **CAN > Connect > Main bus: the
  busier port (Auto)** turns it back on.
- **Hold, and keys for side buttons.** A Hold action freezes the live values on the screen (on
  the Meter page it's the page's own Hold) while the bus, meters and log carry on; HOLD shows in
  the status strip. Dedicated buttons sending Esc (Back), Pause (Hold), F9 (start / stop log) and
  F10 (timestamp) work with no setting. Button 5 can now be Hold, and with a dedicated Back
  button it can be soft key 5.

- **Profile builder: pick what moved, live.** After each step it shows up to eight values with
  their live readings and a dot that lights when one changes. Work the control a few more times:
  the one that follows lights up and counts up, and a clear winner is ticked for you. Nothing is
  ticked on one sighting any more. The list puts the most-moved first but holds still for a few
  seconds after any key press. Kept values that moved with the control are saved as "checked".
- **Profile builder: far less noise offered.** Values that change while nobody touches the machine
  (in the hands-off stretch or any "Get ready") are left out, and so are the low digits of a
  reading (a battery voltage or temperature wobbling between neighbouring numbers), judged over
  the last two minutes rather than a few seconds. Bits of a value a decoder already names count
  as named. On the first real run, 5 of the 7 values kept were the battery voltage's low byte or
  the ambient temperature's low bit; replayed now, none of them is offered.
- **Profile builder: says what's already named.** A step whose control the profile in use
  already decodes (an accelerator pedal, the engine speed) lists those values live under
  **Already named, moved too**, instead of offering the next-best field. A pedal pressed and let
  go within the step, or revs on the way up, now count as moved (also in **What changed**).
- **Profile builder: saves on top of the profile in use.** **Add to** the profile in use (a
  built-in or plugin profile gets a profile of its own on top of it, so it keeps everything and
  still gets updates) or a new profile built on it. Before, the new profile was built on the
  generic one and switched on, which hid everything the old profile decoded.
- **Profile builder: sharing works, and says so.** Sharing a saved profile with the upload server
  was refused while the log was being saved (one job at a time); it now runs on its own and says
  whether it worked. A profile not shared is marked on the Profile builder page, with **Share
  it** to try again.
- **Which profile fits this bus.** A few seconds after connecting, the handheld says so when a
  known profile fits the bus clearly better than the one in use (most of that profile's own
  messages are on the bus). The Profile builder page lists the ones that fit, with **Load**, and
  the builder offers the best before it starts.
- **Explore the bus.** **Profile builder > Explore the bus**: every value on the bus, live, named
  or not, moving ones first. Select one to see its last minute, then **Name it** from the list
  (with **Not sure (?)**); it's saved at once into the profile in use or a new one, and Live shows
  it.
- **The Waveshare HAT's "can't connect" now points at the cable.** When the HAT's CAN chip can't
  join the bus (it never hears the bus go quiet), the message says to check the wiring and try
  with the cable unplugged, not to unplug a USB adapter.
- **Recording with no bus connected says so.** Starting a data log while not connected (with a
  meter on) now says "Recording meters only: not connected to a bus, so no CAN data".
- `canopener-cli profiles --upload all` sends only your own profiles, not a plugin's.
- The engine starter mode (EEC1) is a standard live value now (a raw number), so the builder no
  longer offers its bits as "Forward selected". The builder's glow plug step is gone: it needed the
  key off mid-run, and the wait-to-start lamp is decoded already.

## 0.4.2 (2026-11-16)

- **Profile builder: says when the bus is too quiet.** After the hands-off stretch it warns if
  the bus carries under about 10 frames a second (key off, the wrong connector, or controllers
  that only answer a service tool that asks), instead of quietly finding nothing. Each step is
  also written to the log as a timestamp when it starts, so leaving without Save keeps them.

- **Waveshare 2-CH CAN FD HAT.** A new entry under **System > Installed options** sets the Pi up
  for the HAT as it ships: two CAN channels (`can0`, `can1`), so two buses can be watched at once.
  Its second channel uses the pins planned for the encoder, the sync input and the buzzer, so the
  buttons and sync are left out while it's switched on. Run the Pi installer once more first: it
  updates the helper that writes the boot settings.

- **Meter inputs can be switched off.** **Meter > Inputs** lists every input; one that's off
  isn't shown on the Meter screen, in live data or charts, and isn't recorded. Meter setup is on
  the same page.
- **CAN ports by name, and a port can be switched off.** With two CAN ports on the unit (the
  Waveshare HAT, or CAN stacks A and B), **CAN > Connect** lists them as **CAN 1** / **CAN 2**
  wherever Linux put them, and a port you aren't using can be switched off so it leaves the list.
- **A second bus, watched alongside.** With two ports, **CAN > Connect > Watch CAN 2 too** opens
  the other port at the same time: listen-only, at its own bitrate (or Auto), with its own lamp
  in the status strip. The data log records it to `capture-can2.log` on the same clock. Codes
  and live data still come from the main bus; **Make CAN 2 the main bus** swaps the two ports,
  bitrates included.
- **"Little traffic" warning.** The bus lamp turns amber and says **little traffic** under about
  10 frames a second, and a few seconds after connecting the handheld says so once, with what to
  try: key on, the other connector, or the other port (it names the port when that's where the
  traffic is). Some buses really are that quiet, so it's a warning, not a fault.
- **More standard J1939 messages decode.** Found by going through every capture on the upload
  server for standard messages the built-in tables had no name for. 46 now have names, and these
  also show live values: the speed / torque another controller asks the engine for (TSC1), throttle
  actuator command, gaseous fuel used (LPG / CNG engines), DEF temperature and concentration, DOC
  and SCR temperatures, turbo intake temperature and pressure, the vehicle speed limit, requested
  fan speed and the regeneration switches, instrument lighting level, auxiliary temperatures and
  pressures, retarder oil temperature, and the requested gear.
- **Fixed: Engine Temperature 2 was filed under the wrong message number**, so ECU temperature and
  the EGR values never showed. They do now.

## 0.4.1 (2026-11-16)

- **Games, reworked and two more.** Harness and Cargo play better: pieces come from a shuffled
  bag, a landed piece can still be slid or turned for a moment, pieces glide, clears spark and
  score floats up, and each has a How to play page. Harness's landing outline says whether the
  segment would join live wire or close a circuit; Cargo adds streak and clean-sweep bonuses.
  New: **Loom**, a no-clock wiring puzzle with growing levels and stars, and **Arbitration**,
  pick the frame that wins the bus by CAN's real rules, until too many mistakes put you bus-off.
- **Doom, as an optional download.** Turning the games menu on asks once whether to fetch the
  Chocolate Doom engine and the free Freedoom levels from the system's package archive (the Doom
  row asks again any time). It plays full screen with keys set for the handheld, and the
  machine's CAN signals can play it too (walk, fire, use, steer), through a virtual keyboard and
  mouse that `install.sh --device` allows.
- **Games > Configure controls**, one menu for how the games are played: hold the handheld
  turned either way for Harness and Cargo, the encoder push to swap or drop, reverse the encoder,
  Doom's turn speed and keys, and the machine's signals as controls, showing each one's value now.
  Picking a signal there no longer fails while connected.
- **Arrange the home screen**: press and hold a tile, then drag it. The order is kept.
- **CAN > Live and the Meter remember the enlarged value.** What's ticked, the grid and the
  double-tapped value stay put through other screens and restarts, and come back as soon as
  those values are on the bus again.

- **Installed options now set the Pi up.** On the handheld, setting a part on the Pi's own pins
  (CAN stack A or B, the ADC, the sync input, the buttons, the ESP32) to Installed shows the lines
  it adds to the Pi's boot settings, asks, writes them into one marked block of `config.txt` and
  loads the driver straight away where it can; the buttons and the ESP32, and taking a part out,
  need a restart, which it offers. Each CAN stack also gets its crystal and SPI speed (1 MHz for
  breadboard wiring). `canopener-cli hardware --apply` does it over SSH. The root helper behind
  it, `canopener-boot-config`, only ever writes its own fixed lines into its own block.
- **The Pi installer now always sets up the passwordless CAN rule and that helper** (they were the
  `--sudoers` option; leave them out with `--no-sudoers`). A Pi installed before this needs the
  installer run once more, since an in-app update can't install anything as root; that also
  installs the rules Doom needs (its download and, with `--device`, CAN signals as its controls).

## 0.4.0 (2026-11-13)

Working out an unfamiliar machine from what its bus does, and getting help while you do. Built
around one idea: most CAN buses aren't a black box; the same messages repeat with each value in
the same bytes, so changing one thing on the machine shows which bytes follow.

- **What changed?** `canopener-cli changes CAPTURE` compares what the bus said between a
  capture's timestamps ("key on", "idling", "lifting" ...) with no decoder: it splits every
  message into values, switches, counters and noise, ranks the bytes and bits that differ from one
  state to the next, lists messages that started or stopped, and names the scale of an unknown
  value that moves with a known one ("follows Engine speed: rpm = 1 x this"). `--parts N` cuts a
  capture without timestamps into slices; `--json` writes every field per phase
  (`core/changes.py`).
- **Name what changed and keep it: observed profiles.** Handheld: **Data log > More > What
  changed between the timestamps?** lists what followed each timestamp; pick a line, take the
  suggested name (when it follows a value CANopener knows) or one from a short list by part of
  the machine, and **Save** makes a profile and switches it on, so Live shows the new values at
  once. Each value keeps how it was found and how sure it is (`core/observed.py`).
- **Guided Profile Builder.** Handheld: the **Profile builder** tile on the home screen (also
  under Data log > Plan). It asks
  what the machine is (IC forklift, electric forklift, telehandler, golf cart, pallet jack,
  something else), what an engine runs on (LP, petrol, diesel, dual fuel) and whether a diesel has
  aftertreatment, how the hydraulics are controlled (electronic levers or joystick, or manual),
  whether the mast reports its height (switch, encoder, both) and whether side shift is fitted,
  and builds the list from the answers. Control inputs come first, with the key on and nothing
  running (each lever both ways when the hydraulics are electronic), then the engine, then the
  working functions and the mast height. It walks you through the list
  of things to do, one at a time: "Get ready", then "do it and hold", then "let go". It tags the
  log by itself, learns first what changes when nobody touches anything, and after each step shows
  what followed and names it after the step ("Lift", "Seat switch"). Nothing followed? Try again
  or skip; the same answer twice is marked "seen again". Save makes the profile, switches it on
  and shares it. It only listens (`core/guide.py`).
- **SSH tunnel for hands-on help.** **System > Remote help > Open an SSH tunnel** (it warns
  first) makes a reverse tunnel to the upload server's optional `canopener-tunnel` container, so a
  helper who can already log in to the server, and to the unit, gets a full login to it from
  anywhere. On the server the unit's key can do nothing but hold that tunnel (`core/tunnel.py`).
- **Remote help.** Handheld: **System > Remote help > Start** calls out to the upload server (so it
  works from a phone hotspot) and lets someone holding one of the server's remote tokens look at
  the unit: its state, a screenshot, logs and profiles, fault codes, adapter commands, and requests
  to the vehicle. Reads run straight away and are listed on the page; anything that could change
  the vehicle waits for a Yes on the unit (default No). `canopener-cli remote` is the helper's
  end. The server only relays (`deploy/upload-server`, `REMOTE_TOKENS`); `core/remote.py`.
- **Profiles go to the upload server too.** A profile saved on the handheld is sent along
  (`upload.profiles`, on by default; `canopener-cli profiles --upload ID`), every change kept as a
  new revision, readable with a read token (`/api/profiles`).
- **The deep probe's result as data:** `kline_probe.json` beside the text report, with the plan
  the value recorder reads again (it parsed the text before).

## 0.3.8 (2026-11-13)

What a day on a real K-line machine taught us, turned into the app.

- **Health watch.** While the deep probe or the value recorder runs, the battery voltage is read
  every 20 requests and whenever a request goes unanswered. If the engine was running and the
  voltage shows it has stopped, everything stops at once and the report names the last request.
  The report also gives the voltage at the start, the lowest and the end.
- **A frame the adapter couldn't send is not logged as traffic.** Through a Bluetooth OBD adapter,
  `CAN ERROR` / `BUS ERROR` / `BUS BUSY` / `FB ERROR` with no answer now fails the send with the
  reason in plain words ("nothing on the bus acknowledged it: wrong bus speed, or no CAN on this
  connector?"), and a scan stops after its first request fails instead of sending the rest.
- **Captures say when nothing answered.** The summary and the README open with "Nothing
  answered: all N frames in this log are this tool's own requests" when that's the case, a module
  that answered with no codes is listed as such, and the ECU list says "none heard" instead of
  staying blank.
- **"No codes" from a module that lists no live data comes with a warning** (handheld toast and
  Codes > More..., `canopener-cli obd`, the capture summary): some engine computers answer
  OBD-II only as a formality and keep their codes elsewhere.
- **A simulated thin K-line computer** (`sim:thin-kline`, "K-line computer that says almost
  nothing" in the simulator list), modelled on the real one: an empty OBD-II shell, a KWP2000
  session that only wakes for one tester address and one request shape, an answer that comes 256
  times, and an identifier page that stops the engine if swept.
- **`can/kwp.py`**: a KWP2000 session addressed to one module (see 0.3.7's notes below).
- Tests leave installed plugins out (`CANOPENER_NO_INSTALLED_PLUGINS`), so they pass on a machine
  with a private plugin installed.

## 0.3.7 (2026-11-13)

- **Deep probe: tries other request shapes, and is quick again.** A module that refuses every
  block or identifier number the same way may want more bytes after the number, so the probe
  tries a few numbers with the endings the standard allows and reports any answer that differs.
  The scans of every number are now a separate menu entry (**Deep probe with the full scan**,
  `--scan` on the command line) and use the shape that answered; the plain deep probe is back to
  about two minutes.
- **Safety: the full scan is engine-off only, and sweeps fewer pages.** On the first real machine
  a full scan with the engine running ended with the engine computer going silent partway through
  an identifier page nobody has described (page 14), and the engine stopped. The full scan now
  refuses to run when the engine looks to be running, says so plainly before it starts (default
  No), and only ever sweeps pages 11-13; other pages that answer are reported, not swept.
- **`can/kwp.py`: a KWP2000 session addressed to one module** through a Bluetooth OBD adapter
  (wake-up, one request at a time, keep-alive, hand the adapter back to OBD-II). Only the
  transport: what a maker's module understands belongs in a profile or a plugin.
- **Read the values the last full scan found** (handheld: Codes > More...): wakes the module the
  way the saved report says and reads each data block and identifier that answered, twice over,
  into `kline_values.csv` in a log. Run it engine off, idling and revving, and the values that
  differ show which is which. Only requests that already answered are sent (pages 11-13).
- The shape that works on the first real machine is number + `04 01`; the bare `04` ending is no
  longer tried (it made the module repeat its answer 256 times).

## 0.3.6 (2026-11-13)

- **Deep probe: scans common identifiers too.** Where the module has the two-byte
  common-identifier read (service `22`), the probe samples every page (`22 xx 00` / `22 xx 01`),
  then scans in full each page that answered and pages 11-15. A run on such a machine now takes
  up to ten minutes. Still read-only.
- A second deep probe in the same log is added to `kline_probe.txt` below the first (it replaced
  it before).

## 0.3.5 (2026-11-13)

- **Deep probe: lists a K-line computer's data blocks.** Where the module has the KWP2000
  data-block read (service `21`), the probe now tries every block number (`21 00` - `21 FF`) and
  every identification option (`1A 80` - `1A 9F`), and checks whether three more read services
  exist. Still read-only. The transcript lists what answered and counts the refusals; the run
  takes two to four minutes.
- First result from a real machine (docs/BLUETOOTH-OBD.md, "Thin K-line computers").

## 0.3.4 (2026-11-13)

For older K-line machines whose generic OBD-II answers are thin (no live data listed, no codes
shown while the lamp is on).

- **Deep probe (read-only).** On the handheld: **Codes > More... > Deep probe**; on a computer:
  `canopener-cli bluetooth kline --out probe.txt`. Through a Bluetooth OBD adapter it asks the
  generic OBD-II reads whether or not the computer lists them, tries each K-line wake-up (ISO
  9141-2, KWP2000 slow and fast), and tries the standard KWP2000 reads (identification, codes by
  status, a data block) addressed to the module itself. It never clears, writes or runs a test.
  Every reply is kept as the adapter printed it, in `kline_probe.txt` inside a log the handheld
  records and saves for it, so it uploads like any capture. Takes about two minutes.
- **Adapter trace:** each command is stamped when it was sent and its reply when it was complete
  (they shared one time before), so the trace shows how long the vehicle took to answer.
- First real non-CAN vehicle checked (docs/BLUETOOTH-OBD.md, gaps 3 and 4).

## 0.3.3 (2026-11-12)

The protocol libraries, filled out. Layouts are the published standard ones, written in our own
words; the ones not cross-checked against a real machine yet are worth a look on one.

- **J1939 live data: 149 standard values across 47 messages** (was 39 across 17). New: torque
  mode and demand, cruise / brake / clutch / parking-brake switches, PTO, fan drive, turbo speed,
  fuel economy and totals, trip and total distance, transmission oil temperature and pressure,
  lockup and range, ABS / ASR status, retarder, hydraulic pressure, NOx before and after the SCR,
  DPF regeneration status and lamps, DPF and exhaust temperatures, GPS position, and more.
  Switches and states read as words ("Pressed", "Regeneration needed").
- **J1939 codes:** all-pending (DM27) and permanent (DM28) codes are read in the emissions and
  full scans. Machine info adds calibration IDs with their verification numbers (DM19) and the
  distance / time with the MIL on (DM21). Decoders for DM29 (code counts) and DM31 (which lamp
  each code lights). 318 SPN names (was 285) and labels for about 40 more messages.
- **OBD-II: 21 more live PIDs**, mostly diesel and aftertreatment: EGR, intake and throttle
  actuators, boost, turbo speed and inlet pressure, exhaust pressure, charge-air temperature, DPF
  pressures and temperatures, NOx sensors, DEF level and dosing, transmission gear.
- **UDS:** `canopener-cli uds --info` reads a module's standard identification (VIN, part and
  serial numbers, hardware and software versions, dates) with each item named; `--supported`
  lists every code the module can set. Codes show what kind of fault their last byte means
  ("short to ground").
- **CANopen: process data without a hand-written profile.** `canopener-cli canopen --pdo-map`
  asks each node what's in its PDOs; `--save-profile NAME` saves a profile that splits them into
  values. `canopener-cli import device.eds --node 05` does the same from the maker's EDS / DCF
  file, with every value named.
- **DBC import:** `canopener-cli import machine.dbc` makes a profile from a DBC file (J1939 and
  11-bit messages, scale, offset, units, value tables).
- **Logs:** Vector `.asc` and PEAK `.trc` files open, replay and decode like candump logs.

## 0.3.2 (2026-11-12)

- **Settings shared by two programs are no longer lost.** The handheld UI and `canopener-cli`
  can both change `settings.json`: a save now writes only what that program changed, and a
  running app notices the other's changes within about a second. Before, a running app wrote its
  old copy back over anything the command line had changed (`canopener-cli upload --setup`).
- **Upload server (version 2):**
  - a connection that goes quiet is dropped after `REQUEST_TIMEOUT` (120 s) instead of holding
    a thread for ever;
  - unfinished uploads are cleaned up every hour, not only when the server starts;
  - `DELETE /api/captures/<key>` with a token from the new `ADMIN_TOKENS`, and `KEEP_DAYS` to
    expire old captures (both off by default);
  - a token with unusual characters gets a 401 instead of a server error;
  - a revision left half unpacked by a crash no longer mixes into the next upload;
  - the log shows the caller's address from `X-Forwarded-For`;
  - the README covers running behind a proxy you already have (Nginx Proxy Manager and the
    like) and the data folder's owner.
- **Handheld meter: Hold freezes the graphs too**, not just the numbers and min / max (the meter
  and the data log keep running underneath).
- **Handheld: double-tap a reading to enlarge it** (a meter channel, or a value in Live's grid).
  It takes most of the screen and the others stack beside it; double-tap it again or press Back
  to return to the grid.
- **Plugins can provide the upload server.** `core.upload.set_default_server` lets a private
  plugin hand every unit the server's address and upload token; a unit's own still win.
- `canopener-cli upload` shows small captures in kB and no longer leaves its progress line stuck
  to the result (or in a log file).
- CI also tests Python 3.13, which the Raspberry Pi runs.
- Removed `legacy/` (the first Tkinter scripts). They're in the git history before this version.
