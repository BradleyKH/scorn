"""Simple mode: publishing finished scans to a folder, plain rhythm sentences and diagnostics zips."""

import io
import json
import platform
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import scorn
from scorn import layout, rhythm

SUFFIXES = (".mxl", ".musicxml")


def movement_name(path: Path) -> str:
    """The file name minus its final MusicXML suffix, so `<stem>.mvt2.mxl` gives `<stem>.mvt2`."""
    for suffix in SUFFIXES:
        if path.name.lower().endswith(suffix):
            return path.name[: -len(suffix)]
    return path.name


def claim_folder(publish_dir: Path, stem: str) -> Path:
    """Create a new folder named after the PDF, adding (2), (3) and so on rather than reuse one."""
    publish_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        folder = publish_dir / (stem if n == 1 else f"{stem} ({n})")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            n += 1


def write_uncompressed(source: Path, dest: Path) -> None:
    if source.suffix.lower() != ".mxl":
        shutil.copyfile(source, dest)
        return
    with zipfile.ZipFile(source) as z:
        rootfile = ET.fromstring(z.read("META-INF/container.xml")).find(".//rootfile").get("full-path")
        dest.write_bytes(z.read(rootfile))


def publish(xml: Path, report: Path | None, folder: Path, name: str) -> dict[str, str | None]:
    """Write the score with breaks, a copy without them and the report; return paths relative to folder."""
    main = folder / f"{name}.musicxml"
    no_breaks = folder / f"{name} (no page breaks).musicxml"
    write_uncompressed(xml, main)
    shutil.copyfile(main, no_breaks)
    layout.strip_breaks(no_breaks)
    out = {"musicxml": main.name, "no_breaks": no_breaks.name, "report": None}
    if report and report.is_file():
        dest = folder / f"{name}.report.json"
        shutil.copyfile(report, dest)
        out["report"] = dest.name
    return out


def _part_name(label: str) -> str:
    # rhythm labels parts as "<id> <part-name>"; the id alone when the part has no name.
    return label.split(" ", 1)[1] if " " in label else label


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _unique(labels) -> list[str]:
    return list(dict.fromkeys(_part_name(label) for label in labels))


def sentence(error: rhythm.BarError) -> str | None:
    """One plain sentence for a bar, or None when the only finding is parts padded to match."""
    clauses = []
    if error.voices:
        clauses.append(f"doesn't add up in {_join(_unique(v.part for v in error.voices))}")
    if error.missing:
        clauses.append(f"is missing in {_join(_unique(error.missing))}")
    if error.empty:
        clauses.append(f"is empty in {_join(_unique(error.empty))}")
    if not clauses:
        return None
    return f"Bar {error.measure} on page {error.page} {_join(clauses)}"


def sentences(errors: list[rhythm.BarError]) -> list[str]:
    return [s for e in errors if (s := sentence(e))]


def diagnostics(job_dir: Path, pdf: Path, summary: dict, rhythm_reports: dict[str, str], include_pdf: bool) -> bytes:
    """A zip of what's needed to look into a scan, without the user's PDF unless they allow it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        about = {
            "scorn": scorn.__version__,
            "python": sys.version,
            "platform": platform.platform(),
            "windows": " ".join(platform.win32_ver()).strip() or None,
        }
        z.writestr("about.json", json.dumps(about, indent=2))
        z.writestr("job.json", json.dumps(summary, indent=2, default=str))
        for log in job_dir.rglob("*.log"):
            z.write(log, log.relative_to(job_dir).as_posix())
        for report in job_dir.rglob("*.report.json"):
            z.write(report, report.relative_to(job_dir).as_posix())
        for name, text in rhythm_reports.items():
            z.writestr(f"rhythm/{name}.txt", text + "\n")
        if include_pdf and pdf.is_file():
            z.write(pdf, pdf.name)
    return buf.getvalue()
