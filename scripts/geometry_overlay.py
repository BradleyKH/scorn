"""Draw detected score geometry over each PDF page and compare measure counts with a sibling MusicXML.

usage: uv run --offline python scripts/geometry_overlay.py <pdf> <out_dir> [--no-images]
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pymupdf

from scorn.reconcile.geometry import read_geometry

COLORS = {
    "staff": (0.1, 0.3, 1.0),
    "system": (0.0, 0.7, 0.2),
    "barline": (1.0, 0.0, 0.0),
    "repeat": (1.0, 0.0, 1.0),
    "stem": (1.0, 0.55, 0.0),
    "beam": (0.8, 0.0, 0.8),
    "ledger": (0.55, 0.3, 0.1),
    "curve": (0.0, 0.75, 0.85),
    "wedge": (0.5, 0.0, 1.0),
    "lyric": (0.45, 0.45, 0.45),
    "dot": (1.0, 0.0, 0.5),
}


def truth_counts(pdf: Path) -> tuple[int, int] | None:
    """Measures in the sibling MusicXML's first part, raw and with each multi-bar rest counted once."""
    for ext in (".mxl", ".musicxml", ".xml"):
        path = pdf.with_suffix(ext)
        if path.exists():
            break
    else:
        return None
    if path.suffix == ".mxl":
        with zipfile.ZipFile(path) as z:
            rootfile = ET.fromstring(z.read("META-INF/container.xml")).find(".//rootfile").get("full-path")
            root = ET.fromstring(z.read(rootfile))
    else:
        root = ET.parse(path).getroot()
    part = root.find("part")
    if part is None:
        return None
    measures = part.findall("measure")
    collapsed = len(measures)
    for m in measures:
        mr = m.find("attributes/measure-style/multiple-rest")
        if mr is not None and mr.text and mr.text.strip().isdigit():
            collapsed -= int(mr.text) - 1
    return len(measures), collapsed


def draw(page: pymupdf.Page, geo) -> None:
    sh = page.new_shape()

    def line(p, q, color, width=0.8):
        sh.draw_line(p, q)
        sh.finish(color=color, width=width)

    for sys_ in geo.systems:
        r = pymupdf.Rect(sys_.x0 - 3, sys_.top - 3, sys_.x1 + 3, sys_.bottom + 3)
        sh.draw_rect(r)
        sh.finish(color=COLORS["system"], width=1.2, dashes="[3 2] 0")
        for st in sys_.staves:
            for y in st.lines:
                line((st.x0, y), (st.x1, y), COLORS["staff"], 0.5)
        for b in sys_.barlines:
            color = COLORS["repeat"] if b.repeat_start or b.repeat_end or b.kind != "single" else COLORS["barline"]
            sh.draw_rect(pymupdf.Rect(b.x0 - 0.8, b.top, b.x1 + 0.8, b.bottom))
            sh.finish(color=color, width=1.0)
    for m in geo.measures:
        st = geo.systems[m.system].staves[0]
        sh.insert_text((m.x0 + 1, st.top - 2), str(m.index + 1), fontsize=6, color=COLORS["barline"])
    for ln in geo.stems:
        line((ln.x0, ln.y0), (ln.x1, ln.y1), COLORS["stem"], 1.0)
    for ln in geo.beams:
        line((ln.x0, ln.y0), (ln.x1, ln.y1), COLORS["beam"], max(ln.width, 1.0))
    for ln in geo.ledgers:
        line((ln.x0, ln.y0), (ln.x1, ln.y1), COLORS["ledger"], 1.2)
    for ln in geo.extenders + geo.hyphens:
        line((ln.x0, ln.y0), (ln.x1, ln.y1), COLORS["lyric"], 1.5)
    for c in geo.curves:
        pts = c.points
        for i in range(0, len(pts) - 3, 3):
            sh.draw_bezier(pts[i], pts[i + 1], pts[i + 2], pts[i + 3])
        sh.finish(color=COLORS["curve"], width=1.0)
    for w in geo.wedges:
        sh.draw_rect(pymupdf.Rect(w.start[0], min(w.start[1], w.end[1]) - w.opening, w.end[0], max(w.start[1], w.end[1]) + w.opening))
        sh.finish(color=COLORS["wedge"], width=1.0)
    for x, y in geo.dots:
        sh.draw_circle((x, y), 1.2)
        sh.finish(color=COLORS["dot"], width=0.8)
    sh.commit()


def main(argv: list[str]) -> int:
    images = "--no-images" not in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    pdf, out = Path(args[0]), Path(args[1])
    doc = pymupdf.open(pdf)
    if images:
        out.mkdir(parents=True, exist_ok=True)
    total = 0
    for page in doc:
        geo = read_geometry(page)
        total += len(geo.measures)
        counts = [len(s.barlines) for s in geo.systems]
        print(
            f"p{page.number + 1}: staves={len(geo.staves)} systems={len(geo.systems)} barlines={counts} "
            f"measures={len(geo.measures)} stems={len(geo.stems)} beams={len(geo.beams)} curves={len(geo.curves)} "
            f"wedges={len(geo.wedges)} ledgers={len(geo.ledgers)}"
        )
        if images:
            draw(page, geo)
            page.get_pixmap(dpi=110).save(out / f"{pdf.stem}-p{page.number + 1:02d}.png")
    truth = truth_counts(pdf)
    verdict = ""
    if truth:
        verdict = f" truth={truth[0]} (multirests collapsed {truth[1]}) {'MATCH' if total in truth else 'MISMATCH'}"
    print(f"TOTAL measures={total}{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
