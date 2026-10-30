"""Music font detection and glyph classification for born-digital PDFs.

Font names are unreliable (Finale via Ghostscript embeds Maestro as `TT14Ct00`),
so a font is identified by the characters it draws. Legacy Sonata-compatible
fonts (Maestro, Petrucci, Engraver, Jazz, Opus) share one byte layout, but PDFs
expose those bytes as Unicode in several ways: MacRoman glyph names (`œ`),
WinAnsi (`Ï` for the same byte), private-use U+F0xx, and Symbol-font Greek for
glyphs whose names are letters. Normalizing back to the MacRoman character lets
one table serve all of them.
"""

from __future__ import annotations

import enum
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import pymupdf


class Encoding(enum.StrEnum):
    SONATA = "sonata"
    OPUS = "opus"
    SMUFL = "smufl"


class Charmap(enum.StrEnum):
    """How a legacy font's bytes surface as Unicode in the PDF text layer."""

    MACROMAN = "macroman"
    WINANSI = "winansi"
    # Named glyphs (œ, ˙) as MacRoman; everything else as Adobe Symbol
    # encoding (Greek for letters) one byte below the Sonata code.
    SYMBOL = "symbol"
    # SMuFL fonts carry real codepoints.
    NATIVE = "native"


@dataclass(frozen=True)
class FontEncoding:
    encoding: Encoding
    charmap: Charmap


class GlyphClass(enum.Enum):
    """What a music-font glyph means. `category` groups classes; `params` carry
    the details (clef sign, line, octave shift; flag count and direction; etc.)."""

    def __init__(self, category: str, *params: object) -> None:
        self.category = category
        self.params = params

    # Clefs: sign, staff line counted from the bottom (None: read from position), octave shift.
    CLEF_G = ("clef", "G", 2, 0)
    CLEF_G_8VB = ("clef", "G", 2, -1)
    CLEF_G_8VA = ("clef", "G", 2, 1)
    CLEF_G_15MB = ("clef", "G", 2, -2)
    CLEF_G_15MA = ("clef", "G", 2, 2)
    CLEF_F = ("clef", "F", 4, 0)
    CLEF_F_8VB = ("clef", "F", 4, -1)
    CLEF_F_8VA = ("clef", "F", 4, 1)
    CLEF_F_15MB = ("clef", "F", 4, -2)
    CLEF_C = ("clef", "C", None, 0)
    CLEF_C_8VB = ("clef", "C", None, -1)
    CLEF_PERCUSSION = ("clef", "percussion", None, 0)
    CLEF_TAB = ("clef", "TAB", None, 0)

    NOTEHEAD_DOUBLE_WHOLE = ("notehead", "breve")
    NOTEHEAD_WHOLE = ("notehead", "whole")
    NOTEHEAD_HALF = ("notehead", "half")
    NOTEHEAD_BLACK = ("notehead", "black")
    NOTEHEAD_X = ("notehead", "x")
    NOTEHEAD_CIRCLE_X = ("notehead", "circle-x")
    NOTEHEAD_SLASH = ("notehead", "slash")
    NOTEHEAD_SLASH_WHITE = ("notehead", "slash-white")
    NOTEHEAD_DIAMOND_WHITE = ("notehead", "diamond-white")
    NOTEHEAD_DIAMOND_BLACK = ("notehead", "diamond-black")

    # Rests: MusicXML note type; multi-bar is the H-bar.
    REST_DOUBLE_WHOLE = ("rest", "breve")
    REST_WHOLE = ("rest", "whole")
    REST_HALF = ("rest", "half")
    REST_QUARTER = ("rest", "quarter")
    REST_8TH = ("rest", "eighth")
    REST_16TH = ("rest", "16th")
    REST_32ND = ("rest", "32nd")
    REST_64TH = ("rest", "64th")
    REST_128TH = ("rest", "128th")
    REST_MULTI = ("rest", "multi")

    # Accidentals: alter in semitones.
    FLAT = ("accidental", -1)
    SHARP = ("accidental", 1)
    NATURAL = ("accidental", 0)
    DOUBLE_SHARP = ("accidental", 2)
    DOUBLE_FLAT = ("accidental", -2)

    DOT = ("dot",)
    # Sonata draws augmentation dots and staccatos with one code; position decides.
    DOT_OR_STACCATO = ("dot-or-staccato",)

    # Flags: beam count, stem direction.
    FLAG_8TH_UP = ("flag", 1, "up")
    FLAG_8TH_DOWN = ("flag", 1, "down")
    FLAG_16TH_UP = ("flag", 2, "up")
    FLAG_16TH_DOWN = ("flag", 2, "down")
    FLAG_32ND_UP = ("flag", 3, "up")
    FLAG_32ND_DOWN = ("flag", 3, "down")
    FLAG_64TH_UP = ("flag", 4, "up")
    FLAG_64TH_DOWN = ("flag", 4, "down")

    # Dynamics: MusicXML element name. Single letters (m, r, s, z, n) occur when
    # a font builds a marking from parts; readers join adjacent ones.
    DYN_PPP = ("dynamic", "ppp")
    DYN_PP = ("dynamic", "pp")
    DYN_P = ("dynamic", "p")
    DYN_MP = ("dynamic", "mp")
    DYN_MF = ("dynamic", "mf")
    DYN_F = ("dynamic", "f")
    DYN_FF = ("dynamic", "ff")
    DYN_FFF = ("dynamic", "fff")
    DYN_SF = ("dynamic", "sf")
    DYN_SFZ = ("dynamic", "sfz")
    DYN_SFP = ("dynamic", "sfp")
    DYN_FZ = ("dynamic", "fz")
    DYN_FP = ("dynamic", "fp")
    DYN_RFZ = ("dynamic", "rfz")
    DYN_RF = ("dynamic", "rf")
    DYN_M = ("dynamic", "m")
    DYN_R = ("dynamic", "r")
    DYN_S = ("dynamic", "s")
    DYN_Z = ("dynamic", "z")
    DYN_N = ("dynamic", "n")

    STACCATO = ("articulation", "staccato")
    STACCATISSIMO = ("articulation", "staccatissimo")
    ACCENT = ("articulation", "accent")
    MARCATO = ("articulation", "strong-accent")
    TENUTO = ("articulation", "tenuto")
    PORTATO = ("articulation", "detached-legato")

    TRILL = ("ornament", "trill-mark")
    MORDENT = ("ornament", "mordent")
    INVERTED_MORDENT = ("ornament", "inverted-mordent")
    TURN = ("ornament", "turn")
    TREMOLO_1 = ("tremolo", 1)
    TREMOLO_2 = ("tremolo", 2)
    TREMOLO_3 = ("tremolo", 3)

    FERMATA_ABOVE = ("fermata", "upright")
    FERMATA_BELOW = ("fermata", "inverted")
    BREATH_MARK = ("breath-mark",)
    CAESURA = ("caesura",)
    PEDAL_DOWN = ("pedal", "start")
    PEDAL_UP = ("pedal", "stop")
    ARPEGGIO = ("arpeggio",)
    # Ottava mark: octave shift applied to the notes it spans.
    OTTAVA_8VA = ("ottava", 1)

    # Digits serve time signatures, tuplets and multi-rest counts alike.
    DIGIT_0 = ("digit", 0)
    DIGIT_1 = ("digit", 1)
    DIGIT_2 = ("digit", 2)
    DIGIT_3 = ("digit", 3)
    DIGIT_4 = ("digit", 4)
    DIGIT_5 = ("digit", 5)
    DIGIT_6 = ("digit", 6)
    DIGIT_7 = ("digit", 7)
    DIGIT_8 = ("digit", 8)
    DIGIT_9 = ("digit", 9)
    TIME_COMMON = ("time", "common")
    TIME_CUT = ("time", "cut")

    REPEAT_DOTS = ("repeat-dots",)
    SEGNO = ("segno",)
    CODA = ("coda",)
    # Glyphs that carry text-like meaning (parentheses, tuplet colons, etc.).
    TEXT_LIKE = ("text",)

    @property
    def clef(self) -> tuple[str, int | None, int]:
        """(sign, line, octave shift) for a clef class."""
        if self.category != "clef":
            raise ValueError(f"{self.name} is not a clef")
        return self.params  # type: ignore[return-value]


_G = GlyphClass
_DIGITS = [_G.DIGIT_0, _G.DIGIT_1, _G.DIGIT_2, _G.DIGIT_3, _G.DIGIT_4,
           _G.DIGIT_5, _G.DIGIT_6, _G.DIGIT_7, _G.DIGIT_8, _G.DIGIT_9]

# Sonata keyboard layout, keyed by the MacRoman character of each byte.
SONATA: dict[str, GlyphClass] = {
    "&": _G.CLEF_G,
    "V": _G.CLEF_G_8VB,
    "?": _G.CLEF_F,
    "t": _G.CLEF_F_8VB,
    "B": _G.CLEF_C,
    "†": _G.CLEF_G_8VA,
    "Ã": _G.OTTAVA_8VA,
    "/": _G.CLEF_PERCUSSION,
    "W": _G.NOTEHEAD_DOUBLE_WHOLE,
    "w": _G.NOTEHEAD_WHOLE,
    "˙": _G.NOTEHEAD_HALF,
    "œ": _G.NOTEHEAD_BLACK,
    "¿": _G.NOTEHEAD_X,
    "∑": _G.REST_WHOLE,
    "Ó": _G.REST_HALF,
    "Œ": _G.REST_QUARTER,
    "‰": _G.REST_8TH,
    "≈": _G.REST_16TH,
    "®": _G.REST_32ND,
    "b": _G.FLAT,
    "#": _G.SHARP,
    "n": _G.NATURAL,
    "Ü": _G.DOUBLE_SHARP,
    # the MacRoman form of the same byte, as Symbol-charmap Maestro PDFs decode it
    "‹": _G.DOUBLE_SHARP,
    "º": _G.DOUBLE_FLAT,
    # Maestro's parenthesized (courtesy) flat and natural
    "A": _G.FLAT,
    "N": _G.NATURAL,
    ".": _G.DOT_OR_STACCATO,
    "j": _G.FLAG_8TH_UP,
    "J": _G.FLAG_8TH_DOWN,
    "r": _G.FLAG_16TH_UP,
    "R": _G.FLAG_16TH_DOWN,
    "∏": _G.DYN_PPP,
    "π": _G.DYN_PP,
    "p": _G.DYN_P,
    "P": _G.DYN_MP,
    "F": _G.DYN_MF,
    "f": _G.DYN_F,
    "ƒ": _G.DYN_FF,
    "Ï": _G.DYN_FFF,
    "S": _G.DYN_SF,
    "ß": _G.DYN_SFZ,
    "Z": _G.DYN_FZ,
    "z": _G.DYN_Z,
    "s": _G.DYN_S,
    ">": _G.ACCENT,
    "^": _G.MARCATO,
    "v": _G.MARCATO,
    "-": _G.TENUTO,
    "U": _G.FERMATA_ABOVE,
    "u": _G.FERMATA_BELOW,
    "m": _G.MORDENT,
    "M": _G.INVERTED_MORDENT,
    "T": _G.TURN,
    "æ": _G.TREMOLO_3,
    ",": _G.BREATH_MARK,
    "c": _G.TIME_COMMON,
    "C": _G.TIME_CUT,
    "Ÿ": _G.TRILL,
    "%": _G.SEGNO,
    "Ø": _G.CODA,
    "*": _G.PEDAL_UP,
    # Stemmed notes for metronome marks.
    "q": _G.TEXT_LIKE,
    "h": _G.TEXT_LIKE,
    "(": _G.TEXT_LIKE,
    ")": _G.TEXT_LIKE,
    **{str(d): _DIGITS[d] for d in range(10)},
}

# Opus follows the Sonata layout; overrides hold the codes where it differs.
OPUS: dict[str, GlyphClass] = {
    **SONATA,
    "∫": _G.DOUBLE_FLAT,
    "'": _G.STACCATISSIMO,
    "Æ": _G.STACCATISSIMO,
}
# Codes seen only in Opus; their presence is the sole character-level tell.
_OPUS_ONLY = {"∫", "'", "Æ"}


def _smufl() -> dict[str, GlyphClass]:
    t: dict[int, GlyphClass] = {
        0xE050: _G.CLEF_G, 0xE051: _G.CLEF_G_15MB, 0xE052: _G.CLEF_G_8VB,
        0xE053: _G.CLEF_G_8VA, 0xE054: _G.CLEF_G_15MA, 0xE05C: _G.CLEF_C,
        0xE05D: _G.CLEF_C_8VB, 0xE062: _G.CLEF_F, 0xE063: _G.CLEF_F_15MB,
        0xE064: _G.CLEF_F_8VB, 0xE065: _G.CLEF_F_8VA, 0xE069: _G.CLEF_PERCUSSION,
        0xE06A: _G.CLEF_PERCUSSION, 0xE06D: _G.CLEF_TAB, 0xE07A: _G.CLEF_G,
        0xE07B: _G.CLEF_C, 0xE07C: _G.CLEF_F,
        0xE0A0: _G.NOTEHEAD_DOUBLE_WHOLE, 0xE0A2: _G.NOTEHEAD_WHOLE,
        0xE0A3: _G.NOTEHEAD_HALF, 0xE0A4: _G.NOTEHEAD_BLACK,
        # Bravura optional range; Dorico draws noteheads with these.
        0xF4BC: _G.NOTEHEAD_WHOLE, 0xF4BD: _G.NOTEHEAD_HALF, 0xF4BE: _G.NOTEHEAD_BLACK,
        0xE0A7: _G.NOTEHEAD_X, 0xE0A8: _G.NOTEHEAD_X, 0xE0A9: _G.NOTEHEAD_X,
        0xE0B3: _G.NOTEHEAD_CIRCLE_X,
        0xE0D9: _G.NOTEHEAD_DIAMOND_WHITE, 0xE0DA: _G.NOTEHEAD_DIAMOND_WHITE,
        0xE0DB: _G.NOTEHEAD_DIAMOND_BLACK,
        0xE100: _G.NOTEHEAD_SLASH, 0xE101: _G.NOTEHEAD_SLASH,
        0xE102: _G.NOTEHEAD_SLASH_WHITE, 0xE103: _G.NOTEHEAD_SLASH_WHITE,
        0xE1E7: _G.DOT,
        0xE4E2: _G.REST_DOUBLE_WHOLE, 0xE4E3: _G.REST_WHOLE, 0xE4E4: _G.REST_HALF,
        0xE4E5: _G.REST_QUARTER, 0xE4E6: _G.REST_8TH, 0xE4E7: _G.REST_16TH,
        0xE4E8: _G.REST_32ND, 0xE4E9: _G.REST_64TH, 0xE4EA: _G.REST_128TH,
        0xE4EE: _G.REST_MULTI, 0xE4EF: _G.REST_MULTI, 0xE4F0: _G.REST_MULTI,
        0xE4F1: _G.REST_MULTI,
        0xE260: _G.FLAT, 0xE261: _G.NATURAL, 0xE262: _G.SHARP,
        0xE263: _G.DOUBLE_SHARP, 0xE264: _G.DOUBLE_FLAT,
        0xE240: _G.FLAG_8TH_UP, 0xE241: _G.FLAG_8TH_DOWN, 0xE242: _G.FLAG_16TH_UP,
        0xE243: _G.FLAG_16TH_DOWN, 0xE244: _G.FLAG_32ND_UP, 0xE245: _G.FLAG_32ND_DOWN,
        0xE246: _G.FLAG_64TH_UP, 0xE247: _G.FLAG_64TH_DOWN,
        0xE520: _G.DYN_P, 0xE521: _G.DYN_M, 0xE522: _G.DYN_F, 0xE523: _G.DYN_R,
        0xE524: _G.DYN_S, 0xE525: _G.DYN_Z, 0xE526: _G.DYN_N, 0xE52A: _G.DYN_PPP,
        0xE52B: _G.DYN_PP, 0xE52C: _G.DYN_MP, 0xE52D: _G.DYN_MF, 0xE52F: _G.DYN_FF,
        0xE530: _G.DYN_FFF, 0xE534: _G.DYN_FP, 0xE535: _G.DYN_FZ, 0xE536: _G.DYN_SF,
        0xE537: _G.DYN_SFP, 0xE539: _G.DYN_SFZ, 0xE53C: _G.DYN_RF, 0xE53D: _G.DYN_RFZ,
        0xE4A0: _G.ACCENT, 0xE4A1: _G.ACCENT, 0xE4A2: _G.STACCATO, 0xE4A3: _G.STACCATO,
        0xE4A4: _G.TENUTO, 0xE4A5: _G.TENUTO, 0xE4A6: _G.STACCATISSIMO,
        0xE4A7: _G.STACCATISSIMO, 0xE4A8: _G.STACCATISSIMO, 0xE4A9: _G.STACCATISSIMO,
        0xE4AA: _G.STACCATISSIMO, 0xE4AB: _G.STACCATISSIMO, 0xE4AC: _G.MARCATO,
        0xE4AD: _G.MARCATO, 0xE4B2: _G.PORTATO, 0xE4B3: _G.PORTATO,
        0xE566: _G.TRILL, 0xE567: _G.TURN, 0xE56C: _G.INVERTED_MORDENT,
        0xE56D: _G.MORDENT,
        0xE220: _G.TREMOLO_1, 0xE221: _G.TREMOLO_2, 0xE222: _G.TREMOLO_3,
        0xE4C0: _G.FERMATA_ABOVE, 0xE4C1: _G.FERMATA_BELOW,
        0xE4CE: _G.BREATH_MARK, 0xE4CF: _G.BREATH_MARK, 0xE4D1: _G.CAESURA,
        0xE4D2: _G.CAESURA,
        0xE650: _G.PEDAL_DOWN, 0xE655: _G.PEDAL_UP,
        0xEAA9: _G.ARPEGGIO, 0xEAAA: _G.ARPEGGIO, 0xE63C: _G.ARPEGGIO,
        0xE08A: _G.TIME_COMMON, 0xE08B: _G.TIME_CUT,
        0xE043: _G.REPEAT_DOTS, 0xE044: _G.REPEAT_DOTS, 0xE047: _G.SEGNO,
        0xE048: _G.CODA, 0xE049: _G.CODA,
        0xE88A: _G.TEXT_LIKE, 0xE094: _G.TEXT_LIKE, 0xE095: _G.TEXT_LIKE,
        0xE26A: _G.TEXT_LIKE, 0xE26B: _G.TEXT_LIKE,
    }
    for d in range(10):
        t[0xE080 + d] = _DIGITS[d]  # time signature digits
        t[0xE880 + d] = _DIGITS[d]  # tuplet digits
    return {chr(k): v for k, v in t.items()}


SMUFL: dict[str, GlyphClass] = _smufl()

_TABLES: dict[Encoding, dict[str, GlyphClass]] = {
    Encoding.SONATA: SONATA,
    Encoding.OPUS: OPUS,
    Encoding.SMUFL: SMUFL,
}

# Adobe Symbol encoding for the bytes music fonts expose as Greek or math
# characters when their glyph names are plain letters.
_SYMBOL_TO_BYTE: dict[str, int] = {
    **{g: 0x41 + i for i, g in enumerate("ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ")},
    **{g: 0x61 + i for i, g in enumerate("αβχδεφγηιϕκλμνοπθρστυϖωξψζ")},
    "∀": 0x22, "∃": 0x24, "∋": 0x27, "∗": 0x2A, "−": 0x2D, "≅": 0x40,
    "∴": 0x5C, "⊥": 0x5E, "µ": 0x6D,
}
_WIN_SIGNATURE = set("ÏúÎîä·")
_MAC_SIGNATURE = set("œ˙∑Œ‰Ó")


def _legacy_char(char: str, charmap: Charmap) -> str:
    """Map a legacy font's character back to its MacRoman form."""
    cp = ord(char)
    if 0xF020 <= cp <= 0xF0FF:
        return bytes([cp - 0xF000]).decode("mac_roman")
    if charmap is Charmap.SYMBOL:
        if 0x21 <= cp < 0x7E:
            return chr(cp + 1)
        if char in _SYMBOL_TO_BYTE:
            return chr(_SYMBOL_TO_BYTE[char] + 1)
        return char
    if cp < 0x80:
        return char
    if charmap is Charmap.WINANSI:
        try:
            return char.encode("cp1252").decode("mac_roman")
        except UnicodeEncodeError:
            return char
    return char


def normalize(font: FontEncoding | Encoding, char: str) -> str:
    """The table key for `char`: MacRoman form for legacy fonts, as-is for SMuFL."""
    if isinstance(font, Encoding):
        font = FontEncoding(font, Charmap.NATIVE if font is Encoding.SMUFL else Charmap.MACROMAN)
    if font.encoding is Encoding.SMUFL:
        return char
    return _legacy_char(char, font.charmap)


def classify(font_encoding: FontEncoding | Encoding | None, char: str) -> GlyphClass | None:
    """Class of `char` drawn in a font with the given encoding, or None if unknown.

    A bare `Encoding` assumes MacRoman names for legacy fonts; pass the
    `FontEncoding` from `detect_font` to handle WinAnsi and Symbol PDFs."""
    if font_encoding is None:
        return None
    enc = font_encoding if isinstance(font_encoding, Encoding) else font_encoding.encoding
    return _TABLES[enc].get(normalize(font_encoding, char))


def _is_smufl(cp: int) -> bool:
    return 0xE000 <= cp <= 0xEFFF or 0xF100 <= cp <= 0xF8FF


_SMUFL_ANCHORS = {chr(c) for c in (*range(0xE0A0, 0xE0A5), *range(0xF4BC, 0xF4BF), 0xE050, 0xE062, 0xE05C,
                                   *range(0xE4E2, 0xE4EB), *range(0xE260, 0xE265))}
# Codes no text font draws as body text; seeing them marks a Sonata-layout font.
_SONATA_STRONG = set("œ˙∑Œ‰Ó≈ƒ∏π") | {chr(c) for c in range(0xF020, 0xF100)}
# U and u are fermatas in Sonata, so only the commonest text vowels count.
_VOWELS = set("aeioAEIO")


def detect_font(chars: Counter[str]) -> FontEncoding | None:
    """Fingerprint a font by the characters it draws (counts over a document)."""
    counts = Counter({c: n for c, n in chars.items() if not c.isspace()})
    total = sum(counts.values())
    if not total:
        return None
    smufl = sum(n for c, n in counts.items() if _is_smufl(ord(c)))
    if smufl / total >= 0.5 and any(c in _SMUFL_ANCHORS for c in counts):
        return FontEncoding(Encoding.SMUFL, Charmap.NATIVE)

    win = sum(n for c, n in counts.items() if c in _WIN_SIGNATURE)
    mac = sum(n for c, n in counts.items() if c in _MAC_SIGNATURE)
    # π, ∏, µ and Ω double as MacRoman Sonata's pp, ppp and other real codes, so a
    # lone one is no evidence; the shift shows as several distinct Symbol-only codes.
    sym_codes = {c for c in counts if c in _SYMBOL_TO_BYTE and not c.isascii() and c not in "π∏µΩ"}
    symbol = len(sym_codes) >= 2
    charmap = Charmap.WINANSI if win > mac else Charmap.SYMBOL if symbol else Charmap.MACROMAN

    norm = Counter()
    for c, n in counts.items():
        norm[_legacy_char(c, charmap)] += n
    strong = sum(n for c, n in norm.items() if c in _SONATA_STRONG)
    known = sum(n for c, n in norm.items() if c in SONATA)
    vowels = sum(n for c, n in norm.items() if c in _VOWELS)
    # Text fonts draw vowels; music fonts almost never do. Requiring a strong
    # code keeps short text runs like "f" or "B" from qualifying.
    if strong and strong / total >= 0.1 and known / total >= 0.6 and vowels / total < 0.1:
        return FontEncoding(_legacy_family(norm), charmap)
    # A sparse font (e.g. one cue staff) may lack strong codes but still draw
    # a clef and nothing outside the table.
    if known == total and ("&" in norm or "?" in norm) and len(norm) >= 2:
        return FontEncoding(_legacy_family(norm), charmap)
    return None


def _legacy_family(norm: Counter[str]) -> Encoding:
    # Opus shares Sonata's codes for everything common, so a font with none of
    # the Opus-only codes reads the same either way.
    return Encoding.OPUS if any(c in _OPUS_ONLY for c in norm) else Encoding.SONATA


def detect_encoding(chars: Counter[str]) -> Encoding | None:
    font = detect_font(chars)
    return font.encoding if font else None


@dataclass(frozen=True)
class Glyph:
    char: str
    font: str
    size: float
    # Baseline origin in PDF points, y down (PyMuPDF page coordinates).
    x: float
    y: float
    bbox: tuple[float, float, float, float]
    encoding: FontEncoding | None
    cls: GlyphClass | None


def _spans(page: pymupdf.Page) -> Iterator[dict]:
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", ()):
            yield from line["spans"]


def font_chars(pages: Iterable[pymupdf.Page]) -> dict[str, Counter[str]]:
    """Character counts per font name across `pages`."""
    out: dict[str, Counter[str]] = {}
    for page in pages:
        for span in _spans(page):
            cnt = out.setdefault(span["font"], Counter())
            cnt.update(c["c"] for c in span["chars"])
    return out


def document_fonts(doc: pymupdf.Document) -> dict[str, FontEncoding | None]:
    """Detected encoding of every font in `doc`; detection pools all pages
    because a single page can be too sparse to fingerprint."""
    return {name: detect_font(cnt) for name, cnt in font_chars(doc).items()}


def page_glyphs(page: pymupdf.Page, fonts: dict[str, FontEncoding | None] | None = None,
                music_only: bool = True) -> Iterator[Glyph]:
    """Glyphs on `page` with their font's encoding and class.

    Pass `fonts` from `document_fonts` to reuse whole-document detection;
    otherwise the page alone is fingerprinted."""
    if fonts is None:
        fonts = {name: detect_font(cnt) for name, cnt in font_chars([page]).items()}
    for span in _spans(page):
        enc = fonts.get(span["font"])
        if music_only and enc is None:
            continue
        for c in span["chars"]:
            if c["c"].isspace():
                continue
            x, y = c["origin"]
            yield Glyph(c["c"], span["font"], span["size"], x, y, tuple(c["bbox"]), enc,
                        classify(enc, c["c"]))


_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def base_font_name(name: str) -> str:
    """Font name without a PDF subset tag (`ABCDEF+`)."""
    return _SUBSET_PREFIX.sub("", name)
