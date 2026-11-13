# Changelog

What changed in each version, newest first. Versions are git tags (`v0.3.3`): to put a unit back
on an older one, `git checkout v0.3.2` in its checkout.

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
