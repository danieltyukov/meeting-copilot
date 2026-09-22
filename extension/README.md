# Sparky, the Chrome extension

<p align="center"><img src="../docs/extension.png" width="380" alt="Sparky side panel"></p>

A browser version of the desktop copilot for **Google Meet and Microsoft Teams**
(web). It transcribes the call's audio live and, on one click, drafts what you
would say next in a Chrome **side panel**: a first-person **answer** if you were
just asked something, **talking points** to keep the conversation going
otherwise, both grounded in the context you pasted.

It is a **visible aid**: it lives in the normal side panel and is **not hidden
from screen sharing**. There is deliberately no stealth or anti-capture mode.
Use it where you have consent to transcribe the call, and disclose it where
required (interview accommodations, and so on).

## Setup

1. **Load the extension** (unpacked):
   - Open `chrome://extensions` and turn on **Developer mode** (top right).
   - Click **Load unpacked** and select this `extension/` folder.
2. **Add your keys**, by clicking the toolbar icon to open the side panel, expanding
   **Settings**, and pasting:
   - **Deepgram API key**, for streaming transcription.
   - **Anthropic API key**, which drafts the answers. *(Stored in `chrome.storage.local`
     on your machine. A client-side key is fine for personal use but is visible
     to anything with access to your profile, so do not ship this key elsewhere.)*
3. Optionally paste your **context** (résumé, project summary, agenda, job description)
   so answers are grounded in it, and pick an answer model.

## Usage

1. Open your Meet or Teams call in a tab and **focus that tab**.
2. Click the extension icon; the side panel opens. Accept the consent notice.
3. Press **Start**. The first time, Chrome asks to let Sparky **read and change
   data on all websites**: that is the access Chrome requires before an
   extension may capture a tab's audio, and it is only ever used for the call
   tab. Accept it. (If you decline, a button **Allow capturing the call tab**
   appears under Start/Stop and asks again; the alternative is to click the
   Sparky toolbar icon while the call tab is in front, then Start.) It also
   asks for **microphone** access. It then captures two sources and the
   transcript fills in live (you still hear the call normally).
4. **Speakers are detected automatically.** Your **mic** is always **Me**. The
   call's audio is split by Deepgram diarization, so several people on the far
   end become **Speaker 1**, **Speaker 2**, **Speaker 3** (a call with one other
   voice just reads **Speaker**). No manual marking. The `you` / `them` dots in
   the header light up when each side is being heard, so you can confirm capture.
5. Press **Help** (or `h`, when the panel has focus) whenever you need
   something to say. If the other side has put a question to you since you last
   spoke, you get a spoken-length answer to it; otherwise you get three to five
   first-person talking points that pick up from the last thing said, including
   something to ask back. The box says which it chose (`Q:` or `From:`). The
   note field above the buttons steers the next draft ("keep it to two
   sentences", "bring up the migration"), and **Copy** puts the draft on the
   clipboard. Pressing Help again while a draft is still streaming replaces it.
6. Press **Stop** when done. The call is saved to **History** automatically.

## History

Every call is written to `chrome.storage.local` **as it happens**, not at Stop, so
closing the side panel mid-call or forgetting to press Stop does not lose the
transcript. Open **History** in the panel to get the last 50 calls, newest first
(older ones are pruned automatically).

Each entry is named for you: the title is the first question asked of you,
quoted, plus the call app (`"Tell me about yourself." - Meet`). If an Anthropic
key is set, Stop then upgrades that to a one-line summary of what the call was
actually about, using haiku. A failed or absent title call just leaves the quote.

Per entry you can:

- **click the title** to read the whole transcript inline, labelled and coloured
  per voice exactly as it was live
- **Copy** it as labelled plain text
- **Download** it as Markdown to `Downloads/sparky/YYYY-MM-DD-<title>.md`
- **Rename** it (Enter saves, Escape cancels)
- **Delete** it, or **Clear all**

Delete and Clear all arm on the first click and fire on the second, and nothing
leaves your machine: transcripts sit in the extension's own local storage, which
is why deleting a call you would rather not keep is one click away.

## Troubleshooting: "I don't see any transcript"

Text only appears when **someone actually speaks** into a captured source. While
recording with no text yet, the panel tells you exactly what it is hearing:

- **`them … waiting for the call tab`** means the meeting tab is not producing
  audio. Make sure the **call tab** (the one you focused when pressing Start)
  actually has sound playing. If you are alone in the call, there is nothing to
  transcribe.
- **`mic off`** means Chrome **did not show the mic prompt inside the side
  panel**, so the request was auto-denied and there is no microphone icon to
  click. Press the **Fix microphone access** button that appears under
  Start/Stop: it opens a normal tab where the prompt *does* render. Click
  **Allow** there, close the tab, then press **Fix microphone access** once
  more. The grant is keyed to the extension, so the side panel inherits it, and
  the mic binds to the recording already in progress (no Stop/Start round trip,
  and nothing already transcribed is lost). Until that succeeds your own voice
  has no channel of its own, which is why it can end up attributed to the call.

**Definitive 30-second self-test (no call needed):** open any tab playing speech,
for example a talking **YouTube** video, focus that tab, open the side panel,
press **Start**, then switch back to the video. Within a second or two you will
see **`Speaker:`** lines fill in. That confirms the capture -> Deepgram ->
display path end-to-end. (Run `node extension/test_render.cjs` to verify the
rendering path in code, and `node extension/test_history.cjs` for the history path.)

## How it works

```
  your mic ──────────────────→ Deepgram ──────────→ "Me"        ┐
  meeting tab (tabCapture) ───→ Deepgram (diarize) ─→ "Speaker N" ┘─→ live transcript
                                                                 │
   your context + transcript + the question, or the last line ────┼─→ Anthropic API
                                                                 ↓
                                  an answer, or talking points, in the side panel
```

- Cloud-only by nature (a browser cannot run local Whisper or a local LLM the way
  the desktop app does), so it needs internet and both API keys.
- Auth quirks handled: Deepgram over the browser WebSocket uses the
  `["token", key]` subprotocol; Anthropic uses the
  `anthropic-dangerous-direct-browser-access` header.

## Limitations

- **Not invisible to screen share**, by design. If you share your screen, this
  panel is part of your screen. The honest path for a real need (anxiety or
  accessibility, for example) is disclosure or an accommodation, not concealment.
- Captures **two sources**: your microphone (which becomes "Me") and the meeting
  tab's audio (which becomes "Speaker N"). Which *side* you are on is exact,
  because it comes from the capture source rather than a guess. Telling the
  far-end voices apart is Deepgram diarization, so it is good but not infallible:
  voice indices can drift on heavy crosstalk, and the numbering restarts on each
  Start. The first Start needs mic permission.
- If you run on speakers rather than headphones, your mic also hears the call.
  Browser echo cancellation plus a text-level duplicate filter (a line arriving
  on both sources within a few seconds is dropped) keeps that out of the
  transcript. Short backchannels like "yes" are deliberately never filtered.
- Personal-use tool: keys live client-side; there is no server. Saved transcripts
  live in the same local storage, unencrypted, so anything with access to your
  Chrome profile can read them.
