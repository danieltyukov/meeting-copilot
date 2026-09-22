#!/usr/bin/env bash
# Regenerate every Sparky raster from the source SVGs. Requires inkscape.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 tools/sparky_assets.py
.venv/bin/python tools/render_screenshot.py

png() { inkscape "$1" --export-type=png --export-filename="$2" -w "$3" >/dev/null 2>&1; }

# app icon at the three manifest sizes
for s in 16 48 128; do png extension/icons/icon.svg "extension/icons/icon${s}.png" "$s"; done
# README hero + extension banner + screenshot mock
png docs/logo.svg       docs/logo.png       760
png docs/extension.svg  docs/extension.png  380
png docs/screenshot.svg docs/screenshot.png 820
png docs/screenshot-points.svg docs/screenshot-points.png 820

echo "Sparky rasters regenerated."
