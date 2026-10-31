"""Parts pass grouping: which PDF staves make one instrument lane, and which lanes one output part."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .. import instruments
from ..align import Alignment, StaffMatch
from .parts_compose import Src


def lanes(alignment: Alignment) -> dict[tuple[int, int], str]:
    """Lane per PDF staff (page, staff). Most lanes are the aligned instrument itself; single-staff
    choir groups ("S / A", "(Unison Women)", "(Women - Group 2)") come and go between systems, so
    they share upper and lower voice lanes numbered top down within each system, keeping the lane
    an instrument had before whenever the staff order allows."""
    by_key = {i.key: i for i in alignment.instruments}
    known: dict[str, str] = {}
    out: dict[tuple[int, int], str] = {}
    by_system: dict[int, list[StaffMatch]] = defaultdict(list)
    for s in alignment.staves:
        by_system[s.system].append(s)
    for system in sorted(by_system):
        last: dict[str, int] = {}
        taken: set[str] = set()
        for s in sorted(by_system[system], key=lambda s: s.staff_in_system):
            inst = by_key[s.instrument]
            reg = instruments.voice_register(inst.name) if inst.staves == 1 else None
            if reg not in ("upper", "lower"):
                out[(s.page, s.staff)] = s.instrument
                continue
            lane = known.get(s.instrument)
            n = int(lane.rsplit(":", 1)[1]) if lane else 0
            if lane is None or n <= last.get(reg, 0) or lane in taken:
                n = last.get(reg, 0) + 1
                while f"choir:{reg}:{n}" in taken:
                    n += 1
                lane = f"choir:{reg}:{n}"
                known.setdefault(s.instrument, lane)
            last[reg] = n
            taken.add(lane)
            out[(s.page, s.staff)] = lane
    return out


@dataclass(eq=False)
class Unit:
    # (lane, instrument staff) for each staff of the output part, in order
    slots: tuple[tuple[str, int], ...]
    instruments: list[str] = field(default_factory=list)
    id: str = ""
    # per xml measure index: one source per staff (None: fill with rests), and whether the PDF prints it
    sources: dict[int, list[Src | None]] = field(default_factory=dict)
    visible: dict[int, list[bool]] = field(default_factory=dict)

    @property
    def lane(self) -> str:
        return self.slots[0][0]

    @property
    def staves(self) -> tuple[int, ...]:
        """Instrument staff numbers of the main lane."""
        return tuple(n for lane, n in self.slots if lane == self.lane)


def lane_groupings(alignment: Alignment, slot_of, bars_in: dict[int, int]) -> dict[str, list[tuple[int, ...]]]:
    """Per lane, how its staves split into output parts: the split Audiveris used for the most
    bars (a grand staff kept together beats one split across two parts on a tie)."""
    votes: dict[str, dict[tuple[tuple[int, ...], ...], int]] = defaultdict(lambda: defaultdict(int))
    seen: dict[str, set[int]] = defaultdict(set)
    by_system: dict[tuple[str, int], list[StaffMatch]] = defaultdict(list)
    for s in alignment.staves:
        lane, ist = slot_of[(s.page, s.staff)]
        seen[lane].add(ist)
        if s.part is not None:
            by_system[(lane, s.system)].append(s)
    for (lane, system), ss in sorted(by_system.items()):
        groups: dict[str, list[int]] = defaultdict(list)
        for ist, part in sorted((slot_of[(s.page, s.staff)][1], s.part) for s in ss):
            groups[part].append(ist)
        split = tuple(sorted(tuple(g) for g in groups.values()))
        votes[lane][split] += bars_in.get(system, 0)
    out: dict[str, list[tuple[int, ...]]] = {}
    for lane in sorted(seen):
        options = sorted(votes[lane].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
        chosen = list(options[0][0]) if options else []
        covered = {n for g in chosen for n in g}
        chosen += [(n,) for n in sorted(seen[lane] - covered)]
        out[lane] = sorted(chosen)
    return out


def merge_units(units: list[Unit], alignment: Alignment, slot_of, bars_in: dict[int, int]) -> list[Unit]:
    """Join units that Audiveris keeps as the staves of one part in most bars where both are
    printed: an unlabeled piano reads as two instruments ("Staff 1", "Staff 2") but is one part."""
    by_system: dict[int, dict[tuple[str, int], StaffMatch]] = defaultdict(dict)
    for s in alignment.staves:
        by_system[s.system][slot_of[(s.page, s.staff)]] = s
    together: dict[tuple[int, int], int] = defaultdict(int)
    apart: dict[tuple[int, int], int] = defaultdict(int)
    first_seen: dict[int, tuple[int, int]] = {}
    for system in sorted(by_system):
        slots = by_system[system]
        held: dict[int, set[str]] = {}
        for i, u in enumerate(units):
            ms = [slots.get(sl) for sl in u.slots]
            if all(m is not None and m.part is not None for m in ms):
                held[i] = {m.part for m in ms}
                first_seen.setdefault(i, (system, min(m.staff_in_system for m in ms)))
        for i in held:
            for j in held:
                if i < j:
                    if len(held[i]) == 1 and held[i] == held[j]:
                        together[(i, j)] += bars_in.get(system, 0)
                    else:
                        apart[(i, j)] += bars_in.get(system, 0)
    root = list(range(len(units)))

    def find(i: int) -> int:
        while root[i] != i:
            i = root[i]
        return i

    for (i, j), n in sorted(together.items()):
        if n > apart.get((i, j), 0):
            a, b = find(i), find(j)
            root[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(units)):
        groups[find(i)].append(i)
    out = []
    for g in sorted(groups):
        members = sorted(groups[g], key=lambda i: (first_seen.get(i, (10**9, 0)), i))
        out.append(Unit(tuple(sl for i in members for sl in units[i].slots)))
    return out


def score_order(units: list[Unit], alignment: Alignment, slot_of) -> list[Unit]:
    """Score order: each system's top-to-bottom order, with a unit first seen later placed right
    after the unit above it in that system."""
    unit_of = {sl: u for u in units for sl in u.slots}
    order: list[Unit] = []
    by_system: dict[int, list[StaffMatch]] = defaultdict(list)
    for s in alignment.staves:
        by_system[s.system].append(s)
    for system in sorted(by_system):
        prev: Unit | None = None
        for s in sorted(by_system[system], key=lambda s: s.staff_in_system):
            u = unit_of[slot_of[(s.page, s.staff)]]
            if u not in order:
                order.insert(order.index(prev) + 1 if prev is not None else len(order), u)
            prev = u
    order += [u for u in units if u not in order]
    return order


def lane_text(u: Unit, inst_by_key) -> str:
    return " / ".join(inst_by_key[k].name for k in u.instruments) or u.lane
