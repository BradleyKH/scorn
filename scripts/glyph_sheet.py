"""Contact sheet of music-font glyphs per class, to check the glyph tables by eye.

    uv run --offline python scripts/glyph_sheet.py a.pdf b.pdf [--out out/glyph_sheet/x.png] [--raw]

Each row is a glyph class (or, with --raw, one normalized font code) followed by
up to N crops at 300 dpi, 40 pt square centered on the glyph's baseline origin
(red cross). Long sheets split into numbered PNGs.
"""

import argparse
from collections import defaultdict
from pathlib import Path

import pymupdf

from music_scan.reconcile.glyphs import document_fonts, normalize, page_glyphs

BOX = 40.0
CELL = 64.0
LABEL_W = 220.0
ROW_H = 72.0
ROWS_PER_SHEET = 30


def collect(pdfs: list[Path], per: int, raw: bool) -> dict[str, list[tuple[Path, int, float, float]]]:
    by_pdf: dict[str, list[list[tuple[Path, int, float, float]]]] = defaultdict(list)
    for pdf in pdfs:
        doc = pymupdf.open(pdf)
        fonts = document_fonts(doc)
        here: dict[str, list[tuple[Path, int, float, float]]] = defaultdict(list)
        for page in doc:
            for g in page_glyphs(page, fonts):
                if raw:
                    n = normalize(g.encoding, g.char)
                    key = f"{g.encoding.encoding} {g.encoding.charmap[:3]} U+{ord(n):04X} {n} {g.cls.name if g.cls else '?'}"
                elif g.cls is None:
                    continue
                else:
                    key = g.cls.name
                if len(here[key]) < per:
                    here[key].append((pdf, page.number, g.x, g.y))
        doc.close()
        for key, hits in here.items():
            by_pdf[key].append(hits)
    # Round-robin across PDFs so every input gets a share of each row.
    found = {}
    for key, lists in by_pdf.items():
        merged = [hit for tier in zip(*(lst + [None] * per for lst in lists)) for hit in tier if hit]
        found[key] = merged[:per]
    return dict(sorted(found.items()))


def render(found: dict[str, list[tuple[Path, int, float, float]]], out: Path, per: int) -> list[Path]:
    docs: dict[Path, pymupdf.Document] = {}
    keys = list(found)
    written = []
    for start in range(0, len(keys), ROWS_PER_SHEET):
        chunk = keys[start:start + ROWS_PER_SHEET]
        sheet = pymupdf.open()
        page = sheet.new_page(width=LABEL_W + per * CELL + 8, height=len(chunk) * ROW_H + 8)
        for row, key in enumerate(chunk):
            top = 4 + row * ROW_H
            page.insert_textbox(pymupdf.Rect(4, top + 4, LABEL_W - 4, top + ROW_H - 4),
                                f"{key}\n({len(found[key])})", fontsize=9, fontname="helv")
            for col, (pdf, pno, x, y) in enumerate(found[key]):
                doc = docs.get(pdf) or docs.setdefault(pdf, pymupdf.open(pdf))
                clip = pymupdf.Rect(x - BOX / 2, y - BOX / 2, x + BOX / 2, y + BOX / 2)
                src = doc[pno]
                # Text coordinates are in the rotated page; clips are not.
                clip = (clip & src.rect) * src.derotation_matrix
                if clip.is_empty:
                    continue
                pix = src.get_pixmap(dpi=300, clip=clip)
                cell = pymupdf.Rect(LABEL_W + col * CELL, top, LABEL_W + col * CELL + CELL - 4, top + CELL - 4)
                page.insert_image(cell, pixmap=pix)
                page.draw_rect(cell, color=(0.7, 0.7, 0.7), width=0.5)
                cx, cy = (cell.x0 + cell.x1) / 2, (cell.y0 + cell.y1) / 2
                page.draw_line((cx - 3, cy), (cx + 3, cy), color=(1, 0, 0), width=0.4)
                page.draw_line((cx, cy - 3), (cx, cy + 3), color=(1, 0, 0), width=0.4)
        path = out if len(keys) <= ROWS_PER_SHEET else out.with_name(f"{out.stem}-{start // ROWS_PER_SHEET + 1}{out.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(dpi=144).save(path)
        written.append(path)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/glyph_sheet/sheet.png"))
    ap.add_argument("--per", type=int, default=6, help="examples per row")
    ap.add_argument("--raw", action="store_true", help="one row per font code instead of per class")
    args = ap.parse_args()
    found = collect(args.pdf, args.per, args.raw)
    for path in render(found, args.out, args.per):
        print(path)


if __name__ == "__main__":
    main()
