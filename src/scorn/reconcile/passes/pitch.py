"""Pitch pass: rewrite pitches, chord members, clefs and key signatures from what the PDF draws.

Only pitch is touched here. Events whose kind differs (a note where the MusicXML has a rest, a
different duration) belong to the rhythm pass, and every edit leaves durations alone, so a bar that
adds up before this pass adds up after it.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import defaultdict

from ..align import Alignment, Cell, ChangeLog, EventPair, XmlEvent
from ..pdf_notes import Clef, Note, TimeSignature
from ..pdf_score import PdfScore

CATEGORY = "pitch"
# A cell is trusted only when this share of its sounding events pair up with a counterpart; below
# that the staff was probably matched to the wrong part or the bar is scrambled.
TRUST = 0.6
_ACCIDENTAL = {-2: "flat-flat", -1: "flat", 0: "natural", 1: "sharp", 2: "double-sharp"}
_SEMIS = (0, 2, 4, 5, 7, 9, 11)
# Children of <note> that come after <accidental>, so it can be inserted before the first of them.
_AFTER_ACCIDENTAL = ("time-modification", "stem", "notehead", "notehead-text", "staff", "beam", "notations", "lyric", "play")
_CLEF_AFTER = ("staff-details", "transpose", "directive", "measure-style")
_CLEF_SIGNS = ("G", "F", "C")


def run(root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
    rank = {p: i for i, p in enumerate(alignment.part_order)}
    # Clefs go first: a clef whose octave sign Audiveris dropped is what explains octave-only pitch errors.
    xml_clefs = _clefs_at_cell_starts(root, alignment)
    _fix_clefs_and_keys(root, alignment, log)
    fix_times(root, alignment, log)
    for cell in sorted(alignment.cells.values(), key=lambda c: (rank.get(c.part, 0), c.measure.index, c.xml_staff)):
        have = xml_clefs.get((cell.part, cell.measure.index, cell.xml_staff))
        shift = 12 * (cell.content.clef.octave - have.octave) if have is not None and cell.content is not None else 0
        _fix_cell(cell, log, shift)


# ---------------------------------------------------------------- notes


def _midi(pitch: ET.Element) -> int:
    step = pitch.findtext("step") or "C"
    return 12 * (int(pitch.findtext("octave") or 4) + 1) + _SEMIS["CDEFGAB".index(step)] + round(float(pitch.findtext("alter") or 0))


def _name(pitch: ET.Element) -> str:
    alter = round(float(pitch.findtext("alter") or 0))
    return f"{pitch.findtext('step')}{ {-2: 'bb', -1: 'b', 0: '', 1: '#', 2: '##'}.get(alter, '?') }{pitch.findtext('octave')}"


def _names(notes: list[ET.Element]) -> str:
    return " ".join(_name(n.find("pitch")) for n in notes)


def _pitched(event: XmlEvent) -> list[ET.Element]:
    return [n for n in event.notes if n.find("pitch") is not None]


def _fix_cell(cell: Cell, log: ChangeLog, shift: int = 0) -> None:
    if cell.multirest or cell.content is None:
        return
    pairs = cell.pairs
    paired = [p for p in pairs if p.kind in ("match", "substitute") and p.pdf.notes and p.xml.pitches]
    width = max(sum(1 for p in pairs if p.pdf is not None and p.pdf.notes),
                sum(1 for p in pairs if p.xml is not None and p.xml.pitches), 1)
    subs = [p for p in paired if p.kind == "substitute"]
    if not subs:
        return
    if len(paired) < TRUST * width:
        log.disagree(CATEGORY, cell.part, cell.measure.number, "", "",
                     f"staff {cell.xml_staff}: only {len(paired)} of {width} events pair with the PDF, pitches left alone")
        return
    for p in subs:
        _fix_event(cell, p, log, shift)


def _fix_event(cell: Cell, pair: EventPair, log: ChangeLog, shift: int) -> None:
    pdf_ev, xml_ev = pair.pdf, pair.xml
    # Unpitched or mixed events (percussion) have no pitch to compare.
    if len(_pitched(xml_ev)) != len(xml_ev.notes):
        return
    # Without a shared onset or duration the pairing may be one off, so pitches aren't provable.
    if pdf_ev.onset != xml_ev.onset and pdf_ev.duration != xml_ev.duration:
        if sorted(_midi(n.find("pitch")) for n in xml_ev.notes) == sorted(n.midi for n in pdf_ev.notes):
            return
        log.disagree(CATEGORY, cell.part, cell.measure.number, _names(list(xml_ev.notes)),
                     " ".join(n.name for n in pdf_ev.notes), "pitches differ but the event's onset and duration do not line up")
        return
    xml_notes = list(xml_ev.notes)
    want = list(pdf_ev.notes)

    # Notes both sides agree on stay as they are.
    left_x: list[ET.Element] = []
    pool = list(want)
    for n in xml_notes:
        hit = next((w for w in pool if w.midi == _midi(n.find("pitch"))), None)
        if hit is None:
            left_x.append(n)
        else:
            pool.remove(hit)
    left_p = pool
    if not left_x and not left_p:
        return

    before = _names(xml_notes)
    left_x.sort(key=lambda n: _midi(n.find("pitch")))
    left_p.sort(key=lambda w: w.midi)
    pairing = _pair_sorted([_midi(n.find("pitch")) for n in left_x], [w.midi for w in left_p])

    # An octave shift can come from an "8va" the readers don't see, so it is reported, not applied;
    # the exception is a shift that exactly matches a clef octave sign the MusicXML lacks.
    blocked = [(i, j) for i, j in pairing
               if _midi(left_x[i].find("pitch")) % 12 == left_p[j].midi % 12
               and left_p[j].midi - _midi(left_x[i].find("pitch")) != shift]
    if blocked:
        log.disagree(CATEGORY, cell.part, cell.measure.number, before, " ".join(n.name for n in want),
                     f"staff {cell.xml_staff}: octave-only difference, not provable from the PDF")
    pairing = [pr for pr in pairing if pr not in blocked]
    # Engraving rules confine an accidental to its octave, but some engravers let it cover the same
    # letter in another octave and leave the second note bare; the scan following that reading is
    # as plausible as the PDF's strict one.
    cross = [(i, j) for i, j in pairing if _cross_octave(cell, left_p[j], left_x[i].find("pitch"))]
    if cross:
        log.disagree(CATEGORY, cell.part, cell.measure.number, before, " ".join(n.name for n in want),
                     f"staff {cell.xml_staff}: the scan follows an accidental drawn in another octave")
    pairing = [pr for pr in pairing if pr not in cross]
    blocked = blocked + cross
    blocked_x = {i for i, _ in blocked}
    blocked_p = {j for _, j in blocked}

    paired_x = {i for i, _ in pairing} | blocked_x
    paired_p = {j for _, j in pairing} | blocked_p
    removals = [n for i, n in enumerate(left_x) if i not in paired_x]
    # A unison between two voices draws two heads; one chord member already carries that pitch.
    paired_at = {id(left_x[i]): left_p[j].midi for i, j in pairing}
    kept = {paired_at.get(id(n), _midi(n.find("pitch"))) for n in xml_notes if n not in removals}
    additions = [w for j, w in enumerate(left_p) if j not in paired_p and w.midi not in kept]

    for i, j in pairing:
        _write_pitch(left_x[i], left_p[j])
    if removals:
        _remove(cell.element, xml_notes, removals)
    for w in additions:
        _add_member(cell.element, w, xml_ev)
    if pairing or removals or additions:
        log.add(CATEGORY, cell.part, cell.measure.number, before, " ".join(n.name for n in want),
                f"PDF page {cell.pdf_measure.page + 1}, staff {cell.xml_staff}")


def _cross_octave(cell: Cell, want: Note, have: ET.Element) -> bool:
    """`want` carries no accidental of its own, and `have` is the same letter altered as an earlier
    accidental on that letter in another octave of this bar."""
    if want.accidental is not None or have.findtext("step") != want.step:
        return False
    alter = round(float(have.findtext("alter") or 0))
    if alter == want.alter:
        return False
    return any(n.step == want.step and n.octave != want.octave and n.accidental == alter and n.x <= want.x
               for e in cell.content.events for n in e.notes)


def _pair_sorted(a: list[int], b: list[int]) -> list[tuple[int, int]]:
    """Order-preserving pairing of two sorted lists that minimizes total distance and pairs
    min(len) of them; ties favor lower indexes."""
    n, m = len(a), len(b)
    k = min(n, m)
    inf = float("inf")
    # dp[i][j][c]: best cost using the first i of a, j of b, with c pairs made
    dp = [[[inf] * (k + 1) for _ in range(m + 1)] for _ in range(n + 1)]
    for i in range(n + 1):
        for j in range(m + 1):
            dp[i][j][0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            for c in range(1, k + 1):
                best = min(dp[i - 1][j][c], dp[i][j - 1][c])
                if dp[i - 1][j - 1][c - 1] < inf:
                    best = min(best, dp[i - 1][j - 1][c - 1] + abs(a[i - 1] - b[j - 1]))
                dp[i][j][c] = best
    out: list[tuple[int, int]] = []
    i, j, c = n, m, k
    while c > 0:
        here = dp[i][j][c]
        if i and j and dp[i - 1][j - 1][c - 1] < inf and here == dp[i - 1][j - 1][c - 1] + abs(a[i - 1] - b[j - 1]):
            out.append((i - 1, j - 1))
            i, j, c = i - 1, j - 1, c - 1
        elif i and here == dp[i - 1][j][c]:
            i -= 1
        else:
            j -= 1
    return sorted(out)


def _write_pitch(note: ET.Element, want: Note) -> None:
    pitch = note.find("pitch")
    for tag, text in (("step", want.step), ("alter", str(want.alter) if want.alter else None), ("octave", str(want.octave))):
        el = pitch.find(tag)
        if text is None:
            if el is not None:
                pitch.remove(el)
            continue
        if el is None:
            el = ET.Element(tag)
            # step, alter, octave order
            idx = {"step": 0, "alter": 1 if pitch.find("step") is not None else 0, "octave": len(pitch)}[tag]
            pitch.insert(idx, el)
        el.text = text
    _set_accidental(note, want.accidental)


def _set_accidental(note: ET.Element, alter: int | None) -> None:
    el = note.find("accidental")
    if alter is None:
        if el is not None:
            note.remove(el)
        return
    if el is None:
        el = ET.Element("accidental")
        idx = next((i for i, c in enumerate(note) if c.tag in _AFTER_ACCIDENTAL), len(note))
        note.insert(idx, el)
    el.text = _ACCIDENTAL.get(alter, "natural")


def _remove(parent: ET.Element, event_notes: list[ET.Element], removals: list[ET.Element]) -> None:
    """Delete chord members. The head carries the lyrics, beams and slurs, so when it is to go a
    survivor's pitch moves onto it and that survivor is deleted instead."""
    head = event_notes[0]
    drop = list(removals)
    if head in drop:
        survivors = [n for n in event_notes[1:] if n not in drop]
        donor = survivors[-1]
        head.remove(head.find("pitch"))
        head.insert(next(i for i, c in enumerate(head) if c.tag == "duration"), copy.deepcopy(donor.find("pitch")))
        _set_accidental(head, _accidental_alter(donor))
        drop.remove(head)
        drop.append(donor)
    for n in drop:
        parent.remove(n)


def _accidental_alter(note: ET.Element) -> int | None:
    text = note.findtext("accidental")
    return None if text is None else next((k for k, v in _ACCIDENTAL.items() if v == text), 0)


def _add_member(parent: ET.Element, want: Note, event: XmlEvent) -> None:
    """Add a chord member after the event's last note, copied from its head minus the per-note extras."""
    new = copy.deepcopy(event.notes[0])
    for tag in ("chord", "tie", "notations", "lyric", "accidental"):
        for el in new.findall(tag):
            new.remove(el)
    new.insert(0, ET.Element("chord"))
    _write_pitch(new, want)
    order = list(parent)
    last = max(event.notes, key=order.index)
    parent.insert(order.index(last) + 1, new)


# ---------------------------------------------------------------- clefs and keys


def _clef_text(c: Clef) -> str:
    return f"{c.sign}{c.line}{f' {c.octave:+d}8' if c.octave else ''}"


def _xml_clef(el: ET.Element) -> Clef:
    return Clef(el.findtext("sign") or "G", int(el.findtext("line") or 2), int(el.findtext("clef-octave-change") or 0))


def _set_clef(el: ET.Element, c: Clef) -> None:
    for child in list(el):
        el.remove(child)
    ET.SubElement(el, "sign").text = c.sign
    ET.SubElement(el, "line").text = str(c.line)
    if c.octave:
        ET.SubElement(el, "clef-octave-change").text = str(c.octave)


def _attr_insert_index(attrs: ET.Element, before: tuple[str, ...]) -> int:
    return next((i for i, c in enumerate(attrs) if c.tag in before), len(attrs))


def _clefs_at_cell_starts(root: ET.Element, alignment: Alignment) -> dict[tuple[str, int, int], Clef]:
    """The MusicXML clef in force at each cell's start, as the file has it before any edit."""
    out: dict[tuple[str, int, int], Clef] = {}
    for part in root.findall("part"):
        pid = part.get("id") or ""
        seen: dict[str, int] = defaultdict(int)
        clefs: dict[int, Clef] = {}
        for m in part.findall("measure"):
            number = m.get("number") or ""
            idx = alignment.measure_index(number, seen[number])
            seen[number] += 1
            noted = False
            late: list[ET.Element] = []
            for el in m:
                if el.tag == "note":
                    noted = True
                elif el.tag == "attributes":
                    if noted:
                        late.append(el)
                    else:
                        clefs, _ = _apply(el, clefs, None)
            for staff, clef in clefs.items():
                out[(pid, idx, staff)] = clef
            for el in late:
                clefs, _ = _apply(el, clefs, None)
    return out


def _fix_clefs_and_keys(root: ET.Element, alignment: Alignment, log: ChangeLog) -> None:
    staves_of: dict[str, int] = defaultdict(lambda: 1)
    for part_id, _, staff in alignment.cells:
        staves_of[part_id] = max(staves_of[part_id], staff)
    for part in root.findall("part"):
        pid = part.get("id") or ""
        seen: dict[str, int] = defaultdict(int)
        clefs: dict[int, Clef] = {}
        fifths: int | None = None
        for m in part.findall("measure"):
            number = m.get("number") or ""
            idx = alignment.measure_index(number, seen[number])
            seen[number] += 1
            start: list[ET.Element] = []
            mid: list[ET.Element] = []
            noted = False
            for el in m:
                if el.tag == "note":
                    noted = True
                elif el.tag == "attributes":
                    (mid if noted else start).append(el)
            prior = dict(clefs)
            for a in start:
                clefs, fifths = _apply(a, clefs, fifths)
            for staff in range(1, staves_of[pid] + 1):
                cell = alignment.cells.get((pid, idx, staff)) if idx is not None else None
                if cell is None or cell.content is None:
                    continue
                want, have = cell.content.clef, clefs.get(staff)
                if have is None or want == have or want.sign not in _CLEF_SIGNS or have.sign not in _CLEF_SIGNS:
                    continue
                _edit_clef(m, start, staff, want, prior.get(staff), staves_of[pid])
                clefs[staff] = want
                log.add("clef", pid, number, _clef_text(have), _clef_text(want),
                        f"PDF page {cell.pdf_measure.page + 1}, staff {staff}")
            first = alignment.cells.get((pid, idx, 1)) if idx is not None else None
            if first is not None and first.content is not None and first.content.key_shown and fifths is not None                     and first.content.key != fifths:
                _edit_key(m, start, first.content.key)
                log.add("key", pid, number, str(fifths), str(first.content.key),
                        f"PDF page {first.pdf_measure.page + 1}, staff 1")
                fifths = first.content.key
            for a in mid:
                clefs, fifths = _apply(a, clefs, fifths)


def _apply(attrs: ET.Element, clefs: dict[int, Clef], fifths: int | None) -> tuple[dict[int, Clef], int | None]:
    clefs = dict(clefs)
    for c in attrs.findall("clef"):
        clefs[int(c.get("number") or 1)] = _xml_clef(c)
    key = attrs.find("key")
    if key is not None and key.findtext("fifths") is not None:
        fifths = int(key.findtext("fifths"))
    return clefs, fifths


def _edit_clef(m: ET.Element, start: list[ET.Element], staff: int, want: Clef, prior: Clef | None, nstaves: int) -> None:
    own = next(((a, c) for a in start for c in a.findall("clef") if int(c.get("number") or 1) == staff), None)
    if own is not None:
        a, c = own
        # Restating the clef already in force is noise, so the element goes instead of changing.
        if prior is not None and prior == want:
            a.remove(c)
            if len(a) == 0:
                m.remove(a)
                start.remove(a)
        else:
            _set_clef(c, want)
        return
    c = ET.Element("clef")
    if nstaves > 1:
        c.set("number", str(staff))
    _set_clef(c, want)
    attrs = start[0] if start else _new_attributes(m, start)
    # after lower-numbered clefs already there
    at = _attr_insert_index(attrs, _CLEF_AFTER)
    attrs.insert(at, c)


def _new_attributes(m: ET.Element, start: list[ET.Element]) -> ET.Element:
    attrs = ET.Element("attributes")
    idx = 0
    for i, el in enumerate(m):
        if el.tag not in ("print", "barline"):
            break
        idx = i + 1
    m.insert(idx, attrs)
    start.append(attrs)
    return attrs


def _edit_key(m: ET.Element, start: list[ET.Element], fifths: int) -> None:
    for a in start:
        key = a.find("key")
        if key is not None:
            f = key.find("fifths")
            if f is None:
                f = ET.SubElement(key, "fifths")
            f.text = str(fifths)
            return
    attrs = start[0] if start else _new_attributes(m, start)
    key = ET.Element("key")
    ET.SubElement(key, "fifths").text = str(fifths)
    attrs.insert(_attr_insert_index(attrs, ("time", "staves", "part-symbol", "instruments", "clef", "staff-details", "transpose")), key)


# ---------------------------------------------------------------- time signatures


def _xml_time(el: ET.Element | None) -> tuple[int, int] | None:
    if el is None:
        return None
    try:
        return int(el.findtext("beats") or ""), int(el.findtext("beat-type") or "")
    except ValueError:
        return None


def _set_time(el: ET.Element, t: TimeSignature) -> None:
    for child in list(el):
        el.remove(child)
    if t.symbol:
        el.set("symbol", "common" if t.symbol == "common" else "cut")
    elif "symbol" in el.attrib:
        del el.attrib["symbol"]
    ET.SubElement(el, "beats").text = str(t.beats)
    ET.SubElement(el, "beat-type").text = str(t.beat_type)


def fix_times(root: ET.Element, alignment: Alignment, log: ChangeLog) -> None:
    """Make the time signature in force match the PDF, so bars are measured against the right length.

    The PDF value is trusted where it is printed, where the MusicXML carries a time change of its own
    (which may be spurious), and at the first bar; elsewhere the carried-over value is left alone.
    """
    for part in root.findall("part"):
        pid = part.get("id") or ""
        seen: dict[str, int] = defaultdict(int)
        have: tuple[int, int] | None = None
        for m in part.findall("measure"):
            number = m.get("number") or ""
            idx = alignment.measure_index(number, seen[number])
            seen[number] += 1
            start: list[ET.Element] = []
            noted = False
            for el in m:
                if el.tag == "note":
                    noted = True
                elif el.tag == "attributes" and not noted:
                    start.append(el)
            own = next((t for a in start for t in a.findall("time")), None)
            own_val = _xml_time(own)
            before = have
            if own is not None and own_val is not None:
                have = own_val
            cell = next((alignment.cells[(pid, idx, s)] for s in (1, 2, 3, 4)
                         if idx is not None and (pid, idx, s) in alignment.cells
                         and alignment.cells[(pid, idx, s)].content is not None), None)
            want = cell.content.time if cell is not None else None
            if want is None or (not cell.content.multirest and not cell.content.events):
                continue
            printed = cell.content.time_shown
            if not (printed or own is not None or before is None) or have == (want.beats, want.beat_type):
                continue
            if own is not None and not printed and before == (want.beats, want.beat_type):
                # A change Audiveris invented: the PDF carries the earlier value through.
                for a in start:
                    if own in list(a):
                        a.remove(own)
            elif own is not None:
                _set_time(own, want)
            else:
                attrs = start[0] if start else _new_attributes(m, start)
                t = ET.Element("time")
                _set_time(t, want)
                attrs.insert(_attr_insert_index(attrs, ("staves", "part-symbol", "instruments", "clef", "staff-details", "transpose")), t)
            log.add("time", pid, number, f"{have[0]}/{have[1]}" if have else "none", f"{want.beats}/{want.beat_type}",
                    f"PDF page {cell.pdf_measure.page + 1}")
            have = (want.beats, want.beat_type)
