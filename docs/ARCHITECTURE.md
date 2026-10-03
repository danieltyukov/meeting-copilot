# Architecture

Two front ends, one idea: transcribe the conversation live, and on a keypress
draft something for you to say in the first person, grounded in context you
provided. The terminal app and the Chrome extension share no code, but they
share the shapes below, and the tests hold them to it.

## The terminal app

```
  mic / room audio            (audio.py: ffmpeg -> 16 kHz mono int16 frames)
        |
        |-- LevelGate         (audio.py: per-frame RMS -> throttled "level" on/off)
        v
  Backend.feed(frame)         (backends.py: never blocks)
        |-- DeepgramBackend   streaming websocket, diarize=true
        |-- LocalBackend      Whisper on a worker thread, UtteranceSegmenter, Diarizer
        '-- FallbackSttBackend  Deepgram first, local when it drops or the network does
        |
        v  on_final(text, speaker)
  Session                     (session.py: utterances, who is me, names, question
        |                      picker, export)
        |-- names.py          detect_name: "Hi, I'm Sarah" -> Sarah
        '-- roster.py         --people / --invite / the one *.ics in the launch dir:
        |                     match an intro to the roster, name the last voice left
        v  request_help(note, mode)
  ChainAssistant              (assistant.py: API -> claude CLI -> Ollama, skipping
        |                      network backends when offline)
        v  help_delta / help events
  CopilotTUI                  (app.py: rich dashboard, raw-mode keys, answer box above
                               the transcript; clipboard.py behind 'y')
```

`engine.py` owns the wiring and publishes events; the TUI only reads them. The
capture loop only forwards bytes, and transcription and drafting run on their
own threads, so the UI never blocks and the audio pipe never stalls. That
separation is the fix for the original bug, where transcribing inline dropped
everything after the first utterance.

The capture loop also feeds each frame's level to a `LevelGate`, which turns
on above one threshold and off only after 300 ms below a lower one, and reports
a change at most every 200 ms. The `level` event drives the mic dot in the
header, before and during a meeting. The TUI takes it without its render lock,
so the capture loop never waits on a frame being drawn.

`net.py` polls connectivity with short TCP connects. On a transition the engine
switches transcription to local mid-session and the answer chain skips the
network backends, so a dropped Wi-Fi costs a few seconds rather than the
meeting.

### Names

The diarizer only gives letters (`A`, `B`, ... and `?`). `Session` keeps a name
per letter and where it came from, strongest first: typed with `n` (`user`),
said by the voice itself (`intro`, from `names.detect_name` on final lines
only), or left over on the roster (`roster`). A typed name is never replaced,
and the first intro of a voice sticks. With `--me NAME` (or `MY_NAME=`), an
intro in your own name marks that voice as you instead of naming it.

The roster is the other people in the meeting: `--people`, `--invite`, or the
single `*.ics` at the root of the launch directory (`ATTENDEE` and `ORGANIZER`
`CN=` values; rooms and declines are skipped). An intro is matched to it, so
"I'm Sarah" labels the voice "Sarah Chen". Once a voice is marked as you,
elimination names the one unnamed voice with the one unused roster name, but
only when the counts match exactly, so it never guesses between two. An intro
on a voice replaces its roster name, and a voice that held the same person by
elimination goes back to automatic. Clearing an eliminated name with `n`
keeps elimination off that voice, so a wrong guess does not come straight
back, until a typed name or an intro names it. `names.py` and `roster.py` share their
rules with `extension/names.js` through `tests/name_cases.json` and
`tests/roster_cases.json`.

A new meeting starts with no names and nobody marked as you: the letters
restart with every stream, so the old ones would point at the wrong voices.

Lines are stored by letter and resolved to names when drawn, so naming a voice
relabels everything it already said, on screen and in the saved Markdown. The
export lists the participants: you, each voice by name or letter, and roster
names no voice has been matched to. Your name and the roster also go into the
drafting prompt (`=== WHO I AM ===` and `PEOPLE IN THIS MEETING:`).

### Events

The engine publishes plain dicts with a `type`:

- `utterance`, `partial`: transcript lines, with the raw `speaker` label.
- `names`: the name map, who is me, the roster, and what just changed (`label`,
  `name`, `source`). Sent on every naming, rename, mark and roster load.
- `me`: a voice was marked as you, `by` a key press or by your name.
- `level`: the mic dot, on or off.
- `help_started`, `help_delta`, `help`: a draft streaming in. Each draft carries
  a generation number; a newer `h` supersedes one still streaming, and nothing
  from the older one (deltas, the result, a backend failure) reaches the UI or
  the saved assists.
- `state`, `connectivity`, `stt_switch`, `answer_switch`, `model`, `info`,
  `error`, `exported`, `ready`, `audio_stopped`.

`y` copies the last finished draft with the first clipboard tool that works
(wl-copy, xclip, xsel, pbcopy), and otherwise writes an OSC 52 escape so the
terminal sets the clipboard, which is what makes it work over SSH. It runs on
a side thread and reports in the status line, so a tool that hangs until its
timeout never holds up the keys.

## The extension

```
  your mic  --------------> Deepgram (no diarize) --> "Me"           \
  meeting tab (tabCapture) -> Deepgram (diarize)  --> "Speaker 1/2/3"  > utterances[]
                                                                      /
  context + transcript + anchor --> Anthropic API (streamed) --> the draft box
```

One `AudioContext`, resumed under the Start click so it actually runs, taps
both legs through an `AudioWorklet` (with a `ScriptProcessor` fallback). Which
side you are on is exact because it comes from the capture source; telling the
far-end voices apart is Deepgram diarization. A text-level echo guard drops a
line that arrives on both legs within a few seconds, which is what happens on
speakers, and a leak of your own voice into the tab stream is caught the same
way instead of becoming a new speaker.

`history.js` writes the transcript to `chrome.storage.local` while the call
happens, keyed by speaker rather than by rendered label, so labels can upgrade
retroactively ("Speaker" becomes "Speaker 1" when a second voice appears) in
the saved copy exactly as they do live.

## Drafting

One keypress, `h` in the terminal and Help in the panel, and the transcript
decides what it drafts. `Session.decide_help()` and `decideHelp()` in
`sidepanel.js` apply the same rule: if the far end has said something
question-shaped since you last spoke, the draft is an answer to it; otherwise
(you just spoke, they only acknowledged, or nothing has been said yet) the
draft is talking points from the last thing said. The box header shows which
one it chose (`Q:` or `From:`), and the note box steers either.

Every backend draws from the same two prompts, selected by that `mode`:

- `answer`: reply to the latest question put to you. Spoken length, concrete,
  first person.
- `points`: three to five first-person lines to carry the conversation on from
  the last thing said. Build on it, bring in something from your context the
  other side has not heard, ask something back. With nothing said yet, they
  are openers.

The user message is the same in both modes except for one section: `LATEST
QUESTION (answer this)` or `LAST THING SAID (continue from here)`. An optional
`MY EXTRA INSTRUCTION:` line carries whatever you typed in the note box.

## Picking the question

`Session.latest_question()` and `latestQuestion()` in `sidepanel.js` implement
the same rule. The newest run of consecutive lines from one far-end voice is
the default, joined, so a lead-in and its question arrive together. If nothing
in that run reads as a question (ends in `?`, or opens with a question word),
the most recent question-shaped line up to eight far-end lines back wins
instead, which is how "Right, yes." after your answer does not become the thing
you are asked to answer. Beyond that window the run is returned as it is, so a
stale question is never dug up. Everything the model needs to know about what
was already answered is in the transcript it also receives.

## Tests

`tests/` is pytest and never loads a model or opens a socket: fakes stand in
for the transcriber and the assistant, and the TUI renders into a recording
console. `extension/test_render.cjs` loads the real extension scripts into a
`vm` context with a shimmed DOM and pushes real Deepgram frames through the
real parser into the real render, then drives the drafting path through a fake
streaming `fetch`. `extension/test_history.cjs` does the same for persistence.
