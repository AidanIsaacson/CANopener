"""A stand-in plugin for tests/test_elm327.py: two adapter bus setups (core.adapter_buses)."""

PLUGIN_NAME = "test buses"


def adapter_buses():
    return [{"id": "demo", "label": "Demo second bus", "protocol": "6", "commands": ["ATI"], "undo": ["ATD"],
             "adapter": "simulated", "verified": "simulator"},
            {"id": "empty", "label": "Empty one", "protocol": "B", "commands": []}]
