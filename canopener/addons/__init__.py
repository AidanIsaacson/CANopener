"""Bundled plugins: shipped with CANopener, off until switched on (``plugins.BUNDLED``).

Each one is an ordinary plugin module (``canopener.plugins`` hooks: ``register_device``,
``register_gui``, ``register_cli`` ...), so it could move to a repository of its own unchanged.
Keep each one's logic in Qt- and Pygame-free modules, its screens in ``gui.py`` / ``device.py``,
and import those only from the hook that needs them.
"""
