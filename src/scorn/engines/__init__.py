import importlib
import importlib.util
from collections.abc import Callable
from pathlib import Path

ENGINE_NAMES = ("audiveris", "homr")

# Engine modules are imported on demand so the core build never loads homr or onnxruntime.
_REQUIRES = {"homr": "homr"}


def available() -> list[str]:
    return [
        name
        for name in ENGINE_NAMES
        if name not in _REQUIRES or importlib.util.find_spec(_REQUIRES[name]) is not None
    ]


def get_engine(name: str) -> Callable[[Path, Path], list[Path]]:
    if name not in ENGINE_NAMES:
        raise ValueError(f"unknown engine {name!r}")
    if name not in available():
        raise RuntimeError(f"{name} isn't installed in this build of scorn")
    return importlib.import_module(f"scorn.engines.{name}").scan
