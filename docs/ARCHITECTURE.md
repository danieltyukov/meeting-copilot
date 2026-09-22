# Architecture

Two front ends, one idea: transcribe the conversation live, and on a keypress
draft something for you to say in the first person, grounded in context you
provided. The terminal app and the Chrome extension share no code, but they
share the shapes below, and the tests hold them to it.

## The terminal app

```
  mic / room audio            (audio.py: ffmpeg -> 16 kHz mono int16 frames)
        |
        v
  Backend.feed(frame)         (backends.py: never blocks)
        |-- DeepgramBackend   streaming websocket, diarize=true
        |-- LocalBackend      Whisper on a worker thread, UtteranceSegmenter, Diarizer
        '-- FallbackSttBackend  Deepgram first, local when it drops or the network does
        |
        v  on_final(text, speaker)
  Session                     (session.py: utterances, who is me, question picker, export)
        |
        v  request_help(note, mode)
  ChainAssistant              (assistant.py: API -> claude CLI -> Ollama, skipping
        |                      network backends when offline)
        v  help_delta / help events
  CopilotTUI                  (app.py: rich dashboard, raw-mode keys, answer box above
                               the transcript)
```

`engine.py` owns the wiring and publishes events; the TUI only reads them. The
capture loop only forwards bytes, and transcription and drafting run on their
own threads, so the UI never blocks and the audio pipe never stalls. That
separation is the fix for the original bug, where transcribing inline dropped
everything after the first utterance.

`net.py` polls connectivity with short TCP connects. On a transition the engine
switches transcription to local mid-session and the answer chain skips the
network backends, so a dropped Wi-Fi costs a few seconds rather than the
meeting.

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
