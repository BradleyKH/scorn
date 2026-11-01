import pymupdf
import pytest

from scorn.reconcile import geometry, pdf_marks
from scorn.reconcile.model import Staff


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
