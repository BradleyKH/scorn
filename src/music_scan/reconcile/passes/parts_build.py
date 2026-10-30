"""Parts pass: build one output part bar by bar from the scan staves its instrument occupies."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from fractions import Fraction

from ..align import ChangeLog, XmlMeasure
from .parts_compose import (CATEGORY, SourcePart, State, clean_copy, compose, leading_attributes, make_el, sig,
                            sort_attributes)
from .parts_lanes import Unit, lane_text


def build_part(u: Unit, xms: list[XmlMeasure], parts: dict[str, SourcePart], ref: SourcePart,
                used: set[tuple[str, int, int]], log: ChangeLog, inst_by_key) -> ET.Element:
    multi = len(u.slots) > 1
    width = len(u.slots)
    out = make_el("part", id=u.id)
    state = State()
    # the clef each staff starts in, from its first real bar, for rests ahead of it
    first_clefs: dict[int, ET.Element] = {}
    for xm in xms:
        for k, src in enumerate(u.sources.get(xm.index, [])):
            if src is None or k + 1 in first_clefs:
                continue
            p = parts[src[0]]
            key = (xm.number, xm.occurrence)
            if key in p.measures:
                c = _entry_clef(p, key, src[1])
                if c is not None:
                    first_clefs[k + 1] = c
    prev_vis: list[bool] | None = None
    for i, xm in enumerate(xms):
        key = (xm.number, xm.occurrence)
        ref_m = ref.measures.get(key)
        if ref_m is None:
            ref_m = next((p.measures[key] for p in parts.values() if key in p.measures), make_el("measure", number=xm.number))
        row = list(u.sources.get(xm.index, [None] * width))
        for k, src in enumerate(row):
            if src is not None and key not in parts[src[0]].measures:
                log.disagree(CATEGORY, u.id, xm.number, f"part {src[0]} has no bar {xm.number}",
                             "whole-bar rest", f"{lane_text(u, inst_by_key)} bar missing from the scan")
                row[k] = None
        if i == 0:
            state = ref.entry.get(key, State()).copy()
            state.clefs = {}
        present = [(k, src) for k, src in enumerate(row) if src is not None]
        fast = (present and len(present) == width and len({s[0] for _, s in present}) == 1
                and parts[present[0][1][0]].staves == width
                and all(src[1] == k + 1 for k, src in present))
        req = State(state.divisions, state.key, state.time, {})
        if fast:
            p = parts[present[0][1][0]]
            m = copy.deepcopy(p.measures[key])
            ent = p.entry[key]
            req = State(ent.divisions, ent.key, ent.time, {k + 1: ent.clefs[src[1]]
                                                            for k, src in present if src[1] in ent.clefs})
        else:
            pieces = []
            for k in range(width):
                src = row[k]
                if src is None:
                    pieces.append((k + 1, None, 1, 1))
                    continue
                p = parts[src[0]]
                ent = p.entry[key]
                pieces.append((k + 1, p.measures[key], src[1], ent.divisions))
                if src[1] in ent.clefs:
                    req.clefs[k + 1] = ent.clefs[src[1]]
                if not any(row[j] is not None for j in range(k)):
                    req.key, req.time = ent.key, ent.time
            bar = _bar_at(ref_m, state)
            m, divs = compose(ref_m, pieces, multi, bar, state.divisions)
            req.divisions = divs
            if not present:
                # rests follow the score's key and time changes
                ra = leading_attributes(ref_m, create=False)
                lead = leading_attributes(m)
                if ra is not None:
                    for tag in ("key", "time"):
                        if (g := ra.find(tag)) is not None and lead.find(tag) is None:
                            lead.append(copy.deepcopy(g))
        for k, src in present:
            used.add((src[0], src[1], xm.index))
        for k in range(width):
            if k + 1 not in req.clefs and (k + 1 not in state.clefs) and k + 1 in first_clefs:
                req.clefs[k + 1] = first_clefs[k + 1]
        _fix_attributes(m, state, req, i == 0, multi, width)
        vis = u.visible.get(xm.index, [False] * width)
        if xm.new_system or i == 0 or vis != prev_vis:
            _set_visibility(m, vis, multi)
        else:
            _strip_visibility(m)
        prev_vis = vis
        for a in m.findall("attributes"):
            state.apply(a)
        out.append(m)
    return out


def _bar_at(ref_m: ET.Element, state: State) -> Fraction:
    s = state.copy()
    ra = leading_attributes(ref_m, create=False)
    if ra is not None:
        s.apply(ra)
    return s.bar()


def _entry_clef(p: SourcePart, key: tuple[str, int], staff: int) -> ET.Element | None:
    """The clef a staff shows at the start of a bar: one set before its first note, else the one in force."""
    lead = leading_attributes(p.measures[key], create=False)
    if lead is not None:
        for c in lead.findall("clef"):
            if int(c.get("number") or 1) == staff:
                return c
    return p.entry[key].clefs.get(staff)


def _fix_attributes(m: ET.Element, state: State, req: State, first: bool, multi: bool, width: int) -> None:
    """Add what the bar needs in its new part: divisions, key, time and clefs the source part had
    in force but this part doesn't, and the staff count on the first bar."""
    lead = leading_attributes(m)
    if lead.find("divisions") is None and (first or req.divisions != state.divisions):
        lead.append(make_el("divisions", str(req.divisions)))
    for tag, have, want in (("key", state.key, req.key), ("time", state.time, req.time)):
        if lead.find(tag) is None and want is not None and (first or sig(have) != sig(want)):
            lead.append(clean_copy(want, None))
    for st in lead.findall("staves"):
        lead.remove(st)
    if multi and first:
        lead.append(make_el("staves", str(width)))
    present = {int(c.get("number") or 1) for c in lead.findall("clef")}
    for n in range(1, width + 1):
        want = req.clefs.get(n)
        if n in present or want is None:
            continue
        if first or sig(state.clefs.get(n)) != sig(want):
            lead.append(clean_copy(want, n if multi else None))
    if not len(lead):
        m.remove(lead)
    else:
        sort_attributes(lead)


def _strip_visibility(m: ET.Element) -> None:
    for a in m.findall("attributes"):
        for sd in a.findall("staff-details"):
            sd.attrib.pop("print-object", None)
            if not len(sd) and set(sd.attrib) <= {"number"}:
                a.remove(sd)
        if not len(a):
            m.remove(a)


def _set_visibility(m: ET.Element, vis: list[bool], multi: bool) -> None:
    """staff-details print-object for each staff: shown where the PDF prints the staff."""
    _strip_visibility(m)
    lead = leading_attributes(m)
    if not multi or len(set(vis)) == 1:
        targets = [(None, vis[0] if vis else False)]
    else:
        targets = [(k + 1, v) for k, v in enumerate(vis)]
    for number, v in targets:
        sd = next((s for s in lead.findall("staff-details")
                   if (s.get("number") is None and number is None) or s.get("number") == str(number)), None)
        if sd is None:
            sd = make_el("staff-details") if number is None else make_el("staff-details", number=str(number))
            lead.append(sd)
        sd.set("print-object", "yes" if v else "no")
    sort_attributes(lead)
