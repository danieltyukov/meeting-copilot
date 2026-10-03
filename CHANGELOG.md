# Changelog

All notable changes to Sparky are listed here. The format follows Keep a
Changelog and the project uses semantic versioning. The Python package and the
extension carry their own version numbers, both noted per release.

## [Unreleased]

### Added
- The terminal app runs on macOS and Windows as well as Linux. The microphone
  opens through AVFoundation on macOS and DirectShow on Windows (PulseAudio or
  PipeWire on Linux, as before), keys are read from the Windows console, and
  `y` copies through the Windows clipboard.
- `install.ps1`, the Windows counterpart of `install.sh`. `install.sh` now
  gives the macOS commands and checks for Python 3.10. On any of the three,
  `uv tool install git+https://github.com/danieltyukov/meeting-copilot` works
  without a clone.
- `--list-mics` lists the microphones ffmpeg can open, the default starred,
  and `--mic NAME` picks one. `--self-test` names the microphone it will use.
- CI runs the Python suite, and each desktop's installer, on Linux, macOS and
  Windows.

### Fixed
- The `claude` CLI fallback takes its prompt on stdin. As an argument, a long
  meeting with a full project context could pass Linux's 128 KB limit for one
  argument and fail, and on Windows it was always past the 32 KB command line.
- Text is read and written as UTF-8 everywhere, so a config file saved with a
  BOM, non-ASCII commit messages, and names in any script work on Windows.

## [0.4.2] - 2026-10-03

Extension 0.5.2. The terminal app is unchanged apart from its version.

### Removed
- The panel's screen-sharing warning: the idle status line now says how to
  start, and the welcome notice and the extension description no longer carry
  it.

## [0.4.1] - 2026-10-03

Extension 0.5.1.

### Changed
- Answers use the current Claude models: `sonnet` is now Claude Sonnet 5.5
  (`claude-sonnet-5-5`) and `opus` is Claude Opus 5.5 (`claude-opus-5-5`), in
  both the terminal app and the panel. `haiku` stays Claude Haiku 4.5. Sonnet
  keeps thinking off (`between_tools`, its thinking-off setting); Opus 5.5
  always thinks, so it runs at low effort with room in the token cap for the
  thinking. Transcription stays on Deepgram Nova-3, still Deepgram's newest
  general model with diarization.
- A draft Claude declines now says so. In the terminal app it falls through to
  the next backend; in the panel it shows a message instead of an empty box.
- New README banner, drawn from the site's own stylesheet with a dark variant
  (`tools/banner.html`, rendered by `tools/render_sparky.sh`).

### Removed
- The "visible by design" copy on the site and in the READMEs.

## [0.4.0] - 2026-10-03

Extension 0.5.0.

### Added
- Speakers by name. When a voice introduces itself ("Hi, I'm Sarah", "my name
  is Priya Raman", "this is Marcus from design"), its lines read `Sarah:` from
  then on, and the lines it already spoke are relabelled. The rule is cautious
  ("I'm going to share my screen" or "I'm Dutch" never becomes a name), and the
  terminal app and the panel share it, held to one set of test cases.
- Names from the meeting itself. The panel reads the participant names Meet,
  Teams or Zoom show on the call tab once you invoke Sparky there (names only,
  nothing else on the page), and you can type names into the People row. The
  terminal app reads the attendees of a calendar invite (`.ics`) in the launch
  directory, or takes `--people "Sarah Chen, Marcus Lee"` or `--invite FILE`.
  The list completes a short intro ("I'm Sarah" becomes Sarah Chen), names the
  other side of a 1:1 as soon as they speak and the last unnamed voice in a
  bigger call, never guesses between two candidates, and goes into the drafts.
- Renaming by hand: the panel's People chips (or a click on a speaker label),
  and `n` in the terminal app. A typed name always wins; an empty one hands the
  voice back to automatic. Names are kept in saved calls and exports, with a
  participants list.
- Your own name (`--me` or `MY_NAME=` in the terminal app, a Settings field in
  the panel). Drafts know who "I" am, and in the room, the voice that
  introduces itself with your name is marked as you.
- Terminal app: `y` copies the last draft (wl-copy, xclip, xsel, pbcopy, else
  OSC 52 so it works over SSH), a mic dot in the header lights while speech is
  heard, and a newer `h` replaces a draft that is still streaming.
- Extension: Alt+Shift+S opens Sparky on the current tab. It counts as the
  toolbar click Chrome requires before it lets an extension capture a tab.
- End-to-end checks for both apps on a generated three-voice meeting with real
  Deepgram and Claude (`tools/e2e_terminal.py`, `tools/e2e_extension.mjs`), and
  shared name test cases run by pytest and Node in CI.

### Changed
- A refreshed look. The terminal app has rounded panels, a status pill, a
  People line, a speaker column in the transcript and key caps. The side panel
  has a system font, a draft card at the top, People chips, controls docked
  at the bottom, and light and dark themes. The site is redesigned around the
  people it is for, including those for whom live recall or speech is the hard
  part.
- Extension: the consent and disclosure notice is a calm welcome card shown
  once, and again when its wording changes, instead of a gate on every open.
  It keeps everything it said before and adds that participant names are read
  from the call page, so everyone sees it once more after updating. Settings
  can show it again.

### Fixed
- Extension: the call tab could not be captured once Chrome refused it. The
  "Allow capturing the call tab" button asked for access to all sites, which
  Chrome does not accept in place of a toolbar click on the call tab, so it
  never helped. The button and the all-sites request are gone. A click on the
  Sparky icon (or Alt+Shift+S) while the call tab is in front now joins the
  call to the running recording, with no Stop and Start.
- Terminal app: a failed draft no longer leaves "Drafting..." on screen, long
  wrapped lines no longer push the newest transcript line out of view, and a
  second meeting in one run no longer relabels the first meeting's lines.

## [0.3.0] - 2026-09-22

Extension 0.4.0.

### Added
- Talking points. One press of Help (`h` in the terminal) now drafts what to
  say next, decided from the transcript: a first-person answer when the other
  side has just put a question to you, otherwise three to five first-person
  lines to carry the conversation on from the last thing said, drawn from your
  context. Before anyone has spoken they are openers. The draft box says which
  it chose (`Q:` or `From:`), and the note box steers either kind.
- A smarter pick of the question to answer. A lead-in and its question arrive
  together, a backchannel like "Right, yes." no longer becomes the thing you
  are asked to answer, and a question further back than a few lines is never
  dug up. The same rule runs in the terminal app and the panel.
- Extension: `h` drafts from the keyboard, so you do not have to leave the
  call to click. A Copy chip on the draft.
- Extension: a second press while a draft is still streaming aborts the first,
  so two drafts never interleave in the box.
- The saved transcript labels each draft as an answer or as talking points.
- Extension: Start asks once for access to capture whichever tab the call is
  in, and a button offers it again if Chrome still refuses. Until now capture
  only worked after clicking the toolbar icon on the call tab, which was easy
  to miss mid-call and failed with Chrome's activeTab message.
- Open source scaffolding: MIT licence, contributing guide, security policy,
  privacy page, issue and pull request templates, CI for the Python and
  extension test suites, an architecture note, and the project site at
  https://danieltyukov.github.io/meeting-copilot/.

### Changed
- UI copy in the terminal app, the panel and the mic helper reads plainly,
  without icons. The terminal header shows `stt` and `llm` in words.
- The mic helper tab no longer asks for a Stop and Start round trip; the mic
  joins the recording in progress, as it has done since 0.1.

## [0.2.0] - 2026-08-14

Extension 0.3.0.

### Changed
- Renamed from interview-copilot to meeting-copilot. The far-end label
  `Interviewer N` is now `Speaker N`.

### Added
- Extension: every call is written to a browsable History as it happens, so
  closing the panel or forgetting to press Stop loses nothing. Entries are
  named after the first question asked of you, upgraded to a one-line summary
  by haiku when a key is set, and can be reopened, copied, downloaded as
  Markdown, renamed or deleted.
- Extension: the call tab is diarized, so several people on the far end show
  up as Speaker 1, 2, 3 rather than one merged block. A text-level echo guard
  keeps your own voice out of that bucket on speakers.

## [0.1.0] - 2026-07-01

### Added
- The terminal app: live transcription through Deepgram streaming with
  automatic fallback to local Whisper, first-person answers grounded in the
  launch directory's README, manifests, file tree and hand-written notes,
  through a Claude API to `claude` CLI to Ollama chain that keeps working with
  no keys and no Wi-Fi, best-effort speaker separation, and a Markdown
  transcript on stop.
- The Chrome side panel for Google Meet and Microsoft Teams: your mic and the
  call tab captured separately so your own voice is labelled exactly, a
  consent gate, a mic permission helper, and a live capture diagnostic line.
- The Sparky brand: a one-colour yellow pixel budgie, generated from one ASCII
  map for the icon and the README hero.
