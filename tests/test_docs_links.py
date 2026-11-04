"""Relative links in the Markdown docs point at files that exist, and #anchors at real headings."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist",
             "legacy", "node_modules"}
LINK = re.compile(r"\]\(([^)\s]+)\)|<img [^>]*src=\"([^\"]+)\"")


def _markdown_files():
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def _strip_code(text: str) -> str:
    """Drop fenced code blocks and inline code, so `# comments` and `[x](y)` in code don't count."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    s = re.sub(r"[*_`]|\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip()).lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def _anchors(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        text = re.sub(r"^```.*?^```", "", f.read(), flags=re.S | re.M)
    seen: dict[str, int] = {}
    out = set()
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, flags=re.M):
        slug = _slug(m.group(1))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def test_relative_links_resolve():
    broken = []
    for md in _markdown_files():
        with open(md, encoding="utf-8") as f:
            text = _strip_code(f.read())
        for m in LINK.finditer(text):
            target = m.group(1) or m.group(2)
            if re.match(r"[a-z]+:", target):             # http:, https:, mailto:
                continue
            path, _, anchor = target.partition("#")
            dest = os.path.normpath(os.path.join(os.path.dirname(md), path)) if path else md
            where = f"{os.path.relpath(md, ROOT)}: {target}"
            if not os.path.exists(dest):
                broken.append(f"{where} (no such file)")
            elif anchor and dest.endswith(".md") and anchor not in _anchors(dest):
                broken.append(f"{where} (no such heading)")
    assert not broken, "broken links in the docs:\n" + "\n".join(broken)


def test_slug_matches_github():
    assert _slug("Your USB adapter (CANable 2.0 / 2.0 Pro)") == "your-usb-adapter-canable-20--20-pro"
    assert _slug("Naming things CANopener doesn't know") == "naming-things-canopener-doesnt-know"
    assert _slug("4. Connect the CAN hardware") == "4-connect-the-can-hardware"
