# Contributing

Bug reports and patches are welcome. There is no CLA and no style bikeshed;
match the code that is already there.

## Layout

    meeting_copilot/   the terminal app (Python >= 3.10)
      cli.py           entry point, flags, --self-test
      engine.py        audio -> transcription -> session -> answer chain
      app.py           the rich TUI and its keypress loop
      session.py       transcript state, question picking, Markdown export
      assistant.py     prompts and the Claude API -> CLI -> Ollama chain
      backends.py      Deepgram streaming and local Whisper, with fallback
      audio.py         ffmpeg capture and utterance segmentation
      diarize.py       best-effort speaker clustering for the local path
      context.py       what the launch directory contributes to the prompt
      net.py           online/offline detection
    extension/         the Chrome side panel (Manifest V3, plain scripts)
      sidepanel.js     capture, transcript, drafting, keyboard shortcuts
      history.js       transcript persistence and labels, loaded first
      deepgram.js      the browser Deepgram client
      audio.js         one AudioContext, worklet capture with a fallback
    tests/             pytest for the Python package
    site/              the project site, static, no build step
    docs/              brand assets, architecture notes, design specs
    tools/             regenerates the icons and mock screenshots

`session.py` and `assistant.py` know nothing about audio or the terminal, which
is what lets the question picker and the prompts be tested without a
microphone. The extension mirrors both in `sidepanel.js`; keep the two question
pickers and the two prompt builders in step when you change one.

## Running the tests

```
./install.sh                          # once: venv + editable install
.venv/bin/python -m pytest            # the Python package
node extension/test_render.cjs        # the side panel, end to end through the real Deepgram parser
node extension/test_history.cjs       # transcript persistence
```

The Node tests have no dependencies. They load the real extension scripts into
a `vm` context with a shimmed DOM, so a change to a script that breaks the
render path fails here rather than in Chrome. CI runs all three on every push.

## Running the app

The terminal app needs `ffmpeg` and the `claude` CLI, and works with no keys at
all (local Whisper for transcription, the CLI for answers). Keys make it faster;
see the README. `meeting-copilot --self-test` checks the install.

The extension loads unpacked from `extension/` through `chrome://extensions`
with Developer mode on. After changing a file, press the reload icon on the
extension card, then close and reopen the side panel: Chrome keeps the old
panel document alive across a reload, and it will run new scripts against the
old page.

## Style

- Plain prose in docs, comments and UI copy. No emojis, and no em or en dashes
  as punctuation; use a comma, a colon or a new sentence.
- The drafted text is always first person, and the tool never speaks for you
  unprompted: every draft is one keypress or one click.
- The panel is a visible aid. Nothing that hides it from screen sharing will be
  merged.
- Conventional commit prefixes: `feat:`, `fix:`, `docs:`, `test:`, `chore:`,
  `ci:`. Add a line under Unreleased in `CHANGELOG.md`.

## Pull requests

One change per pull request, with a test for anything in `meeting_copilot/` or
`extension/*.js`. Say in the description what you ran and what CI cannot see:
a real call, a phone on speakers, a dropped network.
