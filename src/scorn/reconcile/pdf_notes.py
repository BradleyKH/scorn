"""Notes and rests read from a born-digital PDF: pitch and rhythm per staff and measure.

Positions come from glyph baseline origins (music fonts put the origin of a
notehead, accidental or clef on the staff position it names) and from the
page's vector geometry (staves, measures, stems, beams).
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction

import pymupdf

from .geometry import PageGeometry, read_geometry
from .glyphs import Glyph, GlyphClass, document_fonts, page_glyphs
from .model import Line, Measure, Staff

STEPS = "CDEFGAB"
_SEMITONES = (0, 2, 4, 5, 7, 9, 11)
SHARP_ORDER = "FCGDAEB"
FLAT_ORDER = "BEADGCF"

NOTE_QUARTERS: dict[str, Fraction] = {
    "breve": Fraction(8),
    "whole": Fraction(4),
    "half": Fraction(2),
    "quarter": Fraction(1),
    "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4),
    "32nd": Fraction(1, 8),
    "64th": Fraction(1, 16),
    "128th": Fraction(1, 32),
}
# Note type by beam or flag count on a black notehead.
_BEAMED = ("quarter", "eighth", "16th", "32nd", "64th", "128th")
# Normal notes for an n-tuplet: 3 in the time of 2, 5 in the time of 4, and so on.
TUPLET_NORMAL = {2: 3, 3: 2, 4: 3, 5: 4, 6: 4, 7: 4, 9: 8, 10: 8, 11: 8, 12: 8, 13: 8}
# Grace notes are drawn smaller than the staff's normal noteheads.
GRACE_RATIO = 0.75


@dataclass(frozen=True)
class Clef:
    sign: str
    # staff line counted from the bottom
    line: int
    octave: int = 0

    def bottom_line(self) -> int:
        """Diatonic number (octave * 7 + step index) of the bottom staff line."""
        ref = {"G": 4 * 7 + 4, "F": 3 * 7 + 3, "C": 4 * 7}.get(self.sign, 4 * 7 + 4)
        line = self.line if self.sign in ("G", "F", "C") else 2
        return ref - 2 * (line - 1) + 7 * self.octave


TREBLE = Clef("G", 2)


@dataclass(frozen=True)
class TimeSignature:
    beats: int
    beat_type: int
    # "common" or "cut" when drawn as a symbol
    symbol: str | None = None

    @property
    def quarters(self) -> Fraction:
        return Fraction(4 * self.beats, self.beat_type)


@dataclass(frozen=True)
class Note:
    step: str
    octave: int
    alter: int
    # alter of an accidental drawn on this note; None when none is drawn
    accidental: int | None
    # staff position in line units: 0 top line, 4 bottom line, half steps are spaces
    position: float
    x: float
    y: float
    head: str

    @property
    def midi(self) -> int:
        return 12 * (self.octave + 1) + _SEMITONES[STEPS.index(self.step)] + self.alter

    @property
    def name(self) -> str:
        acc = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}.get(self.alter, "?")
        return f"{self.step}{acc}{self.octave}"


@dataclass(frozen=True)
class Event:
    """A note, chord or rest; `notes` is empty for a rest."""

    x: float
    y: float
    type: str
    dots: int
    # in quarter notes, after dots and tuplets; zero for grace notes
    duration: Fraction
    notes: tuple[Note, ...] = ()
    voice: int = 1
    stem: str | None = None
    grace: bool = False
    # small notes that keep their rhythm (cues), as opposed to grace notes
    cue: bool = False
    tuplet: tuple[int, int] | None = None
    # a rest filling the whole bar, whatever its glyph
    measure_rest: bool = False
    # quarters from the start of the bar within its voice; None for grace notes
    onset: Fraction | None = None

    @property
    def is_rest(self) -> bool:
        return not self.notes

    @property
    def is_chord(self) -> bool:
        return len(self.notes) > 1


@dataclass(frozen=True)
class StaffMeasureContent:
    page: int
    system: int
    # staff index on the page, and within its system
    staff: int
    staff_in_system: int
    # measure index within the system
    measure: int
    x0: float
    x1: float
    # clef, key (fifths) and time in force at the start of the measure
    clef: Clef
    key: int
    time: TimeSignature | None
    clef_shown: bool
    key_shown: bool
    time_shown: bool
    events: tuple[Event, ...]
    # measures spanned by a multi-bar rest; 0 when this is not one
    multirest: int = 0
    repeat_start: bool = False
    repeat_end: bool = False
    # clef changes inside the measure: (x, clef)
    clef_changes: tuple[tuple[float, Clef], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def voices(self) -> int:
        return max((e.voice for e in self.events), default=0)


@dataclass
class ReadState:
    """What carries from one system to the next: clefs per staff slot and the time signature."""

    clefs: dict[int, Clef] = field(default_factory=dict)
    time: TimeSignature | None = None


def read_document_notes(doc: pymupdf.Document,
                        geometries: Sequence[PageGeometry] | None = None) -> list[StaffMeasureContent]:
    """Every page in order; pass `geometries` (one per page) when already read."""
    fonts = document_fonts(doc)
    state = ReadState()
    out: list[StaffMeasureContent] = []
    for page in doc:
        geometry = geometries[page.number] if geometries is not None else read_geometry(page)
        out += read_notes(page, geometry, list(page_glyphs(page, fonts)), state)
    return out


def read_notes(
    page: pymupdf.Page,
    geometry: PageGeometry,
    glyphs: list[Glyph],
    state: ReadState | None = None,
) -> list[StaffMeasureContent]:
    """Per staff and measure, in system, staff, measure order. Pass a shared `state` across pages."""
    state = state if state is not None else ReadState()
    music = [g for g in glyphs if g.encoding is not None]
    staff_glyphs = _assign_glyphs(sorted(music + _text_digits(page), key=lambda g: (g.x, g.y, g.char)), geometry)
    ottavas = _ottava_spans(music, geometry, _text_ottavas(page))
    out: list[StaffMeasureContent] = []
    for system in geometry.systems:
        measures = sorted((m for m in geometry.measures if m.system == system.index), key=lambda m: m.x0)
        if not measures:
            continue
        times: list[TimeSignature | None] = []
        for k, staff in enumerate(system.staves):
            # Every system redraws its clefs; the carried one covers a header the reader missed.
            clef = state.clefs.get(k, TREBLE)
            reader = _StaffReader(staff, geometry, staff_glyphs.get(staff.index, []), clef, state.time,
                                  ottavas.get(staff.index, ()))
            for m in measures:
                out.append(reader.read(m, k))
            state.clefs[k] = reader.clef
            times.append(reader.time)
        shown = [t for t in times if t is not None]
        if shown:
            state.time = statistics.mode(shown) if len(shown) > 2 else shown[0]
    return out


_OTTAVA_TEXT = re.compile(r"\(?(8va|8vb|8ba|15ma|15mb)\)?")
_OTTAVA_SHIFT = {"8va": 1, "8vb": -1, "8ba": -1, "15ma": 2, "15mb": -2}


def _text_ottavas(page: pymupdf.Page) -> list[tuple[float, float, tuple[float, float, float, float], int]]:
    """Ottava signs set in a text font ("8va", "15ma", "8vb"), as (x, baseline y, bbox, shift)."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            if abs(line["dir"][0] - 1) > 1e-3:
                continue
            for span in line["spans"]:
                m = _OTTAVA_TEXT.fullmatch(span["text"].strip())
                if m:
                    out.append((span["bbox"][0], span["origin"][1], tuple(span["bbox"]), _OTTAVA_SHIFT[m.group(1)]))
    return out


def _ottava_spans(glyphs: list[Glyph], geometry: PageGeometry,
                  texts: list[tuple[float, float, tuple[float, float, float, float], int]] = ()
                  ) -> dict[int, tuple[tuple[float, float, int], ...]]:
    """(x0, x1, octave shift) per staff for each ottava sign: an 8va sits above its staff, an 8vb
    below, and its dashed extension runs to the last note it covers. Without a readable extension
    it covers the rest of its measure."""
    out: dict[int, list[tuple[float, float, int]]] = {}
    flat = [ln for ln in (*geometry.lines, *geometry.ledgers) if abs(ln.y1 - ln.y0) <= 0.5]
    hooks = [ln for ln in geometry.lines if abs(ln.x1 - ln.x0) <= 0.5]
    signs = [(g.x, g.y, g.bbox, int(g.cls.params[0]))
             for g in glyphs if g.cls is not None and g.cls.category == "ottava"]
    for x, y, bbox, shift in sorted([*signs, *texts], key=lambda s: (s[0], s[1])):
        if shift > 0:
            near = [(s.top - y, s) for system in geometry.systems for s in system.staves
                    if system.x0 - 3 * s.spacing <= x <= system.x1 and 0 <= s.top - y <= 10 * s.spacing]
        else:
            near = [(bbox[1] - s.bottom, s) for system in geometry.systems for s in system.staves
                    if system.x0 - 3 * s.spacing <= x <= system.x1 and 0 <= bbox[1] - s.bottom <= 10 * s.spacing]
        if not near:
            continue
        staff = min(near, key=lambda t: (t[0], t[1].index))[1]
        sp = staff.spacing
        top = min(bbox[1], y - sp)
        right = bbox[2]
        x1 = right
        for ln in sorted((ln for ln in flat if top - sp <= ln.y0 <= y + 0.5 * sp and ln.x1 > x1), key=lambda ln: ln.x0):
            if ln.x0 - x1 > 3 * sp:
                break
            x1 = ln.x1
        if x1 - right < sp:
            ms = [m for m in geometry.measures if m.x0 - sp <= x < m.x1
                  and any(s.index == staff.index for sy in geometry.systems if sy.index == m.system for s in sy.staves)]
            x1 = ms[0].x1 if ms else staff.x1
        elif any(abs(h.x0 - x1) <= 0.5 * sp and min(h.y0, h.y1) <= y + sp and max(h.y0, h.y1) >= y - sp for h in hooks):
            x1 += 0.1
        out.setdefault(staff.index, []).append((x, x1, shift))
    return {k: tuple(v) for k, v in out.items()}


# Glyph classes that sit on or near one staff and matter to this reader.
_CATEGORIES = {"clef", "notehead", "rest", "accidental", "dot", "dot-or-staccato", "flag", "digit", "time", "repeat-dots"}


def _assign_glyphs(glyphs: list[Glyph], geometry: PageGeometry) -> dict[int, list[Glyph]]:
    """Each relevant glyph goes to the nearest staff of the system whose span holds it."""
    out: dict[int, list[Glyph]] = {}
    for g in glyphs:
        if g.cls is None or g.cls.category not in _CATEGORIES:
            continue
        best: Staff | None = None
        dist = 0.0
        for system in geometry.systems:
            sp = system.staves[0].spacing
            if not (system.x0 - 3 * sp <= g.x <= system.x1 + 2 * sp):
                continue
            if not (system.top - 12 * sp <= g.y <= system.bottom + 12 * sp):
                continue
            for s in system.staves:
                d = 0.0 if s.top <= g.y <= s.bottom else min(abs(g.y - s.top), abs(g.y - s.bottom))
                if best is None or d < dist:
                    best, dist = s, d
        if best is not None:
            out.setdefault(best.index, []).append(g)
    return out


_DIGIT_CLASSES = {str(d.params[0]): d for d in GlyphClass if d.category == "digit"}


def _text_digits(page: pymupdf.Page) -> list[Glyph]:
    """Numbers set in a text font (Finale draws tuplet numerals in Times), as digit glyphs
    with no music encoding so readers can tell them from music-font digits."""
    out: list[Glyph] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", ()):
            if abs(line["dir"][0] - 1) > 1e-3:
                continue
            for span in line["spans"]:
                chars = [c for c in span["chars"] if not c["c"].isspace()]
                if not chars or len(chars) > 2 or not all(c["c"] in _DIGIT_CLASSES for c in chars):
                    continue
                for c in chars:
                    x, y = c["origin"]
                    out.append(Glyph(c["c"], span["font"], span["size"], x, y, tuple(c["bbox"]), None,
                                     _DIGIT_CLASSES[c["c"]]))
    return out


def _width(g: Glyph) -> float:
    return g.bbox[2] - g.bbox[0]


def _right(g: Glyph) -> float:
    return g.bbox[2]


def _cat(g: Glyph) -> str:
    return g.cls.category if g.cls else ""


def _beam_y(b: Line, x: float) -> float:
    if abs(b.x1 - b.x0) < 1e-6:
        return b.y0
    return b.y0 + (b.y1 - b.y0) * (x - b.x0) / (b.x1 - b.x0)


def _key_alter(key: int, step: str) -> int:
    if key > 0 and step in SHARP_ORDER[:key]:
        return 1
    if key < 0 and step in FLAT_ORDER[:-key]:
        return -1
    return 0


def _digits_value(ds: list[Glyph]) -> int:
    return int("".join(str(d.cls.params[0]) for d in sorted(ds, key=lambda d: d.x)))


def _group_digits(ds: list[Glyph], sp: float) -> list[list[Glyph]]:
    """Digits side by side on one baseline form one number."""
    groups: list[list[Glyph]] = []
    for d in sorted(ds, key=lambda d: (round(d.y / sp), d.x)):
        for grp in groups:
            last = grp[-1]
            if abs(last.y - d.y) <= 0.3 * sp and 0 <= d.x - _right(last) <= 0.5 * sp:
                grp.append(d)
                break
        else:
            groups.append([d])
    return groups


@dataclass
class _Chord:
    """Noteheads on one stem (or stemless heads in one column) before pitch and rhythm are read."""

    heads: list[Glyph]
    stem: Line | None
    direction: str | None
    grace: bool
    flags: int = 0
    beams: list[Line] = field(default_factory=list)
    dots: int = 0
    tuplet: tuple[int, int] | None = None
    cue: bool = False

    @property
    def x(self) -> float:
        return min(h.x for h in self.heads)

    @property
    def right(self) -> float:
        return max(_right(h) for h in self.heads)


@dataclass
class _Rest:
    glyph: Glyph
    dots: int = 0
    tuplet: tuple[int, int] | None = None

    @property
    def x(self) -> float:
        return self.glyph.x

    @property
    def right(self) -> float:
        return _right(self.glyph)


class _StaffReader:
    """Reads one staff's measures left to right, carrying clef, key and accidental state."""

    def __init__(self, staff: Staff, geometry: PageGeometry, glyphs: list[Glyph], clef: Clef,
                 time: TimeSignature | None, ottavas: tuple[tuple[float, float, int], ...] = ()):
        self.staff = staff
        self.ottavas = ottavas
        self.sp = staff.spacing
        self.glyphs = glyphs
        self.clef = clef
        # a system always redraws its key signature, so absence means none
        self.key = 0
        self.time = time
        self.tie_carry: dict[tuple[str, int], int] = {}
        sp = self.sp
        lo, hi = staff.top - 9 * sp, staff.bottom + 9 * sp
        self.stems = [s for s in geometry.stems if lo <= s.y1 and s.y0 <= hi and staff.x0 - sp <= s.x0 <= staff.x1 + sp]
        self.beams = [b for b in geometry.beams if lo <= min(b.y0, b.y1) and max(b.y0, b.y1) <= hi]
        self.lines = [ln for ln in geometry.lines if lo <= min(ln.y0, ln.y1) and max(ln.y0, ln.y1) <= hi]
        self.curves = [c for c in geometry.curves if lo <= c.bbox[1] and c.bbox[3] <= hi]
        sizes = [g.size for g in glyphs if _cat(g) == "notehead"]
        self.head_size = statistics.median(sizes) if sizes else 0.0

    def shift(self, x: float) -> int:
        """Octaves added to written pitch at x by 8va lines: MusicXML pitch is the sounding pitch."""
        return sum(s for x0, x1, s in self.ottavas if x0 - 0.5 * self.sp <= x <= x1 + 0.5 * self.sp)

    def pos(self, g: Glyph | float) -> float:
        return self.staff.line_of(g if isinstance(g, float) else g.y)

    def clef_of(self, g: Glyph) -> Clef:
        sign, line, octave = g.cls.params
        if line is None:
            line = min(5, max(1, round(5 - self.pos(g))))
        return Clef(sign, line, octave)

    def read(self, m: Measure, k: int) -> StaffMeasureContent:
        sp = self.sp
        is_first = m.index == 0
        # A start-repeat barline opens the first bar after the clef and key, so the header lies left of it.
        left = min(m.x0, self.staff.x0) if is_first else m.x0
        gl = [g for g in self.glyphs if left - 0.3 * sp <= g.x < m.x1 - 0.1 * sp]
        warnings: list[str] = []
        # Small notes and rests far off the staff belong to tempo marks, not the music.
        gl = [g for g in gl if not (_cat(g) in ("notehead", "rest") and self.head_size
                                    and g.size < GRACE_RATIO * self.head_size and not -2.5 <= self.pos(g) <= 6.5)]
        heads = [g for g in gl if _cat(g) == "notehead"]
        rests = [g for g in gl if _cat(g) == "rest"]
        # Finale can leave a rest drawn underneath a notehead, where nobody sees it.
        rests = [r for r in rests if not any(abs(r.x - h.x) <= 0.3 * sp and abs(r.y - h.y) <= sp for h in heads)]
        first_x = min((g.x for g in heads + rests), default=m.x1)

        accs = [g for g in gl if _cat(g) == "accidental"]
        attached = self._attach_accidentals(accs, heads)

        # Header: clef, key and time ahead of the first note or rest.
        clef_shown = key_shown = time_shown = False
        clef_changes: list[tuple[float, Clef]] = []
        for g in (g for g in gl if _cat(g) == "clef"):
            if g.x < first_x:
                self.clef = self.clef_of(g)
                clef_shown = True
            else:
                clef_changes.append((g.x, self.clef_of(g)))
        start_clef = self.clef

        key_accs = [a for a in accs if id(a) not in attached and a.x < first_x and -2 <= self.pos(a) <= 6]
        if key_accs:
            alters = [a.cls.params[0] for a in key_accs]
            sharps = sum(1 for a in alters if a > 0)
            flats = sum(1 for a in alters if a < 0)
            self.key = sharps if sharps >= flats else -flats
            key_shown = True
        elif is_first:
            self.key = 0

        text_digits = [g for g in gl if _cat(g) == "digit" and g.encoding is None]
        gl = [g for g in gl if not (_cat(g) == "digit" and g.encoding is None)]
        digits = [g for g in gl if _cat(g) == "digit"]
        time_glyphs = [g for g in digits + [g for g in gl if _cat(g) == "time"]
                       if g.x < first_x and -0.6 <= self.pos(g) <= 4.6]
        if time_glyphs:
            ts = self._time_signature(time_glyphs)
            if ts:
                self.time = ts
                time_shown = True
        used_digits = {id(g) for g in time_glyphs}
        digits = [d for d in digits if id(d) not in used_digits]

        # Repeat dots: a pair at the two middle spaces hugging a barline.
        dots = [g for g in gl if _cat(g) in ("dot", "dot-or-staccato")]
        repeat_start = repeat_end = False
        rep = [g for g in gl if _cat(g) == "repeat-dots"]
        for side, edge in (("start", m.x0), ("end", m.x1)):
            near = [d for d in dots if abs(d.x - edge) <= 2.0 * sp]
            mids = [d for d in near if abs(self.pos(d) - 1.5) <= 0.35 or abs(self.pos(d) - 2.5) <= 0.35]
            hit = any(abs(self.pos(d) - 1.5) <= 0.35 for d in mids) and any(abs(self.pos(d) - 2.5) <= 0.35 for d in mids)
            hit = hit or any(abs(r.x - edge) <= 2.0 * sp for r in rep)
            if hit:
                dots = [d for d in dots if d not in mids]
                if side == "start":
                    repeat_start = True
                else:
                    repeat_end = True

        # Multi-bar rest: an empty bar with a number above the staff.
        multi = [g for g in rests if g.cls.params[0] == "multi"]
        notes_or_rests = heads + [r for r in rests if r not in multi]
        if not notes_or_rests:
            if clef_changes:
                self.clef = clef_changes[-1][1]
            above = [d for d in digits if self.pos(d) < -0.3]
            count = _digits_value(above) if above else 0
            if count and (multi or self._hbar(m, above)):
                return self._content(m, k, start_clef, clef_shown, key_shown, time_shown, (), clef_changes,
                                     repeat_start, repeat_end, warnings, multirest=count)
            return self._content(m, k, start_clef, clef_shown, key_shown, time_shown, (), clef_changes,
                                 repeat_start, repeat_end, warnings)

        chords = self._chords(heads, [g for g in gl if _cat(g) == "flag"], warnings)
        rest_items = [_Rest(r) for r in rests if r not in multi]
        self._attach_dots(dots, chords, rest_items)
        self._cues(chords, rest_items)
        self._tuplets(digits, chords, rest_items)
        self._tuplets(text_digits, chords, rest_items, fallback=False)
        events = self._events(chords, rest_items, attached, clef_changes, m, warnings)
        events = self._voices(events, warnings)
        self._carry_ties(events, m)
        if clef_changes:
            self.clef = clef_changes[-1][1]
        return self._content(m, k, start_clef, clef_shown, key_shown, time_shown, tuple(events), clef_changes,
                             repeat_start, repeat_end, warnings)

    def _content(self, m, k, clef, clef_shown, key_shown, time_shown, events, clef_changes,
                 repeat_start, repeat_end, warnings, multirest=0) -> StaffMeasureContent:
        return StaffMeasureContent(
            page=m.page, system=m.system, staff=self.staff.index, staff_in_system=k, measure=m.index,
            x0=m.x0, x1=m.x1, clef=clef, key=self.key, time=self.time, clef_shown=clef_shown,
            key_shown=key_shown, time_shown=time_shown, events=events, multirest=multirest,
            repeat_start=repeat_start, repeat_end=repeat_end, clef_changes=tuple(clef_changes),
            warnings=tuple(warnings),
        )

    def _hbar(self, m: Measure, number: list[Glyph]) -> bool:
        """The H-bar of a multi-bar rest, or failing that a number centered over the empty bar
        (geometry rejects H-bars thicker than a beam)."""
        sp = self.sp
        for ln in self.beams + self.lines:
            mid = (ln.y0 + ln.y1) / 2
            if m.x0 <= ln.x0 and ln.x1 <= m.x1 and ln.x1 - ln.x0 >= 2 * sp and 1 <= self.pos(mid) <= 3:
                return True
        cx = (min(d.x for d in number) + max(_right(d) for d in number)) / 2
        return abs(cx - (m.x0 + m.x1) / 2) <= 0.2 * (m.x1 - m.x0)

    def _time_signature(self, gs: list[Glyph]) -> TimeSignature | None:
        sym = [g for g in gs if _cat(g) == "time"]
        if sym:
            return TimeSignature(4, 4, "common") if sym[0].cls.params[0] == "common" else TimeSignature(2, 2, "cut")
        top = [g for g in gs if self.pos(g) < 2]
        bottom = [g for g in gs if self.pos(g) >= 2]
        if not top or not bottom:
            return None
        beats, beat_type = _digits_value(top), _digits_value(bottom)
        if beat_type not in (1, 2, 4, 8, 16, 32) or not beats:
            return None
        return TimeSignature(beats, beat_type)

    def _attach_accidentals(self, accs: list[Glyph], heads: list[Glyph]) -> dict[int, Glyph]:
        """Accidental id -> notehead. An accidental belongs to the head on its line just right of it,
        either directly or through a column of other accidentals stacked on the same chord."""
        sp = self.sp
        attached: dict[int, Glyph] = {}
        # Leftmost thing each accidental must reach: a head or an already attached accidental.
        anchors: list[Glyph] = list(heads)
        pending = sorted(accs, key=lambda a: -a.x)
        changed = True
        while changed:
            changed = False
            for a in pending:
                if id(a) in attached:
                    continue
                same_line = [h for h in heads if abs(h.y - a.y) <= 0.3 * sp and 0 < h.x - a.x <= 7 * sp
                             and not any(attached.get(id(o)) is h for o in accs)]
                if not same_line:
                    continue
                gap = min((o.x - _right(a) for o in anchors if o.x > a.x - 0.1 and abs(o.y - a.y) <= 4.5 * sp),
                          default=99.0)
                if gap <= 0.9 * sp:
                    attached[id(a)] = min(same_line, key=lambda h: h.x)
                    anchors.append(a)
                    changed = True
        return attached

    def _chords(self, heads: list[Glyph], flags: list[Glyph], warnings: list[str]) -> list[_Chord]:
        sp = self.sp
        by_stem: dict[int, list[Glyph]] = {}
        stemless: list[Glyph] = []
        for h in heads:
            kind = h.cls.params[0]
            if kind in ("whole", "breve"):
                stemless.append(h)
                continue
            cands = []
            for i, s in enumerate(self.stems):
                tol = 0.3 * sp + s.width
                dx = min(abs(s.x0 - h.x), abs(s.x0 - _right(h)))
                if dx > tol or not (s.y0 - 0.6 * sp <= h.y <= s.y1 + 0.6 * sp):
                    continue
                dend = min(abs(h.y - s.y0), abs(h.y - s.y1))
                cands.append(((round(dx / (0.1 * sp)), dend), i))
            cands.sort()
            # Two voices on one pitch draw two heads, one per stem; keep them on separate stems.
            free = [i for _, i in cands if not any(abs(o.y - h.y) <= 0.2 * sp for o in by_stem.get(i, ()))]
            if free:
                by_stem.setdefault(free[0], []).append(h)
            elif cands:
                by_stem.setdefault(cands[0][1], []).append(h)
            else:
                stemless.append(h)

        chords: list[_Chord] = []
        for i, hs in by_stem.items():
            s = self.stems[i]
            lo, hi = min(h.y for h in hs), max(h.y for h in hs)
            if hi >= s.y1 - 0.8 * sp and lo > s.y0 + 0.8 * sp:
                direction = "up"
            elif lo <= s.y0 + 0.8 * sp and hi < s.y1 - 0.8 * sp:
                direction = "down"
            else:
                direction = "up" if abs(hi - s.y1) < abs(lo - s.y0) else "down"
            chords.append(_Chord(sorted(hs, key=lambda h: h.y), s, direction, self._grace(hs)))
        # Stemless heads in one column (a second displaced by a head width) make one chord.
        stemless.sort(key=lambda h: (h.x, h.y))
        cols: list[list[Glyph]] = []
        for h in stemless:
            if cols and h.x - cols[-1][0].x <= 1.3 * _width(cols[-1][0]) and h.cls == cols[-1][0].cls:
                cols[-1].append(h)
            else:
                cols.append([h])
        for col in cols:
            if col[0].cls.params[0] not in ("whole", "breve"):
                warnings.append(f"stemless {col[0].cls.params[0]} notehead at x={col[0].x:.1f}")
            chords.append(_Chord(sorted(col, key=lambda h: h.y), None, None, self._grace(col)))

        for f in flags:
            cands = [c for c in chords if c.stem is not None
                     and abs(f.x - c.stem.x0) <= 0.6 * sp
                     and c.stem.y0 - 2 * sp <= f.y <= c.stem.y1 + 2 * sp]
            if cands:
                c = min(cands, key=lambda c: abs(f.x - c.stem.x0))
                c.flags = max(c.flags, f.cls.params[0])
        for c in chords:
            if c.stem is None:
                continue
            s = c.stem
            hit: list[Line] = []
            for b in self.beams:
                if not (min(b.x0, b.x1) - 0.3 - s.width <= s.x0 <= max(b.x0, b.x1) + 0.3 + s.width):
                    continue
                y = _beam_y(b, s.x0)
                if c.direction == "up":
                    ok = s.y0 - 0.6 * sp <= y <= s.y0 + 3.5 * sp and y < s.y1 - 0.5 * sp
                else:
                    ok = s.y1 - 3.5 * sp <= y <= s.y1 + 0.6 * sp and y > s.y0 + 0.5 * sp
                if ok and not any(abs(_beam_y(o, s.x0) - y) <= 0.5 * b.width for o in hit):
                    hit.append(b)
            c.beams = hit
        return chords

    def _grace(self, hs: list[Glyph]) -> bool:
        return bool(self.head_size) and max(h.size for h in hs) < GRACE_RATIO * self.head_size

    def _attach_dots(self, dots: list[Glyph], chords: list[_Chord], rests: list[_Rest]) -> None:
        """Augmentation dots sit right of a notehead or rest, level with it or a half space above."""
        sp = self.sp
        items: list[_Chord | _Rest] = [*chords, *rests]
        counts: dict[int, dict[int, int]] = {}
        for d in sorted(dots, key=lambda d: d.x):
            best = None
            for idx, it in enumerate(items):
                gap = d.x - it.right
                if not (-0.2 * sp <= gap <= 3.0 * sp):
                    continue
                if isinstance(it, _Chord):
                    dy = min(abs(d.y - h.y) for h in it.heads)
                    if dy > 0.8 * sp:
                        continue
                else:
                    dy = abs(d.y - it.glyph.y)
                    if dy > 1.6 * sp:
                        continue
                score = (gap, dy)
                if best is None or score < best[0]:
                    best = (score, idx)
            if best is None:
                continue
            row = round(d.y / (0.5 * sp))
            counts.setdefault(best[1], {}).setdefault(row, 0)
            counts[best[1]][row] += 1
        for idx, rows in counts.items():
            items[idx].dots = min(3, max(rows.values()))

    def _cues(self, chords: list[_Chord], rests: list[_Rest]) -> None:
        """Small notes are grace notes unless they keep time: unflagged, or needed to fill the bar."""
        small = [c for c in chords if c.grace]
        if not small:
            return
        bar = self.time.quarters if self.time else None
        normal = sum((_duration(_chord_type(c), c.dots, None) for c in chords if not c.grace), Fraction(0))
        normal += sum((_duration(r.glyph.cls.params[0], r.dots, None) for r in rests), Fraction(0))
        short = bar is not None and normal < bar
        for c in small:
            if short or (c.flags == 0 and not c.beams):
                c.grace, c.cue = False, True

    def _tuplets(self, digits: list[Glyph], chords: list[_Chord], rests: list[_Rest], fallback: bool = True) -> None:
        sp = self.sp
        items: list[_Chord | _Rest] = sorted([*[c for c in chords if not c.grace], *rests], key=lambda i: i.x)
        for grp in _group_digits(digits, sp):
            n = _digits_value(grp)
            if n not in TUPLET_NORMAL:
                continue
            dx = (min(d.x for d in grp) + max(_right(d) for d in grp)) / 2
            dy = grp[0].y
            ratio = (n, TUPLET_NORMAL[n])
            target: list[_Chord | _Rest] = []
            beams = [b for b in self.beams
                     if min(b.x0, b.x1) - sp <= dx <= max(b.x0, b.x1) + sp and abs(_beam_y(b, dx) - dy) <= 3.5 * sp]
            if beams:
                b = min(beams, key=lambda b: abs(_beam_y(b, dx) - dy))
                target = [c for c in chords if b in c.beams and not c.grace]
            if not target:
                left = [ln for ln in self.lines if abs(ln.y1 - dy) <= 2 * sp and dx - 4 * sp <= ln.x1 <= dx + 0.5 * sp]
                right = [ln for ln in self.lines if abs(ln.y0 - dy) <= 2 * sp and dx - 0.5 * sp <= ln.x0 <= dx + 4 * sp]
                if left and right:
                    x0 = min(ln.x0 for ln in left) - 0.5 * sp
                    x1 = max(ln.x1 for ln in right) + 0.5 * sp
                    target = [i for i in items if x0 <= i.x <= x1]
            if not target and fallback and len(items) >= n:
                windows = [items[j:j + n] for j in range(len(items) - n + 1)]
                target = min(windows, key=lambda w: abs((w[0].x + w[-1].right) / 2 - dx))
            for it in target:
                if it.tuplet is None:
                    it.tuplet = ratio

    def _events(self, chords: list[_Chord], rests: list[_Rest], attached: dict[int, Glyph],
                clef_changes: list[tuple[float, Clef]], m: Measure, warnings: list[str]) -> list[Event]:
        sp = self.sp
        acc_of: dict[int, int] = {}
        for a in (g for g in self.glyphs if id(g) in attached):
            acc_of[id(attached[id(a)])] = a.cls.params[0]
        measure_alters: dict[tuple[str, int], int] = {}
        first_x = min([c.x for c in chords] + [r.x for r in rests], default=m.x1)
        events: list[Event] = []
        for c in sorted(chords, key=lambda c: (c.x, c.heads[0].y)):
            clef = self.clef
            for x, cl in clef_changes:
                if x < c.x:
                    clef = cl
            notes = []
            shift = self.shift(c.x)
            for h in c.heads:
                pos = self.pos(h)
                d = clef.bottom_line() + round((4 - pos) * 2)
                step, octave = STEPS[d % 7], d // 7
                acc = acc_of.get(id(h))
                if acc is not None:
                    alter = acc
                    measure_alters[(step, octave)] = acc
                elif (step, octave) in measure_alters:
                    alter = measure_alters[(step, octave)]
                elif (step, octave) in self.tie_carry and c.x - first_x <= 0.5 * sp:
                    alter = self.tie_carry[(step, octave)]
                else:
                    alter = _key_alter(self.key, step)
                notes.append(Note(step, octave + shift, alter, acc, round(pos * 2) / 2, h.x, h.y, h.cls.params[0]))
            ntype = _chord_type(c)
            dur = Fraction(0) if c.grace else _duration(ntype, c.dots, c.tuplet)
            events.append(Event(c.x, statistics.mean(h.y for h in c.heads), ntype, c.dots, dur,
                                tuple(sorted(notes, key=lambda n: (n.midi, n.y))), stem=c.direction,
                                grace=c.grace, cue=c.cue, tuplet=c.tuplet))
        self.tie_carry = {}
        bar = self.time.quarters if self.time else None
        for r in rests:
            rtype = r.glyph.cls.params[0]
            dur = _duration(rtype, r.dots, r.tuplet)
            events.append(Event(r.x, r.glyph.y, rtype, r.dots, dur, tuplet=r.tuplet))
        events.sort(key=lambda e: (e.x, e.y))
        # A whole rest alone in the bar fills it, whatever the meter.
        sounding = [e for e in events if not e.grace]
        if len(sounding) == 1 and sounding[0].is_rest and sounding[0].type in ("whole", "breve") and bar:
            e = sounding[0]
            events = [_replace(e, duration=bar, measure_rest=True) if x is e else x for x in events]
        elif bar and any(e.is_rest and e.type == "whole" for e in sounding):
            # whole rests are also bar rests for one voice of a two-voice bar
            events = [_replace(e, duration=bar, measure_rest=True) if e.is_rest and e.type == "whole" and not e.dots
                      else e for e in events]
        return events

    def _voices(self, events: list[Event], warnings: list[str]) -> list[Event]:
        """Split a staff into two voices by stem direction when events stack or overfill the bar."""
        sp = self.sp
        idx = [i for i, e in enumerate(events) if not e.grace]
        tol = 0.9 * sp
        coincide = [(i, j) for n, i in enumerate(idx) for j in idx[n + 1:] if abs(events[i].x - events[j].x) <= tol]
        bar = self.time.quarters if self.time else None
        total = sum((events[i].duration for i in idx), Fraction(0))
        directions = {events[i].stem for i in idx if events[i].stem}
        if not coincide and not (bar and total > bar and len(directions) == 2):
            return _onsets(events, tol)

        def voice_of(i: int) -> int:
            e = events[i]
            if e.stem == "up":
                return 1
            if e.stem == "down":
                return 2
            partners = [events[b if a == i else a] for a, b in coincide if i in (a, b)]
            if e.is_rest:
                pos = self.pos(e.y)
                if pos < 1.25:
                    return 1
                if pos > 2.75:
                    return 2
            if partners:
                p = partners[0]
                if p.stem == "up":
                    return 2
                if p.stem == "down":
                    return 1
                return 1 if e.y < p.y else 2
            return 1

        voices = [1 if e.grace and e.stem != "down" else voice_of(i) for i, e in enumerate(events)]
        if any(voices[a] == voices[b] for a, b in coincide):
            warnings.append("more than two voices")
        return _onsets([_replace(e, voice=v) for e, v in zip(events, voices)], tol)

    def _carry_ties(self, events: list[Event], m: Measure) -> None:
        """Altered notes tied over the barline keep their alter in the next bar."""
        sp = self.sp
        carry: dict[tuple[str, int], int] = {}
        last_x = max((e.x for e in events if e.notes), default=None)
        if last_x is None:
            self.tie_carry = carry
            return
        for e in events:
            if not e.notes or e.x < last_x - 3 * sp:
                continue
            for n in e.notes:
                if n.alter == _key_alter(self.key, n.step):
                    continue
                for c in self.curves:
                    x0, y0, x1, y1 = c.bbox
                    if n.x - sp <= x0 <= n.x + 4 * sp and x1 >= m.x1 - sp and min(abs(y0 - n.y), abs(y1 - n.y)) <= 2 * sp:
                        carry[(n.step, n.octave - self.shift(n.x))] = n.alter
                        break
        self.tie_carry = carry


_HEAD_ORDER = ("black", "x", "circle-x", "slash", "diamond-black", "half", "slash-white", "diamond-white",
               "whole", "breve")


def _chord_type(c: _Chord) -> str:
    kinds = [h.cls.params[0] for h in c.heads]
    kind = max(kinds, key=lambda k: _HEAD_ORDER.index(k) if k in _HEAD_ORDER else 0)
    if kind in ("whole", "breve"):
        return kind
    if kind in ("half", "slash-white", "diamond-white"):
        return "half"
    return _BEAMED[min(len(_BEAMED) - 1, max(c.flags, len(c.beams)))]


def _onsets(events: list[Event], tol: float) -> list[Event]:
    """Voice 1 runs in sequence; a later voice takes the onset of the voice-1 event it lines up with."""
    onset: dict[int, Fraction] = {}
    pos: dict[int, Fraction] = {}
    anchors: list[tuple[float, Fraction]] = []
    order = sorted(range(len(events)), key=lambda i: (events[i].voice, events[i].x, events[i].y))
    for i in order:
        e = events[i]
        if e.grace:
            continue
        at = pos.get(e.voice, Fraction(0))
        if e.measure_rest:
            at = Fraction(0)
        elif e.voice > 1:
            partner = [o for x, o in anchors if abs(x - e.x) <= tol and o >= at]
            if partner:
                at = min(partner)
        else:
            anchors.append((e.x, at))
        onset[i] = at
        pos[e.voice] = at + e.duration
    return [_replace(e, onset=onset.get(i)) for i, e in enumerate(events)]


def _duration(ntype: str, dots: int, tuplet: tuple[int, int] | None) -> Fraction:
    base = NOTE_QUARTERS.get(ntype, Fraction(1))
    d = base * (2 - Fraction(1, 2 ** dots))
    if tuplet:
        d = d * tuplet[1] / tuplet[0]
    return d


def _replace(e: Event, **kw) -> Event:
    return replace(e, **kw)


def flatten(contents: Iterable[StaffMeasureContent]) -> Iterator[tuple[StaffMeasureContent, Event]]:
    for c in contents:
        for e in c.events:
            yield c, e
