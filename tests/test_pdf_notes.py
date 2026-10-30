from fractions import Fraction
from pathlib import Path

import pymupdf
import pytest
from music21 import converter

from music_scan import compare
from music_scan.reconcile import pdf_notes
from music_scan.reconcile.geometry import read_geometry
from music_scan.reconcile.glyphs import document_fonts, page_glyphs


@pytest.fixture(scope="module")
def stopford_page1(stopford_pdf):
    doc = pymupdf.open(stopford_pdf)
    page = doc[0]
    contents = pdf_notes.read_notes(page, read_geometry(page), list(page_glyphs(page, document_fonts(doc))))
    return {(c.staff, c.measure): c for c in contents}


def _summary(c: pdf_notes.StaffMeasureContent) -> list[tuple[str, str, int]]:
    return [("r" if e.is_rest else "+".join(n.name for n in e.notes), e.type, e.dots) for e in c.events]


def test_stopford_trumpet2_m3(stopford_page1):
    c = stopford_page1[(1, 2)]
    assert _summary(c) == [
        ("r", "quarter", 0),
        ("F4", "eighth", 0),
        ("E4", "eighth", 0),
        ("D4", "quarter", 0),
        ("C4", "quarter", 0),
    ]
    assert sum(e.duration for e in c.events) == c.time.quarters


def test_stopford_timpani_m4_single_note(stopford_page1):
    c = stopford_page1[(5, 3)]
    notes = [e for e in c.events if not e.is_rest]
    # the tremolo strokes on the stem must not read as extra chord notes
    assert len(notes) == 1 and [n.name for n in notes[0].notes] == ["C3"]
    assert notes[0].type == "half"


def test_stopford_header(stopford_page1):
    trumpet, timpani = stopford_page1[(0, 0)], stopford_page1[(5, 0)]
    assert trumpet.clef == pdf_notes.Clef("G", 2) and timpani.clef == pdf_notes.Clef("F", 4)
    assert trumpet.key == -1 and trumpet.key_shown
    assert trumpet.time == pdf_notes.TimeSignature(2, 2) and trumpet.time_shown
    # later measures carry the header without redrawing it
    assert stopford_page1[(0, 1)].time == pdf_notes.TimeSignature(2, 2) and not stopford_page1[(0, 1)].time_shown


def test_stopford_bars_add_up(stopford_pdf):
    contents = pdf_notes.read_document_notes(pymupdf.open(stopford_pdf))
    assert contents
    full = [c for c in contents if c.time and not c.multirest and c.voices == 1]
    ok = [c for c in full if sum(e.duration for e in c.events) == c.time.quarters]
    # pickups and the odd misread aside, single-voice bars should fill their meter
    assert len(ok) / len(full) >= 0.95


def test_clef_pitch_mapping():
    assert pdf_notes.TREBLE.bottom_line() == 4 * 7 + 2  # E4
    assert pdf_notes.Clef("F", 4).bottom_line() == 2 * 7 + 4  # G2
    assert pdf_notes.Clef("C", 3).bottom_line() == 3 * 7 + 3  # F3
    assert pdf_notes.Clef("G", 2, -1).bottom_line() == 3 * 7 + 2  # E3


def _events(contents) -> list[tuple[str, float]]:
    """Single-staff reading as compare.events labels: MIDI numbers or "r", and quarter lengths."""
    out = []
    for c in contents:
        if c.multirest:
            bar = c.time.quarters if c.time else Fraction(4)
            out += [("r", round(float(bar), 3))] * c.multirest
            continue
        for e in sorted(c.events, key=lambda e: (e.voice, e.x)):
            if e.grace:
                continue
            label = "r" if e.is_rest else "+".join(str(m) for m in sorted(n.midi for n in e.notes))
            out.append((label, round(float(e.duration), 3)))
    return out


# Single-line Finale parts whose MusicXML was exported from the same file.
CORPUS = [
    "The Godfather Waltz",
    "careless whisper sax",
    "fairest lord jesus - cello",
    "lessons/Panis Angelicus - tpt",
    "lessons/lyrical etude",
    "lessons/Trumpet scale exercises",
]


@pytest.mark.parametrize("stem", CORPUS)
def test_corpus_single_line(scores_dir: Path, stem: str):
    pdf, truth = scores_dir / f"{stem}.pdf", scores_dir / f"{stem}.mxl"
    if not pdf.exists() or not truth.exists():
        pytest.skip(f"missing corpus pair: {stem}")
    found = _events(pdf_notes.read_document_notes(pymupdf.open(pdf)))
    expected = compare.events(converter.parse(truth).parts[0])
    assert compare.accuracy([e[0] for e in expected], [e[0] for e in found]) >= 0.97
    assert compare.accuracy(expected, found) >= 0.95


# Ghostscript prints of Finale files: beams drawn as hatches of hairline strokes.
GHOSTSCRIPT = [
    "Old Scores Folder/Finale/Compositions/Instrumental/Horn/lydian litany_h",
    "noble airedale",
]


@pytest.mark.parametrize("stem", GHOSTSCRIPT)
def test_corpus_ghostscript_hatched_beams(scores_dir: Path, stem: str):
    pdf = scores_dir / f"{stem}.pdf"
    truth = next((scores_dir / f"{stem}{ext}" for ext in (".mxl", ".musicxml") if (scores_dir / f"{stem}{ext}").exists()), None)
    if not pdf.exists() or truth is None:
        pytest.skip(f"missing corpus pair: {stem}")
    found = _events(pdf_notes.read_document_notes(pymupdf.open(pdf)))
    expected = compare.events(converter.parse(truth).parts[0])
    assert compare.accuracy(expected, found) >= 0.95


def test_hatch_strokes_merge_into_one_beam():
    from music_scan.reconcile.geometry import _merge_hatches, _Seg

    stack = [_Seg(100.0, 50 + 0.12 * i, 124.0, 51.3 + 0.12 * i, 0.12) for i in range(21)]
    separate = [_Seg(100.0, 54 + 0.12 * i, 124.0, 55.3 + 0.12 * i, 0.12) for i in range(21)]
    merged = sorted(_merge_hatches(stack + separate), key=lambda s: s.y0)
    assert len(merged) == 2
    assert merged[0].width == pytest.approx(2.52)
    assert merged[0].y0 == pytest.approx(51.2) and merged[0].y1 - merged[0].y0 == pytest.approx(1.3)


def test_text_ottavas():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "8va", fontname="tiro")
    page.insert_text((100, 200), "(8vb)", fontname="tiro")
    page.insert_text((100, 300), "clar. 8va", fontname="tiro")
    found = pdf_notes._text_ottavas(page)
    assert [(round(x), round(y), s) for x, y, _, s in found] == [(100, 100, 1), (100, 200, -1)]


# Bar counts that hinge on a courtesy key change after a double bar, a time signature between
# repeat barlines, and a rehearsal box over a start repeat.
BAR_COUNTS = ["circles/circles", "circles/circles 2022", "Alison/No Words"]


@pytest.mark.parametrize("stem", BAR_COUNTS)
def test_corpus_bar_counts(scores_dir: Path, stem: str):
    pdf, truth = scores_dir / f"{stem}.pdf", scores_dir / f"{stem}.mxl"
    if not pdf.exists() or not truth.exists():
        pytest.skip(f"missing corpus pair: {stem}")
    found = sum(len(read_geometry(page).measures) for page in pymupdf.open(pdf))
    assert found == len(compare.load_xml(truth).find("part").findall("measure"))
