"""Ground-truth fixtures: a known score engraved to a clean PDF and a degraded "scanned" PDF."""

import re
from io import BytesIO
from pathlib import Path

import numpy as np
import pymupdf
import verovio
from music21 import corpus, stream
from PIL import Image, ImageFilter

PIECES = {
    "melody": ("handel/rinaldo/lascia_chio_pianga", [0]),
    "chorale": ("bach/bwv66.6", None),
    "piano": ("joplin/maple_leaf_rag", None),
}


def build(name: str, out_dir: Path) -> dict[str, Path]:
    source, part_indexes = PIECES[name]
    score = corpus.parse(source)
    if part_indexes is not None:
        kept = stream.Score([score.parts[i] for i in part_indexes])
        kept.metadata = score.metadata
        score = kept

    out_dir.mkdir(parents=True, exist_ok=True)
    truth = out_dir / f"{name}.truth.musicxml"
    score.write("musicxml", fp=truth)

    clean = out_dir / f"{name}.clean.pdf"
    engrave_pdf(truth, clean)
    scanned = out_dir / f"{name}.scanned.pdf"
    degrade_pdf(clean, scanned)
    return {"truth": truth, "clean": clean, "scanned": scanned}


def engrave_pdf(musicxml: Path, out: Path) -> None:
    tk = verovio.toolkit()
    # A4 in Verovio's tenths of a millimeter; scale 100 gives a real-size 7.2 mm staff.
    tk.setOptions({"pageWidth": 2100, "pageHeight": 2970, "scale": 100, "footer": "none"})
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio could not load {musicxml}")
    pdf = pymupdf.open()
    a4 = pymupdf.paper_rect("a4")
    for n in range(1, tk.getPageCount() + 1):
        svg_text = mupdf_compatible(tk.renderToSVG(n))
        with pymupdf.open(stream=svg_text.encode("utf-8"), filetype="svg") as svg:
            src = pymupdf.open("pdf", svg.convert_to_pdf())
            pdf.new_page(width=a4.width, height=a4.height).show_pdf_page(a4, src, 0)
    pdf.save(out)


def mupdf_compatible(svg: str) -> str:
    """MuPDF ignores Verovio's CSS stroke rule and draws nothing in its unsized inner <svg>.

    The inner <svg> gets the root's explicit size; MuPDF resolves a percentage size too small.
    Text opts out of the inherited stroke, which would otherwise bold every lyric.
    """
    root = re.search(r'<svg width="([\d.]+)px" height="([\d.]+)px"', svg)
    if not root:
        raise RuntimeError("unexpected Verovio SVG header")
    width, height = root.groups()
    svg = svg.replace(
        '<svg class="definition-scale"',
        f'<svg class="definition-scale" width="{width}" height="{height}" stroke="black"',
        1,
    )
    return svg.replace("<text ", '<text stroke="none" ')


def degrade_pdf(clean: Path, out: Path, dpi: int = 200, seed: int = 7) -> None:
    """Image-only pages with a slight skew, blur, and speckle, roughly a phone or flatbed scan."""
    rng = np.random.default_rng(seed)
    scanned = pymupdf.open()
    with pymupdf.open(clean) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            img = img.rotate(rng.uniform(-1.2, 1.2), resample=Image.BICUBIC, fillcolor=255)
            img = img.filter(ImageFilter.GaussianBlur(0.6))
            arr = np.asarray(img, dtype=np.float32)
            arr = arr * rng.uniform(0.88, 0.95) + rng.normal(0, 10, arr.shape) + 12
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

            jpeg = _jpeg_bytes(img)
            target = scanned.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, stream=jpeg)
    scanned.save(out)


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
