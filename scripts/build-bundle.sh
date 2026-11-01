#!/usr/bin/env bash
# Assemble the self-contained Windows folder the installer ships: embedded CPython with Scorn and
# its core dependencies, Audiveris unpacked from its MSI, third-party licenses and the app icon.
#
# Usage: scripts/build-bundle.sh [--no-smoke]
# Set SCORN_UV_OFFLINE=1 to keep uv offline (cache only). Every locked package must then already
# be in uv's cache; one online run fills it.
set -euo pipefail

PBS_RELEASE="20261026"
PYTHON_VERSION="3.12.16"
PYTHON_ASSET="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PYTHON_VERSION}%2B${PBS_RELEASE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
PYTHON_SHA256="1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149"
AUDIVERIS_VERSION="5.11.0"
AUDIVERIS_ASSET="Audiveris-${AUDIVERIS_VERSION}-windowsConsole-x86_64.msi"
AUDIVERIS_URL="https://github.com/Audiveris/audiveris/releases/download/${AUDIVERIS_VERSION}/${AUDIVERIS_ASSET}"
AUDIVERIS_SHA256="5f1b4e96a12c53c7da426814b76e599363c4181e291855996e0a6878dda95f71"

SMOKE=1
for arg in "$@"; do
  case "$arg" in
    --no-smoke) SMOKE=0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL="$ROOT/tools/dl"
BUNDLE="$ROOT/build/bundle"
PY="$BUNDLE/python/python.exe"
UV_FLAGS=()
[[ "${SCORN_UV_OFFLINE:-}" == "1" ]] && UV_FLAGS+=(--offline)

# Downloads to the cache on a miss, then checks the pinned hash on every run, cached or not.
fetch() {
  local url="$1" file="$2" sha="$3"
  mkdir -p "$DL"
  if [[ -f "$DL/$file" ]]; then
    echo "Using cached $file"
  else
    echo "Downloading $file"
    curl -fL --retry 3 -o "$DL/$file.part" "$url"
    mv "$DL/$file.part" "$DL/$file"
  fi
  local actual
  actual="$(sha256sum "$DL/$file" | cut -d' ' -f1)"
  if [[ "$actual" != "$sha" ]]; then
    echo "ERROR: checksum mismatch for $DL/$file" >&2
    echo "  expected $sha" >&2
    echo "  actual   $actual" >&2
    echo "Delete the file to download it again, or update the pin if the release changed." >&2
    exit 1
  fi
  echo "Checksum OK: $file"
}

fetch "$PYTHON_URL" "$PYTHON_ASSET" "$PYTHON_SHA256"
fetch "$AUDIVERIS_URL" "$AUDIVERIS_ASSET" "$AUDIVERIS_SHA256"

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"

echo "Extracting CPython $PYTHON_VERSION"
tar -xzf - -C "$BUNDLE" <"$DL/$PYTHON_ASSET"
"$PY" -I -c "import sys; assert sys.version.startswith('$PYTHON_VERSION'), sys.version"

echo "Building the scorn wheel and installing core dependencies"
cd "$ROOT"
VERSION="$("$PY" -I -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
WHEEL="$ROOT/dist/scorn-$VERSION-py3-none-any.whl"
uv build "${UV_FLAGS[@]}" --wheel --out-dir "$ROOT/dist"
REQS="$ROOT/build/bundle-requirements.txt"
uv export "${UV_FLAGS[@]}" --frozen --no-default-groups --no-hashes --no-emit-project \
  --format requirements-txt -o "$REQS" >/dev/null
uv pip install "${UV_FLAGS[@]}" --python "$PY" -r "$REQS" "$WHEEL"

"$PY" -I - <<'EOF'
import importlib.util, sys
present = [m for m in ("homr", "onnxruntime", "music21") if importlib.util.find_spec(m)]
missing = [m for m in ("pymupdf", "verovio", "fastapi", "uvicorn", "scorn") if not importlib.util.find_spec(m)]
if present or missing:
    sys.exit(f"bundle contents wrong: unexpected {present}, missing {missing}")
print("Bundle imports OK")
EOF

# Console-script launchers (scorn.exe and those of dependencies) embed this build machine's
# python.exe path, so they break once installed; the shortcut runs pythonw -m scorn.app instead.
for exe in "$BUNDLE"/python/Scripts/*.exe; do
  if grep -qaiF "$(cygpath -w "$PY")" "$exe"; then
    rm -f "$exe"
  fi
done

echo "Removing test suites from installed packages"
find "$BUNDLE/python/Lib/site-packages" -type d -name tests -prune -exec rm -rf {} +

echo "Precompiling bytecode"
find "$BUNDLE/python" -type d -name __pycache__ -prune -exec rm -rf {} +
"$PY" -I -m compileall -q -j 0 "$BUNDLE/python/Lib" >/dev/null || {
  echo "ERROR: compileall failed" >&2
  exit 1
}

echo "Extracting Audiveris $AUDIVERIS_VERSION"
# An administrative install only unpacks the MSI into TARGETDIR; nothing is registered.
MSYS_NO_PATHCONV=1 msiexec /a "$(cygpath -w "$DL/$AUDIVERIS_ASSET")" /qn \
  TARGETDIR="$(cygpath -w "$BUNDLE/audiveris")"
rm -f "$BUNDLE"/audiveris/*.msi
[[ -x "$BUNDLE/audiveris/Audiveris/Audiveris.exe" ]] || {
  echo "ERROR: Audiveris.exe missing after extraction" >&2
  exit 1
}

echo "Collecting licenses"
LIC="$BUNDLE/licenses"
mkdir -p "$LIC"
cp "$ROOT/LICENSE" "$LIC/scorn-LICENSE.txt"
cp "$BUNDLE/python/LICENSE.txt" "$LIC/python-LICENSE.txt"
# Audiveris is AGPL v3 like Scorn, and its MSI carries no license file of its own.
AUD_LICENSE="$(find "$BUNDLE/audiveris" -path '*/runtime/legal' -prune -o -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' \) -print | head -n 1)"
cp "${AUD_LICENSE:-$ROOT/LICENSE}" "$LIC/audiveris-LICENSE.txt"
cp "$ROOT/packaging/licenses/SOURCE.txt" "$LIC/SOURCE.txt"
"$PY" -I - "$LIC" <<'EOF'
import re, shutil, sys
from importlib.metadata import distributions
from pathlib import Path

out = Path(sys.argv[1])
pattern = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
for dist in distributions():
    name = dist.metadata["Name"]
    for f in dist.files or []:
        parts = f.parts
        if not parts[0].endswith(".dist-info") or not pattern.search(f.name):
            continue
        rel = parts[2:] if len(parts) > 2 and parts[1] == "licenses" else parts[1:]
        target = out / f"{name}-{'_'.join(rel)}"
        shutil.copyfile(f.locate(), target)
# Skips empty shim packages (pymupdf4llm and pymupdf_layout ship only metadata).
missing = [d.metadata["Name"] for d in distributions()
           if any(not f.parts[0].endswith(".dist-info") for f in d.files or [])
           and not any(p.name.startswith(d.metadata["Name"] + "-") for p in out.iterdir())]
if missing:
    print(f"WARNING: no license file found for {', '.join(missing)}", file=sys.stderr)
EOF

cp "$ROOT/src/scorn/static/scorn.ico" "$BUNDLE/scorn.ico"

echo
echo "Bundle size:"
du -sh "$BUNDLE"
du -sh "$BUNDLE"/*

if [[ "$SMOKE" == "1" ]]; then
  bash "$ROOT/scripts/smoke-bundle.sh" "$BUNDLE"
fi
