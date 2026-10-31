"""Find bars whose voices don't add up to the time signature, the usual sign of an OMR misread."""

import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


@dataclass
class VoiceError:
    part: str
    voice: str
    # in whole notes, so a stray 16th rest reads as +1/16
    off_by: Fraction


@dataclass
class BarError:
    measure: str
    page: int
    voices: list[VoiceError] = field(default_factory=list)
    padded: list[str] = field(default_factory=list)
    # Parts with no such bar, or a bar with no notes or rests; both shift the part's later bars in Dorico.
    missing: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)

    def format(self) -> str:
        culprits = ", ".join(
            [f"{v.part} v{v.voice} {'+' if v.off_by > 0 else '-'}{abs(v.off_by)}" for v in self.voices]
            + [f"{p} missing" for p in self.missing]
            + [f"{p} empty" for p in self.empty]
        )
        if self.padded:
            culprits += f"  ({len(self.padded)} other part(s) padded to match)"
        return f"  m{self.measure:<5} page {self.page:<4} {culprits}"


def load(path: Path) -> ET.Element:
    if path.suffix.lower() != ".mxl":
        return ET.parse(path).getroot()
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//rootfile").get("full-path")
        return ET.fromstring(z.read(rootfile))


def check(path: Path) -> list[BarError]:
    return check_root(load(path))


def check_root(root: ET.Element) -> list[BarError]:
    """`check` on an already parsed score-partwise tree, so callers can validate an in-memory edit."""
    names = {sp.get("id"): sp.findtext("part-name") or sp.get("id") for sp in root.iter("score-part")}
    bars: dict[str, BarError] = {}
    pages: dict[str, int] = {}
    present: dict[str, set[str]] = {}

    for part in root.findall("part"):
        label = f"{part.get('id')} {names.get(part.get('id'), '')}".strip()
        present[label] = set()
        divisions = 1
        bar = Fraction(1)
        page = 1
        for m in part.findall("measure"):
            if (p := m.find("print")) is not None and p.get("new-page") == "yes":
                page += 1
            pages.setdefault(m.get("number"), page)
            present[label].add(m.get("number"))
            if (a := m.find("attributes")) is not None:
                divisions = int(a.findtext("divisions") or divisions)
                if a.find("time") is not None:
                    bar = Fraction(int(a.findtext("time/beats")), int(a.findtext("time/beat-type")))
            if m.find("note") is None:
                bars.setdefault(m.get("number"), BarError(m.get("number"), page)).empty.append(label)
                continue
            # Pickup bars are short on purpose.
            if m.get("implicit") == "yes":
                continue

            pos = 0
            ends: dict[str, int] = defaultdict(int)
            content: dict[str, bool] = defaultdict(bool)
            for el in m:
                if el.tag == "note":
                    if el.find("chord") is not None or el.find("grace") is not None:
                        continue
                    voice = el.findtext("voice") or "1"
                    pos += int(el.findtext("duration") or 0)
                    ends[voice] = max(ends[voice], pos)
                    rest = el.find("rest")
                    # A whole-bar rest is Audiveris's filler; it gets stretched when another part overflows.
                    if rest is None or (rest.get("measure") != "yes" and el.find("type") is not None):
                        content[voice] = True
                elif el.tag == "backup":
                    pos -= int(el.findtext("duration"))
                elif el.tag == "forward":
                    pos += int(el.findtext("duration"))

            expected = bar * 4 * divisions
            for voice, end in ends.items():
                if end == expected:
                    continue
                err = bars.setdefault(m.get("number"), BarError(m.get("number"), page))
                if content[voice]:
                    err.voices.append(VoiceError(label, voice, Fraction(end, 4 * divisions) - bar))
                elif label not in err.padded:
                    err.padded.append(label)

    for label, numbers in present.items():
        for num in pages.keys() - numbers:
            bars.setdefault(num, BarError(num, pages[num])).missing.append(label)

    return sorted(bars.values(), key=lambda b: (len(b.measure), b.measure))


def format_report(errors: list[BarError]) -> str:
    if not errors:
        return "rhythm check: every bar adds up"
    lines = [f"rhythm check: {len(errors)} bar(s) don't add up (part, voice, off by in whole notes)"]
    lines += [e.format() for e in errors]
    return "\n".join(lines)
