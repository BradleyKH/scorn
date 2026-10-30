from pathlib import Path

import pytest

import music_scan
from music_scan import rhythm

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_package_imports():
    assert music_scan.__name__ == "music_scan"


def test_clean_fixture_has_no_bar_errors():
    truth = FIXTURES / "melody.truth.musicxml"
    if not truth.exists():
        pytest.skip("run `uv run --offline music-scan fixtures` first")
    assert rhythm.check(truth) == []


def test_scores_dir_is_a_folder(scores_dir):
    assert scores_dir.is_dir()
