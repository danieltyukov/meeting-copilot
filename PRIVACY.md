# Privacy

Sparky is a local tool with no server of its own. This page lists everything it
touches, for both the terminal app and the Chrome extension.

## What is sent, and where

| Data | Terminal app | Extension |
| --- | --- | --- |
| Audio | To Deepgram while a key is set. With no key, or when the network drops, it is transcribed on your machine by Whisper and nothing leaves. `--stt local` forces that. | To Deepgram, always. A browser cannot run Whisper the way the desktop app does. |
| Your context | To the Anthropic API when a key is set, otherwise through the `claude` CLI, otherwise to a local model through Ollama. Only when you press `h`. | To the Anthropic API, only when you press Help. |
| The transcript | The same, as part of the same prompt. | The same. A short excerpt also goes to the API once, on Stop, to name the saved call, if a key is set. |
| Speaker names | Worked out on your machine from what people say ("Hi, I'm Sarah") and from the attendees of a calendar invite you put in the launch directory or pass with `--people`/`--invite`, or typed by you. They label the transcript, so they go wherever the transcript goes. | The same, with the participant names shown on the call page in place of the invite (see `activeTab` below). |
| Your name, if you set it | In the prompt, so drafts know who "I" am. | The same. |
| Anything to the author | Nothing. | Nothing. |

There is no analytics, no telemetry and no update check of the tool's own. The
network probes the terminal app makes to notice a dropped connection are plain
TCP connects to well-known hosts and carry no data.

## What is stored

**Terminal app.** Keys in `~/.config/meeting-copilot/config.env` if you put them
there. One `meeting-*.md` per meeting, written on `e`, into the directory you
launched from. That file holds the transcript, the participants' names as
Sparky heard or you typed them, and every draft you asked for. Your own name, if
you set one, sits in the same config file as the keys.

**Extension.** Keys, the model choice, the language, your name if you set one,
and the pasted context in `chrome.storage.local`. The last fifty calls, in the
same place, written while the call happens so that closing the panel loses
nothing, each with the names given to its voices and the participant names read
from the call page. Nothing is put in `chrome.storage.sync`, so nothing follows
you to another browser.

## Permissions the extension asks for

- `tabCapture`: the audio of the meeting tab, only after you press Start.
- `sidePanel`: the panel itself.
- `storage`: keys, settings and saved calls, on this machine.
- `tabs`: the title of the active tab, to name a saved call after the app it
  was captured from (Meet, Teams, Zoom). Nothing else is read.
- `downloads`: the Download button on a saved call.
- `activeTab` and `scripting`: when you invoke Sparky on the call tab (its icon
  or Alt+Shift+S), Chrome lets it run one read-only function on that tab, which
  returns the participant names Meet, Teams or Zoom show there, so voices can be
  labelled with real names. It runs at Start and every 15 seconds while
  recording, only on that tab, and reads nothing but those names. The names stay
  on your machine except as part of a prompt, like the transcript.
- Host access to `api.deepgram.com` and `api.anthropic.com`: the two services
  above, and no others.

The microphone is a normal browser permission, asked for once in a helper tab
because Chrome does not show the prompt inside a side panel.

## Consent

The tool transcribes other people. It is built to be visible: it lives in the
normal side panel or in your terminal, it is not hidden from screen sharing,
and there is no stealth mode. Use it where you have consent to transcribe the
conversation, and disclose it where that is required.
