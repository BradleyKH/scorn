from pathlib import Path

import pytest

from scorn import rhythm
from scorn.reconcile import align, instruments, pdf_score
from scorn.reconcile.passes import parts

ROOT = Path(__file__).resolve().parent.parent
MOZART_PDF = ROOT / "examples" / "A Tune By Mozart Horn (F).pdf"
MOZART_RAW = ROOT / "out" / "A Tune By Mozart Horn (F)" / "audiveris" / "audiveris" / "A Tune By Mozart Horn (F).mxl"


def _run(tree, score):
    log = align.ChangeLog()
    parts.run(tree, score, align.align(score, tree), log)
    return log


def test_mozart_horn_in_f():
    if not MOZART_PDF.exists() or not MOZART_RAW.exists():
        pytest.skip("missing Mozart horn example or its Audiveris output")
    score = pdf_score.read_pdf(MOZART_PDF)
    tree = rhythm.load(MOZART_RAW)
    _run(tree, score)
    tr = tree.find("part/measure/attributes/transpose")
    assert tr is not None
    assert (tr.findtext("diatonic"), tr.findtext("chromatic")) == ("-4", "-7")
    assert tree.findtext("part-list/score-part/midi-instrument/midi-program") == "61"


@pytest.mark.parametrize("label,name,program,transpose", [
    ("Trumpet in Bb", "Trumpet", 57, (-1, -2)),
    ("Tpt. II", "Trumpet", 57, None),
    ("Horn in F", "Horn", 61, (-4, -7)),
    ("Hn. (F)", "Horn", 61, (-4, -7)),
    ("Corno in Fa", "Horn", 61, (-4, -7)),
    ("Bb Clarinet", "Clarinet", 72, (-1, -2)),
    ("Clarinet in A", "Clarinet", 72, (-2, -3)),
    ("Klarinette in B", "Clarinet", 72, (-1, -2)),
    ("Alto Sax", "Alto Saxophone", 66, (-5, -9)),
    ("Tenor Saxophone", "Tenor Saxophone", 67, (-8, -14)),
    ("Timp.", "Timpani", 48, None),
    ("Org.", "Organ", 20, None),
    ("Vc.", "Cello", 43, None),
    ("Double Bass", "Double Bass", 44, None),
    ("S / A", "Choir", 53, None),
    ("(Men - Group 1)", "Choir", 53, None),
])
def test_instrument_table(label, name, program, transpose):
    info = instruments.lookup(label)
    assert info is not None and info.name == name and info.program == program
    got = None if info.transpose is None else (info.transpose.diatonic, info.transpose.chromatic)
    assert got == transpose


def test_instrument_registers_and_misses():
    assert instruments.voice_register("Soprano / Alto") == "upper"
    assert instruments.voice_register("(Unison Women)") == "upper"
    assert instruments.voice_register("T / B") == "lower"
    assert instruments.voice_register("Choir") == "mixed"
    assert instruments.voice_register("Trumpet I") is None
    assert instruments.lookup("A Tune") is None
    assert instruments.find_in("A Tune By Mozart Horn (F)")[0] == "Horn (F)"
