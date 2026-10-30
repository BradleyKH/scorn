import xml.etree.ElementTree as ET

import pytest

from music_scan import rhythm
from music_scan.reconcile import align
from music_scan.reconcile.align import ChangeLog
from music_scan.reconcile.passes import text
from music_scan.reconcile.pdf_text import Syllable


def _run(root, score):
    log = ChangeLog()
    text.run(root, score, align.align(score, root), log)
    return log


def _lyrics(root, part, first, last):
    """Lyrics of one part over a bar range, joined the way a hymnal reads: "Pas-chal Lamb"."""
    out, hyphen = "", False
    for m in root.find(f"part[@id='{part}']").findall("measure"):
        if not first <= int(m.get("number")) <= last:
            continue
        for note in m.findall("note"):
            for ly in note.findall("lyric"):
                if ly.get("number") != "1":
                    continue
                out += ("" if hyphen else " ") + ly.findtext("text")
                hyphen = ly.findtext("syllabic") in ("begin", "middle")
                if hyphen:
                    out += "-"
    return out.strip()


def _words(root, part):
    found = {}
    for m in root.find(f"part[@id='{part}']").findall("measure"):
        for w in m.iter("words"):
            found.setdefault(m.get("number"), []).append(w.text)
    return found


@pytest.fixture
def stripped(stopford_truth_tree):
    """The hand-corrected tree without any text direction, for the pass to restore."""
    for m in stopford_truth_tree.iter("measure"):
        for d in m.findall("direction"):
            if d.find("direction-type/words") is not None or d.find("direction-type/metronome") is not None:
                m.remove(d)
    return stopford_truth_tree


def test_stopford_lyrics(stopford_score, stripped):
    _run(stripped, stopford_score)
    assert not any(n.find("lyric") is not None and n.find("rest") is not None for n in stripped.iter("note"))
    # the unison line in m29-32 sits in one part
    holders = [p.get("id") for p in stripped.findall("part") if _lyrics(stripped, p.get("id"), 29, 32)]
    assert len(holders) == 1
    assert _lyrics(stripped, holders[0], 29, 32) == "is our Pas-chal Lamb to-day; we too sing for joy, and say"
    # six-part al-le-lu-ia! in the last two bars
    ends = [p.get("id") for p in stripped.findall("part") if _lyrics(stripped, p.get("id"), 75, 76) == "al-le-lu-ia!"]
    assert len(ends) == 6


def test_stopford_lyric_count_matches_pdf(stopford_score, stripped):
    _run(stripped, stopford_score)
    pdf_count = sum(len(tc.lyrics) for tc in stopford_score.text.pages)
    assert sum(1 for _ in stripped.iter("lyric")) == pdf_count


def test_stopford_credits(stopford_score, stripped):
    _run(stripped, stopford_score)
    assert stripped.findtext("work/work-title") == "Christ the Lord is risen again"
    assert stripped.findtext("identification/creator[@type='composer']") == "Philip W. J. Stopford"
    assert stripped.findtext("identification/creator[@type='lyricist']") == "Michael Weisse; tr. Catherine Winkworth"
    assert all(r.text and "Copyright" in r.text.title() for r in stripped.findall("identification/rights"))
    credits = {c.findtext("credit-type"): c.findtext("credit-words") for c in stripped.findall("credit")}
    assert credits["title"] == "Christ the Lord is risen again"
    assert credits["subtitle"].startswith("SATB divisi")
    assert credits["composer"] == "Philip W. J. Stopford"


def test_stopford_directions_and_tempo(stopford_score, stripped, stopford_truth_tree):
    _run(stripped, stopford_score)
    words = {p.get("id"): _words(stripped, p.get("id")) for p in stripped.findall("part")}
    p1 = words["P1"]
    assert p1["1"] == ["Joyfully"]
    assert p1["58"] == ["rit."] and p1["59"] == ["Tempo I"] and p1["75"] == ["ritardando"]
    # marks printed on every staff of a system are written once
    assert sum(len(v) for part in words.values() for k, v in part.items() if k in ("1", "58", "59", "75")) == 4
    m1 = stripped.find("part[@id='P1']/measure[@number='1']")
    metro = m1.find(".//metronome")
    assert metro.findtext("beat-unit") == "half" and metro.findtext("per-minute") == "64"
    assert m1.find(".//sound[@tempo]").get("tempo") == "128"
    rit = stripped.find("part[@id='P1']/measure[@number='58']//words")
    assert rit.get("font-style") == "italic"
    assert stripped.find("part[@id='P1']/measure[@number='59']//words").get("font-weight") == "bold"
    # no chord symbols or dynamics leak in as words
    assert not any(w.text in ("f", "mf", "p", "pp") for w in stripped.iter("words"))


def test_unsupported_words_removed_and_rerun_is_quiet(stopford_score, stopford_truth_tree):
    root = stopford_truth_tree
    m = root.find("part[@id='P2']/measure[@number='10']")
    d = ET.Element("direction", {"placement": "above"})
    ET.SubElement(ET.SubElement(d, "direction-type"), "words").text = "Hallo zzq"
    m.insert(0, d)
    log = _run(root, stopford_score)
    assert "Hallo zzq" not in {w.text for w in root.iter("words")}
    assert any(c.category == "directions" and c.before == "Hallo zzq" and c.after == "" for c in log)
    # the real directions the file already carries are untouched, and a second run changes nothing
    again = _run(root, stopford_score)
    assert [c for c in again if c.applied] == []
    assert rhythm.check_root(root) == []


def test_misread_words_corrected(stopford_score, stopford_truth_tree):
    root = stopford_truth_tree
    w = root.find("part[@id='P1']/measure[@number='58']//words")
    w.text = "rlt."
    _run(root, stopford_score)
    assert root.find("part[@id='P1']/measure[@number='58']//words").text == "rit."


def test_raw_tree_keeps_bars_valid(stopford_score, stopford_raw_tree):
    before = len(rhythm.check_root(stopford_raw_tree))
    _run(stopford_raw_tree, stopford_score)
    assert len(rhythm.check_root(stopford_raw_tree)) == before
    assert sum(1 for _ in stopford_raw_tree.iter("lyric")) >= 500


def _syl(text, x0, x1, hyphen=False):
    return Syllable(0, 0, 0, 0, 1, text, x0, x1, 0.0, hyphen_after=hyphen)


def test_assign_is_ordered_and_skips_unplaceable():
    sylls = [_syl("a", 10, 14), _syl("b", 50, 54), _syl("c", 90, 94)]
    assert text._assign(sylls, [10.0, 52.0, 70.0, 91.0]) == [0, 1, 3]
    # more syllables than notes: the far one is dropped, order kept
    assert text._assign(sylls, [10.0, 90.0]) == [0, None, 1]


def _chars(text, widths_overlap):
    """Glyph boxes of `text`, 4 pt wide, with the listed (index, overlap) joins pulled back by that much."""
    out, x = [], 0.0
    for i, c in enumerate(text):
        x -= dict(widths_overlap).get(i, 0.0)
        out.append((c, x, x + 4.0))
        x += 4.0
    return out


def test_glued_span_splits_at_overlapping_glyphs_over_covered_notes():
    s = _syl("won'tcall", 0, 36)
    chars = _chars(s.text, [(5, 0.7)])
    assert text._split_glued(s, [2.0, 20.0], chars) == ["won't", "call"]
    # a single covered note, or glyphs that never overlap, keep the span whole
    assert text._split_glued(s, [2.0], chars) == ["won'tcall"]
    assert text._split_glued(s, [2.0, 20.0], _chars(s.text, [])) == ["won'tcall"]


def test_glued_span_without_glyphs_splits_at_capitals_only_when_notes_cover_it():
    s = _syl("JoyToThe", 0, 60)
    assert text._split_glued(s, [0.0, 20.0, 40.0], []) == ["Joy", "To", "The"]
    assert text._split_glued(s, [0.0], []) == ["JoyToThe"]
    assert text._split_glued(_syl("Joy", 0, 60), [0.0, 20.0, 40.0], []) == ["Joy"]
    assert text._split_glued(_syl("JoyTo", 0, 60, hyphen=True), [0.0, 20.0], []) == ["JoyTo"]


def _voice_cells(onsets_by_voice):
    """A fake cell whose PDF notes sit at the given x per MusicXML voice."""
    from types import SimpleNamespace as NS

    pairs = [NS(pdf=NS(x=x, is_rest=False, grace=False), xml=NS(voice=v, is_rest=False, grace=False, notes=[ET.Element("note")]))
             for v, xs in onsets_by_voice.items() for x in xs]
    return [NS(pairs=pairs)]


def _row(xs):
    return [_syl("w", x - 2, x + 6) for x in xs]


def test_two_lyric_rows_follow_two_voices_when_their_rhythms_differ():
    cells = _voice_cells({"1": [10, 50, 90, 130], "2": [10, 70, 130]})
    assert text._rows_by_voice(cells, [_row([10, 50, 90, 130]), _row([10, 70, 130])]) == ["1", "2"]
    assert text._rows_by_voice(cells, [_row([10, 70, 130]), _row([10, 50, 90, 130])]) == ["2", "1"]


def test_two_lyric_rows_stay_verses_when_voices_share_a_rhythm_or_there_is_one_voice():
    same = _voice_cells({"1": [10, 50, 90], "2": [10, 50, 90]})
    assert text._rows_by_voice(same, [_row([10, 50, 90]), _row([10, 50, 90])]) is None
    one = _voice_cells({"1": [10, 50, 90]})
    assert text._rows_by_voice(one, [_row([10, 50, 90]), _row([10, 50, 90])]) is None


def test_metronome_element_carries_parentheses():
    from music_scan.reconcile.pdf_text import TempoMark

    mark = TempoMark(0, 0, 0, "Slowly", "quarter", 0, 64.0, 0.0, 0.0, parenthesized=True)
    assert text._metronome_el(mark).get("parentheses") == "yes"
    assert text._metronome_el(TempoMark(0, 0, 0, "", "half", 0, 60.0, 0.0, 0.0)).get("parentheses") is None
