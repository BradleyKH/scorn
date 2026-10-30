from collections import Counter

import pymupdf
import pytest

from music_scan.reconcile import geometry, glyphs, pdf_marks
from music_scan.reconcile.model import Staff


@pytest.fixture(scope="module")
def stopford_marks(stopford_pdf):
    doc = pymupdf.open(stopford_pdf)
    fonts = glyphs.document_fonts(doc)
    marks = []
    for page in doc:
        geo = geometry.read_geometry(page)
        marks += pdf_marks.read_marks(page, geo, list(glyphs.page_glyphs(page, fonts)))
    return marks


def test_stopford_counts(stopford_marks):
    c = Counter((m.kind, m.name) for m in stopford_marks)
    assert sum(n for (k, _), n in c.items() if k == "dynamic") == 105
    assert c[("hairpin", "crescendo")] == 15
    assert c[("hairpin", "diminuendo")] == 3
    assert c[("tremolo", "tremolo")] == 19
    assert c[("fermata", "upright")] == 6
    assert c[("articulation", "staccato")] == 0
    assert c[("articulation", "accent")] == 0


def test_hairpins_carry_their_end(stopford_marks):
    hairpins = [m for m in stopford_marks if m.kind == "hairpin"]
    assert hairpins and all(m.x_end is not None and m.x_end > m.x for m in hairpins)


def test_tremolos_attach_to_stems(stopford_marks):
    # whole-note rolls have no stem, so only the stemmed ones attach
    on_stem = [m for m in stopford_marks if m.kind == "tremolo" and m.detail["on_stem"]]
    assert 4 <= len(on_stem) < 19


def test_ties_and_slurs_found(stopford_marks):
    c = Counter(m.kind for m in stopford_marks)
    assert c["tie"] > 15 and c["slur"] > 80


def test_assign_staff_prefers_staff_above_without_lyrics():
    a = Staff(0, 0, 100, 120, 5, 50, 500)
    b = Staff(0, 1, 160, 180, 5, 50, 500)
    # lower half of the gap: staff above unless it carries lyrics
    assert pdf_marks.assign_staff((a, b), 200, 135, set()).index == 0
    assert pdf_marks.assign_staff((a, b), 200, 150, {0}).index == 1
    assert pdf_marks.assign_staff((a, b), 200, 110, {0}).index == 0


def test_assign_staff_lyric_staff_below_claims_mark_above_it():
    a = Staff(0, 0, 302, 317, 3.6, 50, 500)
    b = Staff(0, 1, 358, 373, 3.6, 50, 500)
    # 8pt above a lyric staff, 33pt below a lyric-free staff: the choir mark belongs to the lower staff
    assert pdf_marks.assign_staff((a, b), 200, 350, {1}).index == 1
    # 12pt below the upper staff: still that staff's own mark
    assert pdf_marks.assign_staff((a, b), 200, 329, {1}).index == 0


def test_stopford_m41_mp_lands_on_choir_staff(stopford_pdf):
    # page 11 holds the bar-41 mp marks; the one 8pt above the Women 1 staff (lyrics below it)
    # went to the staff above it, the timpani
    doc = pymupdf.open(stopford_pdf)
    page = doc[10]
    geo = geometry.read_geometry(page)
    gl = list(glyphs.page_glyphs(page, glyphs.document_fonts(doc)))
    mps = [m for m in pdf_marks.read_marks(page, geo, gl) if m.kind == "dynamic" and m.name == "mp"]
    assert len(mps) == 6 and len({m.staff for m in mps}) == 6
    choir = next(m for m in mps if abs(m.y - 350) < 3)
    assert choir.staff == 6 and choir.placement == "above"


def test_hatched_hairpin_strokes_read_as_one_wedge():
    # Ghostscript strokes each hairpin line as a stack of hairlines 0.12pt apart
    doc = pymupdf.open()
    page = doc.new_page()
    for y in (100, 110, 120, 130, 140):
        page.draw_line((50, y), (500, y), width=0.5)
    for i in range(6):
        d = 0.12 * i
        page.draw_line((200, 170 + d), (260, 166 + d), width=0.12)
        page.draw_line((200, 170 + d), (260, 174 + d), width=0.12)
    wedges = geometry.read_geometry(page).wedges
    assert len(wedges) == 1 and wedges[0].kind == "crescendo"


def test_stopford_long_slurs_stay_on_their_staff(stopford_marks):
    # the horn slur over m25-28 bulges toward the trombone staff; the Trumpet I slurs over m29-31
    # and m69-70 arch higher than six spaces
    slurs = {(m.page, m.staff, round(m.x), round(m.x_end)) for m in stopford_marks if m.kind == "slur"}
    assert (6, 2, 98, 526) in slurs
    assert (7, 0, 126, 272) in slurs and (7, 0, 291, 516) in slurs
    assert (17, 0, 99, 278) in slurs


def test_flattened_ghostscript_slur_reads_as_curve():
    doc = pymupdf.open()
    page = doc.new_page()
    for y in (100, 110, 120, 130, 140):
        page.draw_line((50, y), (500, y), width=0.5)
    # a filled slur outline flattened into short lines, as Ghostscript prints it
    top = [(200 + 5 * i, 95 - 8 * (1 - ((i - 10) / 10) ** 2)) for i in range(21)]
    bottom = [(x, y + 1.0 - 0.8 * abs(x - 250) / 50) for x, y in reversed(top)]
    page.draw_polyline(top + bottom + [top[0]], fill=(0, 0, 0), width=0)
    curves = geometry.read_geometry(page).curves
    assert len(curves) == 1 and curves[0].bbox[2] - curves[0].bbox[0] == pytest.approx(100, abs=1)
