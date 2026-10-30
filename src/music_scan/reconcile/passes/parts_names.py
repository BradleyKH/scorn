"""Parts pass naming: part names, MIDI programs and transpositions from the PDF labels."""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from .. import instruments
from ..align import ChangeLog
from ..pdf_score import PdfScore
from .parts_compose import CATEGORY, SourcePart, leading_attributes, make_el, sort_attributes
from .parts_lanes import Unit


_GROUP = re.compile(r"^\(?\s*([^\W\d_]+)\s*-\s*group\s*(\d+)\s*\)?$", re.IGNORECASE)


_STAFF_KEY = re.compile(r"^Staff \d+$")


def part_names(units: list[Unit], inst_by_key, pdf: PdfScore) -> dict[str, tuple[str, str, instruments.InstrumentInfo | None] | None]:
    """(part-name, part-abbreviation, instrument) per unit id; None leaves the scan's name."""
    out: dict[str, tuple[str, str, instruments.InstrumentInfo | None] | None] = {}
    staves_of: dict[str, int] = defaultdict(int)
    for u in units:
        for lane, n in u.slots:
            staves_of[lane] = max(staves_of[lane], n)
    for u in units:
        insts = [inst_by_key[k] for k in u.instruments]
        if not insts:
            out[u.id] = None
            continue
        if u.lane.startswith("choir:"):
            grouped = next(((m.group(1).title(), m.group(2)) for i in insts if (m := _GROUP.match(i.name))), None)
            if grouped:
                name, abbr = f"{grouped[0]} {grouped[1]}", f"{grouped[0][0]}. {grouped[1]}"
            else:
                name, abbr = insts[0].name, (insts[0].abbreviations or (insts[0].name,))[0]
            out[u.id] = (name, abbr, instruments.lookup(name) or instruments.lookup(insts[0].name))
            continue
        inst = insts[0]
        name, abbr = inst.name, (inst.abbreviations or (inst.name,))[0]
        info = instruments.lookup(name)
        if _STAFF_KEY.match(inst.key) and inst.name == inst.key and not inst.abbreviations:
            found = _title_instrument(pdf) if len(units) == 1 else None
            if found is None:
                out[u.id] = None
                continue
            name, info = found
            abbr = name
        elif info is None and not inst.abbreviations:
            # text left of a single system that names no known instrument is as likely a
            # credit or a stray bar number as a label
            out[u.id] = None
            continue
        if len(u.staves) < staves_of[u.lane] and 1 not in u.staves:
            if info is not None and info.name == "Organ" and u.staves == (staves_of[u.lane],) and staves_of[u.lane] == 3:
                name, abbr = f"{name} Pedal", "Ped."
            else:
                # staves split off an instrument, often unlabeled staves the alignment attached
                # to the label above; the scan's name is the safer one
                out[u.id] = None
                continue
        out[u.id] = (name, abbr, info)
    return out


def _title_instrument(pdf: PdfScore) -> tuple[str, instruments.InstrumentInfo] | None:
    """A single-instrument part rarely labels its staff; the name sits in the title block
    (often top left, where it reads as a lyricist) or the file name."""
    tb = pdf.text.title
    for text in (tb.lyricist, tb.subtitle, *tb.other, tb.arranger, tb.composer, tb.title, pdf.path.stem):
        if text:
            found = instruments.find_in(text)
            if found is not None:
                return found
    return None


def score_part(u: Unit, index: int, named, score_parts: dict, parts: dict[str, SourcePart],
                log: ChangeLog) -> ET.Element:
    counts: dict[str, int] = defaultdict(int)
    for row in u.sources.values():
        for src in row:
            if src is not None:
                counts[src[0]] += 1
    base_id = min(counts, key=lambda pid: (-counts[pid], pid)) if counts else u.id
    base = score_parts.get(base_id)
    sp = copy.deepcopy(base) if base is not None else make_el("score-part")
    old_name = sp.findtext("part-name") or ""
    old_abbr = sp.findtext("part-abbreviation") or ""
    sp.set("id", u.id)
    for k, si in enumerate(sp.findall("score-instrument"), 1):
        si.set("id", f"{u.id}-I{k}")
    for k, mi in enumerate(sp.findall("midi-instrument"), 1):
        mi.set("id", f"{u.id}-I{k}")
    if named is None:
        if sp.find("part-name") is None:
            sp.insert(0, make_el("part-name", old_name))
        return sp
    name, abbr, info = named
    pn = sp.find("part-name")
    if pn is None:
        pn = make_el("part-name")
        sp.insert(0, pn)
    pn.text = name
    pa = sp.find("part-abbreviation")
    if pa is None:
        pa = make_el("part-abbreviation")
        sp.insert(list(sp).index(pn) + 1, pa)
    pa.text = abbr
    for si in sp.findall("score-instrument"):
        nm = si.find("instrument-name")
        if nm is not None:
            nm.text = name
    for mi in sp.findall("midi-instrument"):
        ch = mi.find("midi-channel")
        if ch is not None:
            ch.text = str(index % 16 + 1)
        if info is not None:
            prog = mi.find("midi-program")
            if prog is None:
                prog = make_el("midi-program")
                mi.append(prog)
            prog.text = str(info.program)
    if (old_name, old_abbr) != (name, abbr):
        log.add(CATEGORY, u.id, "", f"{old_name} / {old_abbr}".strip(" /"), f"{name} / {abbr}",
                "PDF staff label")
    return sp


def write_transpose(u: Unit, part: ET.Element, info: instruments.InstrumentInfo | None, log: ChangeLog) -> None:
    """Write the label's transposition on the first bar so concert pitch comes out right."""
    if info is None or info.transpose is None:
        return
    measures = part.findall("measure")
    if not measures:
        return
    before = None
    for m in measures:
        for a in m.findall("attributes"):
            for t in a.findall("transpose"):
                before = before or t
                a.remove(t)
    tr = make_el("transpose")
    tr.append(make_el("diatonic", str(info.transpose.diatonic)))
    tr.append(make_el("chromatic", str(info.transpose.chromatic)))
    lead = leading_attributes(measures[0])
    lead.append(tr)
    sort_attributes(lead)
    old = "none" if before is None else f"{before.findtext('diatonic')}/{before.findtext('chromatic')}"
    new = f"{info.transpose.diatonic}/{info.transpose.chromatic}"
    if old != new:
        log.add(CATEGORY, u.id, measures[0].get("number") or "", f"transpose {old}", f"transpose {new}",
                f"label names {info.name} in a transposing key")
