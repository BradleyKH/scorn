import xml.etree.ElementTree as ET

from scorn.reconcile.passes import pitch


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
