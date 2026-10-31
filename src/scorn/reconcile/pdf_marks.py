"""Markings, ties and slurs read from a born-digital score page.

Marks come from music-font glyphs (dynamics, articulations, ornaments, fermatas,
tremolos, ...) and from vector geometry (hairpins, ties, slurs). Every mark is
attached to a staff on its page; spanners carry an end x.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import pymupdf

from .geometry import PageGeometry
from .glyphs import Glyph
from .model import Curve, Staff

# Marks that map one glyph to one Mark; value is the Mark kind.
_GLYPH_KINDS = {
    "dynamic": "dynamic",
    "articulation": "articulation",
    "ornament": "ornament",
    "fermata": "fermata",
    "breath-mark": "breath-mark",
    "caesura": "caesura",
    "pedal": "pedal",
    "arpeggio": "arpeggio",
}

_GROUPED = set(_GLYPH_KINDS) | {"tremolo", "dot-or-staccato"}

# Dynamics a font builds from letters, joined when the letters sit side by side.
_DYNAMIC_WORDS = {"mf", "mp", "sf", "sfz", "sfp", "fp", "fz", "rf", "rfz", "pp", "ppp", "ff", "fff"}


@dataclass(frozen=True)
class Mark:
    """One marking on a page.

    kind: dynamic, hairpin, articulation, ornament, tremolo, fermata,
    breath-mark, caesura, pedal, arpeggio, tie, slur.
    name: MusicXML-style detail (dynamic name, crescendo/diminuendo, staccato,
    trill-mark, upright/inverted, start/stop); for tremolo the stroke count is in `value`.
    x is the left end (glyph center for point marks); x_end is set for spanners.
    open_start / open_end flag a tie or slur broken at a system edge.
    """

    kind: str
    name: str
    page: int
    staff: int
    x: float
    y: float
    x_end: float | None = None
    value: int | None = None
    # above or below the staff for marks that can be either; None when inside
    placement: str | None = None
    open_start: bool = False
    open_end: bool = False
    # y of both tips for curves, same-pitch evidence for ties
    y_end: float | None = None
    detail: dict = field(default_factory=dict, compare=False, hash=False)

    def __hash__(self) -> int:  # detail is a dict
        return hash((self.kind, self.name, self.page, self.staff, self.x, self.y, self.x_end))


@dataclass(frozen=True)
class _Head:
    x: float
    y: float
    staff: int
    width: float


def _center(g: Glyph) -> float:
    return (g.bbox[0] + g.bbox[2]) / 2


def _gap_distance(staff: Staff, y: float) -> float:
    if y < staff.top:
        return staff.top - y
    if y > staff.bottom:
        return y - staff.bottom
    return 0.0


def _staves_at(staves: tuple[Staff, ...], x: float) -> list[Staff]:
    pad = 2.0
    cand = [s for s in staves if s.x0 - pad <= x <= s.x1 + pad]
    return cand or list(staves)


def _lyric_staves(page: pymupdf.Page, staves: tuple[Staff, ...], music_fonts: set[str]) -> set[int]:
    """Staves with at least two plain-text spans in the band just below them."""
    counts: dict[int, int] = defaultdict(int)
    below = sorted(staves, key=lambda s: s.top)
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                if span["font"] in music_fonts or not span["text"].strip():
                    continue
                # italic and bold words are directions, not lyrics
                if span["flags"] & (2 | 16):
                    continue
                x0, y0, x1, y1 = span["bbox"]
                yc = (y0 + y1) / 2
                for s in below:
                    if s.x0 + 2 < (x0 + x1) / 2 < s.x1 and s.bottom < yc < s.bottom + 5 * s.spacing + 6:
                        # a span inside the next staff's band belongs to that staff
                        if any(t.top <= yc <= t.bottom for t in below):
                            continue
                        counts[s.index] += 1
                        break
    return {i for i, n in counts.items() if n >= 2}


def assign_staff(staves: tuple[Staff, ...], x: float, y: float, lyric_staves: set[int]) -> Staff | None:
    """Staff for a mark at (x, y).

    The nearest staff wins, except that a mark in the lower half of a gap goes to the staff above
    when that staff carries no lyrics: choir marks sit above their staff to clear the lyrics,
    instrument marks sit below theirs."""
    if not staves:
        return None
    cand = _staves_at(staves, x)
    for s in cand:
        if s.top <= y <= s.bottom:
            return s
    above = [s for s in cand if s.bottom < y]
    below = [s for s in cand if s.top > y]
    up = max(above, key=lambda s: s.bottom) if above else None
    down = min(below, key=lambda s: s.top) if below else None
    if up is None:
        return down
    if down is None:
        return up
    gap = down.top - up.bottom
    in_lower_half = y > up.bottom + gap / 2
    if in_lower_half and up.index not in lyric_staves:
        # only when the mark is not hugging the staff below; choir marks ride higher above a
        # lyric staff to clear the text, so that staff claims marks up to 3 spaces above it
        hug = (3.0 if down.index in lyric_staves else 1.5) * down.spacing
        if y - up.bottom <= 3 * up.spacing or _gap_distance(down, y) > hug:
            return up
    return up if (y - up.bottom) <= (down.top - y) else down


def _placement(staff: Staff, y: float) -> str | None:
    if y < staff.top:
        return "above"
    if y > staff.bottom:
        return "below"
    return None


def _staff_of_head(staves: tuple[Staff, ...], x: float, y: float) -> Staff | None:
    best, best_d = None, 3.5
    for s in staves:
        if not (s.x0 - 2 <= x <= s.x1 + 2):
            continue
        d = _gap_distance(s, y) / s.spacing
        if d < best_d:
            best, best_d = s, d
    return best


def _heads(glyphs: list[Glyph], staves: tuple[Staff, ...]) -> list[_Head]:
    out = []
    for g in glyphs:
        if g.cls is None or g.cls.category != "notehead":
            continue
        cx = _center(g)
        s = _staff_of_head(staves, cx, g.y)
        if s is not None:
            out.append(_Head(cx, g.y, s.index, g.bbox[2] - g.bbox[0]))
    return sorted(out, key=lambda h: (h.staff, h.x, h.y))


def _join_dynamics(gs: list[Glyph]) -> list[tuple[str, Glyph, float]]:
    """(name, first glyph, center x) with letter parts joined into words."""
    items = sorted(gs, key=lambda g: (round(g.y), g.x))
    out: list[tuple[str, Glyph, float]] = []
    i = 0
    while i < len(items):
        g = items[i]
        name = g.cls.params[0]
        j = i
        if len(name) == 1:
            # letters that make a word: extend while the next letter abuts
            word, last = name, g
            k = i + 1
            while k < len(items) and len(items[k].cls.params[0]) == 1 \
                    and abs(items[k].y - g.y) < 1.0 and 0 <= items[k].x - last.bbox[2] < 0.35 * g.size:
                word += items[k].cls.params[0]
                last = items[k]
                k += 1
            if word in _DYNAMIC_WORDS:
                out.append((word, g, (g.bbox[0] + last.bbox[2]) / 2))
                i = k
                continue
        out.append((name, g, _center(g)))
        i = j + 1
    return out


def _glyph_marks(geom: PageGeometry, glyphs: list[Glyph], lyric: set[int], heads: list[_Head]) -> list[Mark]:
    marks: list[Mark] = []
    staves = geom.staves
    groups: dict[str, list[Glyph]] = defaultdict(list)
    for g in glyphs:
        if g.cls is not None and g.cls.category in _GROUPED:
            groups[g.cls.category].append(g)

    for name, g, cx in _join_dynamics(groups["dynamic"]):
        s = assign_staff(staves, cx, g.y, lyric)
        if s:
            marks.append(Mark("dynamic", name, geom.page, s.index, cx, g.y, placement=_placement(s, g.y)))

    for cat in ("ornament", "fermata", "breath-mark", "caesura", "pedal"):
        for g in groups[cat]:
            cx = _center(g)
            near = _nearest_staff(staves, cx, g.y)
            if near:
                label = g.cls.params[0] if g.cls.params else cat
                marks.append(Mark(cat, str(label), geom.page, near.index, cx, g.y,
                                  placement=_placement(near, g.y)))

    for x, y0, y1, staff in _arpeggios(groups["arpeggio"], staves):
        marks.append(Mark("arpeggio", "arpeggio", geom.page, staff, x, y0, y_end=y1))

    for g in groups["articulation"]:
        cx = _center(g)
        near = _nearest_staff(staves, cx, g.y)
        if near:
            marks.append(Mark("articulation", g.cls.params[0], geom.page, near.index, cx, g.y,
                              placement=_placement(near, g.y)))

    marks.extend(_staccato_dots(geom, groups["dot-or-staccato"], heads))
    marks.extend(_tremolos(geom, groups["tremolo"]))
    return marks


def _nearest_staff(staves: tuple[Staff, ...], x: float, y: float) -> Staff | None:
    cand = _staves_at(staves, x)
    if not cand:
        return None
    return min(cand, key=lambda s: (_gap_distance(s, y), s.index))


def _arpeggios(gs: list[Glyph], staves: tuple[Staff, ...]):
    """Arpeggio glyphs are drawn as vertical stacks of segments; merge each stack."""
    out = []
    for g in sorted(gs, key=lambda g: (round(_center(g), 1), g.y)):
        cx = _center(g)
        if out and abs(out[-1][0] - cx) < 0.5 and g.y - out[-1][2] < 2.5 * (g.size / 4):
            x, y0, _, _ = out[-1]
            out[-1] = (x, y0, g.y, None)
        else:
            out.append((cx, g.y, g.y, None))
    res = []
    for x, y0, y1, _ in out:
        s = _nearest_staff(staves, x, (y0 + y1) / 2)
        if s:
            res.append((x, y0, y1, s.index))
    return res


def _staccato_dots(geom: PageGeometry, dots: list[Glyph], heads: list[_Head]) -> list[Mark]:
    """A dot centered over or under a notehead is a staccato; one beside it is an augmentation dot."""
    by_staff: dict[int, list[_Head]] = defaultdict(list)
    for h in heads:
        by_staff[h.staff].append(h)
    marks = []
    for g in dots:
        cx = _center(g)
        for s in geom.staves:
            if not (s.x0 - 2 <= cx <= s.x1 + 2) or _gap_distance(s, g.y) > 4 * s.spacing:
                continue
            sp = s.spacing
            hit = None
            for h in by_staff[s.index]:
                dx, dy = cx - h.x, g.y - h.y
                # centered on the head; at least 0.8 space off it so a level dot is not mistaken
                if abs(dx) <= 0.55 * sp and 0.8 * sp <= abs(dy) <= 2.6 * sp:
                    if hit is None or abs(dy) < abs(hit.y - g.y):
                        hit = h
            if hit is not None:
                marks.append(Mark("articulation", "staccato", geom.page, s.index, hit.x, g.y,
                                  placement="above" if g.y < hit.y else "below"))
                break
    return marks


def _tremolos(geom: PageGeometry, gs: list[Glyph]) -> list[Mark]:
    marks = []
    for g in gs:
        cx = _center(g)
        # the stem the strokes cross
        best, best_d = None, 0.9 * g.size / 4 + 0.5
        for st in geom.stems:
            top, bot = sorted((st.y0, st.y1))
            if top - 1 <= g.y <= bot + 1 and abs(st.x0 - cx) < best_d:
                best, best_d = st, abs(st.x0 - cx)
        x = best.x0 if best else cx
        s = _nearest_staff(geom.staves, x, g.y)
        if s:
            marks.append(Mark("tremolo", "tremolo", geom.page, s.index, x, g.y,
                              value=int(g.cls.params[0]), detail={"on_stem": best is not None}))
    return marks


def _hairpins(geom: PageGeometry, lyric: set[int]) -> list[Mark]:
    marks = []
    for w in geom.wedges:
        ym = (w.start[1] + w.end[1]) / 2
        s = assign_staff(geom.staves, (w.start[0] + w.end[0]) / 2, ym, lyric)
        if s:
            marks.append(Mark("hairpin", w.kind, geom.page, s.index, w.start[0], ym, x_end=w.end[0],
                              placement=_placement(s, ym)))
    return marks


def _tips(c: Curve) -> tuple[tuple[float, float], tuple[float, float]]:
    """Left and right tips; a filled slur outlines both edges, so use the x extremes of its on-curve points."""
    pts = c.points[::3]
    left = min(pts, key=lambda p: (p[0], p[1]))
    right = max(pts, key=lambda p: (p[0], -p[1]))
    return left, right


def _curve_marks(geom: PageGeometry, heads: list[_Head]) -> list[Mark]:
    by_staff: dict[int, list[_Head]] = defaultdict(list)
    for h in heads:
        by_staff[h.staff].append(h)

    marks = []
    for c in geom.curves:
        x0, y0, x1, y1 = c.bbox
        w, h = x1 - x0, y1 - y0
        if len(c.points) < 4 or (len(c.points) - 1) % 3:
            continue
        s = _nearest_staff(geom.staves, (x0 + x1) / 2, (y0 + y1) / 2)
        if s is None:
            continue
        sp = s.spacing
        # braces, brackets and clef flourishes are tall or tiny; a long slur arches high but stays flat
        if w < 1.5 * sp or h > 0.7 * w + 0.5 * sp or (h > 6 * sp and h > 0.25 * w):
            continue
        (lx, ly), (rx, ry) = _tips(c)
        # a long slur bulging toward the next staff still starts and ends on its own staff's notes
        hits = []
        for t in _staves_at(geom.staves, (x0 + x1) / 2):
            if _gap_distance(t, (ly + ry) / 2) > 5 * t.spacing:
                continue
            n = sum(_tip_head(by_staff[t.index], x, y, t.spacing) is not None for x, y in ((lx, ly), (rx, ry)))
            if n:
                hits.append((-n, _gap_distance(t, (ly + ry) / 2), t.index, t))
        if hits:
            s = min(hits, key=lambda h: h[:3])[3]
            sp = s.spacing
        elif _gap_distance(s, (y0 + y1) / 2) > 5 * sp:
            continue
        a = _tip_head(by_staff[s.index], lx, ly, sp)
        b = _tip_head(by_staff[s.index], rx, ry, sp)
        # a tip within a space of the staff edge with no head is a curve broken at the system edge
        open_start = a is None and lx <= s.x0 + 6 * sp
        open_end = b is None and rx >= s.x1 - 4 * sp
        kind = "slur"
        if a is not None and b is not None and a is not b and abs(a.y - b.y) < 0.3 * sp:
            between = [hd for hd in by_staff[s.index]
                       if a.x + 0.5 * sp < hd.x < b.x - 0.5 * sp]
            # a short flat arc is a tie even when another voice moves between its ends
            short_flat = w <= 10 * sp and h <= 1.3 * sp
            kind = "slur" if between and not short_flat else "tie"
        elif (open_start or open_end) and w < 8 * sp and (a is not None or b is not None):
            kind = "tie"
        marks.append(Mark(kind, "start", geom.page, s.index, lx, ly, x_end=rx, y_end=ry,
                          open_start=open_start, open_end=open_end))
    return marks


def _tip_head(heads: list[_Head], x: float, y: float, sp: float) -> _Head | None:
    best, best_d = None, 2.2 * sp
    for h in heads:
        d = math.hypot(x - h.x, 0.8 * (y - h.y))
        if d < best_d:
            best, best_d = h, d
    return best


def read_marks(page: pymupdf.Page, geometry: PageGeometry, glyphs: list[Glyph]) -> list[Mark]:
    """All markings on `page`, sorted by (staff, x, kind, name)."""
    glyphs = list(glyphs)
    music_fonts = {g.font for g in glyphs}
    lyric = _lyric_staves(page, geometry.staves, music_fonts)
    heads = _heads(glyphs, geometry.staves)
    marks = _glyph_marks(geometry, glyphs, lyric, heads)
    marks += _hairpins(geometry, lyric)
    marks += _curve_marks(geometry, heads)
    return sorted(marks, key=lambda m: (m.staff, m.x, m.kind, m.name, m.y))


def pair_broken(marks: list[Mark], geometries: dict[int, PageGeometry]) -> list[tuple[Mark, Mark]]:
    """Pair each curve open at a system end with the open-start curve beginning the next system.

    Matching uses the staff's position within its system, so it works across a page break too.
    `marks` may span several pages; `geometries` maps page number to geometry."""
    order: list[tuple[int, int]] = []
    pos: dict[tuple[int, int], tuple[tuple[int, int], int]] = {}
    for pno in sorted(geometries):
        for sy in geometries[pno].systems:
            sysid = (pno, sy.index)
            order.append(sysid)
            for k, st in enumerate(sy.staves):
                pos[(pno, st.index)] = (sysid, k)
    nxt = {a: b for a, b in zip(order, order[1:])}

    starts: dict[tuple[tuple[int, int], int], list[Mark]] = defaultdict(list)
    for m in marks:
        if m.kind in ("tie", "slur") and m.open_start and (m.page, m.staff) in pos:
            starts[pos[(m.page, m.staff)]].append(m)
    pairs = []
    used: set[int] = set()
    for m in sorted(marks, key=lambda m: (m.page, m.staff, m.x)):
        if m.kind not in ("tie", "slur") or not m.open_end or (m.page, m.staff) not in pos:
            continue
        sysid, k = pos[(m.page, m.staff)]
        if sysid not in nxt:
            continue
        cands = [c for c in starts.get((nxt[sysid], k), []) if id(c) not in used]
        if cands:
            c = min(cands, key=lambda c: abs(c.y - m.y_end if m.y_end is not None else 0))
            used.add(id(c))
            pairs.append((m, c))
    return pairs
