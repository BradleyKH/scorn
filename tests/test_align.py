from fractions import Fraction
from pathlib import Path

import pytest

from scorn import rhythm
from scorn.reconcile import align, pdf_score
from scorn.reconcile.pdf_notes import Event, Note

ROOT = Path(__file__).resolve().parent.parent
STOPFORD_RAW = (
    ROOT / "notes" / "stopford" / "out" / "Christ the Lord is risen again (Stopford) - full score" / "audiveris" / "audiveris"
    / "Christ the Lord is risen again (Stopford) - full score.mxl"
)
BRASS = ["Trumpet I", "Trumpet II", "Horn", "Trombone", "Tuba", "Timpani"]


@pytest.fixture(scope="module")
def stopford_raw_alignment(stopford_pdf):
    if not STOPFORD_RAW.exists():
        pytest.skip(f"missing Audiveris output (run a scan first): {STOPFORD_RAW}")
    score = pdf_score.read_pdf(stopford_pdf)
    return align.align(score, rhythm.load(STOPFORD_RAW))


def _holder(a: align.Alignment, instrument: str, number: int) -> str | None:
    idx = a.measure_index(str(number))
    pm = a.measure_map[idx]
    for s in a.staves:
        if s.system == pm.system_order and s.instrument == instrument:
            return s.part
    return None


@pytest.mark.parametrize("first,last,parts", [
    (1, 28, ["P5", "P6", "P7", "P8", "P9", "P10"]),
    (29, 32, ["P3", "P4", "P5", "P6", "P7", "P8"]),
    (33, 48, ["P5", "P6", "P7", "P8", "P9", "P10"]),
    (49, 64, ["P4", "P5", "P6", "P7", "P8", "P9"]),
    (65, 76, ["P1", "P2", "P3", "P4", "P5", "P6"]),
])
def test_stopford_instrument_mapping(stopford_raw_alignment, first, last, parts):
    a = stopford_raw_alignment
    for n in range(first, last + 1):
        assert [_holder(a, i, n) for i in BRASS] == parts, f"m{n}"


def test_stopford_m29_choir_and_organ(stopford_raw_alignment):
    a = stopford_raw_alignment
    assert _holder(a, "Soprano / Alto", 29) == "P9"
    pm = a.measure_map[a.measure_index("29")]
    organ = sorted((s.instrument_staff, s.part) for s in a.staves
                   if s.system == pm.system_order and s.instrument == "Organ")
    assert organ == [(1, "P11"), (2, "P12"), (3, "P14")]


def test_stopford_every_staff_matched_and_conflicts(stopford_raw_alignment):
    a = stopford_raw_alignment
    assert not a.unmatched
    conflicted = {c.part for c in a.conflicts}
    assert {"P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"} <= conflicted
    p5 = next(c for c in a.conflicts if c.part == "P5")
    assert p5.holdings[0] == ("1", "28", "Trumpet I")
    assert ("29", "32", "Horn") in p5.holdings


def test_stopford_cell_lookup(stopford_raw_alignment):
    a = stopford_raw_alignment
    # Trumpet II m3: Audiveris reads quarters where the PDF has eighths
    cell = a.cell("P6", "3")
    assert cell is not None and cell.instrument == "Trumpet II"
    assert cell.content is not None and cell.content.staff == 1
    assert cell.missing or cell.substituted


def _ev(x: float, onset: Fraction, dur: Fraction, *midis: int) -> Event:
    names = {60: ("C", 4), 62: ("D", 4), 64: ("E", 4), 65: ("F", 4)}
    notes = tuple(Note(*names[m], 0, None, 0.0, x, 0.0, "black") for m in midis)
    return Event(x, 0.0, "quarter", 0, dur, notes, onset=onset)


def _xev(onset: Fraction, dur: Fraction, *midis: int) -> align.XmlEvent:
    return align.XmlEvent((), "1", onset, dur, tuple(midis), False, "quarter")


def test_align_events_kinds():
    q, h = Fraction(1), Fraction(1, 2)
    pdf = [_ev(10, Fraction(0), q, 60), _ev(20, Fraction(1), h, 62), _ev(25, Fraction(3, 2), h, 64),
           _ev(30, Fraction(2), q, 65)]
    xml = [_xev(Fraction(0), q, 60), _xev(Fraction(1), q, 62), _xev(Fraction(2), q, 65), _xev(Fraction(3), q)]
    pairs, _ = align.align_events(pdf, xml)
    assert [p.kind for p in pairs] == ["match", "substitute", "missing", "match", "extra"]


def test_label_matches():
    assert align.label_matches("Tpt. II", "Trumpet II")
    assert not align.label_matches("Tpt. II", "Trumpet I")
    assert align.label_matches("S / A", "Soprano / Alto")
    assert not align.label_matches("T", "Soprano / Alto")
    assert align.label_matches("Hn.", "Horn in F")
    assert align.label_matches("Cl. 2", "Clarinet in Bb 2")
    assert not align.label_matches("Cl. 1", "Clarinet in Bb 2")
    assert not align.label_matches("Tpt.", "Horn in F")
    assert align.label_matches("Vc.", "Cello")
    assert not align.label_matches("Vln. 1", "Violin 2")


def test_change_log_order():
    log = align.ChangeLog()
    log.add("pitch", "P2", "10", "C4", "D4", "notehead")
    log.add("pitch", "P1", "9", "E4", "F4", "notehead")
    log.disagree("rhythm", "P1", "2", "q", "e e", "bar would not add up")
    assert [c.seq for c in log] == [0, 1, 2]
    assert [(c.part, c.measure) for c in log.ordered(["P1", "P2"])] == [("P1", "2"), ("P1", "9"), ("P2", "10")]
    assert not list(log)[2].applied


def test_check_root_matches_check(stopford_truth):
    assert [e.format() for e in rhythm.check_root(rhythm.load(stopford_truth))] == \
        [e.format() for e in rhythm.check(stopford_truth)]


def test_unlabeled_staves_follow_their_instrument_past_hidden_staves(scores_dir):
    pdf = scores_dir / "life that shall endless be - choral.pdf"
    if not pdf.exists():
        pytest.skip(f"missing corpus score: {pdf}")
    score = pdf_score.read_pdf(pdf)
    _, inst_of = align._resolve_instruments(score)
    rows = [[inst_of[(sy.page, st.index)] for st in sy.staves] for sy in score.systems]
    # The unlabeled piano under the choir: a single staff's label sits beside it, so the staves
    # below are not more TB staves, and they stay the piano when TB (then SA) is hidden.
    piano = [("Staff 3", 1), ("Staff 4", 1)]
    assert rows[0] == [("SA", 1), ("TB", 1), *piano]
    assert rows[1] == [("SA", 1), *piano]
    assert rows[4] == [("TB", 1), *piano]
    assert rows[-1] == piano
