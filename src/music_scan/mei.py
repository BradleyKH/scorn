from importlib.resources import files
from pathlib import Path

import verovio

RESOURCES = str(files("verovio") / "data")


def toolkit() -> verovio.toolkit:
    # Verovio keeps its default resource path per thread, and the package only sets it on the
    # importing thread, so a toolkit made on any other thread has no fonts and loads nothing.
    tk = verovio.toolkit(False)
    tk.setResourcePath(RESOURCES)
    return tk


def mei_path(musicxml: Path, engine: str) -> Path:
    return musicxml.with_name(musicxml.name.split(".")[0] + f".{engine}.mei")


def to_mei(musicxml: Path, out: Path) -> None:
    """Convert the way Cadence's score dialog does: Verovio loadData then getMEI, ids assigned here."""
    tk = toolkit()
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio could not load {musicxml}")
    out.write_text(tk.getMEI(), encoding="utf-8")


def to_svg(musicxml: Path, out_dir: Path) -> list[Path]:
    """Engrave the recognized score so it can be eyeballed against the source PDF."""
    tk = toolkit()
    # Verovio's page header double-encodes non-ASCII credit text (an en dash shows as mojibake).
    tk.setOptions({"pageWidth": 2100, "pageHeight": 2970, "scale": 40, "header": "none"})
    if not tk.loadFile(str(musicxml)):
        raise RuntimeError(f"Verovio could not load {musicxml}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for n in range(1, tk.getPageCount() + 1):
        path = out_dir / f"page-{n:03d}.svg"
        path.write_text(tk.renderToSVG(n), encoding="utf-8")
        pages.append(path)
    return pages
