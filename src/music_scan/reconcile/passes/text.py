"""Text pass: lyrics, directions, tempo, rehearsal marks and credits from the PDF's text layer.

Everything here is read from `pdf_text` output through the alignment's cells. Lyrics are matched
to notes row by row (one staff of one system) so a syllable that straddles a barline still finds
its note; directions are written once per system (the topmost staff that carries the same text at
the same x), the way full scores print them.
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

import pymupdf

from ..align import Alignment, Cell, ChangeLog
from ..pdf_score import PdfScore
from ..pdf_text import Direction, RehearsalMark, Syllable, TempoMark, TitleBlock

CATEGORY_LYRICS = "lyrics"
CATEGORY_DIRECTIONS = "directions"
CATEGORY_TEMPO = "tempo"
CATEGORY_REHEARSAL = "rehearsal"
CATEGORY_CREDITS = "credits"

# Cost of leaving a syllable unplaced, in points of x distance.
_SKIP_COST = 40.0
# Notes closer than this in x are one onset (voices stacked on a staff).
_SAME_X = 0.6
# A printed direction belongs to the last note starting at or before its x plus this many staff spaces.
_DIRECTION_REACH = 0.8
# Text this similar to existing Audiveris text is the same word, misread.
_SIMILAR = 0.8
_VOLTA = re.compile(r"^\d(?:\s*[.,]\s*\d)*\.?$")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_BEAT_QUARTERS = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25}
_DOT_FACTOR = {0: 1.0, 1: 1.5, 2: 1.75}


def run(root: ET.Element, pdf: PdfScore, alignment: Alignment, log: ChangeLog) -> None:
    cells = _first_cell_per_key(alignment)
    _credits(root, pdf.text.title, log)
    _lyrics(pdf, alignment, cells, log)
    _directions(pdf, alignment, cells, log)


def _first_cell_per_key(alignment: Alignment) -> list[Cell]:
    """One cell per PDF staff-measure (a multi-bar rest maps to several MusicXML bars), in piece order."""
    seen: dict[tuple, Cell] = {}
    for c in sorted(alignment.cells.values(), key=lambda c: (c.measure.index, c.part, c.xml_staff)):
        seen.setdefault(c.key, c)
    return sorted(seen.values(), key=lambda c: (c.measure.index, c.part, c.xml_staff))


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _words_of(el: ET.Element) -> list[ET.Element]:
    return list(el.iter("words"))


# ---------------------------------------------------------------- lyrics


@dataclass
class _Placed:
    order: tuple[int, float]
    syl: Syllable
    note: ET.Element
    cell: Cell
    text: str
    last: bool = True  # final piece of a split span; only it carries the hyphen and extender


def _is_tied_to(note: ET.Element) -> bool:
    return any(t.get("type") == "stop" for t in note.findall("tie"))


def _candidates(cells: list[Cell], voice: str | None = None) -> list[tuple[float, Cell, ET.Element | None]]:
    """Notes of one staff row that can carry a syllable, as (x, cell, first note element or None when the
    PDF has the note but the MusicXML does not). `voice` keeps one MusicXML voice's notes only."""
    out: list[tuple[float, Cell, ET.Element | None]] = []
    for cell in cells:
        for pair in cell.pairs:
            ev = pair.pdf
            if ev is None or ev.is_rest or ev.grace:
                continue
            if voice is not None and (pair.xml is None or pair.xml.voice != voice):
                continue
            note = None
            if pair.xml is not None:
                if pair.xml.is_rest or pair.xml.grace:
                    continue
                note = pair.xml.notes[0]
                if _is_tied_to(note):
                    continue
            out.append((ev.x, cell, note))
    out.sort(key=lambda t: (t[0], t[2] is None))
    merged: list[tuple[float, Cell, ET.Element | None]] = []
    for item in out:
        if merged and item[0] - merged[-1][0] <= _SAME_X:
            continue
        merged.append(item)
    return merged


def _anchor(s: Syllable) -> float:
    return (s.x0 + s.x1) / 2


def _assign(sylls: list[Syllable], xs: list[float]) -> list[int | None]:
    """Order-preserving minimum-distance placement of syllables onto note positions."""
    n, m = len(sylls), len(xs)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(m + 1):
            best, how = dp[i - 1][j] + _SKIP_COST, 1  # syllable i left unplaced
            if j > 0:
                if dp[i][j - 1] < best:
                    best, how = dp[i][j - 1], 2  # note j unused
                c = dp[i - 1][j - 1] + abs(_anchor(sylls[i - 1]) - xs[j - 1])
                if c < best:
                    best, how = c, 0
            dp[i][j], back[i][j] = best, how
    out: list[int | None] = [None] * n
    i, j = n, m
    while i > 0:
        how = back[i][j]
        if how == 0:
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif how == 1:
            i -= 1
        else:
            j -= 1
    return out


# Two words printed with no space glyph overlap by about a kerning unit at the join.
_JOIN_OVERLAP = -0.4


def _span_chars(doc: pymupdf.Document, s: Syllable) -> list[tuple[str, float, float]]:
    """(char, x0, x1) of the drawn glyphs of a syllable, or [] when they do not read back as its text."""
    page = doc[s.page]
    clip = pymupdf.Rect(s.x0 - 0.5, s.y - 12, s.x1 + 0.5, s.y + 4)
    chars: list[tuple[str, float, float]] = []
    for b in page.get_text("rawdict", clip=clip)["blocks"]:
        for line in b.get("lines", ()):
            for sp in line["spans"]:
                for c in sp["chars"]:
                    if abs(c["origin"][1] - s.y) <= 1.5 and s.x0 - 0.3 <= c["bbox"][0] and c["bbox"][2] <= s.x1 + 0.3:
                        chars.append((c["c"], c["bbox"][0], c["bbox"][2]))
    chars.sort(key=lambda t: t[1])
    return chars if "".join(c for c, _, _ in chars if not c.isspace()) == s.text else []


def _split_glued(syl: Syllable, note_xs: list[float], chars: list[tuple[str, float, float]]) -> list[str]:
    """A span printed without spaces covering several notes ("won'tcall") splits where its glyphs
    overlap, one piece per covered note; without glyph positions it splits at lower-to-upper case changes."""
    if len(note_xs) < 2 or syl.hyphen_after or syl.extender_after:
        return [syl.text]
    if not chars:
        parts = [p for p in _CAMEL.split(syl.text) if p]
        return parts if 2 <= len(parts) <= len(note_xs) else [syl.text]
    joins = [i for i in range(1, len(chars)) if chars[i][1] - chars[i - 1][2] <= _JOIN_OVERLAP]
    if not joins:
        return [syl.text]
    # each cut sits nearest the midpoint between neighboring notes
    cuts: list[int] = []
    for a, b in zip(note_xs, note_xs[1:]):
        mid = (a + b) / 2
        free = [i for i in joins if i not in cuts and (not cuts or i > cuts[-1])]
        if free:
            cuts.append(min(free, key=lambda i: abs(chars[i][1] - mid)))
    cuts.sort()
    text = "".join(c for c, _, _ in chars)
    bounds = [0, *cuts, len(chars)]
    return [text[a:b].strip() for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]


# A syllable sits under its note: the note's x falls within the word's extent, give or take a little.
_HIT_BEFORE, _HIT_AFTER = 5.0, 1.0


def _hit_rate(row: list[Syllable], xs: list[float]) -> float:
    hits = sum(any(s.x0 - _HIT_BEFORE <= x <= s.x1 + _HIT_AFTER for x in xs) for s in row)
    return hits / len(row)


def _rows_by_voice(row_cells: list[Cell], rows: list[list[Syllable]]) -> list[str] | None:
    """Voice owning each lyric row when the rows follow different voices of one staff (soprano and alto
    words under a shared staff) rather than being verses. Needs rows that fit one voice's onsets and miss
    the other's; voices with identical rhythm cannot be told apart, so those rows stay verses."""
    voices = sorted({p.xml.voice for c in row_cells for p in c.pairs if p.xml is not None and not p.xml.is_rest})
    if len(voices) < 2 or len(rows) != 2:
        return None
    xs = {v: [x for x, _, n in _candidates(row_cells, v) if n is not None] for v in voices}
    best = None
    for a in voices:
        for b in voices:
            if a == b or not xs[a] or not xs[b]:
                continue
            fit = (_hit_rate(rows[0], xs[a]), _hit_rate(rows[1], xs[b]))
            cross = (_hit_rate(rows[0], xs[b]), _hit_rate(rows[1], xs[a]))
            if min(fit) >= 0.95 and min(cross) <= 0.8 and (best is None or sum(cross) < best[0]):
                best = (sum(cross), [a, b])
    return best[1] if best else None


def _lyric_sig(note: ET.Element) -> list[tuple[str, str, str, bool]]:
    return [(ly.get("number") or "1", ly.findtext("syllabic") or "", ly.findtext("text") or "",
             ly.find("extend") is not None) for ly in note.findall("lyric")]


def _fmt(sig: list[tuple[str, str, str, bool]]) -> str:
    return " ".join(f"{n}:{t}" for n, _, t, _ in sig)


def _lyrics(pdf: PdfScore, alignment: Alignment, cells: list[Cell], log: ChangeLog) -> None:
    rows: dict[tuple[int, int], list[Cell]] = defaultdict(list)
    for c in cells:
        rows[(c.key[0], c.key[1])].append(c)
    syls: dict[tuple[int, int], list[Syllable]] = defaultdict(list)
    for tc in pdf.text.pages:
        for s in tc.lyrics:
            syls[(s.page, s.staff)].append(s)

    placed: dict[tuple[str, int, int, str], list[_Placed]] = defaultdict(list)
    doc = pymupdf.open(pdf.path)
    touched: dict[int, tuple[Cell, dict[ET.Element, list]]] = {}
    for (page, staff), row_syls in sorted(syls.items()):
        row_cells = rows.get((page, staff))
        if not row_cells:
            continue
        all_cands = _candidates(row_cells)
        by_system: dict[int, list[Syllable]] = defaultdict(list)
        for s in row_syls:
            by_system[s.system].append(s)
        for cell in row_cells:
            touched[id(cell)] = (cell, {})
        for _, system_syls in sorted(by_system.items()):
            by_verse: dict[int, list[Syllable]] = defaultdict(list)
            for s in system_syls:
                by_verse[s.verse].append(s)
            ordered = sorted(by_verse.items())
            owners = _rows_by_voice(row_cells, [sorted(vs, key=lambda s: s.x0) for _, vs in ordered])
            for ri, (verse, vs) in enumerate(ordered):
                voice = owners[ri] if owners else None
                cands = all_cands if voice is None else _candidates(row_cells, voice)
                xs = [c[0] for c in cands]
                vs = sorted(vs, key=lambda s: s.x0)
                slots = _assign(vs, xs)
                used = {j for j in slots if j is not None}
                for s, j in zip(vs, slots):
                    if j is None:
                        continue
                    # a no-space span reaching over unclaimed notes spreads across them
                    lo = hi = j
                    while lo > 0 and lo - 1 not in used and xs[lo - 1] >= s.x0 - 1.0:
                        lo -= 1
                    while hi + 1 < len(xs) and hi + 1 not in used and xs[hi + 1] <= s.x1:
                        hi += 1
                    pieces = [s.text]
                    if hi > lo:
                        pieces = _split_glued(s, xs[lo:hi + 1], _span_chars(doc, s))
                    if len(pieces) > 1:
                        used.update(range(lo, lo + len(pieces)))
                    else:
                        lo = j
                    for k, piece in enumerate(pieces):
                        x, cell, note = cands[lo + k]
                        if note is None:
                            log.disagree(CATEGORY_LYRICS, cell.part, cell.measure.number, "", piece,
                                         f"PDF syllable {piece!r} sits on a note the MusicXML lacks")
                            continue
                        last = k == len(pieces) - 1
                        placed[(cell.part, cell.xml_staff, verse, voice or "")].append(
                            _Placed((cell.measure.index, x), s, note, cell, piece, last))

    doc.close()
    for key, items in placed.items():
        items.sort(key=lambda p: p.order)

    desired: dict[ET.Element, list[tuple[str, str, str, bool]]] = defaultdict(list)
    for (part, xml_staff, verse, voice), items in sorted(placed.items()):
        verse = 1 if voice else verse
        prev_hyphen = False
        for p in items:
            hyphen = p.syl.hyphen_after and p.last
            syllabic = ("middle" if hyphen else "end") if prev_hyphen else ("begin" if hyphen else "single")
            prev_hyphen = hyphen
            desired[p.note].append((str(verse), syllabic, p.text, p.syl.extender_after and p.last))

    for cell, _ in touched.values():
        _rewrite_cell(cell, desired, log)

    for c in cells:
        for note in c.element.findall("note"):
            if note.find("rest") is not None and note.find("lyric") is not None and id(c) not in touched:
                before = _fmt(_lyric_sig(note))
                for ly in note.findall("lyric"):
                    note.remove(ly)
                log.add(CATEGORY_LYRICS, c.part, c.measure.number, before, "", "lyric on a rest removed")


def _rewrite_cell(cell: Cell, desired: dict[ET.Element, list], log: ChangeLog) -> None:
    notes = [n for n in cell.element.findall("note") if int(n.findtext("staff") or 1) == cell.xml_staff]
    before_sig = [(n, _lyric_sig(n)) for n in notes]
    after_sig = [(n, sorted(desired.get(n, []), key=lambda t: int(t[0]) if t[0].isdigit() else 0)) for n in notes]
    if all(b == a for (_, b), (_, a) in zip(before_sig, after_sig)):
        return
    for n, sig in after_sig:
        for ly in n.findall("lyric"):
            n.remove(ly)
        for number, syllabic, text, extend in sig:
            ly = ET.SubElement(n, "lyric", {"number": number})
            ET.SubElement(ly, "syllabic").text = syllabic
            ET.SubElement(ly, "text").text = text
            if extend:
                ET.SubElement(ly, "extend")
    before = " ".join(_fmt(s) for _, s in before_sig if s)
    after = " ".join(_fmt(s) for _, s in after_sig if s)
    log.add(CATEGORY_LYRICS, cell.part, cell.measure.number, before, after, "PDF lyric row aligned to notes")


# ---------------------------------------------------------------- directions


def _has_staff_numbers(cell: Cell) -> bool:
    if cell.xml_staff > 1:
        return True
    return any(int(n.findtext("staff") or 1) > 1 for n in cell.element.findall("note"))


def _target_note(cell: Cell, x: float, reach: float) -> ET.Element | None:
    evs = sorted(((p.pdf.x, p.xml.notes[0]) for p in cell.pairs
                  if p.pdf is not None and p.xml is not None and not p.pdf.grace), key=lambda t: t[0])
    if evs:
        before = [n for ex, n in evs if ex <= x + reach]
        return before[-1] if before else evs[0][1]
    notes = [n for n in cell.element.findall("note") if int(n.findtext("staff") or 1) == cell.xml_staff]
    return notes[0] if notes else None


def _insert(cell: Cell, el: ET.Element, target: ET.Element | None) -> None:
    m = cell.element
    if target is not None and target in list(m):
        m.insert(list(m).index(target), el)
        return
    idx = len(m)
    while idx > 0 and m[idx - 1].tag == "barline":
        idx -= 1
    m.insert(idx, el)


def _make_direction(cell: Cell, placement: str, types: list[ET.Element], tempo: str | None) -> ET.Element:
    d = ET.Element("direction")
    if placement in ("above", "below"):
        d.set("placement", placement)
    for t in types:
        dt = ET.SubElement(d, "direction-type")
        dt.append(t)
    if _has_staff_numbers(cell):
        ET.SubElement(d, "staff").text = str(cell.xml_staff)
    if tempo is not None:
        ET.SubElement(d, "sound", {"tempo": tempo})
    return d


def _words_el(text: str, italic: bool, bold: bool) -> ET.Element:
    w = ET.Element("words")
    if italic:
        w.set("font-style", "italic")
    if bold:
        w.set("font-weight", "bold")
    w.text = text
    return w


def _metronome_el(t: TempoMark) -> ET.Element:
    m = ET.Element("metronome")
    ET.SubElement(m, "beat-unit").text = t.unit
    for _ in range(t.dots):
        ET.SubElement(m, "beat-unit-dot")
    bpm = _num(t.bpm)
    if t.bpm_max is not None:
        bpm += "-" + _num(t.bpm_max)
    ET.SubElement(m, "per-minute").text = bpm
    if t.parenthesized:
        m.set("parentheses", "yes")
    return m


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}".rstrip("0").rstrip(".")


def _quarter_tempo(t: TempoMark) -> str:
    return _num(round(t.bpm * _BEAT_QUARTERS[t.unit] * _DOT_FACTOR.get(t.dots, 1.0), 2))


def _metronome_of(m: ET.Element) -> tuple[str | None, int, str | None]:
    return m.findtext("beat-unit"), len(m.findall("beat-unit-dot")), m.findtext("per-minute")


def _find_words(measure: ET.Element, text: str) -> ET.Element | None:
    want = _norm(text)
    best, best_ratio = None, 0.0
    for w in _words_of(measure):
        have = _norm(w.text or "")
        r = 1.0 if have == want else difflib.SequenceMatcher(None, have, want).ratio()
        if r > best_ratio:
            best, best_ratio = w, r
    return best if best_ratio >= _SIMILAR else None


@dataclass
class _Item:
    kind: str  # words | tempo | rehearsal
    cell: Cell
    obj: Direction | TempoMark | RehearsalMark
    spacing: float = 4.0  # staff line spacing, the scale for matching text x to a note

    @property
    def text(self) -> str:
        return self.obj.text

    @property
    def placement(self) -> str:
        return getattr(self.obj, "placement", "above")


def _directions(pdf: PdfScore, alignment: Alignment, cells: list[Cell], log: ChangeLog) -> None:
    items: list[_Item] = []
    # texts the PDF prints that this pass leaves to others, so removal must not touch them
    foreign: dict[tuple[str, int], set[str]] = defaultdict(set)
    for c in cells:
        system = pdf.system_of(c.key[0], c.key[1])
        staff = next((st for st in system.staves if st.index == c.key[1]), None) if system else None
        sp = staff.spacing if staff else 4.0
        for d in pdf.directions_in(c.key):
            if d.chord or d.dynamic:
                foreign[(c.part, c.measure.index)].add(_norm(d.text))
            elif d.text.strip() and not _VOLTA.match(d.text.strip()):
                items.append(_Item("words", c, d, sp))
        items += [_Item("tempo", c, t, sp) for t in pdf.tempos_in(c.key)]
        items += [_Item("rehearsal", c, r, sp) for r in pdf.rehearsals_in(c.key)]

    kept = _dedupe(items)
    supported: dict[tuple[str, int], set[str]] = defaultdict(set)
    for it in kept:
        supported[(it.cell.part, it.cell.measure.index)].add(_norm(it.text))
        if it.kind == "tempo" and it.obj.text:
            supported[(it.cell.part, it.cell.measure.index)].add(_norm(it.obj.text))

    for it in kept:
        if it.kind == "words":
            _write_words(it, log)
        elif it.kind == "tempo":
            _write_tempo(it, log)
        else:
            _write_rehearsal(it, log)

    for c in cells:
        ok = supported[(c.part, c.measure.index)] | foreign[(c.part, c.measure.index)]
        _remove_unsupported(c, ok, log)


def _dedupe(items: list[_Item]) -> list[_Item]:
    """The same text at the same x on several staves of a system is one system-wide mark; keep the topmost."""
    groups: dict[tuple, list[_Item]] = defaultdict(list)
    for it in sorted(items, key=lambda i: (i.cell.key[0], i.cell.key[1], i.obj.x)):
        key = (it.kind, it.cell.measure.index, it.cell.pdf_measure.system_order, _norm(it.text), it.placement)
        bucket = groups[key]
        for g in bucket:
            if abs(g.obj.x - it.obj.x) <= 3.0:
                break
        else:
            bucket.append(it)
    out = [g for bucket in groups.values() for g in bucket]
    return sorted(out, key=lambda i: (i.cell.measure.index, i.cell.part, i.cell.xml_staff, i.obj.x, i.kind))


def _write_words(it: _Item, log: ChangeLog) -> None:
    d: Direction = it.obj
    cell = it.cell
    text = d.text.strip()
    existing = _find_words(cell.element, text)
    if existing is not None:
        if (existing.text or "") != text:
            before = existing.text or ""
            existing.text = text
            log.add(CATEGORY_DIRECTIONS, cell.part, cell.measure.number, before, text, "PDF text replaces misread words")
        return
    el = _make_direction(cell, d.placement, [_words_el(text, d.italic, d.bold)], None)
    _insert(cell, el, _target_note(cell, d.x, _DIRECTION_REACH * it.spacing))
    log.add(CATEGORY_DIRECTIONS, cell.part, cell.measure.number, "", text, f"PDF direction on page {d.page + 1}")


def _write_rehearsal(it: _Item, log: ChangeLog) -> None:
    r: RehearsalMark = it.obj
    cell = it.cell
    for el in cell.element.iter("rehearsal"):
        if _norm(el.text or "") == _norm(r.text):
            return
    el = ET.Element("rehearsal")
    if r.boxed:
        el.set("enclosure", "square")
    el.text = r.text.strip()
    d = _make_direction(cell, "above", [el], None)
    _insert(cell, d, _target_note(cell, r.x, _DIRECTION_REACH * it.spacing))
    log.add(CATEGORY_REHEARSAL, cell.part, cell.measure.number, "", r.text.strip(), f"PDF rehearsal mark on page {r.page + 1}")


def _write_tempo(it: _Item, log: ChangeLog) -> None:
    t: TempoMark = it.obj
    cell = it.cell
    words = t.text.strip()
    metro_ok = t.unit is not None and t.bpm is not None and t.unit in _BEAT_QUARTERS
    reach = _DIRECTION_REACH * it.spacing
    existing_words = _find_words(cell.element, words) if words else None
    existing_metro = next(iter(cell.element.iter("metronome")), None)

    new_types: list[ET.Element] = []
    if words and existing_words is None:
        new_types.append(_words_el(words, t.italic, t.bold))
    elif words and (existing_words.text or "") != words:
        before = existing_words.text or ""
        existing_words.text = words
        log.add(CATEGORY_TEMPO, cell.part, cell.measure.number, before, words, "PDF text replaces misread tempo words")

    if metro_ok and existing_metro is not None:
        want = (t.unit, t.dots, _num(t.bpm) + ("-" + _num(t.bpm_max) if t.bpm_max is not None else ""))
        if _metronome_of(existing_metro) != want:
            before = "{}={}".format(*[_metronome_of(existing_metro)[i] for i in (0, 2)])
            new = _metronome_el(t)
            existing_metro.clear()
            existing_metro.extend(list(new))
            existing_metro.attrib.update(new.attrib)
            owner = next((d for d in cell.element.iter("direction") if d.find(".//metronome") is existing_metro), None)
            if owner is not None:
                _set_sound(owner, _quarter_tempo(t))
            log.add(CATEGORY_TEMPO, cell.part, cell.measure.number, before, f"{t.unit}={want[2]}",
                    "PDF metronome mark replaces Audiveris reading")
        elif t.parenthesized and existing_metro.get("parentheses") != "yes":
            existing_metro.set("parentheses", "yes")
            log.add(CATEGORY_TEMPO, cell.part, cell.measure.number, "", "parentheses",
                    "PDF metronome mark is parenthesized")
        metro_new = False
    else:
        metro_new = metro_ok
    if metro_new:
        new_types.append(_metronome_el(t))
    if new_types:
        d = _make_direction(cell, "above", new_types, _quarter_tempo(t) if metro_new else None)
        _insert(cell, d, _target_note(cell, t.x, reach))
        what = " ".join(x for x in (words, f"{t.unit}={_num(t.bpm)}" if metro_new else "") if x)
        log.add(CATEGORY_TEMPO, cell.part, cell.measure.number, "", what, f"PDF tempo mark on page {t.page + 1}")


def _set_sound(direction: ET.Element, tempo: str) -> None:
    snd = direction.find("sound")
    if snd is None:
        snd = ET.SubElement(direction, "sound")
    snd.set("tempo", tempo)


def _remove_unsupported(cell: Cell, supported: set[str], log: ChangeLog) -> None:
    for d in list(cell.element.findall("direction")):
        for dt in list(d.findall("direction-type")):
            w = dt.find("words")
            if w is None or _norm(w.text or "") in supported or not _norm(w.text or ""):
                continue
            if any(difflib.SequenceMatcher(None, _norm(w.text or ""), s).ratio() >= _SIMILAR for s in supported):
                continue
            d.remove(dt)
            log.add(CATEGORY_DIRECTIONS, cell.part, cell.measure.number, w.text or "", "",
                    "words direction the PDF does not print")
        if d.find("direction-type") is None:
            cell.element.remove(d)


# ---------------------------------------------------------------- credits


def _credits(root: ET.Element, tb: TitleBlock, log: ChangeLog) -> None:
    creators = [("composer", tb.composer), ("lyricist", tb.lyricist), ("arranger", tb.arranger)]
    creators = [(k, v.strip()) for k, v in creators if v and v.strip()]
    rights = [r.strip() for r in tb.copyright if r.strip()]
    title = tb.title.strip() if tb.title else None
    subtitle = tb.subtitle.strip() if tb.subtitle else None
    if not (title or subtitle or creators or rights):
        return

    if title:
        work = root.find("work")
        if work is None:
            work = ET.Element("work")
            root.insert(0, work)
        wt = work.find("work-title")
        if wt is None:
            wt = ET.SubElement(work, "work-title")
            work.remove(wt)
            work.insert(0, wt)
        if (wt.text or "") != title:
            log.add(CATEGORY_CREDITS, "", "", wt.text or "", title, "PDF title block")
            wt.text = title

    ident = root.find("identification")
    if ident is None:
        ident = ET.Element("identification")
        root.insert(list(root).index(root.find("part-list")) if root.find("part-list") is not None else 0, ident)
    kinds = {k for k, _ in creators}
    have = [(c.get("type"), (c.text or "").strip()) for c in ident.findall("creator")]
    want = [(k, v) for k, v in creators]
    if [h for h in have if h[0] in kinds] != want:
        for c in ident.findall("creator"):
            if c.get("type") in kinds:
                ident.remove(c)
        for i, (k, v) in enumerate(want):
            c = ET.Element("creator", {"type": k})
            c.text = v
            ident.insert(i, c)
        log.add(CATEGORY_CREDITS, "", "", "; ".join(f"{t}: {v}" for t, v in have if t in kinds),
                "; ".join(f"{k}: {v}" for k, v in want), "PDF title block")
    if rights:
        old = [(r.text or "").strip() for r in ident.findall("rights")]
        if old != rights:
            for r in ident.findall("rights"):
                ident.remove(r)
            at = len(ident.findall("creator"))
            for i, text in enumerate(rights):
                r = ET.Element("rights")
                r.text = text
                ident.insert(at + i, r)
            log.add(CATEGORY_CREDITS, "", "", "; ".join(old), "; ".join(rights), "PDF copyright lines")

    wanted = [("title", title), ("subtitle", subtitle)] + creators + [("rights", r) for r in rights]
    wanted = [(k, v) for k, v in wanted if v]
    page1 = [c for c in root.findall("credit") if (c.get("page") or "1") == "1"]
    have_credits = [(c.findtext("credit-type") or "", (c.findtext("credit-words") or "").strip()) for c in page1]
    if have_credits == wanted:
        return
    first = list(root).index(page1[0]) if page1 else None
    for c in page1:
        root.remove(c)
    if first is None:
        anchor = root.find("defaults") if root.find("defaults") is not None else ident
        first = list(root).index(anchor) + 1
    for i, (kind, text) in enumerate(wanted):
        c = ET.Element("credit", {"page": "1"})
        ET.SubElement(c, "credit-type").text = kind
        ET.SubElement(c, "credit-words").text = text
        root.insert(first + i, c)
    log.add(CATEGORY_CREDITS, "", "", "; ".join(f"{k}: {v}" for k, v in have_credits),
            "; ".join(f"{k}: {v}" for k, v in wanted), "PDF page 1 credits")
