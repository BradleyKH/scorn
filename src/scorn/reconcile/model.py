"""Shared geometry records read from a born-digital PDF; all positions are PDF points, y grows downward."""

from __future__ import annotations

from dataclasses import dataclass

from .glyphs import Glyph  # noqa: F401  (re-exported: the package has one glyph type)

Point = tuple[float, float]
Box = tuple[float, float, float, float]

ITALIC, BOLD = 1, 2


@dataclass(frozen=True)
class TextSpan:
    page: int
    text: str
    font: str
    size: float
    x: float
    y: float
    bbox: Box
    # bitmask of ITALIC and BOLD, from the font's flags and name
    flags: int = 0

    @property
    def italic(self) -> bool:
        return bool(self.flags & ITALIC)

    @property
    def bold(self) -> bool:
        return bool(self.flags & BOLD)


@dataclass(frozen=True)
class Staff:
    page: int
    index: int
    top: float
    bottom: float
    spacing: float
    x0: float
    x1: float

    @property
    def lines(self) -> tuple[float, ...]:
        return tuple(self.top + i * self.spacing for i in range(5))

    def line_of(self, y: float) -> float:
        """Staff position of y in line units: 0 is the top line, 4 the bottom, 0.5 steps are spaces."""
        return (y - self.top) / self.spacing


@dataclass(frozen=True)
class Barline:
    x: float
    x0: float
    x1: float
    top: float
    bottom: float
    # single, double, final, heavy, heavy-light, light-heavy; repeat_* when dots sit beside it
    kind: str
    repeat_start: bool = False
    repeat_end: bool = False


@dataclass(frozen=True)
class System:
    page: int
    index: int
    staves: tuple[Staff, ...]
    barlines: tuple[Barline, ...]
    labels: tuple[str, ...]

    @property
    def barline_xs(self) -> tuple[float, ...]:
        return tuple(b.x for b in self.barlines)

    @property
    def top(self) -> float:
        return self.staves[0].top

    @property
    def bottom(self) -> float:
        return self.staves[-1].bottom

    @property
    def x0(self) -> float:
        return min(s.x0 for s in self.staves)

    @property
    def x1(self) -> float:
        return max(s.x1 for s in self.staves)


@dataclass(frozen=True)
class Measure:
    page: int
    system: int
    index: int
    x0: float
    x1: float


@dataclass(frozen=True)
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    width: float
    # stem, ledger, beam, extender, hyphen, other
    kind: str


@dataclass(frozen=True)
class Curve:
    page: int
    # cubic bezier control points in drawing order, joined end to start
    points: tuple[Point, ...]
    filled: bool
    bbox: Box


@dataclass(frozen=True)
class Wedge:
    page: int
    # start is the left end, end the right; the vertex is start for a crescendo, end for a diminuendo
    start: Point
    end: Point
    opening: float
    kind: str
