import xml.etree.ElementTree as ET

from scorn.reconcile.passes import text
from scorn.reconcile.pdf_text import Syllable


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
    from scorn.reconcile.pdf_text import TempoMark

    mark = TempoMark(0, 0, 0, "Slowly", "quarter", 0, 64.0, 0.0, 0.0, parenthesized=True)
    assert text._metronome_el(mark).get("parentheses") == "yes"
    assert text._metronome_el(TempoMark(0, 0, 0, "", "half", 0, 60.0, 0.0, 0.0)).get("parentheses") is None
