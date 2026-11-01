from fractions import Fraction
from pathlib import Path

import pymupdf
import pytest

converter = pytest.importorskip("music21").converter

from scorn import compare  # noqa: E402
from scorn.reconcile import pdf_notes
from scorn.reconcile.geometry import read_geometry


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
    from scorn.reconcile.geometry import _merge_hatches, _Seg

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
