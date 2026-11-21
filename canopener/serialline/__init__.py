"""Serial lines: listening to (and later talking on) a machine's serial link, beside the CAN bus.

Some controllers talk to their service console over a serial line instead of CAN (older Zapi
controllers' current-loop console port, for one). This package is the transport for that:

* ``framing``: baud rate, data bits, parity and stop bits (``Framing.parse("9600 8N1")``), and a
  baud-rate guess from the bit widths a logic analyzer saw;
* ``port``: serial ports through pyserial (the optional ``serial`` extra): list them, open one,
  and ``Tap`` / ``Sniffer``, reader threads that timestamp every chunk of bytes on arrival with
  ``time.time()``, the same wall clock ``CanFrame`` uses;
* ``log``: the ``serial.log`` file a capture keeps beside ``capture.log``.

Each line is named ("A", "B", or what it carries: "console", "controller"). Sniffing a two-wire
link means one line per direction: the console's transmit wire and the controller's.

Like ``can/``, nothing here imports ``core/``, ``gui/`` or ``sim/``. pyserial is imported only
when a port is opened or listed, so everything else (reading a log, the analysis in
``core/serial_analysis.py``) works without it.
"""
