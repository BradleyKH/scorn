import pymupdf

from scorn.reconcile import geometry, pdf_text
from scorn.reconcile.model import TextSpan


def _span(text, size, x0, x1, y, flags=0):
    return TextSpan(0, text, "Serif", size, x0, y, (x0, y - size, x1, y), flags)


def test_title_block_roles_by_position_and_prefix():
    spans = [
        _span("Hymn of Joy", 22, 230, 380, 40),
        _span("for choir", 11, 280, 330, 60),
        _span("Words: A. Poet", 10, 40, 110, 80),
        _span("Arr. B. Maker", 11, 480, 570, 80),
        _span("Music by C. Writer", 11, 470, 570, 95),
        _span("© 2020 Somebody", 8, 250, 360, 700),
    ]
    t = pdf_text._title_block(spans, 612, first_top=150)
    assert (t.title, t.subtitle) == ("Hymn of Joy", "for choir")
    assert (t.lyricist, t.arranger, t.composer) == ("A. Poet", "B. Maker", "C. Writer")
    assert t.copyright == ("© 2020 Somebody",)


def test_metronome_variants():
    m = pdf_text._METRONOME.match("Allegro ( ‹quarter›. = 92 )")
    assert m["g"] == "quarter" and m["bpm"] == "92" and m["pre"].strip() == "Allegro"
    assert pdf_text._METRONOME.match("q = 120")["bpm"] == "120"
    assert pdf_text._METRONOME.match("Moderato (♩ = 60-66)")["bpm2"] == "66"


def test_chord_symbols_are_not_lyrics():
    assert pdf_text._chordish("B M7 /C F G m7".split())
    assert not pdf_text._chordish("I once was lost".split())


def _synthetic(staff_tops, words, x0=50, height=792, spacing=5.0, font="helv"):
    """One page of bare staves and text, enough for the geometry and text readers."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=height)
    for top in staff_tops:
        for i in range(5):
            page.draw_line((x0, top + i * spacing), (550, top + i * spacing), width=0.6)
        for x in (x0, 300, 550):
            page.draw_line((x, top), (x, top + 4 * spacing), width=0.8)
    for text, x, y, *rest in words:
        page.insert_text((x, y), text, fontname=rest[0] if rest else font, fontsize=9)
    content = pdf_text.read_text(page, geometry.read_geometry(page))
    doc.close()
    return content


def test_fourth_verse_row_beyond_the_old_reach_is_read():
    words = [(w, 80 + 40 * i, y) for y in (150, 165, 180, 195) for i, w in enumerate(("la", "la", "la"))]
    content = _synthetic([100, 400], words)
    assert sorted({s.row for s in content.lyrics}) == [0, 1, 2, 3]
    assert len(content.lyrics) == 12


def test_verse_row_squeezed_against_next_staff_stacks_under_accepted_rows():
    # the last row sits 12 pt above the next staff, closer to it than to its own
    words = [(w, 80 + 40 * i, y) for y in (135, 160, 178) for i, w in enumerate(("la", "la", "la"))]
    content = _synthetic([100, 190], words)
    assert sorted({s.row for s in content.lyrics if s.staff == 0}) == [0, 1, 2]


def test_lone_squeezed_row_of_plain_words_is_lyric():
    content = _synthetic([100, 190], [(w, 80 + 40 * i, 178) for i, w in enumerate(("do", "re", "mi"))])
    assert [s.text for s in content.lyrics] == ["do", "re", "mi"]


def test_lyrics_under_the_last_staff_survive_the_footer_band():
    words = [(w, 80 + 40 * i, 738) for i, w in enumerate(("do", "re", "mi"))] + [("Publisher 2020", 250, 775)]
    content = _synthetic([690], words)
    assert [s.text for s in content.lyrics] == ["do", "re", "mi"]
    assert [s.text for s in content.margin] == ["Publisher 2020"]


def test_text_far_left_of_a_system_is_not_a_staff_label():
    words = [("Flute", 10, 120), ("Queen Anne", 20, 120), ("(sop.)", 10, 135), ("7", 10, 90, "hebo")]
    content = _synthetic([100], words, x0=300)
    assert [lb.text for lb in content.labels] == []
    near = _synthetic([100], [("Flute", 265, 120)], x0=300)
    assert [lb.text for lb in near.labels] == ["Flute"]


def test_cue_text_and_bar_numbers_beside_a_system_are_not_labels():
    content = _synthetic([100], [("(sop.)", 15, 120), ("12", 20, 97, "heit")], x0=50)
    assert [lb.text for lb in content.labels] == []
    assert [d.text for d in content.directions] == ["(sop.)"]
    assert [n.number for n in content.measure_numbers] == [12]


def test_tempo_keeps_style_and_parentheses():
    content = _synthetic([300], [("Slowly (q = 64)", 60, 280, "hebo")])
    (tempo,) = content.tempos
    assert (tempo.text, tempo.unit, tempo.bpm) == ("Slowly", "quarter", 64.0)
    assert tempo.bold and tempo.parenthesized and not tempo.italic
    plain = _synthetic([300], [("Slowly q = 64", 60, 280, "heit")]).tempos[0]
    assert plain.italic and not plain.parenthesized and not plain.bold
