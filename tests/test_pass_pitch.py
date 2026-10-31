"""Pitch pass tests: perturb the hand-corrected Stopford tree, align, run the pass, expect the PDF's pitches back."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import defaultdict

import pytest

from scorn import rhythm
from scorn.reconcile import align
from scorn.reconcile.passes import pitch

STEPS = "CDEFGAB"


def _note_key(n: ET.Element) -> tuple:
    p = n.find("pitch")
    if p is None:
        return ("r",)
    return (p.findtext("step"), p.findtext("alter") or "0", p.findtext("octave"))


def _events(tree: ET.Element) -> dict[tuple[str, str, int], list[tuple]]:
    """(part, measure number, occurrence) -> per-event tuples of chord pitches, in document order."""
    out: dict[tuple[str, str, int], list[tuple]] = {}
    for part in tree.findall("part"):
        seen: dict[str, int] = defaultdict(int)
        for m in part.findall("measure"):
            n = m.get("number")
            ev: list[list] = []
            for note in m.findall("note"):
                if note.find("chord") is not None and ev:
                    ev[-1].append(_note_key(note))
                else:
                    ev.append([_note_key(note)])
            out[(part.get("id"), n, seen[n])] = [tuple(sorted(e)) for e in ev]
            seen[n] += 1
    return out


def _clefs(tree: ET.Element) -> list[tuple]:
    out = []
    for part in tree.findall("part"):
        for m in part.findall("measure"):
            for c in m.iter("clef"):
                out.append((part.get("id"), m.get("number"), c.get("number"), c.findtext("sign"), c.findtext("line"),
                            c.findtext("clef-octave-change")))
    return out


def _notes_of(tree: ET.Element, part: str, number: str) -> list[ET.Element]:
    p = next(p for p in tree.findall("part") if p.get("id") == part)
    m = next(m for m in p.findall("measure") if m.get("number") == number)
    return m.findall("note")


def _shift(note: ET.Element, steps: int, alter: str | None = None) -> None:
    pitch_el = note.find("pitch")
    step = pitch_el.find("step")
    diatonic = 7 * int(pitch_el.findtext("octave")) + STEPS.index(step.text) + steps
    step.text = STEPS[diatonic % 7]
    pitch_el.find("octave").text = str(diatonic // 7)
    al = pitch_el.find("alter")
    if al is not None:
        pitch_el.remove(al)
    if alter:
        el = ET.Element("alter")
        el.text = alter
        pitch_el.insert(1, el)


def _chord_member(note: ET.Element, steps: int) -> ET.Element:
    new = copy.deepcopy(note)
    for tag in ("tie", "notations", "lyric", "accidental"):
        for el in new.findall(tag):
            new.remove(el)
    if new.find("chord") is None:
        new.insert(0, ET.Element("chord"))
    _shift(new, steps)
    return new


def _perturb(tree: ET.Element) -> set[tuple[str, str]]:
    """Wrong steps and accidentals in the brass and choir, tremolo-style extra chord notes in the
    timpani, a lost A5 and C2, and the Women 1 m34 clef change removed. Returns bars to skip."""
    count = 0
    for part in tree.findall("part"):
        pid = part.get("id")
        for m in part.findall("measure"):
            notes = m.findall("note")
            if pid in ("P1", "P3", "P4", "P5", "P7", "P8", "P9", "P10", "P11", "P12"):
                for n in notes:
                    if n.find("pitch") is None or n.find("chord") is not None:
                        continue
                    count += 1
                    if count % 5 == 0:
                        _shift(n, 1 if count % 2 else -1)
                    elif count % 7 == 0:
                        _shift(n, 0, "1")
            if pid == "P6" and int(m.get("number")) <= 28:
                for n in notes:
                    if n.find("pitch") is not None and n.find("chord") is None:
                        m.insert(list(m).index(n) + 1, _chord_member(n, 2))
    # lost chord members
    for part, number, pc in (("P7", "76", ("A", "5")), ("P14", "75", ("C", "2"))):
        for n in _notes_of(tree, part, number):
            if n.find("pitch") is not None and (n.findtext("pitch/step"), n.findtext("pitch/octave")) == pc:
                next(m for p in tree.findall("part") if p.get("id") == part
                     for m in p.findall("measure") if m.get("number") == number).remove(n)
    # Women 1 m34 clef back to the treble-8 still in force
    p7 = next(p for p in tree.findall("part") if p.get("id") == "P7")
    m34 = next(m for m in p7.findall("measure") if m.get("number") == "34")
    for a in m34.findall("attributes"):
        if a.find("clef") is not None:
            m34.remove(a)
    return {("P2", "3"), ("P2", "23")}


def test_pitch_pass_restores_hand_corrected_file(stopford_score, stopford_truth_tree):
    truth = stopford_truth_tree
    tree = copy.deepcopy(truth)
    _perturb(tree)
    assert _events(tree) != _events(truth)
    assert _clefs(tree) != _clefs(truth)

    log = align.ChangeLog()
    pitch.run(tree, stopford_score, align.align(stopford_score, tree), log)

    want, got = _events(truth), _events(tree)
    diffs = [k for k in want if want[k] != got[k]]
    # Horn m47: the PDF says F3 where the hand-corrected file has A3
    assert diffs == [("P3", "47", 0)], diffs
    assert ("F", "0", "3") in [k for e in got[("P3", "47", 0)] for k in e]
    assert _clefs(tree) == _clefs(truth)
    assert any(c.category == "clef" and c.part == "P7" and c.measure == "34" for c in log)


def test_pitch_pass_is_a_no_op_apart_from_the_horn(stopford_score, stopford_truth_tree):
    tree = stopford_truth_tree
    log = align.ChangeLog()
    pitch.run(tree, stopford_score, align.align(stopford_score, tree), log)
    applied = [(c.category, c.part, c.measure, c.before, c.after) for c in log if c.applied]
    assert applied == [("pitch", "P3", "47", "A3", "F3")]


def test_pitch_pass_keeps_bars_valid(stopford_score, stopford_truth_tree):
    tree = stopford_truth_tree
    _perturb(tree)
    before = {(e.measure, tuple((v.part, v.voice, v.off_by) for v in e.voices)) for e in rhythm.check_root(tree)}
    pitch.run(tree, stopford_score, align.align(stopford_score, tree), align.ChangeLog())
    after = {(e.measure, tuple((v.part, v.voice, v.off_by) for v in e.voices)) for e in rhythm.check_root(tree)}
    assert after <= before


def test_octave_only_difference_is_logged_not_applied(stopford_score, stopford_truth_tree):
    tree = stopford_truth_tree
    n = next(n for n in _notes_of(tree, "P1", "2") if n.find("pitch") is not None)
    octave = n.find("pitch/octave")
    octave.text = str(int(octave.text) + 1)
    log = align.ChangeLog()
    pitch.run(tree, stopford_score, align.align(stopford_score, tree), log)
    assert octave.text == "6"
    assert any(not c.applied and c.part == "P1" and c.measure == "2" for c in log)
    assert not any(c.applied and c.part == "P1" and c.measure == "2" for c in log)


def _times(tree):
    out = {}
    for p in tree.findall("part"):
        for m in p.findall("measure"):
            for t in m.iter("time"):
                out[(p.get("id"), m.get("number"))] = (t.findtext("beats"), t.findtext("beat-type"))
    return out


def test_time_signature_wrong_and_spurious_are_corrected(stopford_score, stopford_truth_tree):
    tree = stopford_truth_tree
    truth_times = _times(tree)
    part = next(p for p in tree.findall("part") if p.get("id") == "P2")
    ms = part.findall("measure")
    first_time = next(t for t in ms[0].iter("time"))
    first_time.find("beats").text = "3"
    first_time.find("beat-type").text = "8"
    attrs = ms[5].find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        ms[5].insert(0, attrs)
    spurious = ET.SubElement(attrs, "time")
    ET.SubElement(spurious, "beats").text = "6"
    ET.SubElement(spurious, "beat-type").text = "8"
    assert _times(tree) != truth_times

    log = align.ChangeLog()
    pitch.run(tree, stopford_score, align.align(stopford_score, tree), log)
    assert _times(tree) == truth_times
    assert sum(1 for c in log if c.category == "time" and c.part == "P2") == 2


def test_cross_octave_accidental_is_left_to_the_scan():
    from types import SimpleNamespace

    from scorn.reconcile.pdf_notes import Note

    def note(octave, alter, acc, x):
        return Note("B", octave, alter, acc, 0.0, x, 0.0, "black")

    natural_above = note(2, 0, 0, 10.0)
    bare_below = note(1, -1, None, 50.0)
    cell = SimpleNamespace(content=SimpleNamespace(events=[SimpleNamespace(notes=(natural_above,)),
                                                           SimpleNamespace(notes=(bare_below,))]))
    have = ET.fromstring("<pitch><step>B</step><octave>1</octave></pitch>")
    assert pitch._cross_octave(cell, bare_below, have)
    flat = ET.fromstring("<pitch><step>B</step><alter>-1</alter><octave>1</octave></pitch>")
    assert not pitch._cross_octave(cell, bare_below, flat)
    assert not pitch._cross_octave(cell, note(1, -1, -1, 50.0), have)
