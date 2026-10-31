"""Correct Audiveris MusicXML from the born-digital PDF it was scanned from.

`reconcile` reads the PDF once, runs the passes in a fixed order and reports every change. A pass
that leaves more bars failing the rhythm check than before it ran is undone as a whole; time
signatures are corrected first, outside that gate.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from scorn import rhythm

# Imported as a module: binding the function would shadow the `align` submodule on this package.
from . import align as alignment
from .align import ChangeLog, measure_sort_key
from .passes import marks, parts, pitch, rhythm_fix, text
from .pdf_score import NotApplicable, read_pdf
from .report import Report, failing_bars

__all__ = ["reconcile", "Report"]

PASSES = (("parts", parts.run), ("pitch", pitch.run), ("rhythm", rhythm_fix.run),
          ("marks", marks.run), ("text", text.run))

_PROLOG = re.compile(rb"^\s*((?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*(?:<!DOCTYPE[^>]*>\s*)?)", re.S)


def reconcile(pdf: Path, musicxml: Path, out: Path) -> Report:
    pdf, musicxml, out = Path(pdf), Path(musicxml), Path(out)
    source = _source_bytes(musicxml)
    out.parent.mkdir(parents=True, exist_ok=True)
    score = read_pdf(pdf)
    if isinstance(score, NotApplicable):
        out.write_bytes(source)
        return Report(applicable=False, reason=score.reason, source=musicxml.name)

    root = rhythm.load(musicxml)
    log = ChangeLog()
    before = failing_bars(root)
    report = Report(applicable=True, source=musicxml.name, failing_before=sorted(before, key=measure_sort_key))
    # Time signatures run ungated, ahead of the baseline: a corrected time signature exposes bars that
    # only the rhythm pass can rebuild, so gating it would undo the whole pitch pass with it.
    pitch.fix_times(root, alignment.align(score, root), log)
    current = failing_bars(root)
    for name, run in PASSES:
        snapshot = copy.deepcopy(root)
        pass_log = ChangeLog()
        run(root, score, alignment.align(score, root), pass_log)
        after = failing_bars(root)
        gained = sorted(after - current, key=measure_sort_key)
        if gained:
            root = snapshot
            report.rollbacks.append(name)
            log.disagree("rollback", "", "", f"{name} pass", "undone",
                         "bars that no longer add up: " + ", ".join(gained))
            continue
        for c in pass_log:
            log.add(c.category, c.part, c.measure, c.before, c.after, c.evidence, c.applied)
        current = after

    _write(root, _PROLOG.match(source).group(1), out)
    report.failing_after = sorted(current, key=measure_sort_key)
    report.part_names = {sp.get("id"): sp.findtext("part-name") or sp.get("id") for sp in root.iter("score-part")}
    report.part_order = [p.get("id") for p in root.findall("part")]
    report.changes = log.ordered(report.part_order)
    return report


def _source_bytes(path: Path) -> bytes:
    """The score document itself, unzipped when the input is compressed."""
    if path.suffix.lower() != ".mxl":
        return path.read_bytes()
    with zipfile.ZipFile(path) as z:
        rootfile = ET.fromstring(z.read("META-INF/container.xml")).find(".//rootfile").get("full-path")
        return z.read(rootfile)


def _write(root: ET.Element, prolog: bytes, out: Path) -> None:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode").encode("utf-8")
    if not prolog.strip():
        prolog = b'<?xml version="1.0" encoding="UTF-8"?>\n'
    # The body is always written as UTF-8, whatever the source declared.
    prolog = re.sub(rb"""encoding=(["'])[^"']*\1""", b'encoding="UTF-8"', prolog, count=1)
    out.write_bytes(prolog.rstrip() + b"\n" + body + b"\n")
