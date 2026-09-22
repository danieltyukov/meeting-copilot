# Changelog

All notable changes to Sparky are listed here. The format follows Keep a
Changelog and the project uses semantic versioning. The Python package and the
extension carry their own version numbers, both noted per release.

## [Unreleased]

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
