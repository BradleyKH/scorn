import copy
import xml.etree.ElementTree as ET

from music_scan import rhythm
from music_scan.reconcile import align
from music_scan.reconcile.passes import rhythm_fix


def _measure(root: ET.Element, part: str, number: str) -> ET.Element:
    return next(m for m in root.find(f"part[@id='{part}']").findall("measure") if m.get("number") == number)


def _events(measure: ET.Element) -> list[tuple]:
    out = []
    for n in measure.findall("note"):
        pitch = None if n.find("pitch") is None else n.findtext("pitch/step") + n.findtext("pitch/octave")
        out.append((pitch, n.findtext("type"), n.findtext("duration"), n.find("chord") is not None,
                    n.get("print-object") == "no"))
    return out


def _run(root: ET.Element, score) -> align.ChangeLog:
    log = align.ChangeLog()
    rhythm_fix.run(root, score, align.align(score, root), log)
    return log


def test_restores_trumpet_ii_eighths(stopford_truth_tree, stopford_raw_tree, stopford_score):
    root = stopford_truth_tree
    truth = {n: _events(_measure(root, "P2", n)) for n in ("3", "23")}
    # Audiveris's Trumpet II (raw P6 for m1-28) read the eighths as quarters and dropped the C4
    for n in ("3", "23"):
        _measure(root, "P2", n)[:] = [copy.deepcopy(el) for el in _measure(stopford_raw_tree, "P6", n)]
    f4 = next(el for el in _measure(root, "P2", "3").findall("note") if el.findtext("pitch/step") == "F")
    ET.SubElement(ET.SubElement(f4, "notations"), "slur", {"type": "start", "number": "1"})

    log = _run(root, stopford_score)

    for n in ("3", "23"):
        got = _events(_measure(root, "P2", n))
        assert [(p, t) for p, t, *_ in got] == [(None, "quarter"), ("F4", "eighth"), ("E4", "eighth"),
                                                ("D4", "quarter"), ("C4", "quarter")]
        assert got == truth[n]
    f4 = next(el for el in _measure(root, "P2", "3").findall("note") if el.findtext("pitch/step") == "F")
    assert f4.find("notations/slur") is not None, "surviving notes keep their notations"
    assert f4.findtext("beam") == "begin"
    changed = {(c.part, c.measure) for c in log if c.applied and c.part == "P2"}
    assert changed == {("P2", "3"), ("P2", "23")}
    assert not [e for e in rhythm.check_root(root) if e.voices]


def test_hand_corrected_tree_only_pads_organ_m62(stopford_truth_tree, stopford_score):
    root = stopford_truth_tree
    before = {(p.get("id"), m.get("number")): ET.tostring(m) for p in root.findall("part") for m in p.findall("measure")}

    log = _run(root, stopford_score)

    after = {(p.get("id"), m.get("number")): ET.tostring(m) for p in root.findall("part") for m in p.findall("measure")}
    assert {k for k in before if before[k] != after[k]} <= {("P13", "62")}
    applied = [c for c in log if c.applied]
    assert [(c.part, c.measure) for c in applied] in ([], [("P13", "62")])
    if applied:
        assert "hidden rest" in applied[0].evidence
        lh = [n for n in _measure(root, "P13", "62").findall("note") if n.findtext("staff") == "2"]
        assert lh[-1].find("rest") is not None and lh[-1].get("print-object") == "no"
    assert rhythm.check_root(root) == []


def test_raw_stopford_is_deterministic_and_never_adds_voice_errors(stopford_raw, stopford_score):
    outputs = []
    for _ in range(2):
        root = rhythm.load(stopford_raw)
        bad_before = {(e.measure, v.part, v.voice) for e in rhythm.check_root(root) for v in e.voices}
        log = _run(root, stopford_score)
        bad_after = {(e.measure, v.part, v.voice) for e in rhythm.check_root(root) for v in e.voices}
        assert bad_after <= bad_before
        assert any(c.applied for c in log)
        outputs.append((ET.tostring(root), [tuple(c.__dict__.values()) for c in log]))
    assert outputs[0] == outputs[1]


def test_drops_stray_voice_when_pdf_fills_bar(stopford_truth_tree, stopford_raw_tree, stopford_score):
    root = stopford_truth_tree
    # Audiveris put a stray A4 half note (voice 1) over Women 1's bar rest (raw P11 m24)
    _measure(root, "P7", "24")[:] = [copy.deepcopy(el) for el in _measure(stopford_raw_tree, "P11", "24")]

    log = _run(root, stopford_score)

    assert [(p, t) for p, t, *_ in _events(_measure(root, "P7", "24"))] == [(None, None)]
    assert [(c.part, c.measure) for c in log if c.applied and c.part == "P7"] == [("P7", "24")]
    assert not [e for e in rhythm.check_root(root) if e.measure == "24" and e.voices]


def test_log_skips_rebuilds_that_read_the_same(stopford_raw, stopford_score):
    log = _run(rhythm.load(stopford_raw), stopford_score)
    assert all(c.before != c.after for c in log if c.applied)
