#!/usr/bin/env bash
# Regenerate every Sparky raster from the source SVGs. Requires inkscape.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 tools/sparky_assets.py
.venv/bin/python tools/render_screenshot.py

png() { inkscape "$1" --export-type=png --export-filename="$2" -w "$3" >/dev/null 2>&1; }

# app icon at the three manifest sizes
for s in 16 48 128; do png extension/icons/icon.svg "extension/icons/icon${s}.png" "$s"; done
# README banner, light and dark: tools/banner.html drawn by headless Chrome with
# the site's own stylesheet, at 2x (the fonts come from Google Fonts, so it needs
# network). The light one doubles as the site's social preview.
banner() {
  "${CHROME:-google-chrome}" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --default-background-color=00000000 \
    --virtual-time-budget=6000 --window-size=1200,460 \
    --screenshot="$PWD/$2" "file://$PWD/tools/banner.html$1" >/dev/null 2>&1
}
banner ""      docs/logo.png
banner "?dark" docs/logo-dark.png
cp docs/logo.png site/assets/logo.png

# the terminal screenshots (docs/extension.png is a capture of the real panel:
# node tools/render_extension_shot.mjs)
png docs/screenshot.svg docs/screenshot.png 820
png docs/screenshot-points.svg docs/screenshot-points.png 820

echo "Sparky rasters regenerated."
