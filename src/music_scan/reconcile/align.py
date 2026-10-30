"""Align a PdfScore with a MusicXML tree: measures, staves to parts, and events within each bar.

Audiveris keeps the PDF's top-to-bottom staff order inside every system and marks the parts a
page leaves out with `staff-details print-object="no"`, so staves match parts by an
order-preserving optimal assignment over the visible (part, staff) slots, scored on note content,
clef and label text. Everything here is deterministic: ties break toward the earlier staff,
slot, measure or event.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from fractions import Fraction

from . import instruments
from .pdf_notes import STEPS, Event, StaffMeasureContent
from .pdf_score import Key, PdfMeasure, PdfScore, PdfSystem

_SEMIS = (0, 2, 4, 5, 7, 9, 11)


# ---------------------------------------------------------------- change log


@dataclass(frozen=True)
class Change:
    category: str
    part: str
    measure: str
    before: str
    after: str
    evidence: str
    # False for a disagreement the pass found but left alone
    applied: bool = True
    seq: int = 0


def measure_sort_key(number: str) -> tuple[int, str]:
    m = re.match(r"-?\d+", number or "")
    return (int(m.group()) if m else 10**9, number or "")


class ChangeLog:
    """Append-only record shared by the reconcile passes. Iteration is insertion order, which is
    deterministic because passes run in a fixed order over sorted inputs; `ordered()` groups by
    part and measure for reports."""

    def __init__(self) -> None:
        self._items: list[Change] = []

    def add(self, category: str, part: str, measure: str, before: str, after: str, evidence: str,
            applied: bool = True) -> Change:
        ch = Change(category, part, str(measure), before, after, evidence, applied, len(self._items))
        self._items.append(ch)
        return ch

    def disagree(self, category: str, part: str, measure: str, before: str, after: str, evidence: str) -> Change:
        return self.add(category, part, measure, before, after, evidence, applied=False)

    def __iter__(self) -> Iterator[Change]:
        return iter(tuple(self._items))

    def __len__(self) -> int:
        return len(self._items)

    def ordered(self, part_order: list[str] | None = None) -> list[Change]:
        rank = {p: i for i, p in enumerate(part_order or [])}
        return sorted(self._items, key=lambda c: (rank.get(c.part, len(rank)), c.part,
                                                  measure_sort_key(c.measure), c.seq))


# ---------------------------------------------------------------- MusicXML side


@dataclass(frozen=True)
class XmlEvent:
    """A note, chord or rest of one part and staff; `notes` are the <note> elements, chord members included."""

    notes: tuple[ET.Element, ...]
    voice: str
    # quarters from the start of the bar; None for grace notes
    onset: Fraction | None
    duration: Fraction
    pitches: tuple[int, ...]
    grace: bool
    type: str | None

    @property
    def is_rest(self) -> bool:
        return not self.pitches


@dataclass(frozen=True)
class XmlMeasure:
    # position through the piece, in the order of the part with the most measures
    index: int
    number: str
    # n-th measure carrying this number (numbers can repeat), so (number, occurrence) is unique
    occurrence: int
    system: int
    new_system: bool


@dataclass
class _PartInfo:
    id: str
    name: str
    abbreviation: str
    staves: int
    measures: dict[tuple[str, int], ET.Element]
    # per measure key: {staff: visible}, {staff: clef sign}
    visible: dict[tuple[str, int], dict[int, bool]]
    clefs: dict[tuple[str, int], dict[int, str]]
    divisions: dict[tuple[str, int], int]


def _midi(pitch: ET.Element) -> int:
    step = pitch.findtext("step") or "C"
    alter = round(float(pitch.findtext("alter") or 0))
    octave = int(pitch.findtext("octave") or 4)
    return 12 * (octave + 1) + _SEMIS[STEPS.index(step)] + alter


def _measure_keys(part: ET.Element) -> list[tuple[str, int, ET.Element]]:
    seen: dict[str, int] = defaultdict(int)
    out = []
    for m in part.findall("measure"):
        n = m.get("number") or ""
        out.append((n, seen[n], m))
        seen[n] += 1
    return out


def _parts(root: ET.Element) -> list[_PartInfo]:
    names = {sp.get("id"): (sp.findtext("part-name") or "", sp.findtext("part-abbreviation") or "")
             for sp in root.iter("score-part")}
    out = []
    for part in root.findall("part"):
        pid = part.get("id") or ""
        staves = 1
        measures: dict[tuple[str, int], ET.Element] = {}
        visible: dict[tuple[str, int], dict[int, bool]] = {}
        clefs: dict[tuple[str, int], dict[int, str]] = {}
        divs: dict[tuple[str, int], int] = {}
        vis_state: dict[int, bool] = {}
        clef_state: dict[int, str] = {}
        div = 1
        for n, occ, m in _measure_keys(part):
            key = (n, occ)
            measures[key] = m
            start_clefs = None
            for el in m:
                if el.tag == "note" and start_clefs is None:
                    start_clefs = dict(clef_state)
                if el.tag != "attributes":
                    continue
                div = int(el.findtext("divisions") or div)
                staves = max(staves, int(el.findtext("staves") or 1))
                for c in el.findall("clef"):
                    clef_state[int(c.get("number") or 1)] = c.findtext("sign") or "G"
                for sd in el.findall("staff-details"):
                    vis_state[int(sd.get("number") or 0)] = sd.get("print-object") != "no"
            visible[key] = dict(vis_state)
            clefs[key] = start_clefs if start_clefs is not None else dict(clef_state)
            divs[key] = div
        out.append(_PartInfo(pid, *names.get(pid, ("", "")), staves, measures, visible, clefs, divs))
    return out


def _xml_measures(root: ET.Element) -> list[XmlMeasure]:
    parts = root.findall("part")
    if not parts:
        return []
    longest = max(parts, key=lambda p: len(p.findall("measure")))
    starts: set[tuple[str, int]] = set()
    for p in parts:
        for n, occ, m in _measure_keys(p):
            pr = m.find("print")
            if pr is not None and (pr.get("new-system") == "yes" or pr.get("new-page") == "yes"):
                starts.add((n, occ))
    out = []
    system = 0
    for i, (n, occ, _) in enumerate(_measure_keys(longest)):
        new = i == 0 or (n, occ) in starts
        if new and i:
            system += 1
        out.append(XmlMeasure(i, n, occ, system, new))
    return out


def xml_events(measure: ET.Element, staff: int, divisions: int) -> list[XmlEvent]:
    """Events of one staff of a measure, in document order."""
    out: list[XmlEvent] = []
    pos = 0
    group: list[ET.Element] = []
    group_at = 0

    def flush() -> None:
        if not group:
            return
        head = group[0]
        grace = head.find("grace") is not None
        dur = Fraction(int(head.findtext("duration") or 0), divisions)
        pitches = tuple(sorted(_midi(n.find("pitch")) for n in group if n.find("pitch") is not None))
        out.append(XmlEvent(tuple(group), head.findtext("voice") or "1",
                            None if grace else Fraction(group_at, divisions), Fraction(0) if grace else dur,
                            pitches, grace, head.findtext("type")))

    for el in measure:
        if el.tag == "backup":
            pos -= int(el.findtext("duration") or 0)
        elif el.tag == "forward":
            pos += int(el.findtext("duration") or 0)
        elif el.tag == "note":
            if int(el.findtext("staff") or 1) != staff:
                if el.find("chord") is None and el.find("grace") is None:
                    pos += int(el.findtext("duration") or 0)
                continue
            if el.find("chord") is not None and group:
                group.append(el)
                continue
            flush()
            group = [el]
            group_at = pos
            if el.find("grace") is None:
                pos += int(el.findtext("duration") or 0)
    flush()
    return out


# ---------------------------------------------------------------- event alignment


@dataclass(frozen=True)
class EventPair:
    # match: same pitches, duration and kind; substitute: aligned but different;
    # missing: in the PDF, absent from the MusicXML; extra: in the MusicXML, absent from the PDF
    kind: str
    pdf: Event | None
    xml: XmlEvent | None


def _pdf_pitches(e: Event) -> tuple[int, ...]:
    return tuple(sorted(n.midi for n in e.notes))


def _pair_score(a: Event, b: XmlEvent) -> float:
    pa = _pdf_pitches(a)
    same_onset = a.onset is not None and a.onset == b.onset
    if a.grace != b.grace:
        return -1.0
    if not pa and not b.pitches:
        return 2.0 + 0.5 * (a.duration == b.duration) + 0.5 * same_onset
    if not pa or not b.pitches:
        return -0.75 + 0.5 * same_onset
    if pa == b.pitches:
        p = 2.0
    elif set(pa) & set(b.pitches):
        p = 1.0
    else:
        p = 0.0
    return p + 0.5 * (a.duration == b.duration) + 0.5 * same_onset


def _is_match(a: Event, b: XmlEvent) -> bool:
    return a.grace == b.grace and _pdf_pitches(a) == b.pitches and a.duration == b.duration


_GAP = -0.5


def _ordered(events, onset_of, tiebreak) -> list:
    """Sort by onset, sounding events before rests, higher top note first; grace notes take the
    onset of the next sounding event so they sort just ahead of it."""
    keyed = []
    nxt: Fraction | None = None
    for i in range(len(events) - 1, -1, -1):
        e = events[i]
        on = onset_of(e)
        if on is not None:
            nxt = on
        keyed.append((e, on, nxt))
    keyed.reverse()
    out = []
    for e, on, nxt in keyed:
        pitches = _pdf_pitches(e) if isinstance(e, Event) else e.pitches
        when = on if on is not None else (nxt if nxt is not None else Fraction(10**6))
        out.append(((when, 0 if on is None else 1, 0 if pitches else 1, -max(pitches, default=0), tiebreak(e, len(out))), e))
    return [e for _, e in sorted(out, key=lambda t: t[0])]


def align_events(pdf_events: list[Event] | tuple[Event, ...], xml: list[XmlEvent]) -> tuple[list[EventPair], float]:
    """Global sequence alignment of one staff-measure; returns the pairs and the alignment score."""
    a = _ordered(sorted(pdf_events, key=lambda e: (e.x, e.y)), lambda e: e.onset, lambda e, i: (e.x, e.y))
    b = _ordered(list(xml), lambda e: e.onset, lambda e, i: (i, 0))
    n, m = len(a), len(b)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + _GAP
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + _GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = max(dp[i - 1][j - 1] + _pair_score(a[i - 1], b[j - 1]),
                           dp[i - 1][j] + _GAP, dp[i][j - 1] + _GAP)
    pairs: list[EventPair] = []
    i, j = n, m
    while i or j:
        if i and j and dp[i][j] == dp[i - 1][j - 1] + _pair_score(a[i - 1], b[j - 1]):
            kind = "match" if _is_match(a[i - 1], b[j - 1]) else "substitute"
            pairs.append(EventPair(kind, a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
        elif i and dp[i][j] == dp[i - 1][j] + _GAP:
            pairs.append(EventPair("missing", a[i - 1], None))
            i -= 1
        else:
            pairs.append(EventPair("extra", None, b[j - 1]))
            j -= 1
    pairs.reverse()
    return pairs, dp[n][m]


# ---------------------------------------------------------------- instruments


@dataclass(frozen=True)
class Instrument:
    """A player persisting across the piece, keyed by its first full label."""

    key: str
    name: str
    abbreviations: tuple[str, ...]
    staves: int


_ROMAN = re.compile(r"^(?:[ivx]+|\d+)$")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s.,()\-:/]+", text.lower()) if t]


def _drop_key(tokens: list[str]) -> list[str]:
    """Tokens without an "in <key>" transposition ("in f", "in bb", "in e flat")."""
    for i in range(1, len(tokens) - 1):
        if tokens[i] == "in" and re.fullmatch(r"[a-g](?:b|#|s|es|is)?", tokens[i + 1]):
            end = i + 3 if i + 2 < len(tokens) and tokens[i + 2] in ("flat", "sharp") else i + 2
            return tokens[:i] + tokens[end:]
    return tokens


def label_matches(short: str, full: str) -> bool:
    """`short` names the same instrument as `full`: equal, or each word an abbreviation of the
    matching word (first letters equal, letters in order), with numbers equal."""
    a, b = _tokens(short), _tokens(full)
    if not a or not b:
        return False
    if a == b:
        return True
    # Abbreviations drop the transposition: "Hn." for "Horn in F", "Cl." for "Clarinet in Bb".
    a, b = _drop_key(a), _drop_key(b)
    if len(a) != len(b):
        return _same_instrument(short, full)
    for s, f in zip(a, b):
        if _ROMAN.match(s) or _ROMAN.match(f):
            if s != f:
                return False
            continue
        if s[0] != f[0]:
            return _same_instrument(short, full)
        it = iter(f)
        if not all(ch in it for ch in s):
            return _same_instrument(short, full)
    return True


def _same_instrument(a: str, b: str) -> bool:
    """Both name one instrument in the table with the same numbering, as "Vc." and "Cello" do,
    though no letters line up. Choir voices are left to the letter match."""
    ia, ib = instruments.lookup(a), instruments.lookup(b)
    if ia is None or ib is None or ia.voice or ib.voice or ia.name != ib.name:
        return False
    return [t for t in _tokens(a) if _ROMAN.match(t)] == [t for t in _tokens(b) if _ROMAN.match(t)]


def _staff_label(system: PdfSystem, staff: int) -> str | None:
    lbs = system.labels_of(staff)
    names = [lb for lb in lbs if lb.kind == "name"] or list(lbs)
    # a shared staff stacks its names ("Soprano" over "Alto")
    return " / ".join(lb.text for lb in sorted(names, key=lambda lb: (lb.y, lb.x))) if names else None


def _grand_label(system: PdfSystem, k: int) -> bool:
    """The staff's label sits in the gap below it, where a grand staff centers its name, rather
    than beside the staff, where a single staff (or a stacked "S / A") puts it."""
    st = system.staves[k]
    names = [lb for lb in system.labels_of(st.index) if lb.kind == "name"] or list(system.labels_of(st.index))
    if not names or k + 1 >= len(system.staves):
        return False
    y = sum(lb.y for lb in names) / len(names)
    gap = (st.bottom + system.staves[k + 1].top) / 2
    return abs(y - gap) < abs(y - (st.top + st.bottom) / 2)


@dataclass(frozen=True)
class _StaffLook:
    """What identifies an unlabeled staff from system to system."""

    clef: str | None
    lyrics: bool
    pitch: float | None  # mean MIDI pitch of the staff's notes in the system


def _look(pdf: PdfScore, system: PdfSystem, staff: int) -> _StaffLook:
    clef = None
    lyrics = False
    pitches: list[int] = []
    for m in system.measures:
        c = pdf.content(m, staff)
        if c is None:
            continue
        if clef is None:
            clef = c.clef.sign
        for e in c.events:
            pitches.extend(_pdf_pitches(e))
        lyrics = lyrics or bool(pdf.lyrics_in(m.key(staff)))
    return _StaffLook(clef, lyrics, sum(pitches) / len(pitches) if pitches else None)


def _look_score(a: _StaffLook, b: _StaffLook) -> float:
    w = 1.0
    if a.clef is not None and a.clef == b.clef:
        w += 1.0
    if a.lyrics == b.lyrics:
        w += 0.5
    if a.pitch is not None and b.pitch is not None:
        # a voice's average wanders a few semitones between systems; only a register change counts
        w -= 0.5 * min(max(abs(a.pitch - b.pitch) - _PITCH_SLACK, 0.0) / 12, 1.0)
    return w


_PITCH_SLACK = 5.0
# Cost of passing over a known staff between two placed ones: on equal looks, a hidden staff is
# assumed as close to the labeled staff above as the order allows.
_SKIP = 0.1


def _place(looks: list[_StaffLook], slots: list[tuple[str, int]], seen: dict[tuple[str, int], _StaffLook]) -> list[int | None]:
    """Order-preserving placement of a run of unlabeled staves on known staves (the template slots
    between the labeled staves around the run), best on clef, lyrics and pitch."""
    n, m = len(looks), len(slots)
    neg = float("-inf")
    # dp[i][j]: best with the first i staves placed among the first j slots, the i-th on slot j-1
    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    back: dict[tuple[int, int], int] = {}
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        for j in range(i, m + 1):
            w = _look_score(looks[i - 1], seen[slots[j - 1]])
            best, arg = neg, -1
            for p in range(i - 1, j):
                if dp[i - 1][p] == neg:
                    continue
                v = dp[i - 1][p] - _SKIP * (j - 1 - p) + w
                if v > best:
                    best, arg = v, p
            dp[i][j], back[(i, j)] = best, arg
    if n == 0 or m < n:
        return [None] * n
    j = max(range(n, m + 1), key=lambda q: (dp[n][q], -q))
    out: list[int | None] = [None] * n
    for i in range(n, 0, -1):
        out[i - 1] = j - 1
        j = back[(i, j)]
    return out


def _resolve_instruments(pdf: PdfScore) -> tuple[list[Instrument], dict[tuple[int, int], tuple[str, int]]]:
    """(instruments, {(page, staff): (instrument key, staff within the instrument)}).

    A labeled staff is its label's instrument. A run of unlabeled staves is placed on the staves
    already known to lie between the labeled staves around it (a condensed score hides resting
    instruments, so the run need not sit where it did in the system before), matched on clef,
    lyrics and pitch. A staff with no known place continues a grand staff whose name is centered
    below the staff above, else starts an instrument of its own."""
    inst: dict[str, dict] = {}
    order: list[str] = []
    out: dict[tuple[int, int], tuple[str, int]] = {}
    template: list[tuple[str, int]] = []  # every staff seen, in score order
    seen: dict[tuple[str, int], _StaffLook] = {}

    def new(key: str, name: str) -> None:
        if key not in inst:
            inst[key] = {"name": name, "abbr": [], "staves": 1}
            order.append(key)

    for sy in pdf.systems:
        looks = [_look(pdf, sy, st.index) for st in sy.staves]
        row: list[tuple[str, int] | None] = []
        for st in sy.staves:
            text = _staff_label(sy, st.index)
            if text is None:
                row.append(None)
                continue
            key = next((k for k in order if text == inst[k]["name"] or text in inst[k]["abbr"]), None)
            if key is None:
                key = next((k for k in order if label_matches(text, inst[k]["name"])), None)
            if key is None:
                key = text
                new(key, text)
            elif text != inst[key]["name"] and text not in inst[key]["abbr"]:
                inst[key]["abbr"].append(text)
            row.append((key, 1))

        labeled = {v[0] for v in row if v is not None}
        k = 0
        while k < len(row):
            if row[k] is not None:
                k += 1
                continue
            end = k
            while end < len(row) and row[end] is None:
                end += 1
            lo = template.index(row[k - 1]) + 1 if k and row[k - 1] in template else 0
            hi = template.index(row[end]) if end < len(row) and row[end] in template else len(template)
            # under a staff seen for the first time, nothing known says what follows it
            fresh = k and row[k - 1] not in template
            slots = [] if fresh else [s for s in template[lo:hi]
                                      if not (s[0] in labeled and s[1] == 1) and s not in row]
            placed = _place(looks[k:end], slots, seen)
            for i, j in zip(range(k, end), placed):
                if j is not None:
                    row[i] = slots[j]
                    continue
                above = row[i - 1] if i else None
                if above is not None and (above[1] > 1 or _grand_label(sy, i - 1)):
                    row[i] = (above[0], above[1] + 1)
                else:
                    key = f"Staff {i + 1}"
                    new(key, key)
                    row[i] = (key, 1)
            k = end

        prev_slot: tuple[str, int] | None = None
        for st, v, lk in zip(sy.staves, row, looks):
            out[(sy.page, st.index)] = v
            inst[v[0]]["staves"] = max(inst[v[0]]["staves"], v[1])
            if v not in template:
                template.insert(template.index(prev_slot) + 1 if prev_slot in template else len(template), v)
            seen[v] = lk
            prev_slot = v
    return [Instrument(k, inst[k]["name"], tuple(inst[k]["abbr"]), inst[k]["staves"]) for k in order], out


# ---------------------------------------------------------------- alignment records


@dataclass(frozen=True)
class StaffMatch:
    system: int  # PdfSystem.order
    page: int
    staff: int  # page-wide Staff.index
    staff_in_system: int
    instrument: str  # Instrument.key
    instrument_staff: int  # 1-based staff within the instrument
    part: str | None  # MusicXML part id; None when no slot fits
    xml_staff: int  # <staff> number within the part
    score: float


@dataclass
class Cell:
    """One MusicXML part-staff-measure and the PDF staff-measure it corresponds to."""

    part: str
    xml_staff: int
    measure: XmlMeasure
    element: ET.Element
    pdf_measure: PdfMeasure
    key: Key
    content: StaffMeasureContent | None
    instrument: str
    pairs: list[EventPair]
    # the PDF bar is a multi-bar rest standing for several MusicXML bars; pairs are left empty
    multirest: bool = False

    def _of(self, kind: str) -> list[EventPair]:
        return [p for p in self.pairs if p.kind == kind]

    @property
    def matched(self) -> list[EventPair]:
        return self._of("match")

    @property
    def substituted(self) -> list[EventPair]:
        return self._of("substitute")

    @property
    def missing(self) -> list[EventPair]:
        return self._of("missing")

    @property
    def extra(self) -> list[EventPair]:
        return self._of("extra")


@dataclass(frozen=True)
class PartConflict:
    """A MusicXML part holding different instruments in different systems."""

    part: str
    # (first measure number, last measure number, instrument key), runs in piece order
    holdings: tuple[tuple[str, str, str], ...]


@dataclass
class Alignment:
    pdf: PdfScore
    instruments: tuple[Instrument, ...]
    xml_measures: tuple[XmlMeasure, ...]
    # PDF bar for each MusicXML measure index (None when the alignment left it unpaired)
    measure_map: dict[int, PdfMeasure | None]
    staves: tuple[StaffMatch, ...]
    cells: dict[tuple[str, int, int], Cell]  # (part, xml measure index, xml staff)
    conflicts: tuple[PartConflict, ...]
    part_order: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    _by_number: dict[tuple[str, int], int] = field(default_factory=dict, repr=False)
    _by_staff: dict[tuple[int, int], StaffMatch] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_number = {(m.number, m.occurrence): m.index for m in self.xml_measures}
        self._by_staff = {(s.page, s.staff): s for s in self.staves}

    def measure_index(self, number: str, occurrence: int = 0) -> int | None:
        return self._by_number.get((str(number), occurrence))

    def cell(self, part: str, number: str, staff: int = 1, occurrence: int = 0) -> Cell | None:
        """The aligned cell for a MusicXML part, measure number and staff."""
        idx = self.measure_index(number, occurrence)
        return None if idx is None else self.cells.get((part, idx, staff))

    def cells_of(self, part: str) -> list[Cell]:
        return sorted((c for c in self.cells.values() if c.part == part),
                      key=lambda c: (c.measure.index, c.xml_staff))

    def match_of(self, page: int, staff: int) -> StaffMatch | None:
        return self._by_staff.get((page, staff))

    @property
    def unmatched(self) -> list[StaffMatch]:
        return [s for s in self.staves if s.part is None]

    def parts_holding(self, instrument: str) -> list[tuple[int, str, int]]:
        """(system order, part, xml staff) wherever `instrument` is matched."""
        return [(s.system, s.part, s.xml_staff) for s in self.staves if s.instrument == instrument and s.part]


# ---------------------------------------------------------------- measures


def _pdf_signature(pdf: PdfScore, m: PdfMeasure, system: PdfSystem) -> int:
    total = 0
    for st in system.staves:
        c = pdf.content(m, st.index)
        if c is not None:
            total += sum(len(e.notes) for e in c.events if not e.grace)
    return total


def _xml_signature(parts: list[_PartInfo], xm: XmlMeasure) -> int:
    total = 0
    for p in parts:
        m = p.measures.get((xm.number, xm.occurrence))
        if m is None:
            continue
        total += sum(1 for n in m.iter("note") if n.find("pitch") is not None and n.find("grace") is None)
    return total


def _map_measures(pdf: PdfScore, parts: list[_PartInfo], xms: list[XmlMeasure]) -> tuple[dict[int, PdfMeasure | None], list[str]]:
    # bars as the MusicXML counts them: a multi-bar rest repeats its PdfMeasure
    units: list[tuple[PdfMeasure, bool]] = []
    for s in pdf.systems:
        for k, m in enumerate(s.measures):
            for j in range(m.span):
                units.append((m, k == 0 and j == 0))
    pdf_sys = [len([1 for m in s.measures for _ in range(m.span)]) for s in pdf.systems]
    xml_sys: list[int] = []
    for xm in xms:
        if xm.new_system:
            xml_sys.append(0)
        xml_sys[-1] += 1
    if pdf_sys == xml_sys:
        return {xm.index: units[i][0] for i, xm in enumerate(xms)}, []

    warnings = [f"system layout differs (PDF {len(pdf_sys)} systems, {len(units)} bars; "
                f"MusicXML {len(xml_sys)} systems, {len(xms)} bars); aligned on content"]
    by_page_sys = {(s.page, s.index): s for s in pdf.systems}
    a = [(_pdf_signature(pdf, m, by_page_sys[(m.page, m.system)]), first) for m, first in units]
    b = [(_xml_signature(parts, xm), xm.new_system) for xm in xms]

    def sim(p, q) -> float:
        s = 1.0 - abs(p[0] - q[0]) / max(p[0], q[0], 1)
        return s + (0.5 if p[1] and q[1] else 0.0)

    gap = -0.4
    n, mm = len(a), len(b)
    dp = [[0.0] * (mm + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap
    for j in range(1, mm + 1):
        dp[0][j] = dp[0][j - 1] + gap
    for i in range(1, n + 1):
        for j in range(1, mm + 1):
            dp[i][j] = max(dp[i - 1][j - 1] + sim(a[i - 1], b[j - 1]), dp[i - 1][j] + gap, dp[i][j - 1] + gap)
    out: dict[int, PdfMeasure | None] = {xm.index: None for xm in xms}
    i, j = n, mm
    while i and j:
        if dp[i][j] == dp[i - 1][j - 1] + sim(a[i - 1], b[j - 1]):
            out[xms[j - 1].index] = units[i - 1][0]
            i, j = i - 1, j - 1
        elif dp[i][j] == dp[i - 1][j] + gap:
            i -= 1
        else:
            j -= 1
    return out, warnings


# ---------------------------------------------------------------- staves to parts


@dataclass(frozen=True)
class _Slot:
    part: _PartInfo
    staff: int
    visible: bool


def _slots(parts: list[_PartInfo], xm: XmlMeasure) -> list[_Slot]:
    out = []
    key = (xm.number, xm.occurrence)
    for p in parts:
        if key not in p.measures:
            continue
        vis = p.visible.get(key, {})
        for s in range(1, p.staves + 1):
            out.append(_Slot(p, s, vis.get(s, vis.get(0, True))))
    return out


def _names_match(label: str | None, inst: Instrument, part: _PartInfo) -> bool:
    names = [n for n in (part.name, part.abbreviation) if n]
    cands = [inst.name, *inst.abbreviations] + ([label] if label else [])
    return any(label_matches(c, n) or label_matches(n, c) for c in cands for n in names)


def _assign(weights: list[list[float | None]]) -> list[int | None]:
    """Order-preserving assignment of rows (staves) to columns (slots) maximizing total weight;
    None weights are forbidden pairs. Returns the column per row."""
    n = len(weights)
    m = len(weights[0]) if n else 0
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            w = weights[i - 1][j - 1]
            if w is not None:
                best = max(best, dp[i - 1][j - 1] + w)
            dp[i][j] = best
    out: list[int | None] = [None] * n
    i, j = n, m
    while i and j:
        w = weights[i - 1][j - 1]
        if w is not None and dp[i][j] == dp[i - 1][j - 1] + w:
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif dp[i][j] == dp[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return out


def align(pdf: PdfScore, root: ET.Element) -> Alignment:
    parts = _parts(root)
    xms = _xml_measures(root)
    measure_map, warnings = _map_measures(pdf, parts, xms)
    instruments, inst_of = _resolve_instruments(pdf)
    inst_by_key = {i.key: i for i in instruments}

    xml_of_pdf: dict[int, list[XmlMeasure]] = defaultdict(list)
    for xm in xms:
        pm = measure_map.get(xm.index)
        if pm is not None:
            xml_of_pdf[pm.index].append(xm)

    events_cache: dict[tuple[str, int, int], list[XmlEvent]] = {}

    def events_of(p: _PartInfo, xm: XmlMeasure, staff: int) -> list[XmlEvent]:
        k = (p.id, xm.index, staff)
        if k not in events_cache:
            el = p.measures.get((xm.number, xm.occurrence))
            events_cache[k] = [] if el is None else xml_events(el, staff, p.divisions[(xm.number, xm.occurrence)])
        return events_cache[k]

    staves: list[StaffMatch] = []
    cells: dict[tuple[str, int, int], Cell] = {}
    for sy in pdf.systems:
        bars = [(m, xm) for m in sy.measures for xm in xml_of_pdf.get(m.index, [])]
        if not bars:
            for k, st in enumerate(sy.staves):
                key, ist = inst_of[(sy.page, st.index)]
                staves.append(StaffMatch(sy.order, sy.page, st.index, k, key, ist, None, 1, 0.0))
            warnings.append(f"page {sy.page + 1} system {sy.index + 1}: no MusicXML measures")
            continue
        slots = _slots(parts, bars[0][1])
        n, m = len(sy.staves), len(slots)
        band = max(4, abs(n - m) + 3)
        weights: list[list[float | None]] = []
        for i, st in enumerate(sy.staves):
            ikey, _ = inst_of[(sy.page, st.index)]
            label = _staff_label(sy, st.index)
            row: list[float | None] = []
            for j, sl in enumerate(slots):
                if abs(i - j) > band:
                    row.append(None)
                    continue
                content = 0.0
                for pm, xm in bars:
                    c = pdf.content(pm, st.index)
                    xev = events_of(sl.part, xm, sl.staff)
                    pev = c.events if c is not None else ()
                    if c is not None and c.multirest:
                        content += 1.0 if all(e.is_rest for e in xev) else 0.0
                        continue
                    _, score = align_events(pev, xev)
                    content += score / max(len(pev), len(xev), 1)
                w = 1.0 + content / len(bars)
                c0 = pdf.content(bars[0][0], st.index)
                xclef = sl.part.clefs.get((bars[0][1].number, bars[0][1].occurrence), {}).get(sl.staff)
                if c0 is not None and xclef is not None and c0.clef.sign == xclef:
                    w += 0.5
                if _names_match(label, inst_by_key[ikey], sl.part):
                    w += 1.0
                if not sl.visible:
                    w -= 1.5
                row.append(w)
            weights.append(row)
        chosen = _assign(weights) if m else [None] * n
        for k, (st, j) in enumerate(zip(sy.staves, chosen)):
            ikey, ist = inst_of[(sy.page, st.index)]
            if j is None:
                staves.append(StaffMatch(sy.order, sy.page, st.index, k, ikey, ist, None, 1, 0.0))
                continue
            sl = slots[j]
            staves.append(StaffMatch(sy.order, sy.page, st.index, k, ikey, ist, sl.part.id, sl.staff,
                                     round(weights[k][j] or 0.0, 4)))
            for pm, xm in bars:
                el = sl.part.measures.get((xm.number, xm.occurrence))
                if el is None:
                    warnings.append(f"part {sl.part.id} has no measure {xm.number}")
                    continue
                c = pdf.content(pm, st.index)
                multirest = pm.span > 1
                pairs = [] if multirest else align_events(c.events if c else (), events_of(sl.part, xm, sl.staff))[0]
                cells[(sl.part.id, xm.index, sl.staff)] = Cell(sl.part.id, sl.staff, xm, el, pm, pm.key(st.index), c,
                                                               ikey, pairs, multirest)

    conflicts = _conflicts(staves, pdf, xml_of_pdf, [p.id for p in parts])
    return Alignment(pdf, tuple(instruments), tuple(xms), measure_map, tuple(staves), cells, tuple(conflicts),
                     tuple(p.id for p in parts), tuple(warnings))


def _conflicts(staves: list[StaffMatch], pdf: PdfScore, xml_of_pdf: dict[int, list[XmlMeasure]],
               part_order: list[str]) -> list[PartConflict]:
    runs: dict[str, list[list[str]]] = defaultdict(list)
    for s in sorted(staves, key=lambda s: (s.system, s.staff)):
        if s.part is None or s.xml_staff != 1:
            continue
        sy = pdf.systems[s.system]
        xs = [xm for m in sy.measures for xm in xml_of_pdf.get(m.index, [])]
        if not xs:
            continue
        first, last = xs[0].number, xs[-1].number
        r = runs[s.part]
        if r and r[-1][2] == s.instrument:
            r[-1][1] = last
        else:
            r.append([first, last, s.instrument])
    out = []
    for part in sorted(runs, key=part_order.index):
        held = runs[part]
        if len({h[2] for h in held}) > 1:
            out.append(PartConflict(part, tuple((a, b, c) for a, b, c in held)))
    return out


__all__ = [
    "Alignment", "Cell", "Change", "ChangeLog", "EventPair", "Instrument", "PartConflict", "StaffMatch",
    "XmlEvent", "XmlMeasure", "align", "align_events", "label_matches", "measure_sort_key", "xml_events",
]
