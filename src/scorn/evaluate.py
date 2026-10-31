"""Ground-truth evaluation: scan PDFs that ship with a MusicXML sibling and score the results."""

import csv
import hashlib
import os
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf

from scorn import compare
from scorn.engines import audiveris

CLASSES = ("vector-music", "outlined", "scan", "other")
TRUTH_SUFFIXES = (".mxl", ".musicxml", ".xml")
# Maestro-style music fonts map notation to these Latin-1/MacRoman glyphs.
MUSIC_GLYPHS = set("œ˙∑ŒÓ")
MUSIC_FONT_HINTS = (
    "maestro", "opus", "bravura", "petrucci", "sonata", "emmentaler", "finale", "sibelius",
    "musejazz", "musicalsymbols", "leland", "november", "engraver",
)
SAMPLE_PAGES = 5
# Corpus pairs whose MusicXML was exported from a different version of the score than the PDF
# (bar counts or notes differ by hand inspection); scoring them measures the mismatch, not the scan.
STALE_TRUTH = frozenset({
    "bread of heaven", "rhythm exercises", "imagine", "handel - eternal source of light divine",
    "happy birthday - brass",
})


@dataclass(frozen=True)
class Pair:
    pdf: Path
    truth: Path
    stem: str
    kind: str  # one of CLASSES


@dataclass
class Result:
    pair: Pair
    status: str = "ok"  # ok | scan-failed | compare-failed | reconcile-failed
    error: str = ""
    raw: compare.Report | None = None
    reconciled: compare.Report | None = None
    raw_xml: Path | None = None
    reconciled_xml: Path | None = None

    @property
    def stale(self) -> bool:
        return self.pair.stem.lower() in STALE_TRUTH


def classify(pdf: Path) -> str:
    """Cheap heuristic over the first pages; no dependence on font-encoding detection."""
    music_chars = text_chars = drawings = images = 0
    try:
        with pymupdf.open(pdf) as doc:
            for page in list(doc)[:SAMPLE_PAGES]:
                if any(h in f[3].lower() for f in page.get_fonts() for h in MUSIC_FONT_HINTS):
                    return "vector-music"
                text = page.get_text()
                text_chars += len(text.strip())
                music_chars += sum(
                    1 for c in text if c in MUSIC_GLYPHS or 0xE000 <= ord(c) <= 0xF8FF or 0x1D100 <= ord(c) <= 0x1D1FF
                )
                drawings += len(page.get_drawings())
                images += len(page.get_images())
    except Exception:
        return "other"
    if music_chars >= 20:
        return "vector-music"
    if drawings >= 500:
        return "outlined"
    if images and drawings < 50 and text_chars < 200:
        return "scan"
    return "other"


def discover(root: Path) -> list[Pair]:
    """PDFs with a same-stem MusicXML sibling, sorted by path; .git folders are skipped."""
    pairs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        by_name = {n.lower(): n for n in filenames}
        for name in sorted(filenames):
            if not name.lower().endswith(".pdf"):
                continue
            stem = name[:-4]
            truth = next((by_name[stem.lower() + s] for s in TRUTH_SUFFIXES if stem.lower() + s in by_name), None)
            if truth is None:
                continue
            pdf = Path(dirpath) / name
            pairs.append(Pair(pdf, Path(dirpath) / truth, stem, classify(pdf)))
    return sorted(pairs, key=lambda p: str(p.pdf))


# Writes its result to the third argument; the return value (a report) is not used here.
Reconcile = Callable[[Path, Path, Path], object]


def run(pairs: list[Pair], out_root: Path, reconcile: Reconcile | None = None) -> list[Result]:
    """Scan each PDF once (cached under <out_root>/eval/<stem>/), score it, write report.md and report.csv."""
    eval_dir = out_root / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    stems = [p.stem for p in pairs]
    results = []
    for i, pair in enumerate(pairs, start=1):
        # Same stem in two folders would share a cache; disambiguate by source path.
        name = pair.stem
        if stems.count(name) > 1:
            name += "-" + hashlib.sha1(str(pair.pdf).encode()).hexdigest()[:6]
        print(f"[{i}/{len(pairs)}] {pair.stem} ({pair.kind})", flush=True)
        results.append(evaluate_one(pair, eval_dir / name, reconcile))
        print(f"  {results[-1].status}{': ' + results[-1].error if results[-1].error else ''}", flush=True)
    write_reports(results, eval_dir)
    return results


def evaluate_one(pair: Pair, work: Path, reconcile: Reconcile | None) -> Result:
    result = Result(pair)
    try:
        result.raw_xml = cached_scan(pair.pdf, work)
    except Exception as e:
        result.status, result.error = "scan-failed", one_line(e)
        return result
    try:
        result.raw = compare.compare(pair.truth, result.raw_xml)
    except Exception as e:
        result.status, result.error = "compare-failed", one_line(e)
        return result
    if reconcile is not None:
        out = work / "reconciled.musicxml"
        try:
            reconcile(pair.pdf, result.raw_xml, out)
            result.reconciled_xml = out
            result.reconciled = compare.compare(pair.truth, result.reconciled_xml)
        except Exception as e:
            result.status, result.error = "reconcile-failed", one_line(e)
            traceback.print_exc()
    return result


def cached_scan(pdf: Path, work: Path) -> Path:
    """First exported score; a recorded failure is replayed rather than re-running Audiveris."""
    failure = work / "failed.txt"
    if failure.exists():
        raise RuntimeError(failure.read_text(encoding="utf-8").strip())
    exported = sorted((work / "audiveris").rglob("*.mxl")) if (work / "audiveris").exists() else []
    if not exported:
        try:
            exported = audiveris.scan(pdf, work)
        except Exception as e:
            work.mkdir(parents=True, exist_ok=True)
            failure.write_text(" ".join(str(e).split()), encoding="utf-8")
            raise
    return exported[0]


def one_line(e: Exception) -> str:
    return " ".join(f"{type(e).__name__}: {e}".split())


@dataclass
class Totals:
    files: int = 0
    truth_events: int = 0
    note: float = 0.0  # sum of accuracy * truth events
    pitch: float = 0.0
    marks: dict[str, compare.CategoryScore] = field(
        default_factory=lambda: {c: compare.CategoryScore(c) for c in compare.CATEGORIES}
    )

    def add(self, report: compare.Report) -> None:
        events = sum(p.truth_events for p in report.parts)
        self.files += 1
        self.truth_events += events
        self.note += report.note_accuracy * events
        self.pitch += report.pitch_accuracy * events
        for c, m in report.markings.items():
            t = self.marks[c]
            t.truth += m.truth
            t.found += m.found
            t.matched += m.matched

    @property
    def note_accuracy(self) -> float:
        return self.note / self.truth_events if self.truth_events else 0.0

    @property
    def pitch_accuracy(self) -> float:
        return self.pitch / self.truth_events if self.truth_events else 0.0


def totals_by_class(results: list[Result], variant: str) -> dict[str, Totals]:
    out: dict[str, Totals] = {c: Totals() for c in CLASSES}
    out["all"] = Totals()
    for r in results:
        report = getattr(r, variant)
        if report is None or r.stale:
            continue
        out[r.pair.kind].add(report)
        out["all"].add(report)
    return {k: v for k, v in out.items() if v.files}


def pct(x: float) -> str:
    return f"{x:.1%}"


def write_reports(results: list[Result], eval_dir: Path) -> None:
    variants = ["raw"] + (["reconciled"] if any(r.reconciled for r in results) else [])
    write_csv(results, variants, eval_dir / "report.csv")
    write_md(results, variants, eval_dir / "report.md")


def write_csv(results: list[Result], variants: list[str], path: Path) -> None:
    header = ["file", "class", "status", "stale", "variant", "truth_events", "note_acc", "pitch_acc"]
    for c in compare.CATEGORIES:
        header += [f"{c}_truth", f"{c}_found", f"{c}_matched"]
    header.append("error")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        for r in results:
            emitted = False
            for v in variants:
                report = getattr(r, v)
                if report is None:
                    continue
                emitted = True
                row = [r.pair.pdf.name, r.pair.kind, r.status, int(r.stale), v, sum(p.truth_events for p in report.parts)]
                row += [f"{report.note_accuracy:.4f}", f"{report.pitch_accuracy:.4f}"]
                for c in compare.CATEGORIES:
                    m = report.markings[c]
                    row += [m.truth, m.found, m.matched]
                w.writerow(row + [r.error])
            if not emitted:
                w.writerow([r.pair.pdf.name, r.pair.kind, r.status, int(r.stale), "", ""] + [""] * (2 + 3 * len(compare.CATEGORIES)) + [r.error])


def write_md(results: list[Result], variants: list[str], path: Path) -> None:
    stale = [r for r in results if r.stale]
    lines = ["# Evaluation report", "", f"{len(results)} file(s); " + ", ".join(
        f"{n} {k}" for k, n in sorted(count_by(results, lambda r: r.pair.kind).items())
    ) + (f"; totals leave out {len(stale)} with stale truth and any that failed" if stale else ""), ""]
    for v in variants:
        lines += [f"## Totals ({v})", ""]
        totals = totals_by_class(results, v)
        lines += ["| class | files | note acc | pitch acc |", "|---|---:|---:|---:|"]
        for k, t in totals.items():
            lines.append(f"| {k} | {t.files} | {pct(t.note_accuracy)} | {pct(t.pitch_accuracy)} |")
        lines += ["", "| class | marking | truth | found | precision | recall |", "|---|---|---:|---:|---:|---:|"]
        for k, t in totals.items():
            for m in t.marks.values():
                if m.truth or m.found:
                    lines.append(f"| {k} | {m.category} | {m.truth} | {m.found} | {pct(m.precision)} | {pct(m.recall)} |")
        lines.append("")
    gains = "reconciled" in variants
    if gains:
        scored = [r for r in results if r.raw and r.reconciled and not r.stale]
        down = sorted((r for r in scored if r.reconciled.note_accuracy < r.raw.note_accuracy),
                      key=lambda r: r.reconciled.note_accuracy - r.raw.note_accuracy)
        lines += ["## Regressions (note accuracy below raw)", ""]
        lines += [f"- {r.pair.pdf.name}: {pct(r.raw.note_accuracy)} to {pct(r.reconciled.note_accuracy)}"
                  for r in down] or ["None."]
        lines.append("")
    lines += ["## Per file", ""]
    head = "| file | class | status | " + " | ".join(f"{v} note | {v} pitch" for v in variants)
    head += " | note gain |" if gains else " |"
    lines += [head, "|" + "---|" * (3 + 2 * len(variants) + gains)]
    for r in results:
        cells = [r.pair.pdf.name.replace("|", "/"), r.pair.kind, r.status + (" (stale truth)" if r.stale else "")]
        for v in variants:
            report = getattr(r, v)
            cells += [pct(report.note_accuracy), pct(report.pitch_accuracy)] if report else ["", ""]
        if gains:
            cells.append(f"{r.reconciled.note_accuracy - r.raw.note_accuracy:+.1%}" if r.raw and r.reconciled else "")
        lines.append("| " + " | ".join(cells) + " |")
    if stale:
        lines += ["", "## Stale truth", "",
                  "Left out of the totals and the regression list: the MusicXML does not match the PDF.", ""]
        lines += [f"- {r.pair.pdf.name}" for r in stale]
    failed = [r for r in results if r.error]
    if failed:
        lines += ["", "## Failures", ""]
        lines += [f"- {r.pair.pdf.name}: {r.status}: {r.error}" for r in failed]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def count_by(items: list, key) -> dict:
    out: dict = defaultdict(int)
    for i in items:
        out[key(i)] += 1
    return out
