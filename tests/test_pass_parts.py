import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import pytest

from music_scan import rhythm
from music_scan.reconcile import align, instruments, pdf_score
from music_scan.reconcile.passes import parts

ROOT = Path(__file__).resolve().parent.parent
MOZART_PDF = ROOT / "examples" / "A Tune By Mozart Horn (F).pdf"
MOZART_RAW = ROOT / "out" / "A Tune By Mozart Horn (F)" / "audiveris" / "audiveris" / "A Tune By Mozart Horn (F).mxl"
# known differences between the PDF and the hand-corrected file: (instrument, instrument staff, bar)
EXCEPTIONS = {("Horn", 1, "47"), ("Organ", 2, "62")}


def _run(tree, score):
    log = align.ChangeLog()
    parts.run(tree, score, align.align(score, tree), log)
    return log


def _divisions(root: ET.Element) -> dict[int, int]:
    out = {}
    for part in root.findall("part"):
        d = 1
        for m in part.findall("measure"):
            for a in m.findall("attributes"):
                d = int(a.findtext("divisions") or d)
            out[id(m)] = d
    return out


def _notes(cell: align.Cell, divs: dict[int, int]) -> list:
    evs = align.xml_events(cell.element, cell.xml_staff, divs[id(cell.element)])
    return [(e.onset, e.pitches, e.duration) for e in evs if not e.is_rest]


def _fully_matched(cell: align.Cell) -> bool:
    return cell.content is not None and not cell.multirest and all(p.kind == "match" for p in cell.pairs)


def test_stopford_one_part_per_instrument(stopford_raw_tree, stopford_score):
    tree = stopford_raw_tree
    _run(tree, stopford_score)
    after = align.align(stopford_score, tree)
    holders = defaultdict(set)
    for s in after.staves:
        holders[(s.instrument, s.instrument_staff)].add((s.part, s.xml_staff))
    assert all(len(v) == 1 and None not in {p for p, _ in v} for v in holders.values()), holders
    assert all(len(p.findall("measure")) == 76 for p in tree.findall("part"))
    assert not after.warnings


def test_stopford_notes_match_hand_corrected(stopford_raw_tree, stopford_truth_tree, stopford_score):
    tree = stopford_raw_tree
    _run(tree, stopford_score)
    out = align.align(stopford_score, tree)
    truth = align.align(stopford_score, stopford_truth_tree)
    out_staff = {(s.system, s.part, s.xml_staff): s for s in out.staves}
    truth_slot = {(s.system, s.instrument, s.instrument_staff): (s.part, s.xml_staff) for s in truth.staves}
    divs, tdivs = _divisions(tree), _divisions(stopford_truth_tree)
    checked, bad = 0, []
    for cell in out.cells.values():
        if not _fully_matched(cell):
            continue
        sm = out_staff[(cell.pdf_measure.system_order, cell.part, cell.xml_staff)]
        if (sm.instrument, sm.instrument_staff, cell.measure.number) in EXCEPTIONS:
            continue
        tpart, tstaff = truth_slot[(sm.system, sm.instrument, sm.instrument_staff)]
        tcell = truth.cell(tpart, cell.measure.number, tstaff, cell.measure.occurrence)
        checked += 1
        if _notes(cell, divs) != _notes(tcell, tdivs):
            bad.append((sm.instrument, sm.instrument_staff, cell.measure.number))
    assert checked > 700
    assert not bad, bad


def test_stopford_names_and_programs(stopford_raw_tree, stopford_truth_tree, stopford_score):
    tree = stopford_raw_tree
    _run(tree, stopford_score)

    def names(root):
        return [(sp.findtext("part-name"), sp.findtext("part-abbreviation")) for sp in root.iter("score-part")]

    assert names(tree) == names(stopford_truth_tree)
    programs = [sp.findtext("midi-instrument/midi-program") for sp in tree.iter("score-part")]
    assert programs[:6] == ["57", "57", "61", "58", "59", "48"]
    assert set(programs[6:12]) == {"53"} and programs[12:] == ["20", "20"]


def test_stopford_staves_clefs_and_visibility(stopford_raw_tree, stopford_score):
    tree = stopford_raw_tree
    _run(tree, stopford_score)
    organ = tree.find("part[@id='P13']")
    m1 = organ.find("measure")
    assert m1.findtext("attributes/staves") == "2"
    # the organ's split bars (29-32) come back as one grand staff with left-hand voices on staff 2
    m29 = organ.find("measure[@number='29']")
    lh = [n for n in m29.findall("note") if n.findtext("staff") == "2"]
    assert lh and all(int(n.findtext("voice")) >= 5 for n in lh)
    # Tuba moves out of treble-clef parts: every bar it holds reads in bass clef
    tuba = tree.find("part[@id='P5']")
    clef = None
    for m in tuba.findall("measure"):
        for c in m.iter("clef"):
            clef = c.findtext("sign")
        assert clef == "F", m.get("number")
    # Women 2 is printed only from bar 65
    w2 = tree.find("part[@id='P8']")
    shown = {m.get("number"): sd.get("print-object") for m in w2.findall("measure") for sd in m.iter("staff-details")}
    assert shown["1"] == "no" and shown["65"] == "yes"


def test_stopford_rhythm_not_worse_and_deterministic(stopford_raw, stopford_score):
    a, b = rhythm.load(stopford_raw), rhythm.load(stopford_raw)
    before = {e.measure for e in rhythm.check_root(a) if e.voices}
    log_a, log_b = _run(a, stopford_score), _run(b, stopford_score)
    after = {e.measure for e in rhythm.check_root(a) if e.voices}
    assert after <= before
    assert ET.tostring(a) == ET.tostring(b)
    assert [tuple(c.__dict__.values()) for c in log_a] == [tuple(c.__dict__.values()) for c in log_b]
    assert any(c.category == "parts" and c.applied for c in log_a)


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
