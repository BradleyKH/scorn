import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The Stopford score is copyrighted, so it and its scan outputs live in the gitignored notes/ folder.
STOPFORD = ROOT / "notes" / "stopford"


@pytest.fixture(scope="session")
def stopford_pdf() -> Path:
    path = STOPFORD / "Christ the Lord is risen again (Stopford) - full score.pdf"
    if not path.exists():
        pytest.skip(f"missing example score: {path}")
    return path


@pytest.fixture(scope="session")
def stopford_truth() -> Path:
    path = (
        STOPFORD
        / "out"
        / "Christ the Lord is risen again (Stopford) - full score"
        / "audiveris"
        / "Christ the Lord is risen again (Stopford) - full score.fixed.musicxml"
    )
    if not path.exists():
        pytest.skip(f"missing Audiveris output (run a scan first): {path}")
    return path


@pytest.fixture(scope="session")
def scores_dir() -> Path:
    path = Path(os.environ.get("MUSIC_SCAN_SCORES", Path.home() / "Dropbox" / "Scores"))
    if not path.is_dir():
        pytest.skip(f"score folder not found (set MUSIC_SCAN_SCORES): {path}")
    return path


@pytest.fixture(scope="session")
def stopford_raw() -> Path:
    path = (
        STOPFORD
        / "out"
        / "Christ the Lord is risen again (Stopford) - full score"
        / "audiveris"
        / "audiveris"
        / "Christ the Lord is risen again (Stopford) - full score.mxl"
    )
    if not path.exists():
        pytest.skip(f"missing Audiveris output (run a scan first): {path}")
    return path


@pytest.fixture(scope="session")
def stopford_score(stopford_pdf):
    from music_scan.reconcile import pdf_score

    return pdf_score.read_pdf(stopford_pdf)


# Function-scoped: passes mutate the tree, so every test needs its own copy.
@pytest.fixture
def stopford_raw_tree(stopford_raw):
    from music_scan import rhythm

    return rhythm.load(stopford_raw)


@pytest.fixture
def stopford_truth_tree(stopford_truth):
    from music_scan import rhythm

    return rhythm.load(stopford_truth)
