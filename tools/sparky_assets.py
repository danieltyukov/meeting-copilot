"""Sparky brand-asset generator.

Single source of truth for the Sparky mascot — a one-colour yellow pixel-budgie
(black eyes only, like the Claude mascot). Emits the app icon from one ASCII map,
so every size is reproducible (the README banner is tools/banner.html):

    python tools/sparky_assets.py          # writes the icon SVG
    # then rasterise (needs inkscape):
    inkscape extension/icons/icon.svg --export-type=png \
        --export-filename=extension/icons/icon128.png -w 128

Run tools/render_sparky.sh to regenerate every PNG at once.
"""
from pathlib import Path

YELLOW = "#FFC61A"   # the one body colour
EYE = "#1A1A1A"      # eyes — the only second colour, as in the reference
CREAM = "#F4F1E8"    # icon tile background

# 16x16 single-colour budgie. 'Y' = body, 'K' = eye, '.' = empty. Budgie-ness is
# carried by the crest tuft + chunky body + two feet — no second colour needed.
SPARKY = [
    "................",
    ".......YY.......",
    "......YYYY......",
    "....YYYYYYYY....",
    "...YYYYYYYYYY...",
    "..YYYYYYYYYYYY..",
    "..YYKKYYYYKKYY..",
    "..YYKKYYYYKKYY..",
    "..YYYYYYYYYYYY..",
    "..YYYYYYYYYYYY..",
    "..YYYYYYYYYYYY..",
    "...YYYYYYYYYY...",
    "...YYYYYYYYYY...",
    "....YYYYYYYY....",
    "....YY....YY....",
    "................",
]
COLS = {"Y": YELLOW, "K": EYE, ".": None}
N = len(SPARKY)


def sparky_group(x0, y0, cell):
    """Pixel <rect>s for Sparky. Cells overlap by 0.6px to kill antialiased seams."""
    ov = 0.6
    out = []
    for r, row in enumerate(SPARKY):
        for c, ch in enumerate(row):
            col = COLS[ch]
            if not col:
                continue
            x = x0 + c * cell
            y = y0 + r * cell
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell+ov:.1f}" '
                       f'height="{cell+ov:.1f}" fill="{col}"/>')
    return "\n  ".join(out)


def write_icon(path):
    """App icon: yellow Sparky mascot only, on a transparent background."""
    size = 128
    cell = 7
    grid = N * cell
    off = (size - grid) / 2
    svg = f'''<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" \
xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">
  {sparky_group(off, off, cell)}
</svg>
'''
    Path(path).write_text(svg)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    write_icon(root / "extension/icons/icon.svg")
    print("wrote extension/icons/icon.svg")
