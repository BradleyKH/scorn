from pathlib import Path

import pymupdf


def rasterize(pdf: Path, out_dir: Path, dpi: int = 300) -> list[Path]:
    """Render each PDF page to a grayscale PNG, the input the image-based engines expect."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            path = out_dir / f"page-{i:03d}.png"
            pix.save(path)
            pages.append(path)
    return pages
