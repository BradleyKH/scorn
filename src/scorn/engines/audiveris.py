import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from scorn import procs

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCAL_EXE = REPO_ROOT / "tools" / "audiveris" / "Audiveris" / "Audiveris.exe"


def _bundled_exe() -> Path | None:
    """Audiveris unpacked beside the installer's python folder; the MSI extract may or may not nest an Audiveris/ folder."""
    root = Path(sys.base_prefix).parent / "audiveris"
    for candidate in (root / "Audiveris.exe", root / "Audiveris" / "Audiveris.exe"):
        if candidate.exists():
            return candidate
    return None


def find_executable() -> Path:
    if env := os.environ.get("AUDIVERIS_EXE"):
        return Path(env)
    if bundled := _bundled_exe():
        return bundled
    if LOCAL_EXE.exists():
        return LOCAL_EXE
    if found := shutil.which("audiveris") or shutil.which("Audiveris"):
        return Path(found)
    raise FileNotFoundError(
        "Audiveris not found. Install Scorn's bundled copy (an audiveris folder beside its python folder), "
        "run scripts/setup-audiveris.sh, or set AUDIVERIS_EXE."
    )


_TOTAL_RE = re.compile(r"Book \d+\s+\| (\d+) sheets? in ")
# Audiveris logs this once a sheet's pipeline has finished and been stored.
_DONE_RE = re.compile(r"Book \d+\s+\| End of Stub#(\d+)\s*$")


def progress(log: Path) -> tuple[int, int] | None:
    """Return (sheets finished, total sheets) from a possibly in-progress log, or None until the total is known."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    total = None
    done: set[int] = set()
    # A final line without a newline may be cut mid-write, so only terminated lines count.
    for line in text.rpartition("\n")[0].splitlines():
        if total is None and (m := _TOTAL_RE.search(line)):
            total = int(m.group(1))
        elif m := _DONE_RE.search(line):
            done.add(int(m.group(1)))
    if total is None:
        return None
    return min(len(done), total), total


def scan(pdf: Path, out_dir: Path) -> list[Path]:
    """Transcribe a whole PDF in one pass; Audiveris reads PDFs natively and joins pages itself."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(find_executable()), "-batch", "-transcribe", "-export", "-output", str(out_dir), str(pdf)]
    log = out_dir / "audiveris.log"
    with log.open("w", encoding="utf-8") as f:
        result = procs.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"Audiveris exited with {result.returncode}; see {log}")

    # A book with several movements exports one .mvtN.mxl per movement.
    exported = sorted(out_dir.rglob("*.mxl"))
    if not exported:
        raise RuntimeError(f"Audiveris exported no MusicXML; see {log}")
    return exported
