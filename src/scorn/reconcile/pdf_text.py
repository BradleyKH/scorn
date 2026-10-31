"""Classify the non-music text of a born-digital score PDF by position and style.

Every text span lands in exactly one bucket: lyric syllables, staff labels,
measure numbers, tempo marks, rehearsal marks, directions (including text
dynamics), page furniture, or the title block. Music-font spans are never
text; they only supply note glyphs for metronome marks. Page-level
classification (`read_text`) cannot tell a running header from a title, so the
cross-page part lives in `read_document_text`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

import pymupdf

from . import geometry as geo
from . import glyphs
from .model import BOLD, ITALIC, Staff, System, TextSpan

# Where margin text may sit: page-number and footer bands.
_MARGIN_TOP, _MARGIN_BOTTOM = 0.05, 0.93
# Lyric rows sit at most this many staff spacings below their staff.
_LYRIC_REACH = 16
# A lyric baseline must clear the next staff's top by this much (staff spacings), or it is a direction above that staff.
_LYRIC_CLEARANCE = 3
_MAX_LYRIC_WORDS = 4
# Instrument names end within this many staff spacings of the system's left edge.
_LABEL_REACH = 20
# In the footer band, only text this close under a staff (staff spacings) can be a lyric.
_MARGIN_LYRIC_REACH = 6

_WATERMARK = re.compile(
    r"purchased by|licensed to|downloaded by|order\s*#|ordered \d+ cop|this copy (?:is|was)", re.I)
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_HYPHENS = "-‐‑"
_DYNAMICS = {"p", "pp", "ppp", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "sfp", "fz", "fp", "rfz", "rf", "sffz"}
_DYNAMIC_FONT = re.compile(r"opus|dynamic|sibelius|petrucci", re.I)
_TEMPO_WORDS = re.compile(
    r"^(?:allegro|allegretto|andante|andantino|adagio|adagietto|moderato|largo|larghetto|lento|presto|"
    r"vivace|vivo|maestoso|grave|prestissimo|con moto|tempo i)\b", re.I)
_WORD_GAP = 0.07
# chord symbols, including ones split by spaces ("B M7 /C")
_CHORD = re.compile(r"^/?[A-G][b#♭♯]?(?:m|M|maj|min|dim|aug|sus|add|alt|b|#|\d|[A-G]|/|\(|\)|°|ø|\+|-)*$")
_VERSE_NUMBER = re.compile(r"^\d{1,2}[.):]?$")
_CHORD_SUFFIX = re.compile(r"^(?:m|M|maj|min|dim|aug|sus|add|alt)?[\d#b/()+-]*$")


def _chordish(words: list[str]) -> bool:
    return bool(words) and any(_CHORD.match(w) for w in words) and all(
        _CHORD.match(w) or (w and _CHORD_SUFFIX.match(w)) for w in words)


# Metronome note glyphs: Sonata-family letters, SMuFL metronome notes, Unicode note symbols.
_METRO_GLYPHS = {
    "w": "whole", "h": "half", "q": "quarter", "e": "eighth", "x": "16th",
    "": "quarter", "": "whole", "": "half", "": "quarter", "": "eighth",
    "": "16th", "\U0001d15d": "whole", "\U0001d15e": "half", "\U0001d15f": "quarter",
    "\U0001d160": "eighth", "♩": "quarter", "♪": "eighth",
}
_METRO_TEXT_UNITS = {**{k: v for k, v in _METRO_GLYPHS.items() if len(k) == 1 and k.isalpha()},
                     "♩": "quarter", "♪": "eighth", "\U0001d15d": "whole",
                     "\U0001d15e": "half", "\U0001d15f": "quarter", "\U0001d160": "eighth"}
_METRONOME = re.compile(
    r"(?P<pre>.*?)[(\[]?\s*(?:‹(?P<g>\w+)›|(?P<t>[♩♪\U0001d15d-\U0001d160])"
    r"|(?<![\w])(?P<l>[qhewx])(?![\w]))(?P<dots>\s*\.*)\s*=\s*(?:c\.?\s*)?(?P<bpm>\d+(?:\.\d+)?)"
    r"(?:\s*[-\u2013]\s*(?P<bpm2>\d+))?\s*[)\]]?\s*(?P<post>.*)$", re.S)


@dataclass(frozen=True)
class Syllable:
    page: int
    system: int
    staff: int  # Staff.index of the staff the row hangs under
    row: int  # 0-based, top to bottom under that staff
    verse: int  # the row's verse number when printed, else row + 1
    text: str  # as printed, punctuation kept, hyphens stripped
    x0: float
    x1: float
    y: float  # baseline
    hyphen_after: bool = False  # a hyphen joins this syllable to the next
    extender_after: bool = False  # an extender line or underscores follow


@dataclass(frozen=True)
class StaffLabel:
    page: int
    system: int
    staff: int
    text: str
    # name: left of the system; group: parenthesized text such as "(Men - Group 1)" above a staff
    kind: str
    # abbreviated names are those after the document's first labeled system; set by read_document_text
    abbreviated: bool = False
    x: float = 0.0
    y: float = 0.0


@dataclass(frozen=True)
class Direction:
    page: int
    system: int
    staff: int
    text: str
    placement: str  # above | below | inside
    italic: bool
    bold: bool
    x: float
    y: float
    # set when the text is a dynamic drawn as a letter string ("mf"); the marks pass treats it as a dynamic
    dynamic: str | None = None
    chord: bool = False  # chord symbol rather than a performance direction


@dataclass(frozen=True)
class TempoMark:
    page: int
    system: int
    staff: int
    text: str  # the words only: "Joyfully"
    unit: str | None  # beat unit of the metronome mark: whole, half, quarter, eighth, 16th
    dots: int
    bpm: float | None
    x: float
    y: float
    bpm_max: float | None = None  # upper end of a range such as "= 60-66"
    bold: bool = False
    italic: bool = False
    parenthesized: bool = False  # the metronome mark sits in parentheses: "(♩ = 64)"


@dataclass(frozen=True)
class RehearsalMark:
    page: int
    system: int
    staff: int
    text: str
    boxed: bool
    x: float
    y: float


@dataclass(frozen=True)
class MeasureNumber:
    page: int
    system: int
    staff: int
    number: int
    x: float
    y: float


@dataclass(frozen=True)
class TitleBlock:
    title: str | None = None
    subtitle: str | None = None
    composer: str | None = None
    lyricist: str | None = None
    arranger: str | None = None
    copyright: tuple[str, ...] = ()
    other: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextContent:
    page: int
    lyrics: tuple[Syllable, ...] = ()
    labels: tuple[StaffLabel, ...] = ()
    directions: tuple[Direction, ...] = ()
    tempos: tuple[TempoMark, ...] = ()
    rehearsals: tuple[RehearsalMark, ...] = ()
    measure_numbers: tuple[MeasureNumber, ...] = ()
    # header/footer band spans; read_document_text decides furniture versus title block
    margin: tuple[TextSpan, ...] = ()
    # page 0 text outside the systems (title block candidates)
    title_zone: tuple[TextSpan, ...] = ()
    # other text far from any staff on later pages
    other: tuple[TextSpan, ...] = ()
    watermarks: tuple[TextSpan, ...] = ()


@dataclass(frozen=True)
class DocumentText:
    pages: tuple[TextContent, ...]
    title: TitleBlock
    furniture: tuple[TextSpan, ...] = field(default=())


@dataclass
class _Item:
    span: TextSpan
    # (char, x0, x1) per character, spaces included
    chars: tuple[tuple[str, float, float], ...]

    @property
    def x0(self) -> float:
        return self.span.bbox[0]

    @property
    def x1(self) -> float:
        return self.span.bbox[2]

    @property
    def y(self) -> float:
        return self.span.y

    @property
    def text(self) -> str:
        return self.span.text


# ---------------------------------------------------------------- reading


def _style(flags: int, font: str) -> int:
    name = glyphs.base_font_name(font).lower()
    out = 0
    if flags & 2 or "italic" in name or "oblique" in name or name.endswith("ital") or "-it" in name:
        out |= ITALIC
    if flags & 16 or "bold" in name or "black" in name or "heavy" in name or "semibold" in name:
        out |= BOLD
    return out


def _read_items(page: pymupdf.Page, fonts) -> tuple[list[_Item], list[TextSpan], list[tuple[str, float, float, float]]]:
    """Horizontal text spans, watermark spans, and metronome-capable music glyphs (char, x, y, size)."""
    items: list[_Item] = []
    marks: list[TextSpan] = []
    music: list[tuple[str, float, float, float]] = []
    seen: set[tuple] = set()
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", ()):
            horizontal = abs(line["dir"][0] - 1) < 1e-3
            for sp in line["spans"]:
                chars = tuple((c["c"], c["origin"][0], c["bbox"][2]) for c in sp["chars"])
                text = "".join(c for c, _, _ in chars)
                # blank placeholders such as "(   )" carry no content
                if not any(c.isalnum() or c in _HYPHENS or c == "_" for c in text):
                    continue
                font = sp["font"]
                if fonts.get(font) is not None:
                    if horizontal:
                        for c, x0, _ in chars:
                            if c in _METRO_GLYPHS or c == ".":
                                music.append((c, x0, sp["origin"][1], sp["size"]))
                    continue
                span = TextSpan(page.number, text, font, sp["size"], sp["origin"][0], sp["origin"][1],
                                tuple(sp["bbox"]), _style(sp["flags"], font))
                if not horizontal or _WATERMARK.search(text):
                    marks.append(span)
                    continue
                # overstruck duplicates (a bold effect drawn twice) would double every hyphen
                key = (text, round(span.bbox[0] * 2), round(span.bbox[1] * 2))
                if key in seen:
                    continue
                seen.add(key)
                items.append(_Item(span, chars))
    return items, marks, music


# ---------------------------------------------------------------- geometry helpers


@dataclass
class _Ctx:
    page: pymupdf.Page
    geo: geo.PageGeometry
    staves: list[Staff]
    system_of: dict[int, System]
    boxes: list[tuple[float, float, float, float]]


def _boxes(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if d.get("type") not in ("s", "fs") or not (4 < r.width < 70 and 4 < r.height < 40):
            continue
        if len(d["items"]) <= 8:
            out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def _boxed(ctx: _Ctx, bbox) -> bool:
    x0, y0, x1, y1 = bbox
    for bx0, by0, bx1, by1 in ctx.boxes:
        if bx0 - 1 <= x0 and by0 - 1 <= y0 and x1 <= bx1 + 1 and y1 <= by1 + 1:
            if (bx1 - bx0) * (by1 - by0) <= 8 * max((x1 - x0) * (y1 - y0), 1):
                return True
    return False


def _vgap(st: Staff, y0: float, y1: float) -> float:
    return max(0.0, st.top - y1, y0 - st.bottom)


def _nearest_staff(ctx: _Ctx, it_bbox) -> Staff | None:
    x0, y0, x1, y1 = it_bbox
    best, dist = None, None
    for st in ctx.staves:
        if x1 < st.x0 - 3 * st.spacing or x0 > st.x1 + 3 * st.spacing:
            continue
        d = _vgap(st, y0, y1)
        if dist is None or d < dist:
            best, dist = st, d
    return best


def _staff_above(ctx: _Ctx, x: float, y: float) -> Staff | None:
    """The lowest staff entirely above `y` whose horizontal extent covers `x`."""
    best = None
    for st in ctx.staves:
        if st.bottom < y and st.x0 - 3 * st.spacing <= x <= st.x1 + 3 * st.spacing:
            if best is None or st.bottom > best.bottom:
                best = st
    return best


def _staff_below(ctx: _Ctx, x: float, y: float) -> Staff | None:
    best = None
    for st in ctx.staves:
        if st.top > y and st.x0 - 3 * st.spacing <= x <= st.x1 + 3 * st.spacing:
            if best is None or st.top < best.top:
                best = st
    return best


# ---------------------------------------------------------------- lyrics


def _words(it: _Item) -> list[tuple[str, float, float]]:
    """Split a span into syllable tokens and hyphen markers: ("-", x, x) for a hyphen."""
    out: list[tuple[str, float, float]] = []
    cur: list[tuple[str, float, float]] = []

    def flush():
        if cur:
            out.append(("".join(c for c, _, _ in cur), cur[0][1], cur[-1][2]))
            cur.clear()

    # Some producers omit the space and only shift the next glyph, so a gap also ends a word.
    gap = _WORD_GAP * it.span.size
    for ch, x0, x1 in it.chars:
        if cur and x0 - cur[-1][2] > gap and not ch.isspace():
            flush()
        if ch in _HYPHENS and (cur or out):
            flush()
            out.append(("-", x0, x1))
        elif ch.isspace():
            flush()
        else:
            cur.append((ch, x0, x1))
    flush()
    return out


def _lyric_rows(ctx: _Ctx, cands: list[tuple[_Item, Staff]]) -> tuple[list[Syllable], list[_Item]]:
    """Group lyric candidates into rows per staff and tokenize them. Returns syllables and rejected items."""
    by_staff: dict[int, list[_Item]] = defaultdict(list)
    staff_by_index = {st.index: st for st in ctx.staves}
    for it, st in cands:
        by_staff[st.index].append(it)
    sylls: list[Syllable] = []
    rejected: list[_Item] = []
    row_bands: list[tuple[int, int, float, float, list[Syllable]]] = []  # staff, row, baseline, size, items
    for si, items in sorted(by_staff.items()):
        st = staff_by_index[si]
        system = ctx.system_of[si]
        items.sort(key=lambda i: (i.y, i.x0))
        rows: list[list[_Item]] = []
        for it in items:
            if rows and abs(it.y - rows[-1][-1].y) <= 1.5:
                rows[-1].append(it)
            else:
                rows.append([it])
        row_no = 0
        for row in rows:
            styled = all(not (i.span.bold or i.span.italic) for i in row)
            prose = any(len(i.text.replace("-", " ").split()) > _MAX_LYRIC_WORDS for i in row)
            words = [w for i in row for w in i.text.split()]
            chords = len(words) >= 2 and _chordish(words)
            if prose or chords or not (styled or len(row) >= 3):
                rejected.extend(row)
                continue
            toks: list[tuple[float, str, float, float]] = []
            for it in row:
                for text, x0, x1 in _words(it):
                    toks.append((x0, text, x0, x1))
            toks.sort()
            verse = None
            if len(toks) > 1 and _VERSE_NUMBER.match(toks[0][1]) and (
                    toks[0][1][-1] in ".):" or len(row[0].chars) <= 3):
                verse = int(re.sub(r"\D", "", toks[0][1]))
                toks = toks[1:]
            out: list[Syllable] = []
            for _, text, x0, x1 in toks:
                if text == "-":
                    if out:
                        out[-1] = replace(out[-1], hyphen_after=True)
                    continue
                if set(text) == {"_"}:
                    if out:
                        out[-1] = replace(out[-1], extender_after=True)
                    continue
                out.append(Syllable(system.page, system.index, si, row_no, verse or row_no + 1,
                                    text, x0, x1, row[0].y))
            if out:
                sylls.extend(out)
                row_bands.append((si, row_no, row[0].y, row[0].span.size, out))
                row_no += 1
    return _attach_lines(ctx, sylls, row_bands), rejected


def _attach_lines(ctx: _Ctx, sylls: list[Syllable], bands) -> list[Syllable]:
    """Mark hyphen and extender drawn as lines onto the syllable they follow."""
    idx = {id(s): i for i, s in enumerate(sylls)}
    sylls = list(sylls)
    for line in ctx.geo.hyphens + ctx.geo.extenders:
        y = (line.y0 + line.y1) / 2
        xm = (line.x0 + line.x1) / 2
        extender = line.kind == "extender"
        for si, _, base, size, out in bands:
            lo, hi = (base - 1.5, base + 3) if extender else (base - 0.8 * size, base + 1.5)
            if not lo <= y <= hi:
                continue
            prev = [s for s in out if s.x1 <= (line.x0 if extender else xm) + size * 0.8]
            if not prev or (line.x0 if extender else xm) - prev[-1].x1 > size * 2:
                continue
            tgt = prev[-1]
            if line.x0 < out[0].x0 - size:
                continue
            i = idx[id(tgt)]
            sylls[i] = replace(sylls[i], extender_after=True) if extender else replace(sylls[i], hyphen_after=True)
            idx[id(sylls[i])] = i
            out[out.index(tgt)] = sylls[i]
            break
    return sylls


def lyric_line(sylls: Iterable[Syllable]) -> str:
    """Join syllables the way a hymnal reads: "Pas-chal Lamb"."""
    out = ""
    for s in sylls:
        out += s.text
        out += "-" if s.hyphen_after else " "
    return out.strip()


# ---------------------------------------------------------------- directional text


def _metronome_glyphs(items: list[_Item], music) -> list[tuple[float, float, str, str, float]]:
    """Note glyphs sitting just before an `=` span, as (x, y, token, kind, size)."""
    found = []
    eq = [it for it in items if it.text.lstrip(" (").startswith("=") or it.text.lstrip().startswith("=")]
    for c, x, y, size in music:
        if c not in _METRO_GLYPHS:
            continue
        for it in eq:
            if 0 <= it.x0 - x <= 3.2 * size and abs(it.y - y) <= 0.6 * size:
                dots = [d for d in music if d[0] == "." and 0 < d[1] - x < size * 1.2 and abs(d[2] - y) < size * 0.6]
                found.append((x, y, _METRO_GLYPHS[c], "." * len(dots), size))
                break
    return found


def _merge_lines(items: list[_Item], metro) -> list[tuple[str, int, float, float, float, float, float, tuple]]:
    """Merge items on one baseline with small gaps into lines: (text, flags, size, x0, x1, y, y0, bbox)."""
    pieces = [(it.x0, it.x1, it.y, it.text, it.span.flags, it.span.size, it.span.bbox) for it in items]
    for x, y, unit, dots, size in metro:
        pieces.append((x, x + 0.6 * size, y, f"‹{unit}›{dots}", 0, size, (x, y - size, x + 0.6 * size, y)))
    pieces.sort(key=lambda p: (round(p[2] / 1.5), p[0]))
    out: list[list] = []
    for p in pieces:
        # a measure number beside a direction is its own mark
        lone = out and (out[-1][3].strip().isdigit() or p[3].strip().isdigit())
        if out and not lone and abs(out[-1][2] - p[2]) <= 1.5 and p[0] - out[-1][1] <= 0.7 * p[5]:
            ln = out[-1]
            gap = p[0] - ln[1]
            sep = " " if gap > 0.2 * p[5] and not ln[3].endswith(" ") and not p[3].startswith(" ") else ""
            ln[3] += sep + p[3]
            ln[1] = max(ln[1], p[1])
            ln[4] |= p[4]
            ln[5] = max(ln[5], p[5])
            ln[6] = (min(ln[6][0], p[6][0]), min(ln[6][1], p[6][1]), max(ln[6][2], p[6][2]), max(ln[6][3], p[6][3]))
        else:
            out.append([p[0], p[1], p[2], p[3], p[4], p[5], p[6]])
    return [(ln[3], ln[4], ln[5], ln[0], ln[1], ln[2], ln[6][1], ln[6]) for ln in out]


def _placement(st: Staff, bbox) -> str:
    cy = (bbox[1] + bbox[3]) / 2
    if cy < st.top:
        return "above"
    return "below" if cy > st.bottom else "inside"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _classify_line(ctx: _Ctx, line, page_no: int, out: dict) -> bool:
    """File one merged line of directional text. Returns False when it is outside every system."""
    text, flags, size, x0, x1, y, _, bbox = line
    st = _nearest_staff(ctx, bbox)
    if st is None:
        return False
    sp = st.spacing
    gap = _vgap(st, bbox[1], bbox[3])
    placement = _placement(st, bbox)
    reach = 6 * sp if placement == "above" else 3 * sp
    if gap > reach:
        return False
    system = ctx.system_of[st.index]
    clean = _clean(text)
    cx = (x0 + x1) / 2
    boxed = _boxed(ctx, bbox)

    m = _METRONOME.match(clean)
    if m and (m["g"] or m["t"] or m["l"] or "=" in clean):
        unit = m["g"] or _METRO_TEXT_UNITS.get(m["t"] or "") or _METRO_TEXT_UNITS.get(m["l"] or "")
        dots = len((m["dots"] or "").replace(" ", ""))
        glyph_at = min((m.start(k) for k in ("g", "t", "l") if m[k]), default=len(m["pre"]))
        # "(" ahead of the note glyph, whether or not it also wraps the words
        parens = bool(re.search(r"[(\[]\s*$", clean[:glyph_at])) and bool(re.search(r"[)\]]\s*$", clean))
        words = re.sub(r"[\s(\[]+$", "", m["pre"]).strip()
        if parens:
            words = words.lstrip("([ ")
        post = m["post"].strip()
        out["tempos"].append(TempoMark(page_no, system.index, st.index, _clean(" ".join(w for w in (words, post) if w)),
                                       unit, dots, float(m["bpm"]), x0, y,
                                       float(m["bpm2"]) if m["bpm2"] else None,
                                       bool(flags & BOLD), bool(flags & ITALIC), parens))
        return True
    if _TEMPO_WORDS.match(clean) and not flags & ITALIC:
        out["tempos"].append(TempoMark(page_no, system.index, st.index, clean.rstrip(" ,"), None, 0, None, x0, y,
                                       bold=bool(flags & BOLD)))
        return True
    # Group names are capitalized ("(Men - Group 1)"); a lowercase start is a playing instruction.
    if clean.startswith("(") and clean.endswith(")") and placement == "above" and clean[1:2].isupper():
        below = _staff_below(ctx, cx, bbox[3] - 1)
        tgt = below if below is not None and below.top - bbox[3] <= 6 * below.spacing else st
        out["labels"].append(StaffLabel(page_no, ctx.system_of[tgt.index].index, tgt.index, clean, "group",
                                        x=x0, y=y))
        return True
    low = clean.lower()
    if low in _DYNAMICS and (_DYNAMIC_FONT.search(line_font(ctx, bbox, out)) or flags & (ITALIC | BOLD)):
        out["directions"].append(Direction(page_no, system.index, st.index, low, placement, bool(flags & ITALIC),
                                           bool(flags & BOLD), x0, y, dynamic=low))
        return True
    if placement == "above" and len(clean) <= 12 and (boxed or (flags & BOLD and len(clean) <= 3
                                                                and re.fullmatch(r"[A-Za-z0-9]{1,3}", clean))):
        out["rehearsals"].append(RehearsalMark(page_no, system.index, st.index, clean, boxed, x0, y))
        return True
    if boxed and len(clean) <= 12 and re.fullmatch(r"[A-Za-z0-9]{1,4}", clean):
        out["rehearsals"].append(RehearsalMark(page_no, system.index, st.index, clean, True, x0, y))
        return True
    if placement == "above" and re.fullmatch(r"\d{1,4}", clean) and not flags & BOLD:
        out["measure_numbers"].append(MeasureNumber(page_no, system.index, st.index, int(clean), x0, y))
        return True
    chord = _chordish(clean.split())
    out["directions"].append(Direction(page_no, system.index, st.index, clean, placement, bool(flags & ITALIC),
                                       bool(flags & BOLD), x0, y, chord=chord))
    return True


def line_font(ctx: _Ctx, bbox, out: dict) -> str:
    """Font name of the item under `bbox`, for the dynamics check."""
    for it in out["_items"]:
        if it.span.bbox[0] <= bbox[0] + 0.5 and it.span.bbox[2] >= bbox[0] and abs(it.span.bbox[1] - bbox[1]) < 3:
            return glyphs.base_font_name(it.span.font)
    return ""


# ---------------------------------------------------------------- page


def read_text(page: pymupdf.Page, geometry: geo.PageGeometry, fonts=None) -> TextContent:
    """Classify the text of one page. `fonts` is `glyphs.document_fonts(page.parent)` when already computed."""
    if fonts is None:
        fonts = glyphs.document_fonts(page.parent)
    items, marks, music = _read_items(page, fonts)
    pno = page.number
    h = page.rect.height
    staves = list(geometry.staves)
    system_of = {st.index: s for s in geometry.systems for st in s.staves}
    ctx = _Ctx(page, geometry, staves, system_of, _boxes(page) if staves else [])
    res: dict = {k: [] for k in ("labels", "directions", "tempos", "rehearsals", "measure_numbers")}
    res["_items"] = items
    margin: list[TextSpan] = []
    title_zone: list[TextSpan] = []
    other: list[TextSpan] = []
    cands: list[tuple[_Item, Staff]] = []
    squeezed: list[tuple[_Item, Staff]] = []
    rest: list[_Item] = []

    for it in items:
        b = it.span.bbox
        if b[3] >= _MARGIN_BOTTOM * h and b[1] >= _MARGIN_BOTTOM * h - 8 or b[3] <= _MARGIN_TOP * h:
            # the last system's lyrics can sit in the footer band; a row hugging its staff is not furniture
            under = _staff_above(ctx, (b[0] + b[2]) / 2, it.y - 0.2 * it.span.size)
            if (under is None or b[3] <= _MARGIN_TOP * h or it.y - under.bottom > _MARGIN_LYRIC_REACH * under.spacing
                    or b[0] < under.x0 - 3 * under.spacing or b[2] > under.x1 + 6 * under.spacing):
                margin.append(it.span)
                continue
            cands.append((it, under))
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        # label: left of a system, within its vertical extent
        sysmatch = None
        for s in geometry.systems:
            sp = s.staves[0].spacing
            # text farther out is title-block material that happens to share the system's rows
            if (b[2] <= s.x0 - 0.5 * sp and s.x0 - b[2] <= _LABEL_REACH * sp
                    and s.top - 2 * sp <= cy <= s.bottom + 2 * sp):
                sysmatch = s
                break
        if sysmatch is not None:
            near = min(sysmatch.staves, key=lambda st: abs((st.top + st.bottom) / 2 - cy))
            clean = _clean(it.text)
            if not any(c.isalnum() for c in clean):
                continue
            if clean.isdigit() and cy < near.top and near is sysmatch.staves[0]:
                # a bar number printed above the first staff, not a player number beside it
                res["measure_numbers"].append(MeasureNumber(pno, sysmatch.index, near.index, int(clean), b[0], it.y))
            elif clean.startswith("(") and clean.endswith(")"):
                res["directions"].append(Direction(pno, sysmatch.index, near.index, clean, _placement(near, b),
                                                   bool(it.span.flags & ITALIC), bool(it.span.flags & BOLD),
                                                   b[0], it.y))
            else:
                res["labels"].append(StaffLabel(pno, sysmatch.index, near.index, clean, "name", x=b[0], y=it.y))
            continue
        # lyric candidate: plain text hanging below a staff, clear of the next staff
        above = _staff_above(ctx, cx, it.y - 0.2 * it.span.size)
        if above is not None:
            sp = above.spacing
            below = _staff_below(ctx, cx, it.y - 0.2 * it.span.size)
            dist = cy - above.bottom
            # tight layouts leave no clearance; then the nearer staff owns the text
            clear = below is None or below.top - it.y >= _LYRIC_CLEARANCE * sp or dist <= below.top - cy
            in_x = b[0] >= above.x0 - 3 * sp and b[2] <= above.x1 + 6 * sp
            if 0 < dist <= _LYRIC_REACH * sp and in_x:
                if clear:
                    cands.append((it, above))
                else:
                    squeezed.append((it, above))
                continue
        rest.append(it)

    sylls, rejected = _lyric_rows(ctx, cands)
    # Text squeezed against the next staff is still lyric when it shares a baseline with, or stacks under, a
    # row already accepted for the same staff.
    # Bar numbers are italic digits; never a lyric.
    squeezed = [pair for pair in squeezed if not (pair[0].text.strip().isdigit() and pair[0].span.flags)]
    while squeezed:
        rows = [(s.staff, s.y) for s in sylls]
        joined = [pair for pair in squeezed
                  if any(si == pair[1].index and -1.5 <= pair[0].y - y <= 2.2 * pair[0].span.size for si, y in rows)]
        if not joined:
            # an unaccompanied squeezed row: plain words on one baseline, three or more spans
            groups: dict[tuple[int, int], list] = defaultdict(list)
            for pair in squeezed:
                groups[(pair[1].index, round(pair[0].y / 1.5))].append(pair)
            joined = [pair for g in groups.values() if len(g) >= 3 and not any(p[0].span.flags for p in g) for pair in g]
        if not joined:
            break
        cands.extend(joined)
        squeezed = [pair for pair in squeezed if pair not in joined]
        sylls, rejected = _lyric_rows(ctx, cands)
    rest.extend(it for it, _ in squeezed)
    rest.extend(rejected)

    metro = _metronome_glyphs(rest, music)
    for ln in _merge_lines(rest, metro):
        if _classify_line(ctx, ln, pno, res):
            continue
        # outside every system: title block material on the first page, stray text otherwise
        spans = [it.span for it in rest if it.span.bbox[1] >= ln[7][1] - 0.5 and it.span.bbox[3] <= ln[7][3] + 0.5
                 and it.span.bbox[0] >= ln[7][0] - 0.5 and it.span.bbox[2] <= ln[7][2] + 0.5]
        (title_zone if pno == 0 else other).extend(spans)

    def order(seq, *keys):
        return tuple(sorted(seq, key=lambda o: tuple(getattr(o, k) for k in keys)))

    return TextContent(
        page=pno,
        lyrics=tuple(sorted(sylls, key=lambda s: (s.system, s.staff, s.row, s.x0))),
        labels=order(res["labels"], "system", "staff", "y"),
        directions=order(res["directions"], "system", "staff", "x"),
        tempos=order(res["tempos"], "system", "x"),
        rehearsals=order(res["rehearsals"], "system", "x"),
        measure_numbers=order(res["measure_numbers"], "system", "staff", "x"),
        margin=tuple(margin),
        title_zone=tuple(title_zone),
        other=tuple(other),
        watermarks=tuple(marks),
    )


# ---------------------------------------------------------------- title block


@dataclass
class _TLine:
    text: str
    size: float
    x0: float
    x1: float
    y: float
    italic: bool
    top: bool


def _title_lines(spans: Sequence[TextSpan], first_top: float | None) -> list[_TLine]:
    spans = sorted(spans, key=lambda s: (round(s.y / 1.5), s.x))
    lines: list[list] = []
    for s in spans:
        if lines and abs(lines[-1][4] - s.y) <= 1.5 and s.bbox[0] - lines[-1][3] <= 0.7 * s.size:
            ln = lines[-1]
            ln[0] += s.text if (ln[0].endswith(" ") or s.text.startswith(" ")) else (
                " " + s.text if s.bbox[0] - ln[3] > 0.2 * s.size else s.text)
            ln[3] = max(ln[3], s.bbox[2])
            ln[1] = max(ln[1], s.size)
        else:
            lines.append([s.text, s.size, s.bbox[0], s.bbox[2], s.y, s.italic])
    return [_TLine(_clean(t), sz, x0, x1, y, it, first_top is None or y < first_top)
            for t, sz, x0, x1, y, it in lines if _clean(t)]


_ARR = re.compile(r"^(?:arr(?:anged|\.)?(?:\s+by)?|transcribed(?:\s+by)?|orch(?:estrated|\.)?(?:\s+by)?)\s*[:.]?\s*", re.I)
_LYR = re.compile(r"^(?:words|text|lyrics?)(?:\s+by)?\s*[:.]?\s+", re.I)
_COMP = re.compile(r"^(?:music|composer|composed)(?:\s+by)?\s*[:.]?\s+", re.I)
_COPY = re.compile(r"©|copyright|\(c\)", re.I)


def _title_block(spans: Sequence[TextSpan], width: float, first_top: float | None) -> TitleBlock:
    lines = _title_lines(spans, first_top)
    used: set[int] = set()
    top = [(i, ln) for i, ln in enumerate(lines) if ln.top]
    title = subtitle = composer = lyricist = arranger = None
    t_idx = None
    if top:
        t_idx, tl = max(top, key=lambda p: (round(p[1].size), -p[1].y))
        title = tl.text
        used.add(t_idx)
        for i, ln in top:
            if i in used:
                continue
            centered = abs((ln.x0 + ln.x1) / 2 - width / 2) < 0.1 * width
            right = ln.x1 > 0.8 * width and ln.x0 > 0.4 * width
            left = ln.x0 < 0.25 * width
            if _ARR.match(ln.text) and arranger is None:
                arranger = _ARR.sub("", ln.text).strip()
            elif _LYR.match(ln.text) and lyricist is None:
                lyricist = _LYR.sub("", ln.text).strip()
            elif _COMP.match(ln.text) and composer is None:
                composer = _COMP.sub("", ln.text).strip()
            elif _COPY.search(ln.text):
                continue
            elif centered and subtitle is None and ln.y > tl.y and ln.size < tl.size:
                subtitle = ln.text
            elif right and composer is None:
                composer = ln.text
            elif left and not centered and lyricist is None and ln.y > tl.y - 40:
                lyricist = ln.text
            else:
                continue
            used.add(i)
    copyright_: list[str] = []
    for i, ln in enumerate(lines):
        if i in used:
            continue
        if _COPY.search(ln.text):
            copyright_.append(ln.text)
            used.add(i)
        elif lyricist is None and _LYR.match(ln.text):
            lyricist = _LYR.sub("", ln.text).strip()
            used.add(i)
        elif composer is None and _COMP.match(ln.text):
            composer = _COMP.sub("", ln.text).strip()
            used.add(i)
    rest = tuple(ln.text for i, ln in enumerate(lines) if i not in used)
    return TitleBlock(title, subtitle, composer, lyricist, arranger, tuple(copyright_), rest)


# ---------------------------------------------------------------- document


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", _clean(text).lower())


def read_document_text(doc: pymupdf.Document, geometries: Sequence[geo.PageGeometry] | None = None) -> DocumentText:
    """Read every page, then separate page furniture from the title block by repetition across pages."""
    fonts = glyphs.document_fonts(doc)
    if geometries is None:
        geometries = geo.read_document(doc)
    pages = [read_text(p, g, fonts) for p, g in zip(doc, geometries)]

    seen: dict[str, set[int]] = defaultdict(set)
    for tc in pages:
        for sp in (*tc.margin, *tc.other):
            seen[_norm(sp.text)].add(tc.page)
    repeated = {k for k, v in seen.items() if len(v) >= 2}

    def furniture(sp: TextSpan) -> bool:
        return (sp.page > 0 or _PAGE_NUMBER.match(_clean(sp.text)) is not None or _norm(sp.text) in repeated)

    furn: list[TextSpan] = []
    out_pages: list[TextContent] = []
    first_label_seen = False
    for tc in pages:
        furn.extend(sp for sp in (*tc.margin, *tc.other) if furniture(sp))
        labels = tc.labels
        if labels:
            first_sys = min(lb.system for lb in labels if lb.kind == "name")if any(lb.kind == "name" for lb in labels) else None
            labels = tuple(
                replace(lb, abbreviated=first_label_seen or (first_sys is not None and lb.system != first_sys))
                if lb.kind == "name" else lb for lb in labels)
            if first_sys is not None:
                first_label_seen = True
        out_pages.append(replace(tc, labels=labels, margin=tuple(s for s in tc.margin if not furniture(s)),
                                 other=tuple(s for s in tc.other if not furniture(s))))
    first_top = None
    if geometries and geometries[0].systems:
        first_top = geometries[0].systems[0].top - 1
    block = TitleBlock()
    if pages:
        pool = [*out_pages[0].title_zone, *out_pages[0].margin]
        block = _title_block(pool, doc[0].rect.width, first_top)
    return DocumentText(tuple(out_pages), block, tuple(furn))


def lyrics_by_staff(content: TextContent) -> dict[tuple[int, int, int], list[Syllable]]:
    """Syllables keyed by (system, staff, verse), left to right."""
    out: dict[tuple[int, int, int], list[Syllable]] = defaultdict(list)
    for s in content.lyrics:
        out[(s.system, s.staff, s.verse)].append(s)
    return out


__all__ = [
    "Syllable", "StaffLabel", "Direction", "TempoMark", "RehearsalMark", "MeasureNumber", "TitleBlock",
    "TextContent", "DocumentText", "read_text", "read_document_text", "lyric_line", "lyrics_by_staff",
]
