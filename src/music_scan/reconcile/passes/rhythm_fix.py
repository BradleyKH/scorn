"""Rhythm pass: rebuild a staff-measure's voices from the PDF where durations or event counts differ.

A voice is rebuilt only when the PDF voice fills the bar exactly (or sums short with nothing
printed after it, which is Finale hiding a rest; that gap gets a hidden rest). Notes that line up
with a PDF event keep their element, so ties, slurs, articulations and lyrics survive; pitches
are left to the pitch pass. Every rebuilt bar must still pass `rhythm.check_root`, else it is
restored and logged as a disagreement.
"""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction

from ... import rhythm
from ..align import Alignment, Cell, ChangeLog, EventPair
from ..pdf_notes import NOTE_QUARTERS, Event
from ..pdf_score import PdfScore

CATEGORY = "rhythm"

# MusicXML <note> child order, so retimed and new notes stay schema-valid.
_NOTE_ORDER = ("grace", "cue", "chord", "pitch", "unpitched", "rest", "duration", "tie", "instrument", "footnote",
               "level", "voice", "type", "dot", "accidental", "time-modification", "stem", "notehead",
               "notehead-text", "staff", "beam", "notations", "lyric", "play", "listen")
_BEAM_LEVEL = {"eighth": 1, "16th": 2, "32nd": 3, "64th": 4, "128th": 5}
_ACCIDENTAL = {-2: "flat-flat", -1: "flat", 0: "natural", 1: "sharp", 2: "double-sharp"}
_ALTER_NAME = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}
# Points between where another staff prints the missing beat and our last event, so a reading
# that merely jitters across staves doesn't count as "printed after".
_X_TOL = 2.0


class _Decline(Exception):
    """The PDF evidence doesn't support a rebuild; the message goes into the log."""


@dataclass(frozen=True)
class _Context:
    divisions: int
    beats: int
    beat_type: int
    implicit: bool

    @property
    def bar(self) -> Fraction:
        return Fraction(4 * self.beats, self.beat_type)


def run(root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
    parts = {p.get("id"): p for p in root.findall("part")}
    for pid in alignment.part_order:
        part = parts.get(pid)
        if part is None:
            continue
        for cell in alignment.cells_of(pid):
            if cell.multirest or cell.content is None:
                continue
            _fix_cell(root, part, pdf, cell, log)


# ---------------------------------------------------------------- per cell


def _differs(p: EventPair) -> bool:
    if p.kind == "missing":
        return True
    if p.kind == "extra":
        # a hidden rest is invisible in the PDF by design
        return not all(n.get("print-object") == "no" for n in p.xml.notes)
    if p.kind == "substitute":
        return p.pdf.duration != p.xml.duration or p.pdf.is_rest != p.xml.is_rest or p.pdf.grace != p.xml.grace
    return False


def _fix_cell(root: ET.Element, part: ET.Element, pdf: PdfScore, cell: Cell, log: ChangeLog) -> None:
    measure = cell.element
    ctx = _context(part, measure)
    diff = [p for p in cell.pairs if _differs(p)]
    short = set() if ctx.implicit or cell.pdf_measure.pickup else _short_voices(cell, ctx)
    if not diff and not short:
        return
    number = measure.get("number") or cell.measure.number
    content = cell.content
    staff = cell.xml_staff
    try:
        if content.time is not None and content.time.quarters != ctx.bar and not ctx.implicit:
            raise _Decline(f"PDF time {content.time.beats}/{content.time.beat_type} disagrees with the MusicXML bar")
        if any("more than two voices" in w for w in content.warnings):
            raise _Decline("PDF reading found more than two voices")
        plan = _voice_plan(cell, diff, short, measure, staff, ctx)
        ctx = _ensure_divisions(part, ctx, [e for _, _, evs, _ in plan for e in evs if not e.grace])
    except _Decline as e:
        # a cell that only lacked padding otherwise agrees with the PDF; nothing to report
        if diff:
            log.disagree(CATEGORY, cell.part, number, _describe_staff(measure, staff, ctx.divisions),
                         _describe_events(content.events), f"{_evidence(cell)}: {e}")
        return

    errors_before = _bar_errors(root, cell.part, number)
    saved = [copy.deepcopy(el) for el in measure]
    saved_attrib = dict(measure.attrib)

    def restore() -> None:
        measure[:] = saved
        measure.attrib.clear()
        measure.attrib.update(saved_attrib)

    div = ctx.divisions
    changes: list[tuple[str, str, str]] = []
    for pv, xv, events, survivors in plan:
        old = _voice_notes(measure, staff, xv) if xv is not None else []
        before, before_xml = _describe_notes(old, div), [ET.tostring(n) for n in old]
        try:
            new_voice = xv if xv is not None else _new_voice_number(part, measure, staff, pv)
            padded = _rebuild(part, measure, staff, xv, new_voice, events, survivors, ctx, pdf, cell)
        except _Decline as e:
            restore()
            if diff:
                log.disagree(CATEGORY, cell.part, number, before, _describe_events(events),
                             f"{_evidence(cell, pv)}: {e}")
            return
        new = _voice_notes(measure, staff, new_voice)
        if [ET.tostring(n) for n in new] == before_xml:
            continue
        evidence = _evidence(cell, pv)
        if padded:
            evidence += f"; padded {_q(padded)} quarter(s) with a hidden rest, nothing printed after the last event"
        changes.append((before, _describe_notes(new, div), evidence))

    if not changes:
        restore()
        return
    errors_after = _bar_errors(root, cell.part, number)
    if errors_after - errors_before:
        restore()
        log.disagree(CATEGORY, cell.part, number, _describe_staff(measure, staff, div),
                     _describe_events(content.events),
                     f"{_evidence(cell)}: rebuild would fail the rhythm check ({', '.join(sorted(errors_after))})")
        return
    for before, after, evidence in changes:
        # a voice can come back with new elements but the same reading (beams, a renumbered voice)
        if before != after:
            log.add(CATEGORY, cell.part, number, before, after, evidence)


def _short_voices(cell: Cell, ctx: _Context) -> set[int]:
    """PDF voices summing short of the bar where the MusicXML voice agrees and is short too:
    candidates for a hidden-rest pad and nothing else."""
    totals: dict[int, Fraction] = {}
    for e in cell.content.events:
        if not e.grace:
            totals[e.voice] = totals.get(e.voice, Fraction(0)) + e.duration
    ends = _voice_ends(cell.element, ctx.divisions)
    out = set()
    for pv, total in sorted(totals.items()):
        if total >= ctx.bar:
            continue
        xml = {p.xml.voice for p in cell.pairs if p.pdf is not None and p.xml is not None and p.pdf.voice == pv}
        if len(xml) == 1 and ends.get((cell.xml_staff, xml.pop()), ctx.bar) < ctx.bar:
            out.add(pv)
    return out


def _voice_plan(cell: Cell, diff: list[EventPair], short: set[int], measure: ET.Element, staff: int,
                ctx: _Context):
    """[(pdf voice, xml voice or None, pdf events, {id(event): xml note elements})] for the voices to rebuild."""
    content = cell.content
    pdf_voices = sorted({e.voice for e in content.events})
    xml_voices = _xml_voices(measure, staff)
    counts: Counter[tuple[int, str]] = Counter()
    for p in cell.pairs:
        if p.pdf is not None and p.xml is not None and not p.pdf.grace:
            counts[(p.pdf.voice, p.xml.voice)] += 1
    mapping: dict[int, str | None] = {}
    used: set[str] = set()
    for pv in pdf_voices:
        cands = sorted(((n, -xml_voices.index(xv)) , xv) for (v, xv), n in counts.items()
                       if v == pv and xv in xml_voices and xv not in used)
        if cands:
            mapping[pv] = cands[-1][1]
            used.add(mapping[pv])
    rest = [xv for xv in xml_voices if xv not in used]
    unmapped = [pv for pv in pdf_voices if pv not in mapping]
    if len(unmapped) == 1 and len(rest) == 1:
        mapping[unmapped[0]] = rest[0]
        used.add(rest[0])
        rest = []
    for pv in pdf_voices:
        mapping.setdefault(pv, None)
    stray = [xv for xv in rest if any(n.find("rest") is None for n in _voice_notes(measure, staff, xv))]
    if stray and not _pdf_fills_bar(content, ctx):
        raise _Decline(f"MusicXML voice {stray[0]} has notes the PDF doesn't place in any voice")

    plan = []
    for pv in pdf_voices:
        xv = mapping[pv]
        hit = any((p.pdf is not None and p.pdf.voice == pv) or (p.xml is not None and p.xml.voice == xv)
                  for p in diff)
        # A stray voice may hold notes paired with this PDF voice's events, so dropping it means
        # writing every PDF voice out in full.
        if not hit and not stray and pv not in short and xv is not None:
            continue
        events = [e for e in content.events if e.voice == pv]
        survivors: dict[int, tuple[ET.Element, ...]] = {}
        for p in cell.pairs:
            if (p.pdf is not None and p.xml is not None and p.pdf.voice == pv and p.xml.voice == xv
                    and p.pdf.is_rest == p.xml.is_rest and p.pdf.grace == p.xml.grace):
                survivors[id(p.pdf)] = p.xml.notes
        plan.append((pv, xv, events, survivors))
    # XML voices the PDF doesn't have go away: rests only, or notes the PDF prints nowhere while
    # its own voices already fill the bar
    for xv in rest:
        plan.append((None, xv, [], {}))
    return plan


def _pdf_fills_bar(content, ctx: _Context) -> bool:
    """Every PDF voice of the staff-measure sums to exactly the bar."""
    if ctx.implicit or not content.events:
        return False
    totals: dict[int, Fraction] = {}
    for e in content.events:
        if not e.grace:
            totals[e.voice] = totals.get(e.voice, Fraction(0)) + e.duration
    return bool(totals) and all(t == ctx.bar for t in totals.values())


# ---------------------------------------------------------------- rebuilding one voice


def _rebuild(part: ET.Element, measure: ET.Element, staff: int, old_voice: str | None, voice: str,
             events: list[Event], survivors: dict[int, tuple[ET.Element, ...]], ctx: _Context,
             pdf: PdfScore, cell: Cell) -> Fraction:
    """Replace one voice block with `events`; returns the hidden-rest padding added (0 when none)."""
    events = sorted(events, key=lambda e: (e.x, e.y))
    div = ctx.divisions
    children = list(measure)
    multi_staff = any(n.find("staff") is not None for n in measure.iter("note")) or staff > 1

    if old_voice is not None:
        first, last = _block(measure, staff, old_voice)
        start = _cursor_at(children, first, div)
        old_end = _cursor_at(children, last + 1, div)
    else:
        first = last = None
        start = Fraction(0)
        old_end = None

    expected = ctx.bar
    if ctx.implicit:
        expected = _longest_voice(measure, div) or ctx.bar

    # The PDF voice, with gaps before late-entering events written as forwards.
    built: list[ET.Element] = []
    leads: list[tuple[ET.Element, Event, Fraction]] = []
    onset_of_el: dict[int, Fraction] = {}
    cursor = start
    for e in events:
        if e.grace:
            notes = survivors.get(id(e)) or _new_notes(e, div, voice, staff, multi_staff)
            for k, n in enumerate(notes):
                _retime(n, e, div, voice)
                _set_chord(n, k > 0)
                built.append(n)
                onset_of_el[id(n)] = cursor
            continue
        on = e.onset if e.onset is not None else cursor
        if on < cursor:
            raise _Decline(f"PDF events overlap at beat {_q(on)}")
        if on > cursor:
            built.append(_move("forward", on - cursor, div))
            cursor = on
        notes = survivors.get(id(e)) or _new_notes(e, div, voice, staff, multi_staff)
        for k, n in enumerate(notes):
            _retime(n, e, div, voice)
            _set_chord(n, k > 0)
            built.append(n)
            onset_of_el[id(n)] = cursor
        leads.append((notes[0], e, cursor))
        cursor += e.duration

    padded = Fraction(0)
    if events and cursor < expected:
        _check_hidden_gap(pdf, cell, events[0].voice, cursor, expected)
        padded = expected - cursor
        rest = _hidden_rest(padded, div, voice, staff, multi_staff)
        built.append(rest)
        onset_of_el[id(rest)] = cursor
        cursor = expected
    elif events and cursor > expected:
        raise _Decline(f"PDF voice sums to {_q(cursor)} quarters, bar holds {_q(expected)}")

    _beam(leads, ctx)
    _tuplet_brackets(leads)

    # Directions, clef changes and the like inside the old block keep their place: before the
    # surviving note they preceded, else before the first new note at or after their old onset.
    if old_voice is not None:
        riders: list[tuple[ET.Element, ET.Element | None, Fraction]] = []
        pending: list[tuple[ET.Element, Fraction]] = []
        pos = start
        for el in children[first:last + 1]:
            if el.tag == "note":
                for r, at in pending:
                    riders.append((r, el, at))
                pending = []
                if el.find("chord") is None and el.find("grace") is None:
                    pos += _dur(el, div)
            elif el.tag == "forward":
                pos += _dur(el, div)
            elif el.tag != "backup":
                pending.append((el, pos))
        riders += [(r, None, at) for r, at in pending]
        built_ids = {id(n) for n in built}
        for r, anchor, at in riders:
            if anchor is not None and id(anchor) in built_ids:
                idx = next(i for i, n in enumerate(built) if n is anchor)
            else:
                idx = next((i for i, n in enumerate(built) if n.tag == "note" and onset_of_el.get(id(n), -1) >= at),
                           len(built))
            built.insert(idx, r)
        new_children = children[:first] + built + children[last + 1:]
        after_idx = first + len(built)
        _shift_following(new_children, after_idx, cursor - old_end, div)
    else:
        if not built:
            return padded
        idx = len(children)
        while idx and children[idx - 1].tag == "barline":
            idx -= 1
        here = _cursor_at(children, idx, div)
        lead_in = [_move("backup", here, div)] if here > 0 else ([_move("forward", -here, div)] if here < 0 else [])
        new_children = children[:idx] + lead_in + built + children[idx:]
    measure[:] = new_children
    return padded


def _block(measure: ET.Element, staff: int, voice: str) -> tuple[int, int]:
    """First and last child index of a voice's contiguous run; a forward just ahead of it belongs to it."""
    children = list(measure)
    idx = [i for i, el in enumerate(children) if el.tag == "note" and _staff_of(el) == staff and _voice_of(el) == voice]
    first, last = idx[0], idx[-1]
    for el in children[first:last + 1]:
        if el.tag == "backup":
            raise _Decline(f"MusicXML voice {voice} is split by a backup")
        if el.tag == "note" and (_staff_of(el) != staff or _voice_of(el) != voice):
            raise _Decline(f"MusicXML voice {voice} is interleaved with another voice")
    if first and children[first - 1].tag == "forward":
        first -= 1
    return first, last


def _shift_following(children: list[ET.Element], idx: int, delta: Fraction, div: int) -> None:
    """Keep later voices at their positions after a block grew or shrank by `delta` quarters."""
    if not delta:
        return
    for i in range(idx, len(children)):
        el = children[i]
        if el.tag == "backup":
            d = Fraction(_int(el.findtext("duration")), div) + delta
        elif el.tag == "forward":
            d = -(Fraction(_int(el.findtext("duration")), div) - delta)
        elif el.tag == "note":
            children.insert(i, _move("backup" if delta > 0 else "forward", abs(delta), div))
            return
        else:
            continue
        if d == 0:
            del children[i]
        else:
            children[i] = _move("backup" if d > 0 else "forward", abs(d), div)
        return


def _check_hidden_gap(pdf: PdfScore, cell: Cell, voice: int, end: Fraction, bar: Fraction) -> None:
    """Accept a short PDF voice only when another staff prints the missing beats and this staff
    prints nothing from there on."""
    system = pdf.systems[cell.pdf_measure.system_order]
    ref_x = None
    for st in system.staves:
        c = pdf.content(cell.pdf_measure, st.index)
        if c is None:
            continue
        for e in c.events:
            if not e.grace and e.onset is not None and end <= e.onset < bar:
                ref_x = e.x if ref_x is None else min(ref_x, e.x)
    if ref_x is None:
        raise _Decline(f"PDF voice sums to {_q(end)} of {_q(bar)} quarters and no staff shows where the gap is")
    # Voice 1 owns the staff's x range; a later voice shares it with voice 1 and only its own events count.
    if any(e.x >= ref_x - _X_TOL for e in cell.content.events if voice == 1 or e.voice == voice):
        raise _Decline(f"PDF voice sums to {_q(end)} of {_q(bar)} quarters but the staff prints events in the gap")


# ---------------------------------------------------------------- note elements


def _new_notes(e: Event, div: int, voice: str, staff: int, multi_staff: bool) -> tuple[ET.Element, ...]:
    out = []
    for note in (e.notes or (None,)):
        n = ET.Element("note")
        if e.grace:
            ET.SubElement(n, "grace")
        elif e.cue:
            ET.SubElement(n, "cue")
        if note is None:
            rest = ET.SubElement(n, "rest")
            if e.measure_rest:
                rest.set("measure", "yes")
        else:
            p = ET.SubElement(n, "pitch")
            ET.SubElement(p, "step").text = note.step
            if note.alter:
                ET.SubElement(p, "alter").text = str(note.alter)
            ET.SubElement(p, "octave").text = str(note.octave)
        if note is not None and note.accidental is not None and note.accidental in _ACCIDENTAL:
            _put(n, "accidental", _ACCIDENTAL[note.accidental])
        if e.stem in ("up", "down") and note is not None:
            _put(n, "stem", e.stem)
        if multi_staff:
            _put(n, "staff", str(staff))
        out.append(n)
    return tuple(out)


def _hidden_rest(length: Fraction, div: int, voice: str, staff: int, multi_staff: bool) -> ET.Element:
    n = ET.Element("note", {"print-object": "no"})
    ET.SubElement(n, "rest")
    _put(n, "duration", str(_divs(length, div)))
    _put(n, "voice", voice)
    ntype, dots = _type_for(length)
    if ntype:
        _put(n, "type", ntype)
        for _ in range(dots):
            _put(n, "dot", None, append=True)
    if multi_staff:
        _put(n, "staff", str(staff))
    return n


def _retime(n: ET.Element, e: Event, div: int, voice: str) -> None:
    for tag in ("duration", "dot", "time-modification", "beam"):
        for c in n.findall(tag):
            n.remove(c)
    for nots in n.findall("notations"):
        for t in nots.findall("tuplet"):
            nots.remove(t)
        if not len(nots) and not nots.attrib:
            n.remove(nots)
    if not e.grace:
        _put(n, "duration", str(_divs(e.duration, div)))
    _put(n, "voice", voice)
    rest = n.find("rest")
    if rest is not None and e.measure_rest:
        rest.set("measure", "yes")
        for t in n.findall("type"):
            n.remove(t)
        return
    if rest is not None:
        rest.attrib.pop("measure", None)
    _put(n, "type", e.type)
    for _ in range(e.dots):
        _put(n, "dot", None, append=True)
    if e.tuplet:
        tm = _put(n, "time-modification", None)
        ET.SubElement(tm, "actual-notes").text = str(e.tuplet[0])
        ET.SubElement(tm, "normal-notes").text = str(e.tuplet[1])


def _set_chord(n: ET.Element, member: bool) -> None:
    has = n.find("chord")
    if member and has is None:
        _put(n, "chord", None)
    elif not member and has is not None:
        n.remove(has)


def _put(n: ET.Element, tag: str, text: str | None, append: bool = False) -> ET.Element:
    """Set (or with `append`, add) child `tag` at its schema position."""
    el = n.find(tag)
    if el is None or append:
        el = ET.Element(tag)
        rank = _NOTE_ORDER.index(tag)
        idx = 0
        for i, c in enumerate(n):
            if c.tag in _NOTE_ORDER and _NOTE_ORDER.index(c.tag) <= rank:
                idx = i + 1
        n.insert(idx, el)
    if text is not None:
        el.text = text
    return el


def _beam(leads: list[tuple[ET.Element, Event, Fraction]], ctx: _Context) -> None:
    """Beam runs of eighths and shorter within a beat (a dotted quarter in compound time)."""
    beat = Fraction(1)
    if ctx.beat_type == 8:
        beat = Fraction(3, 2) if ctx.beats % 3 == 0 else ctx.bar
    runs: list[list[tuple[ET.Element, int]]] = []
    current: list[tuple[ET.Element, int]] = []
    group = None
    for n, e, at in leads:
        level = 0 if e.is_rest or e.grace else _BEAM_LEVEL.get(e.type, 0)
        g = math.floor(at / beat)
        if not level or g != group:
            if len(current) > 1:
                runs.append(current)
            current = []
        group = g
        if level:
            current.append((n, level))
    if len(current) > 1:
        runs.append(current)
    for run in runs:
        for lv in range(1, max(level for _, level in run) + 1):
            k = 0
            while k < len(run):
                if run[k][1] < lv:
                    k += 1
                    continue
                j = k
                while j + 1 < len(run) and run[j + 1][1] >= lv:
                    j += 1
                if j > k:
                    for i in range(k, j + 1):
                        val = "begin" if i == k else "end" if i == j else "continue"
                        _put(run[i][0], "beam", val, append=True).set("number", str(lv))
                else:
                    val = "forward hook" if k < len(run) - 1 else "backward hook"
                    _put(run[k][0], "beam", val, append=True).set("number", str(lv))
                k = j + 1


def _tuplet_brackets(leads: list[tuple[ET.Element, Event, Fraction]]) -> None:
    k = 0
    while k < len(leads):
        e = leads[k][1]
        if not e.tuplet:
            k += 1
            continue
        span = NOTE_QUARTERS.get(e.type, Fraction(1)) * e.tuplet[1]
        total = Fraction(0)
        j = k
        while j < len(leads) and leads[j][1].tuplet == e.tuplet:
            total += leads[j][1].duration * e.tuplet[0] / e.tuplet[1]
            if total >= span:
                break
            j += 1
        j = min(j, len(leads) - 1)
        if j > k:
            for i, kind in ((k, "start"), (j, "stop")):
                n = leads[i][0]
                nots = n.find("notations")
                if nots is None:
                    nots = _put(n, "notations", None)
                ET.SubElement(nots, "tuplet", {"type": kind})
        k = j + 1


# ---------------------------------------------------------------- measure bookkeeping


def _context(part: ET.Element, measure: ET.Element) -> _Context:
    div, beats, beat_type = 1, 4, 4
    for m in part.findall("measure"):
        for a in m.findall("attributes"):
            div = _int(a.findtext("divisions")) or div
            if a.find("time") is not None and a.findtext("time/beats"):
                try:
                    beats, beat_type = int(a.findtext("time/beats")), int(a.findtext("time/beat-type"))
                except ValueError:
                    pass
        if m is measure:
            break
    return _Context(div, beats, beat_type, measure.get("implicit") == "yes")


def _ensure_divisions(part: ET.Element, ctx: _Context, events: list[Event]) -> _Context:
    """Scale the part's divisions when a PDF duration isn't a whole number of them."""
    need = 1
    for d in [e.duration for e in events] + [e.onset for e in events if e.onset is not None]:
        q = d * ctx.divisions
        need = math.lcm(need, q.denominator)
    if need == 1:
        return ctx
    for el in part.iter():
        if el.tag in ("duration", "divisions", "offset") and el.text and el.text.strip().lstrip("-").isdigit():
            el.text = str(int(el.text) * need)
    return _Context(ctx.divisions * need, ctx.beats, ctx.beat_type, ctx.implicit)


def _cursor_at(children: list[ET.Element], idx: int, div: int) -> Fraction:
    pos = Fraction(0)
    for el in children[:idx]:
        if el.tag == "backup":
            pos -= _dur(el, div)
        elif el.tag == "forward":
            pos += _dur(el, div)
        elif el.tag == "note" and el.find("chord") is None and el.find("grace") is None:
            pos += _dur(el, div)
    return pos


def _voice_ends(measure: ET.Element, div: int) -> dict[tuple[int, str], Fraction]:
    pos = Fraction(0)
    ends: dict[tuple[int, str], Fraction] = {}
    for el in measure:
        if el.tag == "backup":
            pos -= _dur(el, div)
        elif el.tag == "forward":
            pos += _dur(el, div)
        elif el.tag == "note" and el.find("chord") is None and el.find("grace") is None:
            pos += _dur(el, div)
            key = (_staff_of(el), _voice_of(el))
            ends[key] = max(ends.get(key, Fraction(0)), pos)
    return ends


def _longest_voice(measure: ET.Element, div: int) -> Fraction:
    pos = best = Fraction(0)
    for el in measure:
        if el.tag == "backup":
            pos -= _dur(el, div)
        elif el.tag == "forward":
            pos += _dur(el, div)
        elif el.tag == "note" and el.find("chord") is None and el.find("grace") is None:
            pos += _dur(el, div)
            best = max(best, pos)
    return best


def _xml_voices(measure: ET.Element, staff: int) -> list[str]:
    out: list[str] = []
    for n in measure.iter("note"):
        if _staff_of(n) == staff and n.get("print-object") != "no":
            v = _voice_of(n)
            if v not in out:
                out.append(v)
    return out


def _voice_notes(measure: ET.Element, staff: int, voice: str) -> list[ET.Element]:
    return [n for n in measure.findall("note") if _staff_of(n) == staff and _voice_of(n) == voice]


def _new_voice_number(part: ET.Element, measure: ET.Element, staff: int, pdf_voice: int) -> str:
    """The part's lowest voice number on this staff, else the Finale/Audiveris convention of 1, 5, 9..."""
    used = [int(_voice_of(n)) for n in part.iter("note") if _staff_of(n) == staff and _voice_of(n).isdigit()]
    base = min(used) if used else 4 * (staff - 1) + 1
    taken = {_voice_of(n) for n in measure.iter("note")}
    v = base + pdf_voice - 1
    while str(v) in taken:
        v += 1
    return str(v)


def _move(tag: str, length: Fraction, div: int) -> ET.Element:
    el = ET.Element(tag)
    ET.SubElement(el, "duration").text = str(_divs(length, div))
    return el


def _divs(q: Fraction, div: int) -> int:
    v = q * div
    if v.denominator != 1:
        raise _Decline(f"{q} quarters is not a whole number of divisions ({div})")
    return int(v)


def _dur(el: ET.Element, div: int) -> Fraction:
    return Fraction(_int(el.findtext("duration")), div)


def _int(text: str | None) -> int:
    try:
        return int((text or "0").strip())
    except ValueError:
        return 0


def _staff_of(n: ET.Element) -> int:
    return _int(n.findtext("staff")) or 1


def _voice_of(n: ET.Element) -> str:
    return (n.findtext("voice") or "1").strip()


def _type_for(length: Fraction) -> tuple[str | None, int]:
    for name, q in NOTE_QUARTERS.items():
        for dots in range(3):
            if q * (2 - Fraction(1, 2 ** dots)) == length:
                return name, dots
    return None, 0


def _bar_errors(root: ET.Element, part: str, number: str) -> set[str]:
    out = set()
    for err in rhythm.check_root(root):
        if err.measure != number:
            continue
        for v in err.voices:
            if v.part == part or v.part.startswith(part + " "):
                out.add(f"voice {v.voice} off by {v.off_by}")
    return out


# ---------------------------------------------------------------- report text


def _q(x: Fraction) -> str:
    return str(x.numerator) if x.denominator == 1 else f"{x.numerator}/{x.denominator}"


def _token(name: str, ntype: str | None, dots: int, hidden: bool = False, length: Fraction | None = None) -> str:
    t = f"{name} {ntype or 'bar'}" + "." * dots
    # show the length where the type doesn't account for it (bar rests, stretched or squeezed notes)
    if length is not None and (ntype is None
                               or NOTE_QUARTERS.get(ntype, Fraction(0)) * (2 - Fraction(1, 2 ** dots)) != length):
        t += f" ={_q(length)}"
    return f"({t})" if hidden else t


def _describe_events(events) -> str:
    out = []
    for e in sorted(events, key=lambda e: (e.voice, e.x, e.y)):
        name = "+".join(f"{n.step}{_ALTER_NAME.get(n.alter, '?')}{n.octave}" for n in e.notes) or "rest"
        out.append(("grace " if e.grace else "") + _token(name, None if e.measure_rest else e.type, e.dots))
    return ", ".join(out)


def _describe_notes(notes: list[ET.Element], div: int) -> str:
    groups: list[tuple[list[str], ET.Element]] = []
    for n in notes:
        p = n.find("pitch")
        if p is not None:
            alter = round(float(p.findtext("alter") or 0))
            name = f"{p.findtext('step')}{_ALTER_NAME.get(alter, '?')}{p.findtext('octave')}"
        else:
            name = "rest"
        if n.find("chord") is not None and groups:
            groups[-1][0].append(name)
        else:
            groups.append(([name], n))
    out = []
    for names, n in groups:
        grace = n.find("grace") is not None
        length = None
        if not grace:
            length = Fraction(_int(n.findtext("duration")), div)
            tm = n.find("time-modification")
            if tm is not None:
                length = length * _int(tm.findtext("actual-notes")) / max(1, _int(tm.findtext("normal-notes")))
        out.append(("grace " if grace else "") + _token("+".join(names), n.findtext("type"), len(n.findall("dot")),
                                                        n.get("print-object") == "no", length))
    return ", ".join(out)


def _describe_staff(measure: ET.Element, staff: int, div: int) -> str:
    return _describe_notes([n for n in measure.findall("note") if _staff_of(n) == staff], div)


def _evidence(cell: Cell, voice: int | None = None) -> str:
    c = cell.content
    where = f"PDF page {c.page + 1} system {c.system + 1} staff {c.staff_in_system + 1} bar {cell.pdf_measure.number}"
    return where + (f" voice {voice}" if voice is not None else "")


__all__ = ["CATEGORY", "run"]
