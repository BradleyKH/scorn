import dataclasses
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

import pytest

from scorn import compare, mei, rhythm
from scorn.reconcile import align, pdf_score, pdf_text
from scorn.reconcile.passes import marks


def strip_marks(root: ET.Element) -> None:
    """Remove every category the marks pass owns."""
    for m in root.iter("measure"):
        for d in list(m.findall("direction")):
            for dt in list(d.findall("direction-type")):
                if any(dt.find(t) is not None for t in ("dynamics", "wedge", "pedal")):
                    d.remove(dt)
            if d.find("direction-type") is None:
                m.remove(d)
        for n in m.findall("note"):
            for t in n.findall("tie"):
                n.remove(t)
            for nt in list(n.findall("notations")):
                for c in list(nt):
                    if c.tag in ("tied", "slur", "ornaments", "articulations", "fermata", "arpeggiate"):
                        nt.remove(c)
                if len(nt) == 0:
                    n.remove(nt)


def run_pass(tree, score) -> align.ChangeLog:
    log = align.ChangeLog()
    marks.run(tree, score, align.align(score, tree), log)
    return log


def counts(root: ET.Element) -> Counter:
    c: Counter = Counter()
    for part in root.findall("part"):
        for category, _, _, key in compare.part_marks(part):
            c[(category, key.split("@")[0])] += 1
    return c


def metrics(tree, tmp_path, truth_path):
    out = tmp_path / "marks.musicxml"
    ET.ElementTree(tree).write(out, encoding="utf-8", xml_declaration=True)
    return compare.compare_markings(truth_path, out), out


@pytest.fixture(scope="module")
def restored(stopford_score, stopford_truth, tmp_path_factory):
    tree = rhythm.load(stopford_truth)
    strip_marks(tree)
    log = run_pass(tree, stopford_score)
    scores, out = metrics(tree, tmp_path_factory.mktemp("marks"), stopford_truth)
    return tree, log, scores, out


def test_restores_stripped_marks(restored):
    _, _, s, _ = restored
    # The hand-corrected file keeps Audiveris's hairpin onsets in m38, m40, m48 and m66, where the PDF
    # draws the hairpin from another note, so those few stay off by a beat.
    assert s["dynamics"].found == s["dynamics"].truth == s["dynamics"].matched == 105
    assert s["hairpins"].found == s["hairpins"].truth == 36
    assert s["hairpins"].matched >= 31
    for cat in ("ornaments", "fermatas", "articulations", "ties", "slurs"):
        assert s[cat].precision == 1.0 and s[cat].recall == 1.0, cat


def test_tremolos_and_fermatas_restored(restored, stopford_truth_tree):
    tree = restored[0]
    got, want = counts(tree), counts(stopford_truth_tree)
    assert got[("ornaments", "tremolo")] == want[("ornaments", "tremolo")] == 19
    assert got[("fermatas", "fermata")] == want[("fermatas", "fermata")] == 6
    assert sum(v for (c, _), v in got.items() if c == "articulations") == 0


def test_verovio_loads_without_warnings(restored):
    tk = mei.toolkit()
    assert tk.loadFile(str(restored[3]))
    assert tk.getLog().strip() == ""


def test_bars_stay_valid(restored, stopford_truth):
    assert len(rhythm.check_root(restored[0])) == len(rhythm.check(stopford_truth))


def test_slur_numbers_never_collide_inside_a_part(restored):
    for part in restored[0].findall("part"):
        open_: set[str] = set()
        for slur in part.iter("slur"):
            number = slur.get("number")
            if slur.get("type") == "start":
                assert number not in open_, (part.get("id"), number)
                open_.add(number)
            else:
                assert number in open_
                open_.discard(number)
        assert not open_


def test_every_hairpin_is_terminated(restored):
    for part in restored[0].findall("part"):
        depth: dict[str, int] = defaultdict(int)
        for w in part.iter("wedge"):
            n = w.get("number")
            depth[n] += 0 if w.get("type") == "stop" else 1
            if w.get("type") == "stop":
                assert depth[n] == 1, (part.get("id"), n)
                depth[n] = 0
        assert not any(depth.values())


def test_organ_marks_stay_on_staff_one(restored):
    organ = next(p for p in restored[0].findall("part") if p.get("id") == "P13")
    staffs = {d.findtext("staff") for d in organ.iter("direction")}
    assert staffs == {"1"}


def test_choir_dynamics_are_placed_above(restored):
    tree = restored[0]
    choir = {"P7", "P8", "P9", "P10", "P11", "P12"}
    placements = Counter(d.get("placement") for p in tree.findall("part") if p.get("id") in choir
                         for d in p.iter("direction") if d.find(".//dynamics") is not None)
    assert placements["above"] >= 10 and placements["below"] <= 1


def test_deterministic(stopford_score, stopford_truth):
    outs = []
    for _ in range(2):
        tree = rhythm.load(stopford_truth)
        strip_marks(tree)
        run_pass(tree, stopford_score)
        outs.append(ET.tostring(tree))
    assert outs[0] == outs[1]


def test_raw_tree_loses_misread_articulations(stopford_score, stopford_raw_tree):
    # Lyric syllables read as staccatos, accents and the like; the PDF has none.
    before = counts(stopford_raw_tree)
    assert sum(v for (c, _), v in before.items() if c == "articulations") > 100
    run_pass(stopford_raw_tree, stopford_score)
    after = counts(stopford_raw_tree)
    assert sum(v for (c, _), v in after.items() if c == "articulations") == 0
    # one fermata sits on a bar the raw output left empty, which cannot take it
    assert after[("fermatas", "fermata")] >= 5
    assert after[("ornaments", "tremolo")] == 19


def test_partial_reader_leaves_category_alone(stopford_score, stopford_truth_tree):
    blind = dataclasses.replace(stopford_score, marks=tuple(m for m in stopford_score.marks if m.kind != "slur"))
    before = counts(stopford_truth_tree)
    log = run_pass(stopford_truth_tree, blind)
    after = counts(stopford_truth_tree)
    assert after[("slurs", "start")] == before[("slurs", "start")]
    assert after[("ties", "start")] > 0
    assert any(c.category == "slurs" and not c.applied and c.part == "*" for c in log)


def test_text_dynamics_count_as_dynamics(stopford_score, stopford_truth, tmp_path):
    glyph = [m for m in stopford_score.marks if m.kind == "dynamic"]
    texts = tuple(pdf_text.Direction(m.page, 0, m.staff, m.name, m.placement or "inside", False, False, m.x, m.y,
                                     dynamic=m.name) for m in glyph)
    pages = list(stopford_score.text.pages)
    by_page: dict[int, list] = defaultdict(list)
    for d in texts:
        by_page[d.page].append(d)
    pages = tuple(dataclasses.replace(tc, directions=tc.directions + tuple(by_page.get(tc.page, ()))) for tc in pages)
    buckets = dict(stopford_score._buckets)
    buckets["marks"] = {k: [m for m in v if m.kind != "dynamic"] for k, v in buckets["marks"].items()}
    directions: dict = defaultdict(list, {k: list(v) for k, v in buckets["directions"].items()})
    for d in texts:
        pm = stopford_score.measure_at(d.page, d.staff, d.x)
        directions[pm.key(d.staff)].append(d)
    buckets["directions"] = dict(directions)
    swapped = dataclasses.replace(
        stopford_score, text=dataclasses.replace(stopford_score.text, pages=pages),
        marks=tuple(m for m in stopford_score.marks if m.kind != "dynamic"), _buckets=buckets)
    tree = rhythm.load(stopford_truth)
    strip_marks(tree)
    run_pass(tree, swapped)
    scores, _ = metrics(tree, tmp_path, stopford_truth)
    assert scores["dynamics"].found == 105
    assert scores["dynamics"].matched >= 104
