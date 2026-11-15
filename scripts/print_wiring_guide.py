"""Build docs/install/handheld-prototype.pdf: the prototype wiring guide, laid out for printing on
US Letter (the overview diagram on page 1, the two pin-by-pin pages each on a page of their own,
a bench-notes page at the end). Run it again after changing the guide or its diagrams.

    pip install markdown            # dev tool only, not a CANopener dependency
    python3 scripts/prototype_wiring_diagram.py && python3 scripts/prototype_wiring_detail.py
    python3 scripts/print_wiring_guide.py

Rendering uses Playwright's Chromium if it's installed (with page numbers), or Chromium / Chrome on its
own.
"""
import os
import re
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
GUIDE = os.path.join(ROOT, "docs", "install", "handheld-prototype.md")
OUT = os.path.join(ROOT, "docs", "install", "handheld-prototype.pdf")
IMAGES = os.path.join(ROOT, "docs", "images")

CSS = """
@page { size: Letter; margin: 0.6in 0.6in 0.7in; }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 9.6pt; line-height: 1.38; color: #111; }
h1 { font-size: 18pt; margin: 0 0 6pt; }
h2 { font-size: 13pt; margin: 14pt 0 5pt; padding-bottom: 2pt; border-bottom: 1.5pt solid #0b6e4f;
     break-after: avoid; }
h2 + p, h2 + table { break-before: avoid; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-left: 16pt; margin: 4pt 0; }
li { margin: 2pt 0; }
blockquote { margin: 6pt 0; padding: 4pt 8pt; border-left: 3pt solid #9a4b00; background: #fff3e3; }
blockquote p { margin: 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0 8pt; break-inside: avoid; font-size: 8.8pt; }
th, td { border: 0.6pt solid #999; padding: 2.5pt 5pt; text-align: left; vertical-align: top; }
th { background: #e8f2ec; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.4pt; }
pre { background: #f4f4f4; border: 0.6pt solid #ccc; padding: 5pt 7pt; line-height: 1.3; break-inside: avoid;
      white-space: pre-wrap; }
pre code { font-size: 8pt; }
figure { margin: 6pt 0 0; break-inside: avoid; }
figure svg { width: 100%; height: auto; }
figure.page { break-before: page; break-after: page; margin: 0; }
figure.page svg { max-height: 9.5in; }
figure.pins svg { max-height: 8.3in; }
figure.pins { break-after: page; }
.newpage { break-before: page; }
.note { color: #555; font-size: 8.5pt; margin-bottom: 8pt; }
section.notes { break-before: page; }
table.fill td { height: 26pt; }
table.fill td:first-child { width: 45%; }
"""

NOTES = [
    "CAN module, 3V3 pin with only 5 V applied",
    "Header signal pins with 5 V + 3.3 V on (any near 5 V?)",
    "Rails: 5 V rail / 3.3 V rail (Pi on)",
    "<code>dmesg | grep mcp251xfd</code>",
    "CAN module's interface name (<code>can0</code> / <code>can1</code>)",
    "SPI clock the driver reports",
    "SPI wire lengths used",
    "<code>i2cdetect -y 1</code> shows",
    "Measured 1 MΩ resistors (CH1-CH4)",
    "Probe on a 12 V battery: meter / second meter",
    "Anything marked [verify] that turned out different",
]

FOOTER = ('<div style="font-size:7pt;width:100%;padding:0 0.6in;color:#666;display:flex;'
          'justify-content:space-between"><span>CANopener handheld prototype: wiring (stages 1-3)</span>'
          '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')


def _svg(name: str) -> str:
    with open(os.path.join(IMAGES, name)) as f:
        text = f.read()
    return text[text.index("<svg"):]


def _links(md: str) -> str:
    """Links mean nothing on paper: keep the words, and name the document when the words don't."""
    def doc(m):
        words, target = m.group(1), m.group(2)
        name = target.rsplit("/", 1)[-1]
        return words if name in words else f"{words} ({name})"
    md = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)#\s]+\.(?:md|pdf))(#[^)]*)?\)", doc, md)
    return re.sub(r"(?<!!)\[([^\]]+)\]\([^)]*\)", r"\1", md)


def html() -> str:
    import markdown
    with open(GUIDE) as f:
        md = f.read()
    md = re.sub(r"\s*A printable copy of this whole guide.*?\.pdf\)\.", "", md, flags=re.S)
    figures = {}

    def image(m):
        key = f"FIGURE{len(figures)}"
        name = m.group(1)
        cls = {"prototype-wiring.svg": ' class="overview"', "prototype-pins.svg": ' class="pins"'}.get(
            name, ' class="page"')
        figures[key] = f"<figure{cls}>{_svg(name)}</figure>"
        return f"\n\n{key}\n\n"
    md = re.sub(r"!\[[^\]]*\]\(\.\./images/([\w-]+\.svg)\)", image, md)
    md = _links(md).replace("- [ ] ", "- ☐ ")
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    overview = ""
    for key, fig in figures.items():
        if 'class="overview"' in fig:       # page 1: the title and the overview diagram
            overview, fig = fig, ""
        body = body.replace(f"<p>{key}</p>", fig)
    first = body.index("</h1>") + 5
    body = (body[:first] + '<p class="note">Printed from docs/install/handheld-prototype.md. Pin decisions: '
            "docs/ELECTRONICS.md section 5.1.</p>" + overview + '<div class="newpage"></div>' + body[first:])
    body = body.replace("<h2>Pin-by-pin diagrams</h2>", '<h2 class="newpage">Pin-by-pin diagrams</h2>')
    rows = "".join(f"<tr><td>{n}</td><td></td></tr>" for n in NOTES)
    notes = (f'<section class="notes"><h2>Bench notes</h2><table class="fill"><tr><th>What</th>'
             f"<th>What I saw</th></tr>{rows}</table></section>")
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>CANopener prototype wiring</title>'
            f"<style>{CSS}</style></head><body>{body}{notes}</body></html>")


def _chrome() -> str | None:
    import shutil
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None


def main():
    with tempfile.TemporaryDirectory() as tmp:
        page_file = os.path.join(tmp, "guide.html")
        with open(page_file, "w") as f:
            f.write(html())
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None
        if sync_playwright is not None:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto("file://" + page_file)
                page.pdf(path=OUT, format="Letter", print_background=True, display_header_footer=True,
                         header_template="<span></span>", footer_template=FOOTER,
                         margin={"top": "0.6in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"})
                browser.close()
        else:                       # no Python Playwright: Chromium / Chrome on its own (no page numbers)
            import subprocess
            chrome = _chrome()
            if chrome is None:
                sys.exit("Needs Playwright (pip install playwright) or Chromium / Chrome to print the PDF.")
            subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={OUT}", "file://" + page_file], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(OUT, file=sys.stderr)


if __name__ == "__main__":
    main()
