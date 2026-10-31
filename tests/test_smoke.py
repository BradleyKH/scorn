from pathlib import Path

import pytest

import scorn
from scorn import rhythm

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_package_imports():
    assert scorn.__name__ == "scorn"


def test_clean_fixture_has_no_bar_errors():
    truth = FIXTURES / "melody.truth.musicxml"
    if not truth.exists():
        pytest.skip("run `uv run --offline scorn fixtures` first")
    assert rhythm.check(truth) == []


def test_scores_dir_is_a_folder(scores_dir):
    assert scores_dir.is_dir()
