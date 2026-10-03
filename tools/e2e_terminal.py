"""End-to-end check of the terminal app on a real meeting recording.

Real Deepgram, real Claude API, no microphone: the three-voice test meeting from
tools/e2e_audio.sh is streamed in at real-time pace. Not run in CI (it needs
keys); run it before a release.

    .venv/bin/python tools/e2e_terminal.py

Three runs:
  1. intros only: every voice is named from what it says ("Hi, I'm Sarah").
  2. with your name and a calendar invite in the launch directory: intros are
     completed from the invite ("Sarah" -> "Sarah Chen"), and the voice that says
     your name becomes Me.
  3. Help, driven through the engine: the question put to you is picked, and a
     first-person answer streams back from the API.
  4. a 1:1 where the other person never says their name: --people names them by
     elimination, once your own introduction has marked your voice.

Keys come from the environment or ~/.config/meeting-copilot/config.env.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meeting_copilot.audio import FileSource  # noqa: E402
from meeting_copilot.cli import _build_parser, _make_config, load_config_env  # noqa: E402
from meeting_copilot.engine import CopilotEngine  # noqa: E402

INVITE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Migration service review
ORGANIZER;CN="Sarah Chen":mailto:sarah.chen@example.com
ATTENDEE;ROLE=REQ-PARTICIPANT;CN=Marcus Lee:mailto:marcus.lee@example.com
ATTENDEE;ROLE=REQ-PARTICIPANT;CN=Daniel Tyukov:mailto:daniel@example.com
END:VEVENT
END:VCALENDAR
"""

failures = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global failures
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond or not extra else "  ->  " + extra))
    if not cond:
        failures += 1


def headless(project: Path, audio: Path, *flags: str) -> tuple[str, str]:
    """Run the CLI headless; return (stdout, the saved Markdown)."""
    out = subprocess.run(
        [sys.executable, "-m", "meeting_copilot.cli", str(project), "--headless",
         "--audio-file", str(audio), *flags],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    print(out.stdout.rstrip())
    if out.returncode:
        print(out.stderr.rstrip())
    saved = sorted(project.glob("meeting-*.md"))
    return out.stdout, (saved[-1].read_text() if saved else "")


def conversation(md: str) -> str:
    return md.split("## Conversation", 1)[-1].split("## Copilot assists", 1)[0]


def main() -> int:
    load_config_env()
    # The recording is cached where tools/e2e_extension.mjs keeps it too, so the
    # voices are synthesised once.
    cache = Path(os.environ.get("E2E_DIR") or Path(tempfile.gettempdir()) / "sparky-e2e")
    subprocess.run([str(ROOT / "tools/e2e_audio.sh"), str(cache)], check=True)
    audio = cache / "meeting.wav"
    work = Path(tempfile.mkdtemp(prefix="sparky-e2e-"))

    print("\n== 1. names from introductions")
    p1 = work / "intros"
    p1.mkdir()
    _, md = headless(p1, audio)
    convo = conversation(md)
    for name in ("Sarah", "Marcus", "Daniel"):
        check(f"{name} is named from the introduction", f"] {name}:**" in convo, convo[:400])
    check("no voice is left numbered", "Speaker" not in convo, convo[:400])

    print("\n== 2. your name and a calendar invite")
    p2 = work / "invite"
    p2.mkdir()
    (p2 / "review.ics").write_text(INVITE)
    _, md = headless(p2, audio, "--me", "Daniel")
    convo = conversation(md)
    check("'I'm Sarah' plus the invite gives Sarah Chen", "] Sarah Chen:**" in convo, convo[:400])
    check("'this is Marcus' plus the invite gives Marcus Lee", "] Marcus Lee:**" in convo, convo[:400])
    check("the voice that says your name is Me", re.search(r"\] Me:\*\* Hi[,.] I'm Daniel", convo) is not None, convo[:600])
    head = md.split("## Conversation", 1)[0]
    check("the header lists the participants", "Sarah Chen" in head and "Marcus Lee" in head, head)

    print("\n== 3. Help on the question put to you")
    p3 = work / "help"
    p3.mkdir()
    (p3 / "README.md").write_text(
        "# Migration service\n\nExpand and contract schema migrations: add the column, "
        "backfill in batches, switch reads, then drop the old column. Rehearsed in CI on a "
        "copy of production data.\n")
    args = _build_parser().parse_args(
        [str(p3), "--audio-file", str(audio), "--me", "Daniel", "--answer-model", "haiku"])
    events: list[dict] = []
    engine = CopilotEngine(_make_config(args), on_event=events.append)
    engine.prepare()
    engine.start_meeting()
    engine.run(FileSource(str(audio), realtime=True))
    engine.request_help()
    engine.end_meeting()
    engine.stop()
    done = [e for e in events if e["type"] == "help"]
    errors = [e["msg"] for e in events if e["type"] == "error"]
    check("Help produced a draft", bool(done), "; ".join(errors))
    if done:
        d = done[-1]
        print("Q:", d["question"])
        print(d["answer"])
        check("it answered the question put to you", d.get("mode") == "answer"
              and "schema migration" in d["question"].lower(), d["question"])
        check("the draft is a real answer", len(d["answer"]) > 80, d["answer"])

    print("\n== 4. a 1:1, named from the people list alone")
    p4 = work / "oneonone"
    p4.mkdir()
    _, md = headless(p4, cache / "oneonone.wav", "--me", "Daniel", "--people", "Sarah Chen")
    convo = conversation(md)
    check("your introduction marks you", re.search(r"\] Me:\*\* Hi[,.] [Tt]hanks for having me", convo) is not None, convo[:500])
    check("the other voice is Sarah Chen without ever saying so",
          convo.count("] Sarah Chen:**") >= 2 and "Speaker" not in convo, convo[:500])

    print("\n" + (f"{failures} FAIL" if failures else "all passed"))
    print("work dir:", work)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
