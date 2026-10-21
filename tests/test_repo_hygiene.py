"""This repository is public: OEM dealer-tool data must stay out of it (see CLAUDE.md)."""

import fnmatch
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN_NAMES = ["hyster_*.json", "vam_*.json", "*pcst*", "*PCST*", "server_field_ids.json"]
# Strings that only appear in extracted dealer-tool data, not in generic J1939 code.
FORBIDDEN_TEXT = ["NMHG", "\"PGN_PROPA\"", "sef_values", "tsm_variants", "ServerIdMap"]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}


def _files():
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            yield os.path.join(dirpath, name)


def test_no_oem_data_files():
    bad = [os.path.relpath(p, ROOT) for p in _files()
           if any(fnmatch.fnmatch(os.path.basename(p), pat) for pat in FORBIDDEN_NAMES)]
    assert not bad, f"OEM data files in the public repo: {bad}"


def test_no_oem_data_markers():
    me = os.path.abspath(__file__)
    hits = []
    for path in _files():
        if path == me or not path.endswith((".py", ".json", ".md", ".txt", ".csv", ".toml")):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        hits += [f"{os.path.relpath(path, ROOT)}: {m}" for m in FORBIDDEN_TEXT if m in text]
    assert not hits, f"dealer-tool data markers found: {hits}"
