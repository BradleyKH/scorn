#!/usr/bin/env bash
# Prove a built bundle works on its own: no repo tools, no user site-packages, almost nothing on PATH.
#
# Usage: scripts/smoke-bundle.sh <bundle dir>
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 <bundle dir>" >&2; exit 2; }
BUNDLE="$(cd "$1" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$BUNDLE/python/python.exe"
PYW="$BUNDLE/python/pythonw.exe"
PDF="$ROOT/examples/A Tune By Mozart Horn (F).pdf"

TMP="$(mktemp -d)"
APP_PID=""
cleanup() {
  if [[ -n "$APP_PID" ]]; then
    taskkill //PID "$APP_PID" //F >/dev/null 2>&1 || true
    wait 2>/dev/null || true
  fi
  cd /
  rm -rf "$TMP"
}
trap cleanup EXIT

SYS32="$(cygpath -u "${SYSTEMROOT:-C:\\Windows}")/System32"
unset AUDIVERIS_EXE
export PYTHONNOUSERSITE=1
cd "$TMP"

# Runs the bundled interpreter with only System32 on PATH; the shell keeps its own PATH for curl.
bpy() { PATH="$SYS32" "$PY" "$@"; }
# Windows Python ends lines with CRLF, which would leave a stray carriage return in captured values.
bval() { bpy "$@" | tr -d '\r'; }
scorn_cli() { bpy -m scorn "$@"; }

echo "Smoke test: $BUNDLE"

EXPECTED="$(bval -c "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['project']['version'])" "$(cygpath -w "$ROOT/pyproject.toml")")"
ACTUAL="$(bval -c "import scorn; print(scorn.__version__)")"
[[ "$ACTUAL" == "$EXPECTED" ]] || { echo "FAIL: scorn.__version__ is $ACTUAL, expected $EXPECTED" >&2; exit 1; }
echo "ok: scorn $ACTUAL"

EXE="$(bval -c "from scorn.engines.audiveris import find_executable; print(find_executable().resolve())")"
BUNDLE_WIN="$(cygpath -w "$BUNDLE")"
case "${EXE,,}" in
  "${BUNDLE_WIN,,}\\"*) echo "ok: Audiveris at $EXE" ;;
  *) echo "FAIL: Audiveris resolved outside the bundle: $EXE" >&2; exit 1 ;;
esac

scorn_cli scan "$(cygpath -w "$PDF")" --engine audiveris --out "$(cygpath -w "$TMP/out")"
FIXED="$(find "$TMP/out" -name '*.reconciled.musicxml' | head -n 1)"
[[ -n "$FIXED" ]] || { echo "FAIL: scan produced no .reconciled.musicxml" >&2; exit 1; }
echo "ok: scan wrote $(basename "$FIXED")"
scorn_cli check "$(cygpath -w "$FIXED")"
echo "ok: rhythm check passed"

mkdir -p "$TMP/state" "$TMP/publish"
PATH="$SYS32" "$PYW" -m scorn.app --no-browser --state-dir "$(cygpath -w "$TMP/state")" \
  --publish-dir "$(cygpath -w "$TMP/publish")" --idle-seconds 30 &
LOCK="$TMP/state/app.json"
for _ in $(seq 60); do
  [[ -s "$LOCK" ]] && break
  sleep 0.5
done
[[ -s "$LOCK" ]] || { echo "FAIL: scorn.app wrote no $LOCK" >&2; exit 1; }
read -r APP_PID PORT < <(bval -c "import json, sys; d = json.load(open(sys.argv[1])); print(d['pid'], d['port'])" "$(cygpath -w "$LOCK")")
BODY=""
for _ in $(seq 30); do
  BODY="$(curl -fsS "http://127.0.0.1:$PORT/api/version" 2>/dev/null)" && break
  sleep 0.5
done
[[ -n "$BODY" ]] || { echo "FAIL: /api/version did not answer on port $PORT" >&2; exit 1; }
echo "ok: /api/version -> $BODY"

echo "Smoke test passed"
