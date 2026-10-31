import os
import shutil
import tempfile
from pathlib import Path

import pymupdf

from scorn import procs
from scorn.mei import toolkit

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
WIDTH = 2000  # px, width of each proof image
DIVIDER = (220, 0, 0)


def find_browser() -> str:
    for candidate in [os.environ.get("SCORN_BROWSER"), *BROWSERS,
                      shutil.which("chrome"), shutil.which("msedge"), shutil.which("chromium")]:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("No Chrome or Edge found; set SCORN_BROWSER to a Chromium-based browser")


def render_pages(musicxml: Path, out_dir: Path) -> list[Path]:
    """Engrave with the file's own page breaks and a number on every bar, so page N lines up with the PDF."""
    tk = toolkit()
    # Verovio's page header double-encodes non-ASCII credit text (an en dash shows as mojibake).
    tk.setOptions({"breaks": "encoded", "pageWidth": 2100, "pageHeight": 2970, "scale": 40,
                   "adjustPageHeight": True, "mnumInterval": 1, "header": "none"})
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio could not load {musicxml}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for n in range(1, tk.getPageCount() + 1):
        path = out_dir / f"page-{n:03d}.svg"
        path.write_text(tk.renderToSVG(n), encoding="utf-8")
        pages.append(path)
    return pages


def rasterize_svg(svg: Path, png: Path, browser: str) -> None:
    # MuPDF draws Verovio's SVG blank, so a headless browser does the rasterizing.
    head = svg.read_text(encoding="utf-8")[:300]
    width = int(float(head.split('width="')[1].split("px")[0]))
    height = int(float(head.split('height="')[1].split("px")[0]))
    with tempfile.TemporaryDirectory() as profile:
        procs.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={profile}", "--force-device-scale-factor=2",
                        f"--window-size={width},{height}", f"--screenshot={png.resolve()}", svg.resolve().as_uri()],
                       check=True, capture_output=True, timeout=120)


def music_area(page: pymupdf.Page) -> pymupdf.Rect:
    """The system on a PDF page: everything drawn as lines, plus the instrument names to its left."""
    # Rect union skips zero-height rects, which is every staff line, so take the extent by hand.
    rects = [d["rect"] for d in page.get_drawings()
             # skip a purchaser watermark or other furniture in the outer margins
             if d["rect"].x1 > page.rect.width * 0.06 and d["rect"].height < page.rect.height * 0.9]
    return pymupdf.Rect(page.rect.width * 0.06, min(r.y0 for r in rects) - 20,
                        max(r.x1 for r in rects) + 4, max(r.y1 for r in rects) + 20)


def ink_rows(pix: pymupdf.Pixmap) -> tuple[int, int]:
    """First and last pixel rows that hold anything darker than near-white."""
    rows = []
    stride, n = pix.stride, pix.n
    data = pix.samples
    for y in range(pix.height):
        row = data[y * stride:y * stride + pix.width * n]
        if min(row[::n]) < 200:
            rows.append(y)
    return rows[0], rows[-1]


Clip = tuple[pymupdf.Document, int, pymupdf.Rect]  # document, page number, region


def stack(top: Clip, bottom: Clip, out: Path) -> None:
    """Scale both regions to the proof width and draw them one above the other, split by a red bar."""
    gap = 12
    h_top = top[2].height * WIDTH / top[2].width
    h_bot = bottom[2].height * WIDTH / bottom[2].width
    doc = pymupdf.open()
    page = doc.new_page(width=WIDTH, height=h_top + gap + h_bot)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    page.show_pdf_page(pymupdf.Rect(0, 0, WIDTH, h_top), top[0], top[1], clip=top[2])
    page.draw_rect(pymupdf.Rect(0, h_top, WIDTH, h_top + gap), color=None, fill=tuple(c / 255 for c in DIVIDER))
    page.show_pdf_page(pymupdf.Rect(0, h_top + gap, WIDTH, h_top + gap + h_bot), bottom[0], bottom[1], clip=bottom[2])
    page.get_pixmap(dpi=72, alpha=False).save(out)


def proof(pdf: Path, musicxml: Path, out_dir: Path) -> list[Path]:
    """Write pNN-top.png and pNN-bot.png per page: the PDF half above a red line, the engraving below."""
    browser = find_browser()
    svgs = render_pages(musicxml, out_dir / "svg")
    images = []
    with pymupdf.open(pdf) as src:
        if len(src) != len(svgs):
            print(f"  warning: {len(src)} PDF pages but {len(svgs)} engraved pages; "
                  "pages line up only when the MusicXML keeps the PDF's page breaks")
        for i, svg in enumerate(svgs):
            if i >= len(src):
                break
            png = svg.with_suffix(".png")
            rasterize_svg(svg, png, browser)
            pix = pymupdf.Pixmap(str(png))
            r0, r1 = ink_rows(pix)
            # the engraving as a one-page document, so both halves are clipped the same way
            eng = pymupdf.open()
            eng.new_page(width=pix.width, height=pix.height).insert_image(pymupdf.Rect(0, 0, pix.width, pix.height),
                                                                          filename=str(png))
            area = music_area(src[i])
            for tag, (a, b) in {"top": (0.0, 0.55), "bot": (0.45, 1.0)}.items():
                pdf_half = pymupdf.Rect(area.x0, area.y0 + a * area.height, area.x1, area.y0 + b * area.height)
                eng_half = pymupdf.Rect(0, r0 + a * (r1 - r0), pix.width, r0 + b * (r1 - r0))
                out = out_dir / f"p{i + 1:02d}-{tag}.png"
                stack((src, i, pdf_half), (eng, 0, eng_half), out)
                images.append(out)
    return images
