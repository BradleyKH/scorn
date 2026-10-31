import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCAL_EXE = REPO_ROOT / "tools" / "audiveris" / "Audiveris" / "Audiveris.exe"


def find_executable() -> Path:
    if env := os.environ.get("AUDIVERIS_EXE"):
        return Path(env)
    if LOCAL_EXE.exists():
        return LOCAL_EXE
    if found := shutil.which("audiveris") or shutil.which("Audiveris"):
        return Path(found)
    raise FileNotFoundError(
        "Audiveris not found. Run scripts/setup-audiveris.sh or set AUDIVERIS_EXE."
    )


def scan(pdf: Path, out_dir: Path) -> list[Path]:
    """Transcribe a whole PDF in one pass; Audiveris reads PDFs natively and joins pages itself."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "audiveris"
    cmd = [str(find_executable()), "-batch", "-transcribe", "-export", "-output", str(work), str(pdf)]
    log = out_dir / "audiveris.log"
    with log.open("w", encoding="utf-8") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"Audiveris exited with {result.returncode}; see {log}")

    # A book with several movements exports one .mvtN.mxl per movement.
    exported = sorted(work.rglob("*.mxl"))
    if not exported:
        raise RuntimeError(f"Audiveris exported no MusicXML; see {log}")
    return exported
