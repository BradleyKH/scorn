"""Marks pass: dynamics, hairpins, articulations, ornaments, tremolos, fermatas, arpeggios,
pedal marks, ties and slurs are removed from every aligned staff-measure and rebuilt from the
PDF's marks.

A category is left alone when the PDF reader found too few of it to be trusted (a font or a
curve style it does not understand); that is judged from whole-document counts and logged.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from ..align import Alignment, Cell, ChangeLog, XmlEvent, xml_events
from ..pdf_marks import Mark
from ..pdf_score import PdfScore

CATEGORIES = ("dynamics", "hairpins", "articulations", "ornaments", "fermatas", "arpeggios",
              "pedals", "ties", "slurs")
_GLYPH_CATEGORIES = ("dynamics", "articulations", "ornaments", "fermatas", "arpeggios", "pedals")
_KIND_CATEGORY = {
    "dynamic": "dynamics", "hairpin": "hairpins", "articulation": "articulations",
    "breath-mark": "articulations", "caesura": "articulations", "ornament": "ornaments",
    "tremolo": "ornaments", "fermata": "fermatas", "arpeggio": "arpeggios", "pedal": "pedals",
    "tie": "ties", "slur": "slurs",
}
_DYNAMICS = {"p", "pp", "ppp", "pppp", "ppppp", "pppppp", "f", "ff", "fff", "ffff", "fffff", "ffffff",
             "mp", "mf", "sf", "sfp", "sfpp", "fp", "rf", "rfz", "sfz", "sffz", "fz", "n", "pf", "sfzp"}
_ARTICULATIONS = {"accent", "strong-accent", "staccato", "tenuto", "detached-legato", "staccatissimo"}
_ORNAMENTS = {"trill-mark", "mordent", "inverted-mordent", "turn"}
# Counts below this never mark a reader as blind; one stray mark is not evidence either way.
_MIN_EVIDENCE = 3
_PARTIAL_SHARE = 0.5
# A broken curve's continuation starts within this many staff spaces of the system start.
_CONTINUATION_REACH = 8.0
# A dynamic this close to the right barline (staff spaces) belongs to the next bar.
_BARLINE_REACH = 0.8
_PARTIAL_MIN = 10
_NOTE_ORDER_AFTER = ("lyric", "play", "footnote", "level")


def run(root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
    _Pass(root, pdf, alignment, log).run()


# ---------------------------------------------------------------- per-bar geometry


@dataclass(frozen=True)
class _Anchor:
    x: float
    event: XmlEvent
    ys: tuple[float, ...]
    midis: tuple[tuple[int, float], ...]  # (pitch, PDF head y) of the PDF event's notes


@dataclass
class _Bar:
    cell: Cell
    events: list[XmlEvent]
    anchors: list[_Anchor]
    end_event: XmlEvent | None
    end_onset: Fraction
    spacing: float
    divisions: int

    @property
    def index(self) -> int:
        return self.cell.measure.index


@dataclass
class _NoteMarks:
    articulations: list[tuple[str, str | None]] = field(default_factory=list)
    ornaments: list[str] = field(default_factory=list)
    tremolo: int | None = None
    fermata: str | None = None
    arpeggio: bool = False
    tie_stop: bool = False
    tie_start: bool = False
    slurs: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class _Insert:
    measure: ET.Element
    anchor: ET.Element
    where: str  # before | after
    order: int  # stops sort ahead of starts at one spot
    element: ET.Element


@dataclass
class _Span:
    start: tuple
    stop: tuple
    start_note: ET.Element | None = None
    stop_note: ET.Element | None = None
    start_insert: ET.Element | None = None
    stop_insert: ET.Element | None = None
    kind: str = ""
    label: str = ""


def _midi(note: ET.Element) -> int | None:
    p = note.find("pitch")
    if p is None:
        return None
    step = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[p.findtext("step")]
    return 12 * (int(p.findtext("octave")) + 1) + step + int(float(p.findtext("alter") or 0))


def _label_time(pos: Fraction | float) -> str:
    return f"{float(pos):.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- the pass


class _Pass:
    def __init__(self, root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
        self.root, self.pdf, self.al, self.log = root, pdf, alignment, log
        self.parts = {p.get("id"): p for p in root.findall("part")}
        self.bars: dict[tuple[str, int, int], _Bar | None] = {}
        self.cell_at: dict[tuple[str, int, int], Cell] = {}
        self.divisions: dict[int, int] = {}
        self.before: dict[tuple[str, str, str], Counter] = defaultdict(Counter)
        self.after: dict[tuple[str, str, str], Counter] = defaultdict(Counter)
        self.staff_tops: dict[tuple[int, int], tuple[float, float]] = {}
        for g in pdf.geometries:
            for s in g.staves:
                self.staff_tops[(g.page, s.index)] = (s.top, s.spacing)

    # -------- orchestration

    def run(self) -> None:
        trusted = self._trusted()
        if not trusted:
            return
        self._continuations()
        cells = self._usable_cells()
        by_part: dict[str, list[Cell]] = defaultdict(list)
        for c in cells:
            by_part[c.part].append(c)
        for part_id in sorted(by_part, key=lambda p: self.al.part_order.index(p) if p in self.al.part_order else 10**6):
            if part_id in self.parts:
                self._part(part_id, by_part[part_id], trusted)
        for (cat, part, number), removed in sorted(self.before.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
            added = self.after.get((cat, part, number), Counter())
            gone, new = removed - added, added - removed
            if gone or new:
                self.log.add(cat, part, number, ", ".join(sorted(gone.elements())),
                             ", ".join(sorted(new.elements())), "rebuilt from PDF marks")
        for key, added in sorted(self.after.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
            if key not in self.before and added:
                self.log.add(key[0], key[1], key[2], "", ", ".join(sorted(added.elements())), "added from PDF marks")

    def _continuations(self) -> None:
        """Pair curves broken at a system end with their continuation. The reader pairs the ones it flags
        as open at both ends; a continuation whose left tip sits a little right of the system start is
        not flagged, so look for it by position."""
        self.partner = {id(a): b for a, b in self.pdf.curve_pairs}
        self.second = {id(b) for _, b in self.pdf.curve_pairs}
        systems = self.pdf.systems
        for m in self.pdf.marks:
            if m.kind not in ("tie", "slur") or not m.open_end or id(m) in self.partner:
                continue
            s = self.pdf.system_of(m.page, m.staff)
            if s is None or s.order + 1 >= len(systems):
                continue
            nxt = systems[s.order + 1]
            k = next(i for i, st in enumerate(s.staves) if st.index == m.staff)
            if k >= len(nxt.staves) or not nxt.measures:
                continue
            st, here = nxt.staves[k], s.staves[k]
            rel = (m.y_end if m.y_end is not None else m.y) - here.top
            cands = [c for c in self.pdf.marks
                     if c.kind == m.kind and c.page == nxt.page and c.staff == st.index
                     and id(c) not in self.second and c.x <= nxt.measures[0].x0 + _CONTINUATION_REACH * st.spacing
                     and abs((c.y - st.top) - rel) <= 2.5 * st.spacing]
            if cands:
                c = min(cands, key=lambda c: c.x)
                self.partner[id(m)] = c
                self.second.add(id(c))

    def _usable_cells(self) -> list[Cell]:
        out = []
        seen: set[tuple[str, int, int]] = set()
        for c in sorted(self.al.cells.values(), key=lambda c: (c.part, c.measure.index, c.xml_staff)):
            if c.content is None:
                continue
            out.append(c)
            k = (c.part, c.xml_staff, c.pdf_measure.index)
            if k not in seen:  # a multi-bar rest maps several bars to one PDF bar; marks belong to the first
                seen.add(k)
                self.cell_at[k] = c
        return out

    # -------- trust

    def _trusted(self) -> set[str]:
        xml: Counter = Counter()
        for n in self.root.iter("dynamics"):
            xml["dynamics"] += len(n)
        for w in self.root.iter("wedge"):
            xml["hairpins"] += w.get("type") != "stop"
        for n in self.root.iter("articulations"):
            xml["articulations"] += len(n)
        for n in self.root.iter("ornaments"):
            xml["ornaments"] += len(n)
        for n in self.root.iter("notations"):
            xml["fermatas"] += len(n.findall("fermata"))
            xml["arpeggios"] += len(n.findall("arpeggiate"))
            xml["slurs"] += sum(s.get("type") == "start" for s in n.findall("slur"))
        xml["pedals"] = sum(1 for _ in self.root.iter("pedal"))
        xml["ties"] = sum(t.get("type") == "start" for t in self.root.iter("tie"))

        seen: Counter = Counter()
        for m in self.pdf.marks:
            seen[_KIND_CATEGORY.get(m.kind, "")] += 1
        for tc in self.pdf.text.pages:
            seen["dynamics"] += sum(1 for d in tc.directions if d.dynamic in _DYNAMICS)
        glyph_total = sum(seen[c] for c in _GLYPH_CATEGORIES)

        trusted = set()
        for cat in CATEGORIES:
            n_xml, n_pdf = xml[cat], seen[cat]
            reason = None
            if cat in _GLYPH_CATEGORIES:
                if (glyph_total == 0 or (cat == "dynamics" and n_pdf == 0)) and n_xml >= _MIN_EVIDENCE:
                    reason = "no glyph marks read from the PDF"
            elif n_pdf == 0 and n_xml >= _MIN_EVIDENCE:
                reason = "none read from the PDF"
            elif n_xml >= _PARTIAL_MIN and n_pdf < _PARTIAL_SHARE * n_xml:
                reason = "PDF reader found under half"
            if reason:
                self.log.disagree(cat, "*", "", f"{n_xml} in MusicXML", f"{n_pdf} read from PDF",
                                  f"{reason}; category left alone")
            else:
                trusted.add(cat)
        return trusted

    # -------- one part

    def _part(self, part_id: str, cells: list[Cell], trusted: set[str]) -> None:
        part = self.parts[part_id]
        div = 1
        for m in part.findall("measure"):
            d = (m.findtext("attributes/divisions") or "").strip()
            if d:
                div = int(d)
            self.divisions[id(m)] = div
        self.nstaves = max([c.xml_staff for c in cells] + [int(part.findtext("measure/attributes/staves") or 1)])

        for c in cells:
            self._strip(part_id, c, trusted)

        notes: dict[ET.Element, _NoteMarks] = {}
        inserts: list[_Insert] = []
        wedges: list[_Span] = []
        slurs: list[_Span] = []
        marks_by_cell = []
        second_pieces, partner = self.second, self.partner
        done_keys: set[tuple[str, int, int]] = set()
        for c in cells:
            k = (c.part, c.xml_staff, c.pdf_measure.index)
            if self.cell_at.get(k) is not c or c.key in done_keys:
                continue
            done_keys.add(c.key)
            marks_by_cell.append((c, self._cell_marks(c, trusted)))

        ctx = (notes, inserts, wedges, slurs, partner, second_pieces)
        for c, marks in marks_by_cell:
            for m in marks:
                self._place(part_id, c, m, ctx)

        self._write(part_id, part, notes, inserts, wedges, slurs, trusted)
        self._drop_dangling(part_id, part, trusted)

    def _cell_marks(self, cell: Cell, trusted: set[str]) -> list[Mark]:
        marks = [m for m in self.pdf.marks_in(cell.key) if _KIND_CATEGORY.get(m.kind) in trusted]
        if "dynamics" in trusted:
            glyph = [m for m in marks if m.kind == "dynamic"]
            for d in self.pdf.directions_in(cell.key):
                if d.dynamic not in _DYNAMICS:
                    continue
                if any(g.name == d.dynamic and abs(g.x - d.x) < 3 for g in glyph):
                    continue
                marks.append(Mark("dynamic", d.dynamic, d.page, d.staff, d.x, d.y,
                                  placement=d.placement if d.placement in ("above", "below") else None))
        return sorted(marks, key=lambda m: (m.x, m.kind, m.name, m.y))

    # -------- removal

    def _strip(self, part_id: str, cell: Cell, trusted: set[str]) -> None:
        measure = cell.element
        div = self.divisions.get(id(measure), 1)
        staff = cell.xml_staff
        number = cell.measure.number
        pos = Fraction(0)
        last_start = Fraction(0)
        drop: list[ET.Element] = []

        def note(cat: str, text: str) -> None:
            self.before[(cat, part_id, number)][text] += 1

        # The reader misses some curves, so the scan's ties stay wherever the PDF bar draws one;
        # in a bar with none, a scan tie has nothing behind it.
        c = cell.content
        drop_ties = "ties" in trusted and c is not None and not any(
            m.kind in ("tie", "slur") and m.page == c.page and m.staff == c.staff
            and m.x <= c.x1 and (m.x_end or m.x) >= c.x0 for m in self.pdf.marks)

        for el in list(measure):
            if el.tag == "backup":
                pos -= Fraction(int(el.findtext("duration") or 0), div)
            elif el.tag == "forward":
                pos += Fraction(int(el.findtext("duration") or 0), div)
            elif el.tag == "direction":
                if int(el.findtext("staff") or 1) != staff:
                    continue
                for dt in list(el.findall("direction-type")):
                    removed = False
                    t = _label_time(pos)
                    if "dynamics" in trusted and dt.find("dynamics") is not None:
                        for d in dt.find("dynamics"):
                            note("dynamics", f"{d.tag}@{t}")
                        removed = True
                    if "hairpins" in trusted and dt.find("wedge") is not None:
                        note("hairpins", f"{dt.find('wedge').get('type')}@{t}")
                        removed = True
                    if "pedals" in trusted and dt.find("pedal") is not None:
                        note("pedals", f"pedal-{dt.find('pedal').get('type')}@{t}")
                        removed = True
                    if removed:
                        el.remove(dt)
                if el.find("direction-type") is None:
                    drop.append(el)
            elif el.tag == "note":
                chord = el.find("chord") is not None
                grace = el.find("grace") is not None
                if not chord:
                    last_start = pos
                if int(el.findtext("staff") or 1) == staff:
                    self._strip_note(el, trusted, _label_time(last_start), note, drop_ties)
                if not chord and not grace:
                    pos += Fraction(int(el.findtext("duration") or 0), div)
        for el in drop:
            measure.remove(el)

    @staticmethod
    def _strip_note(el: ET.Element, trusted: set[str], t: str, note, drop_ties: bool) -> None:
        for n in list(el.findall("notations")):
            if "articulations" in trusted:
                for a in n.findall("articulations"):
                    for c in a:
                        note("articulations", f"{c.tag}@{t}")
                    n.remove(a)
            if "ornaments" in trusted:
                for o in n.findall("ornaments"):
                    for c in o:
                        note("ornaments", f"{c.tag}@{t}")
                    n.remove(o)
            if "fermatas" in trusted:
                for f in n.findall("fermata"):
                    note("fermatas", f"fermata@{t}")
                    n.remove(f)
            if "arpeggios" in trusted:
                for f in n.findall("arpeggiate"):
                    note("arpeggios", f"arpeggio@{t}")
                    n.remove(f)
            if "slurs" in trusted:
                for s in n.findall("slur"):
                    if s.get("type") == "start":
                        note("slurs", f"slur@{t}")
                    n.remove(s)
            if drop_ties:
                for s in n.findall("tied"):
                    n.remove(s)
            if len(n) == 0:
                el.remove(n)
        if drop_ties:
            for tie in el.findall("tie"):
                if tie.get("type") == "start":
                    note("ties", f"tie@{t}")
                el.remove(tie)

    # -------- bars

    def _bar(self, part_id: str, xml_staff: int, cell: Cell) -> _Bar | None:
        key = (part_id, xml_staff, cell.measure.index)
        if key in self.bars:
            return self.bars[key]
        bar = self._build_bar(cell)
        self.bars[key] = bar
        return bar

    def _build_bar(self, cell: Cell) -> _Bar | None:
        div = self.divisions.get(id(cell.element), 1)
        events = [e for e in xml_events(cell.element, cell.xml_staff, div) if not e.grace and e.onset is not None]
        if not events:
            return None
        end = max(range(len(events)), key=lambda i: (events[i].onset + events[i].duration, i))
        end_event = events[end]
        end_onset = end_event.onset + end_event.duration
        anchors: list[_Anchor] = []
        for p in cell.pairs:
            if p.kind in ("match", "substitute") and p.pdf is not None and p.xml is not None and not p.xml.grace:
                anchors.append(_Anchor(p.pdf.x, p.xml, tuple(n.y for n in p.pdf.notes),
                                       tuple((n.midi, n.y) for n in p.pdf.notes)))
        pm = cell.pdf_measure
        if not anchors and end_onset > 0:
            for e in events:
                anchors.append(_Anchor(pm.x0 + float(e.onset / end_onset) * (pm.x1 - pm.x0), e, (), ()))
        anchors.sort(key=lambda a: (a.x, a.event.onset, a.event.voice))
        return _Bar(cell, events, anchors, end_event, end_onset, self._spacing(cell), div)

    def _spacing(self, cell: Cell) -> float:
        return self.staff_tops.get((cell.pdf_measure.page, cell.key[1]), (0.0, 3.6))[1]

    # -------- placing marks

    def _target(self, part_id: str, cell: Cell, mark: Mark) -> tuple[Cell, int]:
        """Dynamics and hairpins drawn between two manual staves belong to the upper one."""
        if mark.kind in ("dynamic", "hairpin", "pedal") and cell.xml_staff > 1:
            top = self.staff_tops.get((mark.page, mark.staff))
            if top is not None and mark.y < top[0]:
                upper = self.al.cells.get((part_id, cell.measure.index, 1))
                if upper is not None and upper.content is not None:
                    return upper, 1
        return cell, cell.xml_staff

    def _pick(self, bar: _Bar, x: float, y: float | None = None, notes_only: bool = False,
              keep=None) -> _Anchor | None:
        cands = [a for a in bar.anchors if not (notes_only and a.event.is_rest)]
        if keep is not None:
            cands = [a for a in cands if keep(a)] or cands
        if not cands:
            return None
        best = min(abs(a.x - x) for a in cands)
        near = [a for a in cands if abs(a.x - x) <= best + 1.5]

        def cost(a: _Anchor):
            dy = min((abs(y - v) for v in a.ys), default=0.0) if y is not None and a.ys else 0.0
            return (round(dy, 1), abs(a.x - x), a.event.onset, a.event.voice)

        return min(near, key=cost)

    def _place(self, part_id: str, cell: Cell, mark: Mark, ctx) -> None:
        notes, inserts, wedges, slurs, partner, second_pieces = ctx
        cell, staff = self._target(part_id, cell, mark)
        bar = self._bar(part_id, staff, cell)
        number = cell.measure.number
        cat = _KIND_CATEGORY[mark.kind]

        def skip(why: str) -> None:
            self.log.disagree(cat, part_id, number, "", f"{mark.kind} {mark.name}", why)

        if bar is None:
            return skip("no events in the aligned staff-measure")
        kind = mark.kind
        if kind in ("tie", "slur"):
            if id(mark) in second_pieces:
                return
            return self._curve(part_id, cell, bar, mark, ctx, skip)
        if kind == "hairpin":
            return self._hairpin(part_id, cell, bar, mark, ctx, skip)
        if kind in ("dynamic", "pedal"):
            if mark.x >= cell.pdf_measure.x1 - _BARLINE_REACH * bar.spacing:
                # drawn against the right barline, it belongs to the first note of the next bar
                nxt = self.cell_at.get((part_id, staff, cell.pdf_measure.index + 1))
                nbar = self._bar(part_id, staff, nxt) if nxt is not None and nxt.pdf_measure.system == cell.pdf_measure.system else None
                if nbar is not None:
                    cell, bar, number = nxt, nbar, nxt.measure.number
            a = self._pick(bar, mark.x)
            if a is None:
                return skip("no event near the mark")
            if kind == "dynamic":
                dt = ET.Element("direction-type")
                ET.SubElement(ET.SubElement(dt, "dynamics"), mark.name)
                label = mark.name
            else:
                dt = ET.Element("direction-type")
                ET.SubElement(dt, "pedal", {"type": mark.name})
                label = f"pedal-{mark.name}"
            self._direction(inserts, bar, a.event, dt, mark.placement, staff, 1)
            self._added(cat, part_id, number, f"{label}@{_label_time(a.event.onset)}")
            return
        a = self._pick(bar, mark.x, mark.y, notes_only=(kind not in ("fermata",)))
        if a is None:
            return skip("no note near the mark")
        nm = notes.setdefault(self._note_for(a, mark.y), _NoteMarks())
        t = _label_time(a.event.onset)
        if kind == "articulation" and mark.name in _ARTICULATIONS:
            nm.articulations.append((mark.name, mark.placement))
            self._added(cat, part_id, number, f"{mark.name}@{t}")
        elif kind in ("breath-mark", "caesura"):
            nm.articulations.append((kind, mark.placement))
            self._added(cat, part_id, number, f"{kind}@{t}")
        elif kind == "ornament" and mark.name in _ORNAMENTS:
            nm.ornaments.append(mark.name)
            self._added(cat, part_id, number, f"{mark.name}@{t}")
        elif kind == "tremolo":
            nm.tremolo = mark.value or 3
            self._added(cat, part_id, number, f"tremolo@{t}")
        elif kind == "fermata":
            nm.fermata = mark.name if mark.name in ("upright", "inverted") else "upright"
            self._added(cat, part_id, number, f"fermata@{t}")
        elif kind == "arpeggio":
            nm.arpeggio = True
            self._added(cat, part_id, number, f"arpeggio@{t}")
        else:
            skip("mark name has no MusicXML form")

    def _added(self, cat: str, part_id: str, number: str, label: str) -> None:
        self.after[(cat, part_id, number)][label] += 1

    @staticmethod
    def _note_for(a: _Anchor, y: float | None) -> ET.Element:
        notes = a.event.notes
        if y is not None and a.midis and len(notes) > 1:
            target = min(a.midis, key=lambda m: abs(m[1] - y))[0]
            for n in notes:
                if _midi(n) == target:
                    return n
        return notes[0]

    def _direction(self, inserts: list[_Insert], bar: _Bar, event: XmlEvent | None, dt: ET.Element,
                   placement: str | None, staff: int, order: int, end: bool = False) -> ET.Element:
        d = ET.Element("direction", {"placement": placement} if placement else {})
        d.append(dt)
        if self.nstaves > 1:
            ET.SubElement(d, "staff").text = str(staff)
        if end:
            inserts.append(_Insert(bar.cell.element, bar.end_event.notes[-1], "after", order, d))
        else:
            inserts.append(_Insert(bar.cell.element, event.notes[0], "before", order, d))
        return d

    # -------- hairpins

    def _hairpin(self, part_id: str, cell: Cell, bar: _Bar, mark: Mark, ctx, skip) -> None:
        notes, inserts, wedges, slurs, partner, second = ctx
        sp = bar.spacing
        before = [a for a in bar.anchors if a.x <= mark.x + 0.75 * sp]
        start = before[-1] if before else (bar.anchors[0] if bar.anchors else None)
        if start is None or mark.x_end is None:
            return skip("no event at the hairpin start")
        staff = bar.cell.xml_staff
        pm = self.pdf.measure_at(mark.page, mark.staff, mark.x_end - 0.5)
        ebar = bar
        if pm is not None and pm.index != cell.pdf_measure.index:
            ecell = self.cell_at.get((part_id, staff, pm.index))
            ebar = self._bar(part_id, staff, ecell) if ecell is not None else None
        if ebar is None:
            return skip("hairpin end is outside the aligned bars")
        end_event = None
        if pm is not None and mark.x_end >= pm.x1 - sp:
            end_event = None
        else:
            later = [a for a in ebar.anchors if a.x >= mark.x_end - 0.6 * sp]
            end_event = later[0].event if later else None
        if end_event is not None and ebar is bar and end_event == start.event:
            end_event = None
        if ebar is bar and end_event is not None and (end_event.onset or 0) <= (start.event.onset or 0):
            end_event = None
        stop_key = (ebar.index, ebar.end_onset if end_event is None else end_event.onset)
        start_key = (bar.index, start.event.onset)
        if stop_key <= start_key:
            return skip("hairpin ends before it starts")
        span = _Span(start_key, stop_key, kind=mark.name)
        dt_start = ET.Element("direction-type")
        w = ET.SubElement(dt_start, "wedge", {"type": mark.name})
        dt_stop = ET.Element("direction-type")
        w2 = ET.SubElement(dt_stop, "wedge", {"type": "stop"})
        span.start_insert, span.stop_insert = w, w2
        self._direction(inserts, bar, start.event, dt_start, mark.placement, staff, 1)
        self._direction(inserts, ebar, end_event, dt_stop, mark.placement, staff, 0, end=end_event is None)
        wedges.append(span)
        number = cell.measure.number
        self._added("hairpins", part_id, number, f"{mark.name}@{_label_time(start.event.onset)}")
        self._added("hairpins", part_id, ebar.cell.measure.number,
                    f"stop@{_label_time(ebar.end_onset if end_event is None else end_event.onset)}")

    # -------- ties and slurs

    def _curve(self, part_id: str, cell: Cell, bar: _Bar, mark: Mark, ctx, skip) -> None:
        notes, inserts, wedges, slurs, partner, second = ctx
        if mark.open_start:
            return skip(f"{mark.kind} has no start note in the PDF")
        last = partner.get(id(mark), mark)
        if last.open_end:
            return skip(f"{mark.kind} runs off the end of the PDF system without a continuation")
        # A tie leaves its first note from the head's right side, past that head's origin, so a
        # short tie can sit nearer the next note than its own.
        tie = mark.kind == "tie"
        sp = bar.spacing
        a = self._pick(bar, mark.x, mark.y, notes_only=True,
                       keep=(lambda c: c.x <= mark.x + 0.25 * sp) if tie else None)
        end_match = self.al.match_of(last.page, last.staff)
        if a is None or end_match is None or end_match.part != part_id:
            return skip(f"{mark.kind} start or end is not on an aligned note")
        pm = self.pdf.measure_at(last.page, last.staff, (last.x_end or last.x) - 0.5)
        ecell = self.cell_at.get((part_id, bar.cell.xml_staff, pm.index)) if pm is not None else None
        if ecell is None or end_match.xml_staff != bar.cell.xml_staff:
            return skip(f"{mark.kind} end is outside the aligned bars")
        ebar = self._bar(part_id, ecell.xml_staff, ecell)
        b = self._pick(ebar, last.x_end or last.x, last.y_end, notes_only=True,
                       keep=(lambda c: c.x > a.x) if tie and ebar is bar else None) if ebar else None
        if b is None:
            return skip(f"{mark.kind} end is not on a note")
        skey, ekey = (bar.index, a.event.onset), (ebar.index, b.event.onset)
        if ekey <= skey:
            return skip(f"{mark.kind} starts and ends on the same note")
        t = _label_time(a.event.onset)
        number = cell.measure.number
        kind = mark.kind
        if kind == "slur" and last is not mark and self._adjacent_repeat(bar, a, ebar, b):
            kind = "tie"  # the reader calls every curve broken at a system end a slur
        if kind == "tie":
            a_notes, b_notes = {}, {}
            for n in a.event.notes:
                a_notes.setdefault(_midi(n), n)
            for n in b.event.notes:
                b_notes.setdefault(_midi(n), n)
            common = [m for m in a_notes if m is not None and m in b_notes]
            if not common:
                return skip("tie ends on a different pitch than it starts")
            if len(common) > 1 and a.midis:
                near = min((m for m in a.midis if m[0] in common), key=lambda m: abs(m[1] - mark.y), default=None)
                if near is not None:
                    common = [near[0]]
            for m in common:
                notes.setdefault(a_notes[m], _NoteMarks()).tie_start = True
                notes.setdefault(b_notes[m], _NoteMarks()).tie_stop = True
            self._added("ties", part_id, number, f"tie@{t}")
        else:
            span = _Span(skey, ekey, self._note_for(a, mark.y), self._note_for(b, last.y_end), "slur")
            slurs.append(span)
            self._added("slurs", part_id, number, f"slur@{t}")

    @staticmethod
    def _adjacent_repeat(bar: _Bar, a: _Anchor, ebar: _Bar, b: _Anchor) -> bool:
        """Same single pitch on consecutive notes of the staff, with nothing sounding between."""
        if a.event.is_rest or b.event.is_rest or a.event.pitches != b.event.pitches:
            return False
        if ebar.index == bar.index:
            between = [e for e in bar.events if not e.is_rest and a.event.onset < e.onset < b.event.onset]
        elif ebar.index == bar.index + 1:
            between = [e for e in bar.events if not e.is_rest and e.onset > a.event.onset]
            between += [e for e in ebar.events if not e.is_rest and e.onset < b.event.onset]
        else:
            return False
        return not between

    # -------- writing

    def _write(self, part_id, part, notes, inserts, wedges, slurs, trusted) -> None:
        for span, number in zip(wedges, self._numbers(wedges, closed=False)):
            span.start_insert.set("number", str(number))
            span.stop_insert.set("number", str(number))
        busy = self._retained_slurs(part)
        for span, number in zip(slurs, self._numbers(slurs, closed=True, busy=busy)):
            notes.setdefault(span.start_note, _NoteMarks()).slurs.append(("start", number))
            notes.setdefault(span.stop_note, _NoteMarks()).slurs.append(("stop", number))
        for el, nm in notes.items():
            self._write_note(el, nm)

        groups: dict[tuple[int, str], list[_Insert]] = defaultdict(list)
        for ins in inserts:
            groups[(id(ins.anchor), ins.where)].append(ins)
        for items in groups.values():
            items.sort(key=lambda i: i.order)
            measure, anchor = items[0].measure, items[0].anchor
            idx = list(measure).index(anchor)
            at = idx if items[0].where == "before" else idx + 1
            for k, ins in enumerate(items):
                measure.insert(at + k, ins.element)

    @staticmethod
    def _retained_slurs(part: ET.Element) -> list[tuple[int, tuple, tuple]]:
        """Slurs left in unaligned bars keep their numbers busy across their whole measure range."""
        busy = []
        open_at: dict[str, int] = {}
        for mi, m in enumerate(part.findall("measure")):
            for s in m.iter("slur"):
                n = s.get("number") or "1"
                if s.get("type") == "start":
                    open_at[n] = mi
                elif n in open_at:
                    busy.append((int(n), (open_at.pop(n), Fraction(-1)), (mi, Fraction(10**6))))
        return busy

    @staticmethod
    def _numbers(spans: list[_Span], closed: bool, busy: list | None = None) -> list[int]:
        assigned: list[tuple[int, tuple, tuple]] = list(busy or [])
        out = [0] * len(spans)
        for i in sorted(range(len(spans)), key=lambda i: (spans[i].start, spans[i].stop, i)):
            s = spans[i]
            n = 1
            while True:
                clash = any(
                    n == k and ((s.start <= e and b <= s.stop) if closed else (s.start < e and b < s.stop))
                    for k, b, e in assigned)
                if not clash:
                    break
                n += 1
            assigned.append((n, s.start, s.stop))
            out[i] = n
        return out

    @staticmethod
    def _write_note(el: ET.Element, nm: _NoteMarks) -> None:
        # Scan ties kept by the strip stay (the cleanup drops any without a same-pitch partner),
        # so the PDF only adds the ones the scan lacks.
        have = {t.get("type") for t in el.findall("tie")}
        nm.tie_stop = nm.tie_stop and "stop" not in have
        nm.tie_start = nm.tie_start and "start" not in have
        if nm.tie_start or nm.tie_stop:
            anchor = next((c for c in el if c.tag == "tie"), None)
            if anchor is None:
                anchor = el.find("duration")
            at = list(el).index(anchor) + (0 if anchor.tag == "tie" else 1) if anchor is not None else len(el)
            for typ in (("stop",) if nm.tie_stop else ()) + (("start",) if nm.tie_start else ()):
                el.insert(at, ET.Element("tie", {"type": typ}))
                at += 1
        has = (nm.tie_start or nm.tie_stop or nm.slurs or nm.ornaments or nm.tremolo is not None
               or nm.articulations or nm.fermata or nm.arpeggio)
        if not has:
            return
        notations = el.find("notations")
        if notations is None:
            notations = ET.Element("notations")
            at = len(el)
            for i, c in enumerate(el):
                if c.tag in _NOTE_ORDER_AFTER:
                    at = i
                    break
            el.insert(at, notations)
        for typ in (("stop",) if nm.tie_stop else ()) + (("start",) if nm.tie_start else ()):
            ET.SubElement(notations, "tied", {"type": typ})
        for typ, number in sorted(nm.slurs, key=lambda s: (s[0] != "stop", s[1])):
            ET.SubElement(notations, "slur", {"type": typ, "number": str(number)})
        if nm.ornaments or nm.tremolo is not None:
            o = ET.SubElement(notations, "ornaments")
            for name in dict.fromkeys(nm.ornaments):
                ET.SubElement(o, name)
            if nm.tremolo is not None:
                ET.SubElement(o, "tremolo", {"type": "single"}).text = str(nm.tremolo)
        if nm.articulations:
            a = ET.SubElement(notations, "articulations")
            for name, placement in dict(((n, p) for n, p in nm.articulations)).items():
                ET.SubElement(a, name, {"placement": placement} if placement else {})
        if nm.fermata:
            ET.SubElement(notations, "fermata", {"type": nm.fermata})
        if nm.arpeggio:
            ET.SubElement(notations, "arpeggiate")

    # -------- leftovers

    def _drop_dangling(self, part_id: str, part: ET.Element, trusted: set[str]) -> None:
        """A start whose stop was lost (or the reverse) in bars the PDF could not align breaks Verovio's spanner."""
        if "slurs" in trusted:
            open_: dict[str, tuple[ET.Element, ET.Element, str]] = {}
            bad: list[tuple[ET.Element, str]] = []
            for m in part.findall("measure"):
                for note in m.findall("note"):
                    for n in note.findall("notations"):
                        for s in n.findall("slur"):
                            num = s.get("number") or "1"
                            if s.get("type") == "start":
                                if num in open_:
                                    bad.append((open_[num][0], open_[num][2]))
                                open_[num] = (s, n, m.get("number"))
                            elif num in open_:
                                del open_[num]
                            else:
                                bad.append((s, m.get("number")))
            bad += [(v[0], v[2]) for v in open_.values()]
            self._remove_from_notations(part, {id(s): m for s, m in bad}, "slurs", part_id)
        if "ties" in trusted:
            open_t: dict[tuple, ET.Element] = {}
            bad_t: list[tuple[ET.Element, str]] = []
            for m in part.findall("measure"):
                for note in m.findall("note"):
                    key = (note.findtext("staff") or "1", _midi(note))
                    for tie in note.findall("tie"):
                        if tie.get("type") == "stop":
                            if key in open_t:
                                del open_t[key]
                            else:
                                bad_t.append((tie, m.get("number")))
                    for tie in note.findall("tie"):
                        if tie.get("type") == "start":
                            if key in open_t:
                                bad_t.append((open_t[key], "?"))
                            open_t[key] = tie
            for tie in open_t.values():
                bad_t.append((tie, "?"))
            self._remove_ties(part, bad_t, part_id)

    def _remove_from_notations(self, part, bad: dict[int, str], cat: str, part_id: str) -> None:
        for m in part.findall("measure"):
            for note in m.findall("note"):
                for n in list(note.findall("notations")):
                    for s in list(n.findall("slur")):
                        if id(s) in bad:
                            n.remove(s)
                            self.log.add(cat, part_id, m.get("number") or "", f"slur {s.get('type')}", "",
                                         "slur without its other end in the aligned score")
                    if len(n) == 0:
                        note.remove(n)

    def _remove_ties(self, part, bad: list[tuple[ET.Element, str]], part_id: str) -> None:
        ids = {id(t) for t, _ in bad}
        for m in part.findall("measure"):
            for note in m.findall("note"):
                for tie in list(note.findall("tie")):
                    if id(tie) not in ids:
                        continue
                    typ = tie.get("type")
                    note.remove(tie)
                    for n in list(note.findall("notations")):
                        for t in list(n.findall("tied")):
                            if t.get("type") == typ:
                                n.remove(t)
                        if len(n) == 0:
                            note.remove(n)
                    self.log.add("ties", part_id, m.get("number") or "", f"tie {typ}", "",
                                 "tie without its other end in the aligned score")
