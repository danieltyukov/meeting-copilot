"""Render the README screenshot of the terminal app from the real TUI.

The dashboard is drawn by CopilotTUI itself into a recording console with a
scripted conversation, then exported as SVG, so the picture can never drift
from what the app draws. Rasterise with tools/render_sparky.sh.

    .venv/bin/python tools/render_screenshot.py
"""
from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme

from meeting_copilot.app import CopilotTUI
from meeting_copilot.engine import CopilotEngine, EngineConfig

ROOT = Path(__file__).resolve().parent.parent

# The panel's own palette (extension/sidepanel.css), so both screenshots match.
THEME = TerminalTheme(
    (13, 17, 23), (230, 237, 243),
    [(13, 17, 23), (248, 113, 113), (52, 211, 153), (251, 191, 36),
     (56, 189, 248), (232, 121, 249), (56, 189, 248), (230, 237, 243)],
    [(125, 133, 144), (248, 113, 113), (52, 211, 153), (251, 191, 36),
     (56, 189, 248), (232, 121, 249), (56, 189, 248), (255, 255, 255)],
)

LINES = [
    (8, "Speaker B", "Walk me through your gain stage: why this topology, and what else did you consider?"),
    (21, "Me", "Sure, let me explain the trade-offs."),
    (34, "Speaker B", "And how does that choice affect your phase margin?"),
]

ANSWER = (
    "We went with a telescopic cascode for the gain stage because it gives the output "
    "impedance we needed for loop gain without burning extra current.\n\n"
    "I did prototype a single PMOS stage first, it is in the repo as single_pmos.cir, but it "
    "fell short on gain. A folded cascode was the other candidate; it buys headroom but "
    "complicates compensation, so for our supply rail it was not worth it."
)
POINTS = (
    "- The dominant pole sits at the cascode output, so the phase margin is set by the load "
    "capacitance, and we have about 65 degrees at the nominal 2 pF.\n"
    "- I can show you the AC sweep in ac_sweep.png if that helps; it is in the repo.\n"
    "- One thing we have not covered yet is the bias network: it tracks the supply, which is "
    "what keeps the margin stable across corners.\n"
    "- What load capacitance are you expecting on your side? That changes which compensation "
    "I would recommend."
)


def render(mode: str, path: Path) -> None:
    eng = CopilotEngine(EngineConfig(root=ROOT, stt_backend="deepgram",
                                     deepgram_api_key="x", anthropic_api_key="x"))
    tui = CopilotTUI(eng, source_factory=lambda: None)
    tui.console = Console(width=100, height=42, record=True, force_terminal=True)
    tui.state = "recording"
    tui.online = True
    tui.me_label = "A"
    tui._start_mono = time.monotonic() - 95
    for t, name, text in LINES:
        tui._on_event({"type": "utterance", "t": t, "name": name, "text": text})
    tui._on_event({"type": "partial", "name": "Me", "text": "Right, so the dominant pole sits at"})
    if mode == "answer":
        q, body = LINES[0][2], ANSWER
    else:
        q, body = LINES[2][2], POINTS
    tui._on_event({"type": "help_started", "question": q, "note": "", "mode": mode})
    tui._on_event({"type": "help", "question": q, "answer": body, "served": "api", "mode": mode})
    tui.console.print(tui._render())
    tui.console.save_svg(str(path), title="meeting-copilot", theme=THEME)
    print("wrote", path)


if __name__ == "__main__":
    render("answer", ROOT / "docs" / "screenshot.svg")
    render("points", ROOT / "docs" / "screenshot-points.svg")
