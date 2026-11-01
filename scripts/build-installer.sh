#!/usr/bin/env bash
# Compile the Inno Setup installer around build/bundle/ into dist/Scorn-Setup-<version>.exe.
#
# Usage: scripts/build-installer.sh [--skip-bundle] [--no-smoke]
# Without --skip-bundle it runs scripts/build-bundle.sh first (--no-smoke is forwarded to it).
set -euo pipefail

SKIP_BUNDLE=0
BUNDLE_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --skip-bundle) SKIP_BUNDLE=1 ;;
    --no-smoke) BUNDLE_ARGS+=(--no-smoke) ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="$ROOT/build/bundle"
DIST="$ROOT/dist"

VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[[ -n "$VERSION" ]] || { echo "ERROR: no version found in pyproject.toml" >&2; exit 1; }

if [[ "$SKIP_BUNDLE" == "0" ]]; then
  bash "$ROOT/scripts/build-bundle.sh" "${BUNDLE_ARGS[@]}"
fi
[[ -x "$BUNDLE/python/python.exe" ]] || {
  echo "ERROR: $BUNDLE is missing or incomplete; run scripts/build-bundle.sh first" >&2
  exit 1
}

ISCC=""
if command -v iscc >/dev/null 2>&1; then
  ISCC="$(command -v iscc)"
else
  for candidate in \
    "${LOCALAPPDATA:-}/Programs/Inno Setup 6/ISCC.exe" \
    "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"; do
    if [[ -f "$candidate" ]]; then ISCC="$candidate"; break; fi
  done
fi
[[ -n "$ISCC" ]] || { echo "ERROR: ISCC.exe not found; install Inno Setup 6" >&2; exit 1; }

MAX_REL_PATH="$(cd "$BUNDLE" && find . -type f | awk '{ if (length($0) - 2 > n) n = length($0) - 2 } END { print n }')"
echo "Longest path inside the bundle: $MAX_REL_PATH characters"

mkdir -p "$DIST"
# MSYS_NO_PATHCONV keeps Git Bash from rewriting the /D switches as paths.
MSYS_NO_PATHCONV=1 "$ISCC" /Q \
  "/DAppVersion=$VERSION" \
  "/DMaxRelPath=$MAX_REL_PATH" \
  "/DBundleDir=$(cygpath -w "$BUNDLE")" \
  "/DOutputDir=$(cygpath -w "$DIST")" \
  "$(cygpath -w "$ROOT/packaging/scorn.iss")"

INSTALLER="$DIST/Scorn-Setup-$VERSION.exe"
[[ -f "$INSTALLER" ]] || { echo "ERROR: $INSTALLER was not produced" >&2; exit 1; }
echo
echo "Installer: $INSTALLER"
du -h "$INSTALLER" | cut -f1
