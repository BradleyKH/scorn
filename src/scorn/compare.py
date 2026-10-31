"""Score a recognized MusicXML file against its ground truth: notes, then markings, per part."""

import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from music21 import chord, converter, note, stream


@dataclass
class PartScore:
    name: str
    truth_events: int
    found_events: int
    pitch_accuracy: float
    note_accuracy: float


@dataclass
class Report:
    truth_parts: int
    found_parts: int
    truth_measures: int
    found_measures: int
    parts: list[PartScore]
    markings: dict[str, "CategoryScore"] = field(default_factory=dict)

    @property
    def note_accuracy(self) -> float:
        return _weighted(self.parts, "note_accuracy")

    @property
    def pitch_accuracy(self) -> float:
        return _weighted(self.parts, "pitch_accuracy")

    def format(self) -> str:
        lines = [
            f"parts     truth {self.truth_parts:>4}   found {self.found_parts:>4}",
            f"measures  truth {self.truth_measures:>4}   found {self.found_measures:>4}",
            "",
            f"{'part':<24}{'events':>14}{'pitch acc':>12}{'note acc':>11}",
        ]
        for p in self.parts:
            events = f"{p.found_events}/{p.truth_events}"
            lines.append(
                f"{p.name[:23]:<24}{events:>14}{p.pitch_accuracy:>11.1%}{p.note_accuracy:>11.1%}"
            )
        shown = [m for m in self.markings.values() if m.truth or m.found]
        if shown:
            lines += ["", f"{'marking':<14}{'truth':>7}{'found':>7}{'match':>7}{'precision':>11}{'recall':>9}"]
            for m in shown:
                lines.append(
                    f"{m.category:<14}{m.truth:>7}{m.found:>7}{m.matched:>7}"
                    f"{m.precision:>11.1%}{m.recall:>9.1%}"
                )
        return "\n".join(lines)


def _weighted(parts: list["PartScore"], attr: str) -> float:
    total = sum(p.truth_events for p in parts)
    if not total:
        return 1.0
    return sum(getattr(p, attr) * p.truth_events for p in parts) / total


def compare(truth_path: Path, found_path: Path) -> Report:
    truth = converter.parse(truth_path)
    found = converter.parse(found_path)
    scores = []
    for i, truth_part in enumerate(truth.parts):
        expected = events(truth_part)
        actual = events(found.parts[i]) if i < len(found.parts) else []
        scores.append(
            PartScore(
                name=truth_part.partName or f"part {i + 1}",
                truth_events=len(expected),
                found_events=len(actual),
                pitch_accuracy=accuracy([e[0] for e in expected], [e[0] for e in actual]),
                note_accuracy=accuracy(expected, actual),
            )
        )
    return Report(
        markings=compare_markings(truth_path, found_path),
        truth_parts=len(truth.parts),
        found_parts=len(found.parts),
        truth_measures=measure_count(truth),
        found_measures=measure_count(found),
        parts=scores,
    )


def events(part: stream.Part) -> list[tuple[str, float]]:
    """(pitch, quarter length) per note, chord, or rest; voices flattened, grace notes skipped.

    Pitches compare by MIDI number so an enharmonic respelling isn't counted as a wrong note.
    Hidden rests are skipped: music21 pads short final measures with them, and no engine can see one.
    """
    out = []
    for el in part.flatten().notesAndRests:
        if el.duration.isGrace:
            continue
        if isinstance(el, note.Rest) and el.style.hideObjectOnPrint:
            continue
        if isinstance(el, chord.Chord):
            label = "+".join(str(p.midi) for p in sorted(el.pitches))
        elif isinstance(el, note.Note):
            label = str(el.pitch.midi)
        else:
            label = "r"
        out.append((label, round(float(el.quarterLength), 3)))
    return out


def measure_count(score: stream.Score) -> int:
    if not score.parts:
        return 0
    return len(score.parts[0].getElementsByClass(stream.Measure))


def accuracy(expected: list, actual: list) -> float:
    """1 - edit distance / truth length, floored at zero, so dropped and extra events both cost."""
    if not expected:
        return 1.0 if not actual else 0.0
    prev = list(range(len(actual) + 1))
    for i, e in enumerate(expected, start=1):
        cur = [i] + [0] * len(actual)
        for j, a in enumerate(actual, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (e != a))
        prev = cur
    return max(0.0, 1 - prev[-1] / len(expected))


# Marking metrics. These read the XML directly: music21 drops or reshapes several of the
# things Audiveris gets wrong (misplaced staves, unterminated spanners, lyrics on rests).

CATEGORIES = (
    "dynamics",
    "hairpins",
    "articulations",
    "ornaments",
    "fermatas",
    "ties",
    "slurs",
    "lyrics",
    "part names",
)
# Onsets within this many quarter notes count as the same place in the measure.
ONSET_TOLERANCE = 0.5


@dataclass
class CategoryScore:
    category: str
    truth: int = 0
    found: int = 0
    matched: int = 0

    @property
    def precision(self) -> float:
        return self.matched / self.found if self.found else (1.0 if not self.truth else 0.0)

    @property
    def recall(self) -> float:
        return self.matched / self.truth if self.truth else (1.0 if not self.found else 0.0)


# (category, measure index, onset in quarters, key)
Mark = tuple[str, int, float, str]


def compare_markings(truth_path: Path, found_path: Path) -> dict[str, CategoryScore]:
    """Per-category counts over all matched parts; precision = matched/found, recall = matched/truth."""
    scores = {c: CategoryScore(c) for c in CATEGORIES}
    for t, f in pair_parts(read_parts(truth_path), read_parts(found_path)):
        t_marks = t[1] if t else []
        f_marks = f[1] if f else []
        for category in CATEGORIES:
            tm = [m for m in t_marks if m[0] == category]
            fm = [m for m in f_marks if m[0] == category]
            s = scores[category]
            s.truth += len(tm)
            s.found += len(fm)
            s.matched += match_marks(tm, fm)
    return scores


def pair_parts(truth: list, found: list) -> list[tuple]:
    """Pair parts by unique matching name first, then by position; leftovers pair with nothing."""
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for i, (name, _) in enumerate(truth):
        same_t = [j for j, (n, _) in enumerate(truth) if n == name]
        same_f = [j for j, (n, _) in enumerate(found) if n == name]
        if name and len(same_t) == 1 and len(same_f) == 1:
            pairs[i] = same_f[0]
            used.add(same_f[0])
    free = [j for j in range(len(found)) if j not in used]
    for i in range(len(truth)):
        if i not in pairs and free:
            pairs[i] = free.pop(0)
    out = [(truth[i], found[pairs[i]] if i in pairs else None) for i in range(len(truth))]
    out += [(None, found[j]) for j in free]
    return out


def match_marks(truth: list[Mark], found: list[Mark]) -> int:
    """Greedy one-to-one match on (measure, key), nearest onset first, within the tolerance."""
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for _, measure, onset, key in found:
        buckets[(measure, key)].append(onset)
    matched = 0
    for _, measure, onset, key in sorted(truth, key=lambda m: (m[1], m[2], m[3])):
        candidates = buckets.get((measure, key))
        if not candidates:
            continue
        best = min(candidates, key=lambda o: abs(o - onset))
        if abs(best - onset) <= ONSET_TOLERANCE:
            candidates.remove(best)
            matched += 1
    return matched


def load_xml(path: Path) -> ET.Element:
    if path.suffix.lower() != ".mxl":
        return ET.parse(path).getroot()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        root_file = None
        if "META-INF/container.xml" in names:
            rf = ET.fromstring(z.read("META-INF/container.xml")).find(".//rootfile")
            root_file = rf.get("full-path") if rf is not None else None
        if root_file is None:
            root_file = next(n for n in sorted(names) if not n.startswith("META-INF") and n.endswith(".xml"))
        return ET.fromstring(z.read(root_file))


def read_parts(path: Path) -> list[tuple[str, list[Mark]]]:
    """(part name, marks) per part in file order; the name itself is a mark at measure 0."""
    root = load_xml(path)
    names = {}
    for sp in root.findall("./part-list/score-part"):
        names[sp.get("id")] = norm(sp.findtext("part-name") or "")
    out = []
    for part in root.findall("part"):
        name = names.get(part.get("id"), "")
        marks: list[Mark] = [("part names", 0, 0.0, name)] if name else []
        out.append((name, marks + part_marks(part)))
    return out


def norm(text: str) -> str:
    return " ".join(text.lower().split())


def part_marks(part: ET.Element) -> list[Mark]:
    marks: list[Mark] = []
    divisions = Fraction(1)
    for mi, measure in enumerate(part.findall("measure")):
        pos = Fraction(0)
        last_start = Fraction(0)
        for el in measure:
            if el.tag == "attributes":
                d = (el.findtext("divisions") or "").strip()
                if d:
                    divisions = Fraction(d)
            elif el.tag == "backup":
                pos -= Fraction(el.findtext("duration", "0").strip()) / divisions
            elif el.tag == "forward":
                pos += Fraction(el.findtext("duration", "0").strip()) / divisions
            elif el.tag == "direction":
                offset = Fraction(el.findtext("offset", "0").strip()) / divisions
                staff = el.findtext("staff", "1").strip()
                for dt in el.findall("direction-type"):
                    marks += direction_marks(dt, mi, float(pos + offset), staff)
            elif el.tag == "note":
                is_chord = el.find("chord") is not None
                if not is_chord:
                    last_start = pos
                staff = el.findtext("staff", "1").strip()
                marks += note_marks(el, mi, float(last_start), staff)
                if not is_chord and el.find("grace") is None:
                    pos += Fraction(el.findtext("duration", "0").strip()) / divisions
    return marks


def direction_marks(dt: ET.Element, mi: int, at: float, staff: str) -> list[Mark]:
    out = []
    for d in dt.findall("dynamics"):
        out += [("dynamics", mi, at, f"{c.tag}@{staff}") for c in d]
    for w in dt.findall("wedge"):
        out.append(("hairpins", mi, at, f"{w.get('type')}@{staff}"))
    return out


def note_marks(note_el: ET.Element, mi: int, at: float, staff: str) -> list[Mark]:
    out = []
    for t in note_el.findall("tie"):
        out.append(("ties", mi, at, f"{t.get('type')}@{staff}"))
    for lyric in note_el.findall("lyric"):
        text = "".join(t.text or "" for t in lyric.findall("text"))
        if text.strip():
            out.append(("lyrics", mi, at, norm(text)))
    for n in note_el.findall("notations"):
        for a in n.findall("articulations"):
            out += [("articulations", mi, at, f"{c.tag}@{staff}") for c in a]
        for o in n.findall("ornaments"):
            out += [("ornaments", mi, at, f"{c.tag}@{staff}") for c in o]
        out += [("fermatas", mi, at, f"fermata@{staff}") for _ in n.findall("fermata")]
        out += [("slurs", mi, at, f"{s.get('type')}@{staff}") for s in n.findall("slur")]
    return out
