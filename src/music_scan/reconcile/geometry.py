"""Vector geometry of a born-digital score page: staves, systems, barlines, measures and line-drawn notation."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import pymupdf

from .model import Barline, Curve, Line, Measure, Point, Staff, System, Wedge

# Purchaser watermarks print vertically down the page edge; drop anything confined to this margin.
MARGIN = 0.06
# Staff spacing seen across parts and full scores.
MIN_SPACING, MAX_SPACING = 2.0, 12.0


@dataclass(frozen=True)
class PageGeometry:
    page: int
    width: float
    height: float
    staves: tuple[Staff, ...]
    systems: tuple[System, ...]
    measures: tuple[Measure, ...]
    ledgers: tuple[Line, ...]
    stems: tuple[Line, ...]
    beams: tuple[Line, ...]
    curves: tuple[Curve, ...]
    wedges: tuple[Wedge, ...]
    extenders: tuple[Line, ...]
    hyphens: tuple[Line, ...]
    # horizontal and diagonal lines nothing else claimed: brackets, endings, ottavas, pedal lines
    lines: tuple[Line, ...]
    # small filled shapes such as repeat dots and drawn staccatos
    dots: tuple[Point, ...]


@dataclass
class _Seg:
    x0: float
    y0: float
    x1: float
    y1: float
    width: float

    @property
    def length(self) -> float:
        return ((self.x1 - self.x0) ** 2 + (self.y1 - self.y0) ** 2) ** 0.5


@dataclass
class _Prims:
    hsegs: list[_Seg] = field(default_factory=list)
    vsegs: list[_Seg] = field(default_factory=list)
    dsegs: list[_Seg] = field(default_factory=list)
    # filled quadrilaterals with two vertical sides: (x0, top0, bottom0, x1, top1, bottom1)
    quads: list[tuple[float, float, float, float, float, float]] = field(default_factory=list)
    curves: list[tuple[tuple[Point, ...], bool, tuple[float, float, float, float]]] = field(default_factory=list)
    # bboxes of everything that could tie staves into one system: verticals and curves
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    # glyph advance extents and baseline: (x0, x1, origin y)
    chars: list[tuple[float, float, float]] = field(default_factory=list)


def _pt(p) -> Point:
    return (float(p.x), float(p.y))


def _is_furniture(r: pymupdf.Rect, w: float, h: float) -> bool:
    if r.width >= 0.5 * w and r.height >= 0.5 * h:
        return True
    in_side = r.x1 <= MARGIN * w or r.x0 >= (1 - MARGIN) * w
    in_top = r.y1 <= MARGIN * h or r.y0 >= (1 - MARGIN) * h
    # a full-score system's opening barline can reach well over half the page, so demand nearly all of it
    return (in_side and r.height >= 0.85 * h) or (in_top and r.width >= 0.85 * w)


def _polygon(items) -> list[Point] | None:
    if len(items) == 1 and items[0][0] == "re":
        r = items[0][1]
        return [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
    if len(items) == 1 and items[0][0] == "qu":
        q = items[0][1]
        return [_pt(q.ul), _pt(q.ur), _pt(q.lr), _pt(q.ll)]
    if not all(it[0] == "l" for it in items):
        return None
    pts = [_pt(it[1]) for it in items] + [_pt(items[-1][2])]
    if abs(pts[0][0] - pts[-1][0]) < 0.05 and abs(pts[0][1] - pts[-1][1]) < 0.05:
        pts.pop()
    return pts


def _add_filled(pr: _Prims, pts: list[Point]) -> None:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    axis = all(abs(a[0] - b[0]) < 0.05 or abs(a[1] - b[1]) < 0.05 for a, b in zip(pts, pts[1:] + pts[:1]))
    if axis and h <= 1.5 and w > h:
        pr.hsegs.append(_Seg(x0, (y0 + y1) / 2, x1, (y0 + y1) / 2, h))
    elif axis and w <= 3.0 and h > w:
        pr.vsegs.append(_Seg((x0 + x1) / 2, y0, (x0 + x1) / 2, y1, w))
        pr.boxes.append((x0, y0, x1, y1))
    if len(pts) == 4:
        pts = sorted(pts)
        (ax, ay), (bx, by), (cx, cy), (dx, dy) = pts
        if abs(ax - bx) < 0.1 and abs(cx - dx) < 0.1 and cx - ax > 0.1:
            pr.quads.append((ax, min(ay, by), max(ay, by), cx, min(cy, dy), max(cy, dy)))


def _primitives(page: pymupdf.Page) -> _Prims:
    w, h = page.rect.width, page.rect.height
    pr = _Prims()
    for d in page.get_drawings():
        r = d["rect"]
        if _is_furniture(r, w, h):
            continue
        items = d["items"]
        kind = d["type"]
        filled = "f" in kind
        width = float(d.get("width") or 0.0)
        if any(it[0] == "c" for it in items) or _flattened_curve(items):
            box = (r.x0, r.y0, r.x1, r.y1)
            # the same outline is often filled and then stroked
            if any(all(abs(a - b) <= 0.5 for a, b in zip(box, c[2])) for c in pr.curves[-4:]):
                continue
            pts: list[Point] = [_pt(items[0][1])]
            for it in items:
                if it[0] == "c":
                    pts += [_pt(it[2]), _pt(it[3]), _pt(it[4])]
                elif it[0] == "l":
                    pts += [_pt(it[1]), _pt(it[2]), _pt(it[2])]
            pr.curves.append((tuple(pts), filled, box))
            pr.boxes.append(box)
            continue
        if filled:
            poly = _polygon(items)
            if poly and len(poly) >= 3:
                _add_filled(pr, poly)
            continue
        for it in items:
            if it[0] == "l":
                segs = [(it[1], it[2])]
            elif it[0] == "re":
                q = it[1].quad
                segs = [(q.ul, q.ur), (q.ur, q.lr), (q.lr, q.ll), (q.ll, q.ul)]
            elif it[0] == "qu":
                q = it[1]
                segs = [(q.ul, q.ur), (q.ur, q.lr), (q.lr, q.ll), (q.ll, q.ul)]
            else:
                continue
            for a, b in segs:
                ax, ay, bx, by = float(a.x), float(a.y), float(b.x), float(b.y)
                if abs(ay - by) <= 0.3 and abs(ax - bx) > 0.3:
                    y = (ay + by) / 2
                    pr.hsegs.append(_Seg(min(ax, bx), y, max(ax, bx), y, width))
                elif abs(ax - bx) <= 0.3 and abs(ay - by) > 0.3:
                    x = (ax + bx) / 2
                    pr.vsegs.append(_Seg(x, min(ay, by), x, max(ay, by), width))
                    pr.boxes.append((x - width / 2, min(ay, by), x + width / 2, max(ay, by)))
                elif abs(ax - bx) > 0.3:
                    if ax > bx:
                        ax, ay, bx, by = bx, by, ax, ay
                    pr.dsegs.append(_Seg(ax, ay, bx, by, width))
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            lx0, _, lx1, _ = ln["bbox"]
            if abs(ln["dir"][0]) < 0.5 and (lx1 <= MARGIN * w or lx0 >= (1 - MARGIN) * w):
                continue
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    pr.chars.append((ch["bbox"][0], ch["bbox"][2], float(ch["origin"][1])))
    pr.hsegs = _merge_hatches(pr.hsegs)
    pr.dsegs = _merge_hatches(pr.dsegs)
    return pr


def _flattened_curve(items) -> bool:
    """Ghostscript flattens slurs and ties into one connected path of many short lines."""
    if len(items) < 12 or not all(it[0] == "l" for it in items):
        return False
    if any(abs(a[2].x - b[1].x) > 0.05 or abs(a[2].y - b[1].y) > 0.05 for a, b in zip(items, items[1:])):
        return False
    return not all(abs(it[1].x - it[2].x) < 0.05 or abs(it[1].y - it[2].y) < 0.05 for it in items)


def _merge_hatches(segs: list[_Seg]) -> list[_Seg]:
    """Fold stacks of parallel hairline strokes into one line of the stack's thickness.

    Ghostscript fills a heavy line (beam, hairpin stroke) with hairlines a fraction of a point
    apart, each with the same ends and slope; read alone they are neither beams nor single wedges."""
    # each run: [x0, x1, dy, first y0, last y0, stroke width, strokes]
    runs: list[list] = []
    active: list[list] = []
    for s in sorted(segs, key=lambda s: (s.y0, s.x0, s.x1, s.y1)):
        dy = s.y1 - s.y0
        gap = max(0.3, 1.5 * s.width)
        active = [r for r in active if s.y0 - r[4] <= 1.0]
        for r in active:
            if (abs(r[0] - s.x0) <= 0.3 and abs(r[1] - s.x1) <= 0.3 and abs(r[2] - dy) <= 0.3
                    and s.y0 - r[4] <= gap and abs(r[5] - s.width) <= 0.1):
                r[4] = s.y0
                r[6] += 1
                break
        else:
            r = [s.x0, s.x1, dy, s.y0, s.y0, s.width, 1]
            runs.append(r)
            active.append(r)
    out: list[_Seg] = []
    for x0, x1, dy, first, last, width, n in runs:
        y = (first + last) / 2
        out.append(_Seg(x0, y, x1, y + dy, width if n == 1 else last - first + width))
    return out


def _merge_runs(segs: list[_Seg], gap: float) -> list[_Seg]:
    """Join collinear horizontal pieces; MuseScore draws staff lines one measure at a time."""
    # Some drivers stroke a heavy line as two thin ones a fraction of a point apart, so band by y first.
    bands: list[list[_Seg]] = []
    for s in sorted(segs, key=lambda s: (s.y0, s.x0, s.x1)):
        if bands and s.y0 - bands[-1][-1].y0 <= 0.6:
            bands[-1].append(s)
        else:
            bands.append([s])
    runs: list[_Seg] = []
    for band in bands:
        out: list[_Seg] = []
        for s in sorted(band, key=lambda s: (s.x0, s.x1, s.y0)):
            for r in reversed(out):
                dy = abs(r.y0 - s.y0)
                if abs(r.x0 - s.x0) <= 1 and abs(r.x1 - s.x1) <= 1 and dy <= 0.6:
                    # hatch strokes of one heavy line; hatched rests inside the staff stay separate
                    lo = min(r.y0 - r.width / 2, s.y0 - s.width / 2)
                    hi = max(r.y0 + r.width / 2, s.y0 + s.width / 2)
                    r.y0 = r.y1 = (lo + hi) / 2
                    r.width = hi - lo
                    break
                if dy <= 0.25 and r.x0 <= s.x0 <= r.x1 + gap:
                    r.x1 = max(r.x1, s.x1)
                    break
            else:
                out.append(_Seg(s.x0, s.y0, s.x1, s.y1, s.width))
        runs += out
    return runs


def _overlap(a: _Seg, b: _Seg) -> float:
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def _find_staves(hsegs: list[_Seg], page_no: int) -> list[Staff]:
    runs = [r for r in _merge_runs([s for s in hsegs if s.width <= 1.5], 1.0) if r.x1 - r.x0 >= 30]
    runs.sort(key=lambda r: (r.y0, r.x0))
    used: set[int] = set()
    staves: list[tuple[float, float, float, float, float]] = []
    for i, r in enumerate(runs):
        if i in used:
            continue
        span = r.x1 - r.x0

        def near(y: float, tol: float) -> int | None:
            best = None
            for j in range(i + 1, len(runs)):
                c = runs[j]
                if c.y0 > y + tol:
                    break
                if j in used or abs(c.y0 - y) > tol or _overlap(r, c) < 0.8 * max(span, c.x1 - c.x0):
                    continue
                if best is None or abs(c.y0 - y) < abs(runs[best].y0 - y):
                    best = j
            return best

        for j in range(i + 1, len(runs)):
            c = runs[j]
            gap = c.y0 - r.y0
            if gap < MIN_SPACING - 0.2:
                continue
            if gap > MAX_SPACING:
                break
            if j in used or _overlap(r, c) < 0.8 * max(span, c.x1 - c.x0):
                continue
            tol = 0.12 * gap + 0.1
            rest = [near(r.y0 + k * gap, tol) for k in (2, 3, 4)]
            if None in rest:
                continue
            members = [i, j, *rest]
            used.update(members)
            ls = [runs[m] for m in members]
            staves.append(
                (ls[0].y0, ls[4].y0, min(x.x0 for x in ls), max(x.x1 for x in ls), max(x.width for x in ls))
            )
            break
    out = [
        Staff(page_no, 0, top, bottom, (bottom - top) / 4, x0, x1)
        for top, bottom, x0, x1, _ in sorted(staves, key=lambda t: (t[0], t[2]))
    ]
    return out


def _merge_vertical(segs: list[_Seg]) -> list[_Seg]:
    """Join vertical pieces drawn per staff, and hatch strokes laid side by side to fill one heavy line."""
    out: list[_Seg] = []
    for s in sorted(segs, key=lambda s: (s.x0, s.y0, s.y1)):
        for r in reversed(out):
            right = r.x0 + r.width / 2
            if s.x0 - s.width / 2 - right > 0.6:
                out.append(_Seg(s.x0, s.y0, s.x1, s.y1, s.width))
                break
            if abs(r.y0 - s.y0) <= 0.3 and abs(r.y1 - s.y1) <= 0.3 and s.x0 - right <= 0.3:
                left = r.x0 - r.width / 2
                right = max(right, s.x0 + s.width / 2)
                r.x0 = r.x1 = (left + right) / 2
                r.width = right - left
                break
            if abs(r.x0 - s.x0) <= 0.2 and abs(r.width - s.width) <= 0.1 and s.y0 <= r.y1 + 0.5 and s.y1 >= r.y0 - 0.5:
                r.y0, r.y1 = min(r.y0, s.y0), max(r.y1, s.y1)
                break
        else:
            out.append(_Seg(s.x0, s.y0, s.x1, s.y1, s.width))
    return sorted(out, key=lambda s: (s.x0, s.y0, s.y1))


class _Union:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def join(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[max(a, b)] = min(a, b)


def _group_systems(staves: list[Staff], boxes) -> list[list[Staff]]:
    """Staves joined by a system's left barline, bracket or brace form one system."""
    uf = _Union(len(staves))
    for bx0, by0, bx1, by1 in boxes:
        hit = []
        for k, s in enumerate(staves):
            sp = s.spacing
            if not (s.x0 - 4 * sp <= bx1 <= s.x0 + 0.6 * sp):
                continue
            if s.bottom >= by0 - 0.5 * sp and s.top <= by1 + 0.5 * sp:
                hit.append(k)
        for k in hit[1:]:
            uf.join(hit[0], k)
    groups: dict[int, list[Staff]] = {}
    for k, s in enumerate(staves):
        groups.setdefault(uf.find(k), []).append(s)
    return sorted(groups.values(), key=lambda g: (g[0].top, g[0].x0))


_KINDS = {
    "t": "single",
    "tt": "double",
    "tT": "light-heavy",
    "Tt": "heavy-light",
    "T": "heavy",
    "TT": "heavy-heavy",
    "tTt": "light-heavy-light",
    "tTTt": "light-heavy-light",
}


def _notehead_at(v: _Seg, chars, sp: float) -> bool:
    """A stem that spans a staff edge to edge still ends on a notehead flush with its side; a barline never does."""
    for x0, x1, oy in chars:
        if not (0.9 * sp <= x1 - x0 <= 1.55 * sp):
            continue
        if min(abs(x0 - v.x0), abs(x1 - v.x0)) > 0.4 + v.width / 2:
            continue
        if min(abs(oy - v.y0), abs(oy - v.y1)) <= 0.6 * sp:
            return True
    return False


def _barlines(staves: list[Staff], vsegs: list[_Seg], dots: list[Point], chars) -> tuple[list[Barline], set[int]]:
    sp = statistics.median(s.spacing for s in staves)
    x0, x1 = min(s.x0 for s in staves), max(s.x1 for s in staves)
    # Barlines meet the staff edges exactly; stems that happen to cross a staff overshoot or fall short.
    tol = 0.15 * sp + 0.25
    cands: list[tuple[_Seg, set[int], int]] = []
    for idx, v in enumerate(vsegs):
        if not (x0 - 0.5 * sp <= v.x0 <= x1 + 0.5 * sp):
            continue
        covered = {k for k, s in enumerate(staves) if v.y0 <= s.top + tol and v.y1 >= s.bottom - tol}
        if not covered:
            continue
        # A stem can cross a whole staff; a barline starts and ends on staff edges.
        if min(abs(v.y0 - s.top) for s in staves) > tol or min(abs(v.y1 - s.bottom) for s in staves) > tol:
            continue
        # no stem spans two staves, so a notehead beside a long line is only a neighbor
        if len(covered) == 1 and _notehead_at(v, chars, sp):
            continue
        cands.append((v, covered, idx))
    cands.sort(key=lambda c: (c[0].x0, c[0].y0))
    clusters: list[list[tuple[_Seg, set[int], int]]] = []
    gap = max(3.0, 0.9 * sp)
    for c in cands:
        if clusters and c[0].x0 - max(e[0].x0 + e[0].width / 2 for e in clusters[-1]) <= gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    out: list[Barline] = []
    used: set[int] = set()
    every = set(range(len(staves)))
    for cl in clusters:
        if set().union(*(c[1] for c in cl)) != every:
            continue
        used.update(c[2] for c in cl)
        thin = min(c[0].width for c in cl)
        # distinct strokes by x; a barline drawn once per staff repeats the same x
        strokes: list[tuple[float, float]] = []
        for c in sorted(cl, key=lambda c: c[0].x0):
            if strokes and abs(strokes[-1][0] - c[0].x0) <= 0.6:
                continue
            strokes.append((c[0].x0, c[0].width))
        # duplicate thin lines about a point apart at system starts read as one
        pattern = ""
        prev = None
        for x, wd in strokes:
            mark = "T" if wd >= max(2 * thin, 0.3 * sp) and wd > thin + 0.3 else "t"
            if prev is not None and mark == "t" and pattern.endswith("t") and x - prev <= 1.2:
                continue
            pattern += mark
            prev = x
        kind = _KINDS.get(pattern, "other")
        bx0 = min(c[0].x0 - c[0].width / 2 for c in cl)
        bx1 = max(c[0].x0 + c[0].width / 2 for c in cl)
        top = min(c[0].y0 for c in cl)
        bottom = max(c[0].y1 for c in cl)

        def dotted(lo: float, hi: float) -> bool:
            for s in staves:
                mids = (s.top + 1.5 * s.spacing, s.top + 2.5 * s.spacing)
                if all(any(lo <= dx <= hi and abs(dy - m) <= 0.35 * s.spacing for dx, dy in dots) for m in mids):
                    return True
            return False

        out.append(
            Barline(
                x=(bx0 + bx1) / 2,
                x0=bx0,
                x1=bx1,
                top=top,
                bottom=bottom,
                kind=kind,
                repeat_start=dotted(bx1, bx1 + 1.5 * sp),
                repeat_end=dotted(bx0 - 1.5 * sp, bx0),
            )
        )
    return out, used


def _measures(page_no: int, sys_index: int, staves: list[Staff], bars: list[Barline], stems: list[Line]):
    sp = statistics.median(s.spacing for s in staves)
    x0, x1 = min(s.x0 for s in staves), max(s.x1 for s in staves)
    edges = list(bars)
    stems = [st for st in stems if staves[0].top - 8 * sp < st.y0 and st.y1 < staves[-1].bottom + 8 * sp]
    start = x0
    if edges and edges[0].x0 <= x0 + 1.5 * sp:
        start = edges.pop(0).x1
    if edges:
        first = edges[0]
        # A start repeat right after the clef and key is not the end of a measure. Only stems that
        # reach into the system count: a rehearsal box above it draws stem-like sides.
        if (first.repeat_start or first.kind == "heavy-light") and not any(
            start < (s.x0 + s.x1) / 2 < first.x0 and s.y1 >= staves[0].top - 2 * sp for s in stems
        ):
            start = edges.pop(0).x1
    out: list[Measure] = []
    prev: Barline | None = None
    for b in edges:
        # an end repeat and a start repeat with only a new key or time signature between them
        # (repeat dots drawn as glyphs leave only the thick-thin pattern to go by)
        between_repeats = (prev is not None and (prev.repeat_end or prev.kind == "light-heavy")
                           and (b.repeat_start or b.kind == "heavy-light") and b.x0 - start <= 5 * sp
                           and not any(start < (s.x0 + s.x1) / 2 < b.x0 for s in stems))
        if b.x0 - start > 0.5 * sp and not between_repeats:
            out.append(Measure(page_no, sys_index, len(out), start, b.x0))
        start = b.x1
        prev = b
    # Past the last barline sits either an unclosed measure or a courtesy key and time signature.
    tail = [st for st in stems if start < st.x0 < x1]
    # a double barline then a wide courtesy key change (naturals cancelling a long signature) is no bar
    courtesy = bool(edges) and edges[-1].kind != "single"
    if (tail and x1 - start > 3 * sp) or (x1 - start > 12 * sp and not courtesy):
        out.append(Measure(page_no, sys_index, len(out), start, x1))
    return out


def _labels(page: pymupdf.Page, systems: list[list[Staff]]) -> list[tuple[str, ...]]:
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            if abs(ln["dir"][0] - 1) > 1e-3:
                continue
            for sp in ln["spans"]:
                text = sp["text"].strip()
                if any(ch.isalnum() for ch in text):
                    spans.append((sp["bbox"], text))
    out = []
    for staves in systems:
        sp = staves[0].spacing
        x0 = min(s.x0 for s in staves)
        top, bottom = staves[0].top, staves[-1].bottom
        hits = [
            (bb[1], bb[0], t)
            for bb, t in spans
            if bb[2] <= x0 - 0.5 * sp and top - 2 * sp <= (bb[1] + bb[3]) / 2 <= bottom + 2 * sp
        ]
        out.append(tuple(t for _, _, t in sorted(hits)))
    return out


def _nearest_staff(staves: list[Staff], x: float, y: float) -> Staff | None:
    best, dist = None, None
    for s in staves:
        if not (s.x0 - 2 * s.spacing <= x <= s.x1 + 2 * s.spacing):
            continue
        d = 0.0 if s.top <= y <= s.bottom else min(abs(y - s.top), abs(y - s.bottom))
        if dist is None or d < dist:
            best, dist = s, d
    return best


def read_geometry(page: pymupdf.Page) -> PageGeometry:
    """Read the vector notation on one page; page numbers in the result are 0-based."""
    no = page.number
    pr = _primitives(page)
    staves = _find_staves(pr.hsegs, no)
    dots: list[Point] = []
    curves: list[Curve] = []
    sp_page = statistics.median(s.spacing for s in staves) if staves else 4.0
    for pts, filled, box in pr.curves:
        bw, bh = box[2] - box[0], box[3] - box[1]
        if filled and bw <= 0.8 * sp_page and bh <= 0.8 * sp_page:
            dots.append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
        else:
            curves.append(Curve(no, pts, filled, box))

    vsegs = _merge_vertical(pr.vsegs)
    groups = _group_systems(staves, pr.boxes)
    numbered: list[list[Staff]] = []
    k = 0
    for g in groups:
        g = sorted(g, key=lambda s: s.top)
        numbered.append([Staff(no, k + i, s.top, s.bottom, s.spacing, s.x0, s.x1) for i, s in enumerate(g)])
        k += len(g)
    all_staves = [s for g in numbered for s in g]

    bar_used: set[int] = set()
    bars_by_system = []
    for g in numbered:
        bars, used = _barlines(g, vsegs, dots, pr.chars)
        bars_by_system.append(bars)
        bar_used |= used

    stems: list[Line] = []
    others: list[Line] = []
    for idx, v in enumerate(vsegs):
        if idx in bar_used:
            continue
        s = _nearest_staff(all_staves, v.x0, (v.y0 + v.y1) / 2)
        ln = Line(no, v.x0, v.y0, v.x1, v.y1, v.width, "stem")
        if s and v.width <= 0.35 * s.spacing and 1.5 * s.spacing <= v.length <= 12 * s.spacing:
            stems.append(ln)
        else:
            others.append(Line(no, v.x0, v.y0, v.x1, v.y1, v.width, "other"))

    ledgers: list[Line] = []
    extenders: list[Line] = []
    hyphens: list[Line] = []
    for h in pr.hsegs:
        if any(abs(h.y0 - y) <= 0.3 and s.x0 - 1 <= h.x0 and h.x1 <= s.x1 + 1 for s in all_staves for y in s.lines):
            continue
        s = _nearest_staff(all_staves, (h.x0 + h.x1) / 2, h.y0)
        length = h.x1 - h.x0
        if s is None:
            others.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "other"))
            continue
        pos = s.line_of(h.y0)
        outside = pos < -0.6 or pos > 4.6
        if outside and abs(pos - round(pos)) <= 0.2 and abs(pos - 2) <= 14 and 0.8 * s.spacing <= length <= 4 * s.spacing:
            ledgers.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "ledger"))
        elif h.y0 > s.bottom + 2.5 * s.spacing and h.width <= 0.35 * s.spacing:
            if length >= 1.5 * s.spacing:
                extenders.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "extender"))
            elif length >= 0.3 * s.spacing:
                hyphens.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "hyphen"))
        elif 0.3 * s.spacing <= h.width <= 0.8 * s.spacing and length >= s.spacing:
            # flat beams drawn as thick strokes
            others.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "beam"))
        else:
            others.append(Line(no, h.x0, h.y0, h.x1, h.y1, h.width, "other"))

    beams: list[Line] = [ln for ln in others if ln.kind == "beam"]
    others = [ln for ln in others if ln.kind != "beam"]
    for ax, at, ab, cx, ct, cb in pr.quads:
        s = _nearest_staff(all_staves, (ax + cx) / 2, (at + ab) / 2)
        if s is None:
            continue
        thick = ((ab - at) + (cb - ct)) / 2
        if abs((ab - at) - (cb - ct)) <= 0.2 * thick and 0.3 * s.spacing <= thick <= 0.8 * s.spacing and cx - ax >= 0.8 * s.spacing:
            beams.append(Line(no, ax, (at + ab) / 2, cx, (ct + cb) / 2, thick, "beam"))

    wedges: list[Wedge] = []
    diag = []
    for d in pr.dsegs:
        s = _nearest_staff(all_staves, (d.x0 + d.x1) / 2, (d.y0 + d.y1) / 2)
        sp = s.spacing if s else sp_page
        if 0.3 * sp <= d.width <= 0.8 * sp and d.length >= sp:
            beams.append(Line(no, d.x0, d.y0, d.x1, d.y1, d.width, "beam"))
        else:
            diag.append((d, sp))
    paired: set[int] = set()
    for i, (a, sp) in enumerate(diag):
        if i in paired or a.x1 - a.x0 < 2 * sp or abs(a.y1 - a.y0) > 0.4 * (a.x1 - a.x0):
            continue
        for j in range(i + 1, len(diag)):
            b = diag[j][0]
            if j in paired or abs(b.x0 - a.x0) > 0.5 or abs(b.x1 - a.x1) > 0.5:
                continue
            if abs(a.y0 - b.y0) <= 0.5 and abs(a.y1 - b.y1) > 0.5 * sp:
                wedges.append(Wedge(no, (a.x0, (a.y0 + b.y0) / 2), (a.x1, (a.y1 + b.y1) / 2), abs(a.y1 - b.y1) / 2, "crescendo"))
            elif abs(a.y1 - b.y1) <= 0.5 and abs(a.y0 - b.y0) > 0.5 * sp:
                wedges.append(Wedge(no, (a.x0, (a.y0 + b.y0) / 2), (a.x1, (a.y1 + b.y1) / 2), abs(a.y0 - b.y0) / 2, "diminuendo"))
            else:
                continue
            paired |= {i, j}
            break
    for i, (d, _) in enumerate(diag):
        if i not in paired:
            others.append(Line(no, d.x0, d.y0, d.x1, d.y1, d.width, "other"))

    labels = _labels(page, numbered)
    systems: list[System] = []
    measures: list[Measure] = []
    for i, g in enumerate(numbered):
        systems.append(System(no, i, tuple(g), tuple(bars_by_system[i]), labels[i]))
        measures += _measures(no, i, g, bars_by_system[i], stems)

    def key(ln: Line):
        return (ln.y0, ln.x0, ln.y1, ln.x1)

    return PageGeometry(
        page=no,
        width=page.rect.width,
        height=page.rect.height,
        staves=tuple(all_staves),
        systems=tuple(systems),
        measures=tuple(measures),
        ledgers=tuple(sorted(ledgers, key=key)),
        stems=tuple(sorted(stems, key=key)),
        beams=tuple(sorted(beams, key=key)),
        curves=tuple(sorted(curves, key=lambda c: (c.bbox[1], c.bbox[0]))),
        wedges=tuple(sorted(wedges, key=lambda w: (w.start[1], w.start[0]))),
        extenders=tuple(sorted(extenders, key=key)),
        hyphens=tuple(sorted(hyphens, key=key)),
        lines=tuple(sorted(others, key=key)),
        dots=tuple(sorted(dots, key=lambda p: (p[1], p[0]))),
    )


def read_document(doc: pymupdf.Document) -> list[PageGeometry]:
    return [read_geometry(p) for p in doc]
