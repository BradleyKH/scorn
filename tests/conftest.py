import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def scores_dir() -> Path:
    path = Path(os.environ.get("SCORN_SCORES", Path.home() / "Dropbox" / "Scores"))
    if not path.is_dir():
        pytest.skip(f"score folder not found (set SCORN_SCORES): {path}")
    return path
