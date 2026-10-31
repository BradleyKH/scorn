from pathlib import Path

import pytest

from scorn.engines import audiveris

HEADER = (
    "INFO  []                      Main 194  | Submitting 1 task(s) in sequence:\n"
    '    Input "C:\\examples\\Score.pdf"\n'
    "INFO  [Score]                      Book 560  | {total} in C:\\examples\\Score.pdf\n"
    "INFO  []                      Book 2565 | Stored /book.xml\n"
)


def sheet_done(n: int) -> str:
    return (
        f"INFO  [Score#{n}]                DataHolder 348  | Stored /sheet#{n}/BINARY.png\n"
        f"INFO  [Score#{n}]                     Sheet 1583 | Stored /sheet#{n}/sheet#{n}.xml\n"
        f"INFO  [Score#{n}]                 SheetStub 1655 | Sheet disposed\n"
        f"INFO  [Score#{n}]                      Book 2050 | End of Stub#{n}\n"
    )


def write_log(tmp_path: Path, text: str) -> Path:
    log = tmp_path / "audiveris.log"
    log.write_text(text, encoding="utf-8")
    return log


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No env var, no tools/ copy, empty PATH, and a fake install prefix under tmp_path."""
    monkeypatch.delenv("AUDIVERIS_EXE", raising=False)
    monkeypatch.setattr(audiveris, "LOCAL_EXE", tmp_path / "no-tools" / "Audiveris.exe")
    monkeypatch.setattr(audiveris.shutil, "which", lambda name: None)
    prefix = tmp_path / "install" / "python"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(audiveris.sys, "base_prefix", str(prefix))
    return tmp_path


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_env_var_wins(isolated, monkeypatch):
    touch(isolated / "install" / "audiveris" / "Audiveris.exe")
    monkeypatch.setenv("AUDIVERIS_EXE", "C:/custom/Audiveris.exe")
    assert audiveris.find_executable() == Path("C:/custom/Audiveris.exe")


@pytest.mark.parametrize("relative", ["Audiveris.exe", "Audiveris/Audiveris.exe"])
def test_bundle_found_in_both_layouts(isolated, relative):
    exe = touch(isolated / "install" / "audiveris" / relative)
    assert audiveris.find_executable() == exe


def test_bundle_beats_tools_copy(isolated, monkeypatch):
    tools = touch(isolated / "tools" / "Audiveris.exe")
    monkeypatch.setattr(audiveris, "LOCAL_EXE", tools)
    bundled = touch(isolated / "install" / "audiveris" / "Audiveris.exe")
    assert audiveris.find_executable() == bundled


def test_falls_through_to_tools_copy(isolated, monkeypatch):
    tools = touch(isolated / "tools" / "Audiveris.exe")
    monkeypatch.setattr(audiveris, "LOCAL_EXE", tools)
    assert audiveris.find_executable() == tools


def test_falls_through_to_path(isolated, monkeypatch):
    monkeypatch.setattr(audiveris.shutil, "which", lambda name: "C:/bin/audiveris.exe")
    assert audiveris.find_executable() == Path("C:/bin/audiveris.exe")


def test_not_found_mentions_bundle(isolated):
    with pytest.raises(FileNotFoundError, match="audiveris folder"):
        audiveris.find_executable()


def test_progress_missing_file(tmp_path):
    assert audiveris.progress(tmp_path / "nope.log") is None


def test_progress_before_total_known(tmp_path):
    log = write_log(tmp_path, "INFO  []                      Main 259  | Running in batch mode\n")
    assert audiveris.progress(log) is None


def test_progress_single_sheet_wording(tmp_path):
    log = write_log(tmp_path, HEADER.format(total="1 sheet"))
    assert audiveris.progress(log) == (0, 1)


def test_progress_partway(tmp_path):
    log = write_log(tmp_path, HEADER.format(total="19 sheets") + sheet_done(1) + sheet_done(2) + sheet_done(3))
    assert audiveris.progress(log) == (3, 19)


def test_progress_complete(tmp_path):
    body = "".join(sheet_done(n) for n in range(1, 20))
    tail = "INFO  [Score]           PartwiseBuilder 2707 | Exporting sheet(s): [#1#2#3]\n"
    log = write_log(tmp_path, HEADER.format(total="19 sheets") + body + tail)
    assert audiveris.progress(log) == (19, 19)


def test_progress_counts_each_sheet_once_and_caps(tmp_path):
    log = write_log(tmp_path, HEADER.format(total="2 sheets") + sheet_done(1) + sheet_done(1) + sheet_done(2) + sheet_done(3))
    assert audiveris.progress(log) == (2, 2)


def test_progress_ignores_partial_last_line(tmp_path):
    # Cut mid-write: "End of Stub#1" could be the start of "Stub#12".
    text = HEADER.format(total="19 sheets") + sheet_done(1) + "INFO  [Score#2]                      Book 2050 | End of Stub#2"
    assert audiveris.progress(write_log(tmp_path, text)) == (1, 19)


def test_progress_tolerates_odd_bytes(tmp_path):
    log = tmp_path / "audiveris.log"
    log.write_bytes(HEADER.format(total="1 sheet").encode() + b"\xff\xfe junk\n" + sheet_done(1).encode())
    assert audiveris.progress(log) == (1, 1)
