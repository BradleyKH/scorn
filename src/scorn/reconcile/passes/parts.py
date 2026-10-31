"""Parts pass: one MusicXML part per instrument for the whole piece, named from the PDF labels.

Audiveris numbers parts by staff position within each system, so on a condensed score (staves
of resting instruments hidden) an instrument drifts between parts from system to system. The
alignment says which PDF staff, and so which instrument, each (part, staff) holds in every
system; this pass rebuilds the parts around instruments ("lanes") instead, moving measures,
filling bars where an instrument isn't printed with whole-bar rests, joining a grand staff split
across two parts, and re-emitting the attributes (divisions, key, time, clef) a moved bar needs
in its new part.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict

from ..align import Alignment, ChangeLog, StaffMatch, XmlMeasure
from ..pdf_score import PdfScore
from .parts_build import build_part
from .parts_compose import CATEGORY, Src, has_notes, make_el, read_part
from .parts_lanes import Unit, lane_groupings, lane_text, lanes, merge_units, score_order
from .parts_names import part_names, score_part, write_transpose


def run(root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
    part_els = root.findall("part")
    if not part_els or not alignment.staves or not alignment.xml_measures:
        return
    if not _layout_agrees(pdf, alignment):
        # bars were paired on content alone, which can't tell one instrument from another
        log.disagree(CATEGORY, part_els[0].get("id") or "", alignment.xml_measures[0].number, "parts as scanned",
                     "left unchanged", "the PDF and the scan break systems differently")
        return
    parts = {p.get("id") or "": read_part(p) for p in part_els}
    part_ids = [p.get("id") or "" for p in part_els]
    xms = list(alignment.xml_measures)
    lane_of = lanes(alignment)
    slot_of = {(s.page, s.staff): (lane_of[(s.page, s.staff)], s.instrument_staff) for s in alignment.staves}
    inst_by_key = {i.key: i for i in alignment.instruments}

    # PDF system for every xml measure; unpaired bars borrow the nearest earlier (else later) one
    sys_of: list[int | None] = []
    for xm in xms:
        pm = alignment.measure_map.get(xm.index)
        sys_of.append(pm.system_order if pm is not None else None)
    last = None
    for i, s in enumerate(sys_of):
        if s is None:
            sys_of[i] = last
        last = sys_of[i]
    nxt = None
    for i in range(len(sys_of) - 1, -1, -1):
        if sys_of[i] is None:
            sys_of[i] = nxt
        nxt = sys_of[i]
    bars_in: dict[int, int] = defaultdict(int)
    for s in sys_of:
        if s is not None:
            bars_in[s] += 1

    groupings = lane_groupings(alignment, slot_of, bars_in)
    units = [Unit(tuple((lane, n) for n in g)) for lane in sorted(groupings) for g in groupings[lane]]
    units = merge_units(units, alignment, slot_of, bars_in)
    for s in sorted(alignment.staves, key=lambda s: (s.system, s.staff_in_system)):
        lane = slot_of[(s.page, s.staff)][0]
        for u in units:
            if any(sl[0] == lane for sl in u.slots) and s.instrument not in u.instruments:
                u.instruments.append(s.instrument)
    units = score_order(units, alignment, slot_of)

    # sources per unit staff per system
    by_system: dict[int, list[StaffMatch]] = defaultdict(list)
    for s in alignment.staves:
        by_system[s.system].append(s)
    claimed: dict[int, set[Src]] = {sy: {(s.part, s.xml_staff) for s in ss if s.part is not None}
                                    for sy, ss in by_system.items()}
    sys_sources: dict[tuple[int, int], list[Src | None]] = {}
    for ui, u in enumerate(units):
        for sy, ss in by_system.items():
            row: list[Src | None] = []
            for lane, n in u.slots:
                hit = next((s for s in ss if slot_of[(s.page, s.staff)] == (lane, n)
                            and s.part is not None), None)
                row.append((hit.part, hit.xml_staff) if hit is not None else None)
            sys_sources[(ui, sy)] = row

    used: set[tuple[str, int, int]] = set()  # (part, staff, xml measure index)
    systems_in_order = sorted({s for s in sys_of if s is not None})
    xm_by_sys: dict[int, list[XmlMeasure]] = defaultdict(list)
    for xm, sy in zip(xms, sys_of):
        if sy is not None:
            xm_by_sys[sy].append(xm)
    taken = {sy: set(c) for sy, c in claimed.items()}
    # logged once the units have their final ids: (unit, bar, lane text)
    kept_in_place: list[tuple[Unit, str, str]] = []
    for ui, u in enumerate(units):
        prev_row: list[Src | None] = [None] * len(u.slots)
        for sy in systems_in_order:
            row = list(sys_sources.get((ui, sy), [None] * len(u.slots)))
            vis = [r is not None for r in row]
            for k, src in enumerate(row):
                if src is not None or prev_row[k] is None:
                    continue
                # Continuity: the part that held this staff in the previous system still has notes
                # here and no PDF staff claims it, so the PDF reader likely missed the staff.
                cand = prev_row[k]
                if cand in taken.setdefault(sy, set()) or cand in row:
                    continue
                if any(has_notes(parts[cand[0]].measures.get((xm.number, xm.occurrence)), cand[1])
                       for xm in xm_by_sys[sy]):
                    row[k] = cand
                    vis[k] = True
                    taken[sy].add(cand)
                    kept_in_place.append((u, xm_by_sys[sy][0].number, lane_text(u, inst_by_key)))
            for xm in xm_by_sys[sy]:
                u.sources[xm.index] = row
                u.visible[xm.index] = vis
            prev_row = [r if r is not None else p for r, p in zip(row, prev_row)]

    # ids: reuse the existing ids in document order, minting new ones past them
    leftovers = []
    for pid in part_ids:
        if not any(src is not None and src[0] == pid for u in units for row in u.sources.values() for src in row):
            leftovers.append(pid)
    kept = [pid for pid in leftovers if any(has_notes(m, s) for m in parts[pid].measures.values()
                                             for s in range(1, parts[pid].staves + 1))]
    pool = [pid for pid in part_ids if pid not in kept]
    taken_ids = set(part_ids)
    n = len(part_ids)
    for i, u in enumerate(units):
        if i < len(pool):
            u.id = pool[i]
        else:
            n += 1
            while f"P{n}" in taken_ids:
                n += 1
            u.id = f"P{n}"
            taken_ids.add(u.id)

    for u, number, text in kept_in_place:
        log.disagree(CATEGORY, u.id, number, "notes in the scan", "kept in place",
                     f"no PDF staff of {text} found in this system")

    longest = max(part_ids, key=lambda pid: (len(parts[pid].measures), -part_ids.index(pid)))
    new_parts: list[ET.Element] = []
    for u in units:
        new_parts.append(build_part(u, xms, parts, parts[longest], used, log, inst_by_key))
        _log_moves(u, xms, log, inst_by_key)

    # Bars with notes that no instrument took, reported on the part that took that scan staff in
    # the nearest bar, since the scan's own part id may now name another instrument.
    takers: dict[Src, list[tuple[int, str]]] = defaultdict(list)
    for u in units:
        for idx, row in sorted(u.sources.items()):
            for src in row:
                if src is not None:
                    takers[src].append((idx, u.id))
    for pid in part_ids:
        if pid in kept:
            continue
        p = parts[pid]
        for xm in xms:
            for s in range(1, p.staves + 1):
                if (pid, s, xm.index) in used:
                    continue
                if has_notes(p.measures.get((xm.number, xm.occurrence)), s):
                    near = min(takers.get((pid, s), []), key=lambda t: (abs(t[0] - xm.index), t[0]), default=None)
                    log.disagree(CATEGORY, near[1] if near else "", xm.number, f"notes in scan part {pid} staff {s}",
                                 "dropped", "no PDF staff matches this part here")
    for pid in leftovers:
        if pid in kept:
            log.disagree(CATEGORY, pid, xms[0].number, f"part {pid}", "kept unchanged",
                         "no PDF staff matches this part, but it has notes")
        else:
            log.add(CATEGORY, pid, xms[0].number, f"part {pid}", "removed", "no PDF staff matches it and it has no notes")

    score_parts = {sp.get("id"): sp for sp in root.iter("score-part")}
    names = part_names(units, inst_by_key, pdf)
    new_score_parts = []
    for i, u in enumerate(units):
        new_score_parts.append(score_part(u, i, names[u.id], score_parts, parts, log))
    for pid in kept:
        new_score_parts.append(score_parts.get(pid) or make_el("score-part", id=pid))
    for i, u in enumerate(units):
        if names[u.id] is not None:
            write_transpose(u, new_parts[i], names[u.id][2], log)

    part_list = root.find("part-list")
    if part_list is None:
        part_list = make_el("part-list")
        root.insert(list(root).index(part_els[0]), part_list)
    for el in list(part_list):
        part_list.remove(el)
    for sp in new_score_parts:
        part_list.append(sp)
    for p in part_els:
        root.remove(p)
    for p in new_parts:
        root.append(p)
    for pid in kept:
        root.append(parts[pid].element)


def _layout_agrees(pdf: PdfScore, alignment: Alignment) -> bool:
    """The scan has the PDF's systems with the same bar counts, so bars pair by position."""
    pdf_sys = [sum(m.span for m in s.measures) for s in pdf.systems]
    xml_sys: list[int] = []
    for xm in alignment.xml_measures:
        if xm.new_system or not xml_sys:
            xml_sys.append(0)
        xml_sys[-1] += 1
    return pdf_sys == xml_sys


def _log_moves(u: Unit, xms: list[XmlMeasure], log: ChangeLog, inst_by_key) -> None:
    """One entry per run of bars that came from somewhere other than the part's own id and staff."""
    multi = len(u.slots) > 1
    for k in range(len(u.slots)):
        runs: list[list] = []
        prev = None
        for xm in xms:
            src = u.sources.get(xm.index, [None] * len(u.slots))[k]
            if src == (u.id, k + 1):
                prev = None
                continue
            if runs and prev is not None and runs[-1][0] == src:
                runs[-1][2] = xm.number
            else:
                runs.append([src, xm.number, xm.number])
            prev = xm
        where = f"{u.id} staff {k + 1}" if multi else u.id
        for src, first, lastn in runs:
            span = first if first == lastn else f"{first}-{lastn}"
            if src is None:
                log.add(CATEGORY, u.id, span, "", f"whole-bar rests in {where}",
                        f"{lane_text(u, inst_by_key)} is not printed in these bars")
            else:
                log.add(CATEGORY, u.id, span, f"part {src[0]} staff {src[1]}", where,
                        f"PDF staff of {lane_text(u, inst_by_key)}")


__all__ = ["CATEGORY", "run"]
