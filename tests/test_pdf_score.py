from dataclasses import replace

import pymupdf
import pytest

from music_scan import rhythm
from music_scan.reconcile import align, pdf_notes, pdf_score
from music_scan.reconcile.geometry import read_geometry
from music_scan.reconcile.glyphs import Glyph, GlyphClass, document_fonts, page_glyphs
from music_scan.reconcile.model import Line


@pytest.fixture(scope="module")
def stopford(stopford_pdf):
    score = pdf_score.read_pdf(stopford_pdf)
    assert isinstance(score, pdf_score.PdfScore)
    return score


def test_stopford_measures(stopford):
    assert len(stopford.measures) == 76
    assert stopford.bar_count == 76
    assert [m.number for m in stopford.measures] == list(range(1, 77))
    assert len(stopford.systems) == 19
    assert not any(m.pickup for m in stopford.measures)
    # printed system-start numbers agree with the count
    assert not stopford.warnings
    assert stopford.measures[4].printed == 5


def test_stopford_marks_and_text_by_measure(stopford):
    total = sum(len(stopford.marks_in(m.key(st.index))) for s in stopford.systems for m in s.measures
                for st in s.staves)
    assert total == len(stopford.marks)
    # the tempo mark sits over the first bar of the top staff
    first = stopford.systems[0]
    tempos = stopford.tempos_in(first.measures[0].key(first.staves[0].index))
    assert [t.text for t in tempos] == ["Joyfully"]


def test_scan_is_not_applicable(scores_dir):
    pdf = scores_dir / "duke street.pdf"
    if not pdf.exists():
        pytest.skip(f"missing corpus scan: {pdf}")
    result = pdf_score.read_pdf(pdf)
    assert isinstance(result, pdf_score.NotApplicable)


def test_stopford_reader_audit(stopford, stopford_truth):
    """Every PDF note in all 76 bars agrees with the hand-corrected file except the explained ones:
    Horn m47 is printed F3 on the third ledger line (the hand-corrected file keeps Audiveris's A3), and
    the organ left hand's m62 beat-4 rest is not printed in the PDF."""
    a = align.align(stopford, rhythm.load(stopford_truth))
    assert not a.unmatched
    diffs = sorted((c.part, c.measure.number, c.xml_staff, p.kind) for c in a.cells.values() for p in c.pairs
                   if p.kind != "match")
    assert diffs == [("P13", "62", 2, "extra"), ("P3", "47", 1, "substitute")]


def test_ottava_line_raises_notes(stopford_pdf):
    """An 8va sign with its dashed extension over Trumpet II m3 reads those notes an octave up."""
    doc = pymupdf.open(stopford_pdf)
    page = doc[0]
    geometry = read_geometry(page)
    glyphs = list(page_glyphs(page, document_fonts(doc)))
    staff = next(s for s in geometry.staves if s.index == 1)
    m3 = next(m for m in geometry.measures if m.index == 2)
    y = staff.top - 3 * staff.spacing
    sign = Glyph("Ã", "Maestro", 14.0, m3.x0 + 2, y, (m3.x0 + 2, y - 8, m3.x0 + 10, y + 1), glyphs[0].encoding,
                 GlyphClass.OTTAVA_8VA)
    dashes = tuple(Line(0, x, y - 4, x + 4, y - 4, 0.5, "other") for x in range(int(m3.x0) + 12, int(m3.x1) - 4, 7))
    geometry = replace(geometry, lines=geometry.lines + dashes)
    contents = pdf_notes.read_notes(page, geometry, glyphs + [sign])
    c = next(c for c in contents if c.staff == 1 and c.measure == 2)
    assert [n.name for e in c.events for n in e.notes] == ["F5", "E5", "D5", "C5"]
    # the line ends inside m3, so m4 reads as without the sign
    plain = pdf_notes.read_notes(page, read_geometry(page), glyphs)

    def m4(cs):
        cell = next(c for c in cs if c.staff == 1 and c.measure == 3)
        return [n.name for e in cell.events for n in e.notes]

    assert m4(contents) == m4(plain)
