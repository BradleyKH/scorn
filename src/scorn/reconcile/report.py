"""The reconcile change report: JSON for programs, and a short plain-English summary for people."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field

from scorn import rhythm

from .align import Change, measure_sort_key

# How many bars the summary names before it says "and N more".
_LISTED_BARS = 40

# Plural wording for marks, in the order the summary lists them.
_MARK_WORDS = {
    "dynamics": "loudness marks (such as mf)",
    "hairpins": "crescendo and diminuendo wedges",
    "articulations": "articulation marks (such as staccato dots and accents)",
    "ornaments": "ornaments and tremolo strokes",
    "fermatas": "pauses (fermatas)",
    "arpeggios": "rolled-chord signs",
    "pedals": "pedal marks",
    "ties": "ties",
    "slurs": "slurs",
}
_NOTE_CATEGORIES = ("pitch", "rhythm")
_SYLLABLE = re.compile(r"(?:^|\s)\d+:")


def failing_bars(root: ET.Element) -> set[str]:
    """Bar numbers where some part's notes don't add up to the time signature, or a part has no bar."""
    return {e.measure for e in rhythm.check_root(root) if e.voices or e.missing or e.empty}


@dataclass
class Report:
    applicable: bool
    source: str
    reason: str = ""
    changes: list[Change] = field(default_factory=list)
    rollbacks: list[str] = field(default_factory=list)
    failing_before: list[str] = field(default_factory=list)
    failing_after: list[str] = field(default_factory=list)
    part_names: dict[str, str] = field(default_factory=dict)
    part_order: list[str] = field(default_factory=list)

    @property
    def applied(self) -> list[Change]:
        return [c for c in self.changes if c.applied]

    @property
    def disagreements(self) -> list[Change]:
        return [c for c in self.changes if not c.applied and c.category != "rollback"]

    def unchanged_bars(self) -> list[tuple[str, str]]:
        """(part id, bar) pairs left alone because the PDF and the scan disagree, in score order.

        A disagreement counts as settled when a later pass rewrote that bar's notes anyway."""
        rank = {p: i for i, p in enumerate(self.part_order)}
        last_fix: dict[tuple[str, str], int] = {}
        for c in self.applied:
            if c.category in _NOTE_CATEGORIES:
                last_fix[(c.part, c.measure)] = max(c.seq, last_fix.get((c.part, c.measure), -1))
        bars = {(c.part, c.measure) for c in self.disagreements
                if c.part in rank and c.measure
                and last_fix.get((c.part, c.measure), -1) < c.seq}
        return sorted(bars, key=lambda b: (rank[b[0]], measure_sort_key(b[1])))

    def to_dict(self) -> dict:
        return {
            "applicable": self.applicable,
            "reason": self.reason,
            "source": self.source,
            "summary": self.summary(),
            "rolled_back": list(self.rollbacks),
            "failing_bars": {"before": list(self.failing_before), "after": list(self.failing_after)},
            "parts": [{"id": p, "name": self.part_names.get(p, p)} for p in self.part_order],
            "changes": [
                {"category": c.category, "part": c.part, "measure": c.measure, "before": c.before,
                 "after": c.after, "evidence": c.evidence, "applied": c.applied}
                for c in self.changes
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def summary(self) -> str:
        if not self.applicable:
            # The technical reason stays in to_dict(), out of the sentence people read.
            return "The PDF could not be read as printed music, so the scan was left exactly as it was."
        lines = _sentences(self.applied)
        if not lines:
            lines = ["The scan already agreed with the PDF; nothing was changed."]
        for name in self.rollbacks:
            lines.append(f"The {name} corrections were undone because they made some bars stop adding up.")
        before, after = len(self.failing_before), len(self.failing_after)
        if before or after:
            lines.append(f"Bars whose notes don't add up to the time signature: {before} before, {after} after.")
        unchanged = self.unchanged_bars()
        if unchanged:
            lines.append(f"{_n(len(unchanged), 'bar')} left unchanged because the PDF and the scan disagree "
                         "(listed below).")
        text = "\n".join(lines)
        if unchanged:
            text += "\n\nPlease check these bars by eye:\n" + self._bar_list(unchanged)
        return text

    def _bar_list(self, bars: list[tuple[str, str]]) -> str:
        by_part: dict[str, list[str]] = defaultdict(list)
        for part, measure in bars[:_LISTED_BARS]:
            by_part[part].append(measure)
        out = [f"  {self.part_names.get(p, p)}: bar{'s' if len(ms) > 1 else ''} {', '.join(ms)}"
               for p, ms in by_part.items()]
        if len(bars) > _LISTED_BARS:
            out.append(f"  and {len(bars) - _LISTED_BARS} more")
        return "\n".join(out)


def _n(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _items(text: str) -> int:
    return len([t for t in text.split(",") if t.strip()])


def _sentences(changes: list[Change]) -> list[str]:
    by_cat: dict[str, list[Change]] = defaultdict(list)
    for c in changes:
        by_cat[c.category].append(c)
    out = []

    parts = by_cat.get("parts", [])
    moved = [c for c in parts if c.before.startswith("part ") and c.after not in ("removed", "")]
    named = [c for c in parts if not c.measure]
    transposed = [c for c in parts if c.before.startswith("transpose")]
    removed = [c for c in parts if c.after == "removed"]
    if moved:
        out.append(f"Moved music back to the right instrument in {_n(len(moved), 'place')}.")
    if named:
        out.append(f"Named {_n(len(named), 'instrument')} from the PDF.")
    if transposed:
        out.append(f"Set the transposition for {_n(len(transposed), 'instrument')}.")
    if removed:
        out.append(f"Removed {_n(len(removed), 'empty part')} that the PDF doesn't have.")

    notes = by_cat.get("pitch", [])
    if notes:
        bars = {(c.part, c.measure) for c in notes}
        out.append(f"Fixed {_n(len(notes), 'note')} in {_n(len(bars), 'bar')}.")
    clefs = by_cat.get("clef", []) + by_cat.get("key", [])
    if clefs:
        out.append(f"Corrected {_n(len(clefs), 'clef or key signature', 'clefs or key signatures')}.")
    rhythms = {(c.part, c.measure) for c in by_cat.get("rhythm", [])}
    if rhythms:
        out.append(f"Rewrote the rhythm in {_n(len(rhythms), 'bar')}.")

    for cat, words in _MARK_WORDS.items():
        added = sum(_items(c.after) for c in by_cat.get(cat, []))
        dropped = sum(_items(c.before) for c in by_cat.get(cat, []))
        if added and dropped:
            out.append(f"Added {added} and removed {dropped} {words}.")
        elif added:
            out.append(f"Added {added} {words}.")
        elif dropped:
            out.append(f"Removed {dropped} {words} that the PDF doesn't show.")

    lyrics = by_cat.get("lyrics", [])
    written = sum(len(_SYLLABLE.findall(c.after)) for c in lyrics)
    if written:
        bars = {(c.part, c.measure) for c in lyrics if c.after}
        out.append(f"Wrote {_n(written, 'lyric syllable')} in {_n(len(bars), 'bar')}.")
    cleared = sum(1 for c in lyrics if not c.after)
    if cleared:
        out.append(f"Removed stray lyrics from {_n(cleared, 'bar')}.")
    words = len(by_cat.get("directions", [])) + len(by_cat.get("tempo", [])) + len(by_cat.get("rehearsal", []))
    if words:
        out.append(f"Corrected {_n(words, 'tempo mark or text instruction', 'tempo marks and text instructions')}.")
    if by_cat.get("credits"):
        out.append("Set the title and credits from the first page.")
    return out
