from fractions import Fraction

import pytest

from scorn.reconcile import align, pdf_score
from scorn.reconcile.pdf_notes import Event, Note


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
