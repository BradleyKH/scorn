"""Parts pass building blocks: the scan parts' running attributes, and measures composed from
staves of other parts."""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction


CATEGORY = "parts"


_ATTR_ORDER = {t: i for i, t in enumerate((
    "footnote", "level", "divisions", "key", "time", "staves", "part-symbol", "instruments", "clef",
    "staff-details", "transpose", "for-part", "directive", "measure-style"))}


Src = tuple[str, int]  # (part id, staff within the part)


@dataclass
class State:
    divisions: int = 1
    key: ET.Element | None = None
    time: ET.Element | None = None
    clefs: dict[int, ET.Element] = field(default_factory=dict)

    def copy(self) -> State:
        return State(self.divisions, self.key, self.time, dict(self.clefs))

    def apply(self, attrs: ET.Element) -> None:
        self.divisions = int(attrs.findtext("divisions") or self.divisions)
        if (k := attrs.find("key")) is not None:
            self.key = k
        if (t := attrs.find("time")) is not None:
            self.time = t
        for c in attrs.findall("clef"):
            self.clefs[int(c.get("number") or 1)] = c

    def bar(self) -> Fraction:
        """Bar length in quarters."""
        if self.time is None or self.time.findtext("beats") is None:
            return Fraction(4)
        beats = sum(Fraction(b) for b in (self.time.findtext("beats") or "4").split("+"))
        return beats * 4 / int(self.time.findtext("beat-type") or 4)


@dataclass
class SourcePart:
    id: str
    element: ET.Element
    staves: int
    measures: dict[tuple[str, int], ET.Element]
    # state in force just before each measure, by (number, occurrence)
    entry: dict[tuple[str, int], State]


def read_part(part: ET.Element) -> SourcePart:
    seen: dict[str, int] = defaultdict(int)
    measures: dict[tuple[str, int], ET.Element] = {}
    entry: dict[tuple[str, int], State] = {}
    state = State()
    staves = 1
    for m in part.findall("measure"):
        n = m.get("number") or ""
        key = (n, seen[n])
        seen[n] += 1
        measures[key] = m
        entry[key] = state.copy()
        for a in m.findall("attributes"):
            state.apply(a)
            staves = max(staves, int(a.findtext("staves") or 1))
    return SourcePart(part.get("id") or "", part, staves, measures, entry)


def sig(el: ET.Element | None) -> tuple:
    if el is None:
        return ()
    return tuple((c.tag, (c.text or "").strip()) for c in el.iter() if c is not el)


def has_notes(measure: ET.Element | None, staff: int) -> bool:
    if measure is None:
        return False
    return any(n.find("pitch") is not None or n.find("unpitched") is not None
               for n in measure.findall("note") if int(n.findtext("staff") or 1) == staff)


def leading_attributes(measure: ET.Element, create: bool = True) -> ET.Element | None:
    insert_at = 0
    for i, el in enumerate(measure):
        if el.tag in ("note", "backup", "forward"):
            break
        if el.tag == "attributes":
            return el
        if el.tag == "print" or (el.tag == "barline" and el.get("location") == "left"):
            insert_at = i + 1
    if not create:
        return None
    attrs = ET.Element("attributes")
    measure.insert(insert_at, attrs)
    return attrs


def sort_attributes(attrs: ET.Element) -> None:
    kids = sorted(enumerate(list(attrs)), key=lambda t: (_ATTR_ORDER.get(t[1].tag, 99), t[0]))
    for _, el in kids:
        attrs.remove(el)
    for _, el in kids:
        attrs.append(el)


def make_el(tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    e = ET.Element(tag, attrib)
    if text is not None:
        e.text = text
    return e


def _rest(duration: int, staff: int | None) -> ET.Element:
    note = make_el("note")
    note.append(make_el("rest", measure="yes"))
    note.append(make_el("duration", str(duration)))
    note.append(make_el("voice", str(1 + 4 * ((staff or 1) - 1))))
    if staff is not None:
        note.append(make_el("staff", str(staff)))
    return note


def _scale(el: ET.Element, factor: int) -> None:
    if factor == 1:
        return
    for tag in ("duration", "offset"):
        for d in el.iter(tag):
            if d.text and d.text.strip().lstrip("-").isdigit():
                d.text = str(int(d.text) * factor)


_LEAD = -1  # onset marker for attributes ahead of a bar's first note


def _staff_items(measure: ET.Element, staff: int, target: int | None, factor: int, first: bool):
    """The content of one staff of a measure as (onset or None for a chord member, advance,
    element) in source order, re-numbered for `target` (None: a single-staff part) and its
    durations scaled by `factor`. Global attributes (key, time) are kept only when `first`."""
    out: list[tuple[int | None, int, ET.Element]] = []
    pos = 0
    shift = 4 * ((target or 1) - staff)
    lead = True
    for el in measure:
        if el.tag in ("print", "barline", "sound"):
            continue
        if el.tag == "backup":
            pos -= int(el.findtext("duration") or 0)
            continue
        if el.tag == "forward":
            pos += int(el.findtext("duration") or 0)
            continue
        if el.tag == "note":
            lead = False
            chord = el.find("chord") is not None
            adv = 0 if chord or el.find("grace") is not None else int(el.findtext("duration") or 0)
            if int(el.findtext("staff") or 1) == staff:
                n = copy.deepcopy(el)
                _restaff(n, target)
                v = n.find("voice")
                if v is not None and (v.text or "").strip().isdigit() and int(v.text) + shift >= 1:
                    v.text = str(int(v.text) + shift)
                _scale(n, factor)
                out.append((None if chord else pos * factor, adv * factor, n))
            if not chord:
                pos += adv
            continue
        if el.tag == "attributes":
            a = make_el("attributes")
            for c in el:
                if c.tag in ("clef", "staff-details"):
                    if int(c.get("number") or 1) != staff:
                        continue
                    c = copy.deepcopy(c)
                    if target is None:
                        c.attrib.pop("number", None)
                    else:
                        c.set("number", str(target))
                    a.append(c)
                elif c.tag in ("divisions", "staves", "part-symbol"):
                    continue
                elif first:
                    a.append(copy.deepcopy(c))
            if len(a):
                out.append((_LEAD if lead else pos * factor, 0, a))
            continue
        if int(el.findtext("staff") or 1) != staff:
            continue
        e = copy.deepcopy(el)
        _restaff(e, target)
        _scale(e, factor)
        out.append((pos * factor, 0, e))
    return out


_AFTER_STAFF = {"beam", "notations", "lyric", "play", "listen", "sound"}


def _restaff(el: ET.Element, target: int | None) -> None:
    st = el.find("staff")
    if target is None:
        if st is not None:
            el.remove(st)
    elif st is not None:
        st.text = str(target)
    else:
        at = next((i for i, c in enumerate(el) if c.tag in _AFTER_STAFF), len(el))
        el.insert(at, make_el("staff", str(target)))


def compose(xm_ref: ET.Element, pieces: list[tuple[int, ET.Element | None, int, int]], multi: bool,
             bar_quarters: Fraction, divs: int) -> tuple[ET.Element, int]:
    """Build a measure from one piece per target staff: (target staff, source measure or None
    for rests, source staff, source divisions), starting from the part's current divisions.
    Returns the measure and the divisions its durations use."""
    for _, m, _, d in pieces:
        if m is not None:
            divs = math.lcm(divs, d)
    divs = math.lcm(divs, bar_quarters.denominator)
    m = make_el("measure", **dict(xm_ref.attrib))
    primary = next((p[1] for p in pieces if p[1] is not None), None)
    frame = primary if primary is not None else xm_ref
    for el in frame:
        if el.tag == "print":
            pr = copy.deepcopy(el) if primary is not None else _bare_print(el)
            if pr is not None:
                m.append(pr)
    for el in frame:
        if el.tag == "barline" and el.get("location") == "left":
            m.append(copy.deepcopy(el))
    lead = make_el("attributes")
    m.append(lead)
    body: list[ET.Element] = []
    cur = 0
    first = True
    for target, src, sstaff, d in pieces:
        t = target if multi else None
        if src is None:
            items = [(0, int(bar_quarters * divs), _rest(int(bar_quarters * divs), t))]
        else:
            items = _staff_items(src, sstaff, t, divs // d, first)
            first = False
        if cur and items:
            body.append(_dur("backup", cur))
            cur = 0
        for onset, adv, el in items:
            if onset == _LEAD:
                for c in el:
                    lead.append(c)
                continue
            if onset is not None and onset != cur:
                body.append(_dur("backup", cur - onset) if onset < cur else _dur("forward", onset - cur))
                cur = onset
            body.append(el)
            cur += adv
    for el in body:
        m.append(el)
    for el in frame:
        if el.tag == "sound" and primary is not None:
            m.append(copy.deepcopy(el))
    for el in frame:
        if el.tag == "barline" and el.get("location") != "left":
            m.append(copy.deepcopy(el))
    _dedupe_global(lead)
    return m, divs


def _dedupe_global(attrs: ET.Element) -> None:
    for tag in ("key", "time"):
        found = attrs.findall(tag)
        for extra in found[1:]:
            attrs.remove(extra)


def _dur(tag: str, value: int) -> ET.Element:
    e = make_el(tag)
    e.append(make_el("duration", str(value)))
    return e


def _bare_print(p: ET.Element) -> ET.Element | None:
    """Just the system and page breaks of another part's print, for a bar of rests."""
    out = make_el("print")
    for k in ("new-system", "new-page"):
        if p.get(k):
            out.set(k, p.get(k))
    return out if out.attrib else None


def clean_copy(el: ET.Element, number: int | None) -> ET.Element:
    c = copy.deepcopy(el)
    c.tail = None
    if number is None:
        c.attrib.pop("number", None)
    else:
        c.set("number", str(number))
    return c
