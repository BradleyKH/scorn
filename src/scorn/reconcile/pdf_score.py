"""The whole score read from a born-digital PDF: systems, numbered measures, notes, marks and text.

Every staff-measure is keyed by (page, staff, measure), where `staff` is the page-wide
`Staff.index` and `measure` the measure's index within its system, the same key the
readers use (`StaffMeasureContent.page/.staff/.measure`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import pymupdf

from . import geometry as geo
from . import pdf_marks, pdf_notes, pdf_text
from .glyphs import document_fonts, page_glyphs
from .model import Staff

Key = tuple[int, int, int]

# Share of drawn pages that may yield no systems before the PDF counts as unreadable.
_MAX_BLANK_SHARE = 0.25
_VOLTA = re.compile(r"^\d(?:\s*[.,]\s*\d)*\.?$")


@dataclass(frozen=True)
class NotApplicable:
    """The PDF has no readable music text layer (a scan, outlined notation or an unknown font)."""

    reason: str


@dataclass(frozen=True)
class PdfMeasure:
    # 0-based position among drawn bars through the piece
    index: int
    # bar number: a pickup is 0 and the next bar 1; a multi-bar rest takes the first of its numbers
    number: int
    # bars this drawn bar stands for: the multi-bar rest count, else 1
    span: int
    page: int
    # page-local System.index, and the system's 0-based position through the piece
    system: int
    system_order: int
    # index within the system, as in StaffMeasureContent.measure
    measure: int
    x0: float
    x1: float
    pickup: bool = False
    repeat_start: bool = False
    repeat_end: bool = False
    # volta number text such as "1." or "1, 2." when an ending bracket covers this bar
    ending: str | None = None
    ending_start: bool = False
    ending_stop: bool = False
    # bar number printed at this bar, when one is
    printed: int | None = None

    def key(self, staff: int) -> Key:
        return (self.page, staff, self.measure)


@dataclass(frozen=True)
class PdfSystem:
    order: int
    page: int
    index: int
    staves: tuple[Staff, ...]
    measures: tuple[PdfMeasure, ...]
    labels: tuple[pdf_text.StaffLabel, ...]

    def labels_of(self, staff: int) -> tuple[pdf_text.StaffLabel, ...]:
        return tuple(lb for lb in self.labels if lb.staff == staff)


@dataclass
class PdfScore:
    path: Path
    geometries: tuple[geo.PageGeometry, ...]
    systems: tuple[PdfSystem, ...]
    measures: tuple[PdfMeasure, ...]
    text: pdf_text.DocumentText
    contents: dict[Key, pdf_notes.StaffMeasureContent]
    marks: tuple[pdf_marks.Mark, ...]
    # ties and slurs broken across a system break: (piece ending the system, piece starting the next)
    curve_pairs: tuple[tuple[pdf_marks.Mark, pdf_marks.Mark], ...]
    warnings: tuple[str, ...] = ()
    _buckets: dict[str, dict[Key, list]] = field(default_factory=dict, repr=False)

    @property
    def bar_count(self) -> int:
        """Bars in the piece, counting a multi-bar rest as its span."""
        return sum(m.span for m in self.measures)

    def system_of(self, page: int, staff: int) -> PdfSystem | None:
        for s in self.systems:
            if s.page == page and any(st.index == staff for st in s.staves):
                return s
        return None

    def content(self, measure: PdfMeasure, staff: int) -> pdf_notes.StaffMeasureContent | None:
        return self.contents.get(measure.key(staff))

    def measure_at(self, page: int, staff: int, x: float) -> PdfMeasure | None:
        """The measure of `staff`'s system holding x; x left of the first bar belongs to the first,
        right of the last to the last."""
        system = self.system_of(page, staff)
        if system is None or not system.measures:
            return None
        for m in system.measures:
            if m.x0 <= x < m.x1:
                return m
        return system.measures[0] if x < system.measures[0].x0 else system.measures[-1]

    def marks_in(self, key: Key) -> list[pdf_marks.Mark]:
        return self._buckets["marks"].get(key, [])

    def lyrics_in(self, key: Key) -> list[pdf_text.Syllable]:
        return self._buckets["lyrics"].get(key, [])

    def directions_in(self, key: Key) -> list[pdf_text.Direction]:
        return self._buckets["directions"].get(key, [])

    def tempos_in(self, key: Key) -> list[pdf_text.TempoMark]:
        return self._buckets["tempos"].get(key, [])

    def rehearsals_in(self, key: Key) -> list[pdf_text.RehearsalMark]:
        return self._buckets["rehearsals"].get(key, [])


def read_pdf(pdf: Path | str) -> PdfScore | NotApplicable:
    path = Path(pdf)
    with pymupdf.open(path) as doc:
        fonts = document_fonts(doc)
        if not any(enc is not None for enc in fonts.values()):
            return NotApplicable("no font draws music characters (scan, outlined notation or unknown font)")
        geometries = geo.read_document(doc)
        drawn = [g for page, g in zip(doc, geometries) if page.get_cdrawings()]
        blank = sum(1 for g in drawn if not g.systems)
        if not any(g.systems for g in geometries):
            return NotApplicable("no staff systems found")
        if drawn and blank > _MAX_BLANK_SHARE * len(drawn):
            return NotApplicable(f"{blank} of {len(drawn)} drawn pages have no staff systems")
        notes = pdf_notes.read_document_notes(doc, geometries)
        text = pdf_text.read_document_text(doc, geometries)
        marks: list[pdf_marks.Mark] = []
        for page, g in zip(doc, geometries):
            marks += pdf_marks.read_marks(page, g, list(page_glyphs(page, fonts)))
    pairs = pdf_marks.pair_broken(marks, {g.page: g for g in geometries})
    return _assemble(path, tuple(geometries), notes, text, marks, pairs)


def _assemble(path: Path, geometries: tuple[geo.PageGeometry, ...], notes: list[pdf_notes.StaffMeasureContent],
              text: pdf_text.DocumentText, marks: list[pdf_marks.Mark], pairs) -> PdfScore:
    contents = {(c.page, c.staff, c.measure): c for c in notes}
    warnings: list[str] = []
    labels_by_system: dict[tuple[int, int], list[pdf_text.StaffLabel]] = defaultdict(list)
    for tc in text.pages:
        for lb in tc.labels:
            labels_by_system[(tc.page, lb.system)].append(lb)

    systems: list[PdfSystem] = []
    measures: list[PdfMeasure] = []
    number = 1
    for g in geometries:
        for sy in g.systems:
            order = len(systems)
            ms = sorted((m for m in g.measures if m.system == sy.index), key=lambda m: m.x0)
            row: list[PdfMeasure] = []
            for m in ms:
                cells = [contents.get((g.page, st.index, m.index)) for st in sy.staves]
                cells = [c for c in cells if c is not None]
                span = max((c.multirest for c in cells), default=0) or 1
                pm = PdfMeasure(
                    index=len(measures), number=number, span=span, page=g.page, system=sy.index,
                    system_order=order, measure=m.index, x0=m.x0, x1=m.x1,
                    repeat_start=any(c.repeat_start for c in cells), repeat_end=any(c.repeat_end for c in cells),
                )
                measures.append(pm)
                row.append(pm)
                number += span
            systems.append(PdfSystem(order, g.page, sy.index, sy.staves, tuple(row),
                                     tuple(labels_by_system.get((g.page, sy.index), ()))))

    measures = _pickup(measures, contents, systems)
    measures = _voltas(measures, geometries, text, systems)
    measures, mismatches = _printed_numbers(measures, text, systems)
    warnings += mismatches
    by_index = {m.index: m for m in measures}
    systems = [PdfSystem(s.order, s.page, s.index, s.staves, tuple(by_index[m.index] for m in s.measures), s.labels)
               for s in systems]

    score = PdfScore(path, geometries, tuple(systems), tuple(measures), text, contents, tuple(marks), tuple(pairs),
                     tuple(warnings))
    buckets: dict[str, dict[Key, list]] = {k: defaultdict(list) for k in
                                           ("marks", "lyrics", "directions", "tempos", "rehearsals")}

    def put(kind: str, page: int, staff: int, x: float, item) -> None:
        m = score.measure_at(page, staff, x)
        if m is not None:
            buckets[kind][m.key(staff)].append(item)

    for mk in marks:
        put("marks", mk.page, mk.staff, mk.x, mk)
    for tc in text.pages:
        for s in tc.lyrics:
            put("lyrics", s.page, s.staff, s.x0, s)
        for d in tc.directions:
            put("directions", d.page, d.staff, d.x, d)
        for t in tc.tempos:
            put("tempos", t.page, t.staff, t.x, t)
        for r in tc.rehearsals:
            put("rehearsals", r.page, r.staff, r.x, r)
    score._buckets = {k: dict(v) for k, v in buckets.items()}
    return score


def _voice_fill(c: pdf_notes.StaffMeasureContent) -> Fraction:
    totals: dict[int, Fraction] = defaultdict(Fraction)
    for e in c.events:
        if not e.grace:
            totals[e.voice] += e.duration
    return max(totals.values(), default=Fraction(0))


def _pickup(measures: list[PdfMeasure], contents, systems: list[PdfSystem]) -> list[PdfMeasure]:
    """A first bar whose fullest staff falls short of the time signature is a pickup, numbered 0."""
    if not measures or measures[0].span > 1:
        return measures
    first = measures[0]
    cells = [contents.get(first.key(st.index)) for st in systems[0].staves]
    cells = [c for c in cells if c is not None and c.time is not None]
    if not cells:
        return measures
    fill = max(_voice_fill(c) for c in cells)
    bar = cells[0].time.quarters
    if not 0 < fill < bar:
        return measures
    out = [_with(first, number=0, pickup=True)]
    out += [_with(m, number=m.number - 1) for m in measures[1:]]
    return out


def _with(m: PdfMeasure, **kw) -> PdfMeasure:
    return PdfMeasure(**{**m.__dict__, **kw})


def _voltas(measures: list[PdfMeasure], geometries, text: pdf_text.DocumentText,
            systems: list[PdfSystem]) -> list[PdfMeasure]:
    """Ending brackets: a horizontal line over a system's top staff with a "1." style numeral
    just under its left end; the ending covers every bar the line spans."""
    endings: dict[int, tuple[str, bool, bool]] = {}
    for s in systems:
        g = geometries[s.page]
        top = s.staves[0]
        sp = top.spacing
        tc = text.pages[s.page]
        numerals = [(d.text, d.x, d.y) for d in tc.directions if d.system == s.index and d.placement == "above"]
        numerals += [(sp_.text, sp_.x, sp_.y) for sp_ in tc.other
                     if top.top - 8 * sp <= sp_.y <= top.top and s.x0 <= sp_.x <= s.x1]
        numerals = [(t.strip(), x, y) for t, x, y in numerals if _VOLTA.match(t.strip())]
        if not numerals:
            continue
        flat = [ln for ln in g.lines if abs(ln.y1 - ln.y0) <= 0.5 and ln.x1 - ln.x0 >= 2 * sp
                and top.top - 10 * sp <= ln.y0 < top.top]
        for label, x, y in sorted(numerals, key=lambda n: n[1]):
            lines = [ln for ln in flat if 0 < y - ln.y0 <= 3 * sp and abs(ln.x0 - x) <= 2 * sp]
            if not lines:
                continue
            ln = min(lines, key=lambda ln: (abs(ln.x0 - x), ln.y0))
            covered = [m for m in s.measures if m.x1 > ln.x0 + 0.5 * sp and m.x0 < ln.x1 - 0.5 * sp]
            for i, m in enumerate(covered):
                endings[m.index] = (label, i == 0, i == len(covered) - 1)
    if not endings:
        return measures
    return [_with(m, ending=endings[m.index][0], ending_start=endings[m.index][1], ending_stop=endings[m.index][2])
            if m.index in endings else m for m in measures]


def _printed_numbers(measures: list[PdfMeasure], text: pdf_text.DocumentText,
                     systems: list[PdfSystem]) -> tuple[list[PdfMeasure], list[str]]:
    """Attach printed bar numbers and report the ones that disagree with the count."""
    printed: dict[int, int] = {}
    for tc in text.pages:
        for mn in tc.measure_numbers:
            s = next((s for s in systems if s.page == tc.page and s.index == mn.system), None)
            if s is None or not s.measures:
                continue
            m = next((m for m in s.measures if m.x0 <= mn.x < m.x1), None)
            if m is None:
                m = s.measures[0] if mn.x < s.measures[0].x0 else s.measures[-1]
            printed.setdefault(m.index, mn.number)
    warnings = []
    for m in measures:
        n = printed.get(m.index)
        if n is not None and not m.number <= n < m.number + m.span:
            warnings.append(f"page {m.page + 1} system {m.system + 1} bar {m.measure + 1}: "
                            f"printed number {n}, counted {m.number}")
    return [_with(m, printed=printed[m.index]) if m.index in printed else m for m in measures], warnings


__all__ = ["Key", "NotApplicable", "PdfMeasure", "PdfSystem", "PdfScore", "read_pdf"]
