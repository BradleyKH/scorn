import subprocess
import sys
from pathlib import Path

from music21 import converter, stream

from scorn import procs
from scorn.pdf import rasterize


def scan(pdf: Path, out_dir: Path) -> list[Path]:
    """homr reads one image at a time, so rasterize, scan each page, then join the pages."""
    page_dir = out_dir / "homr"
    images = rasterize(pdf, page_dir)
    log = out_dir / "homr.log"
    with log.open("w", encoding="utf-8") as f:
        for image in images:
            # Its console script, run through this interpreter so it works without the venv on PATH.
            # preload_dlls() loads the pip NVIDIA DLLs; without it Windows can't find them and homr
            # falls back to the CPU while still reporting CUDA as available.
            launcher = "import onnxruntime; onnxruntime.preload_dlls(); from homr.main import main; main()"
            cmd = [sys.executable, "-c", launcher, str(image)]
            result = procs.run(cmd, stdout=f, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                raise RuntimeError(f"homr failed on {image.name}; see {log}")

    page_xml = [image.with_suffix(".musicxml") for image in images]
    joined = out_dir / f"{pdf.stem}.homr.musicxml"
    join_pages(page_xml, joined)
    return [joined]


def join_pages(pages: list[Path], out: Path) -> None:
    """Append each page's measures to the matching part of the first page, by part order."""
    scores = [converter.parse(p) for p in pages]
    joined = scores[0]
    for score in scores[1:]:
        for target, part in zip(joined.parts, score.parts):
            offset = target.highestTime
            for measure in part.getElementsByClass(stream.Measure):
                target.insert(offset + measure.offset, measure)
    for part in joined.parts:
        for number, measure in enumerate(part.getElementsByClass(stream.Measure), start=1):
            measure.number = number
    joined.write("musicxml", fp=out)
