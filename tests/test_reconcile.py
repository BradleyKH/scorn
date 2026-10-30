"""The full chain on raw Audiveris output: Stopford raw .mxl in, compared with the hand-corrected file."""

import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

from music_scan import compare, rhythm
from music_scan.reconcile import Report, align, reconcile
from music_scan.reconcile.report import failing_bars

# (instrument, staff, bar) whose notes still differ from the hand-corrected file after the chain.
RESIDUALS = {
    # The PDF prints F3; the hand-corrected file has A3.
    ("Horn", 1, "47"),
}


@pytest.fixture(scope="module")
def runs(stopford_pdf, stopford_raw, tmp_path_factory):
    out = []
    for name in ("a", "b"):
        path = tmp_path_factory.mktemp("reconcile") / f"{name}.musicxml"
        out.append((reconcile(stopford_pdf, stopford_raw, path), path))
    return out


def _notes(root: ET.Element) -> dict:
    names = {sp.get("id"): sp.findtext("part-name") for sp in root.iter("score-part")}
    res = {}
    for part in root.findall("part"):
        divisions, staves = 1, 1
        for m in part.findall("measure"):
            for a in m.findall("attributes"):
                divisions = int(a.findtext("divisions") or divisions)
                staves = int(a.findtext("staves") or staves)
            for s in range(1, staves + 1):
                evs = align.xml_events(m, s, divisions)
                res[(names[part.get("id")], s, m.get("number"))] = [
                    (e.onset, e.pitches, e.duration) for e in evs if not e.is_rest]
    return res


def test_part_names_exact(runs, stopford_truth_tree):
    def names(root):
        return [(sp.findtext("part-name"), sp.findtext("part-abbreviation")) for sp in root.iter("score-part")]

    assert names(rhythm.load(runs[0][1])) == names(stopford_truth_tree)


def test_notes_differ_only_at_pinned_bars(runs, stopford_truth_tree):
    got, want = _notes(rhythm.load(runs[0][1])), _notes(stopford_truth_tree)
    assert got.keys() == want.keys()
    assert {k for k in got if got[k] != want[k]} == RESIDUALS


def test_marking_metrics(runs, stopford_truth):
    s = compare.compare_markings(stopford_truth, runs[0][1])
    assert s["dynamics"].truth == s["dynamics"].found == s["dynamics"].matched == 105
    # ornaments carry the timpani tremolo strokes
    for cat in ("ornaments", "fermatas", "ties", "articulations"):
        assert s[cat].precision == 1.0 and s[cat].recall == 1.0, cat
    assert s["hairpins"].matched >= 31 and s["hairpins"].truth == 36
    assert s["slurs"].recall >= 0.96
    assert s["part names"].matched == 14


def test_rhythm_gate(runs):
    report, path = runs[0]
    voice_errors = [e for e in rhythm.check_root(rhythm.load(path)) if e.voices]
    assert len(voice_errors) <= 7
    assert len(report.failing_before) == 23
    assert report.failing_after == []
    assert report.failing_after == sorted(failing_bars(rhythm.load(path)), key=int)
    assert report.rollbacks == []


def test_deterministic(runs):
    (ra, pa), (rb, pb) = runs
    assert pa.read_bytes() == pb.read_bytes()
    assert ra.to_json() == rb.to_json()


def test_output_keeps_prolog(runs):
    head = runs[0][1].read_bytes()[:300].decode("utf-8")
    assert head.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE score-partwise PUBLIC')
    assert "\n<score-partwise" in head


def test_report(runs):
    report = runs[0][0]
    assert isinstance(report, Report) and report.applicable
    data = json.loads(report.to_json())
    assert data["applicable"] and len(data["changes"]) == len(report.changes)
    assert [p["name"] for p in data["parts"]][:2] == ["Trumpet I", "Trumpet II"]
    text = report.summary()
    assert "Fixed " in text and "lyric syllables" in text
    assert ("check these bars" in text) == bool(report.unchanged_bars())
    assert chr(0x2014) not in text and chr(0x2013) not in text
    for jargon in ("MusicXML", "voice", "ChangeLog"):
        assert jargon not in text
    # bars Audiveris left out are reported on the instrument's own part, not the scan part it came from
    missing = {(report.part_names[c.part], c.measure) for c in report.disagreements if c.category == "parts"}
    assert missing == {("Trumpet I", "48"), ("Trumpet II", "48"), ("Horn", "48")}


def test_not_applicable_passes_input_through(scores_dir, tmp_path):
    pdf = scores_dir / "duke street.pdf"
    if not pdf.exists():
        pytest.skip(f"missing image-only scan: {pdf}")
    src = tmp_path / "in.musicxml"
    src.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="4.0"><part-list/></score-partwise>\n')
    out = tmp_path / "out.musicxml"
    report = reconcile(pdf, src, out)
    assert not report.applicable and report.reason
    assert out.read_bytes() == src.read_bytes()
    assert "left exactly as it was" in report.summary()


def test_not_applicable_unzips_mxl_unchanged(scores_dir, stopford_raw, tmp_path):
    pdf = scores_dir / "duke street.pdf"
    if not pdf.exists():
        pytest.skip(f"missing image-only scan: {pdf}")
    out = tmp_path / "out.musicxml"
    assert not reconcile(pdf, stopford_raw, out).applicable
    with zipfile.ZipFile(stopford_raw) as z:
        inner = next(n for n in z.namelist() if not n.startswith("META-INF"))
        assert out.read_bytes() == z.read(inner)
