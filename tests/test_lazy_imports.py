import subprocess
import sys

OPTIONAL = ("music21", "homr", "onnxruntime")

# A None entry in sys.modules makes `import` fail and find_spec return None, as in the
# core build. pymupdf imports onnxruntime whenever it's installed, so checking
# sys.modules alone would fail in the dev venv.
SCRIPT = f"""
import sys

OPTIONAL = {OPTIONAL!r}
for name in OPTIONAL:
    sys.modules[name] = None

import scorn.cli, scorn.engines, scorn.reconcile, scorn.web

scorn.cli.build_parser()
assert scorn.engines.available() == ["audiveris"], scorn.engines.available()
try:
    scorn.engines.get_engine("homr")
except RuntimeError:
    pass
else:
    raise AssertionError("get_engine('homr') should fail without homr")
print(",".join(m for m in OPTIONAL if sys.modules.get(m) is not None))
"""


def test_core_modules_skip_optional_dependencies():
    # A fresh interpreter, since this pytest process may already have imported them.
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
