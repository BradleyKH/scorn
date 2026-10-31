from collections import Counter

from scorn.reconcile.glyphs import Charmap, Encoding, GlyphClass, classify, detect_font


def test_ghostscript_maestro_with_pp_stays_macroman_sonata():
    chars = Counter({"&": 8, "œ": 200, "b": 12, "#": 4, "π": 3, "˙": 20, "∑": 6, "?": 4})
    font = detect_font(chars)
    assert font is not None
    assert (font.encoding, font.charmap) == (Encoding.SONATA, Charmap.MACROMAN)
    assert classify(font, "b") is GlyphClass.FLAT
    assert classify(font, "&") is GlyphClass.CLEF_G


def test_symbol_shifted_font_uses_symbol_charmap():
    # Bytes one below the real codes, exposed as Symbol Greek letters.
    chars = Counter({"%": 12, "œ": 288, "ϖ": 12, "∀": 15, "α": 26, "3": 2})
    font = detect_font(chars)
    assert font is not None
    assert font.charmap is Charmap.SYMBOL
    assert classify(font, "%") is GlyphClass.CLEF_G


def test_8va_clef_and_ottava_mark_classify():
    assert classify(Encoding.SONATA, "†") is GlyphClass.CLEF_G_8VA
    assert GlyphClass.CLEF_G_8VA.clef == ("G", 2, 1)
    assert classify(Encoding.SONATA, "Ã") is GlyphClass.OTTAVA_8VA


def test_symbol_shifted_double_sharp_and_courtesy_accidentals():
    font = detect_font(Counter({"%": 12, "œ": 288, "ϖ": 12, "∀": 15, "α": 26, "3": 2}))
    assert classify(font, "‹") is GlyphClass.DOUBLE_SHARP
    assert classify(font, "≅") is GlyphClass.FLAT
    assert classify(font, "Μ") is GlyphClass.NATURAL
