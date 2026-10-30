#!/usr/bin/env bash
# Extract Audiveris (with its bundled Java runtime) into tools/audiveris without a system install.
set -euo pipefail

VERSION="${1:-5.11.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MSI="$ROOT/tools/dl/Audiveris-$VERSION-windowsConsole-x86_64.msi"

mkdir -p "$ROOT/tools/dl"
if [[ ! -f "$MSI" ]]; then
  curl -fL -o "$MSI" \
    "https://github.com/Audiveris/audiveris/releases/download/$VERSION/Audiveris-$VERSION-windowsConsole-x86_64.msi"
fi

# An administrative install only unpacks the MSI into TARGETDIR; nothing is registered.
MSYS_NO_PATHCONV=1 msiexec /a "$(cygpath -w "$MSI")" /qn TARGETDIR="$(cygpath -w "$ROOT/tools/audiveris")"
"$ROOT/tools/audiveris/Audiveris/Audiveris.exe" -batch -version
