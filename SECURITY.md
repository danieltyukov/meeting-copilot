# Security

## Reporting

Use GitHub's private vulnerability reporting: the Security tab of
<https://github.com/danieltyukov/meeting-copilot>, then "Report a
vulnerability". That opens a private thread visible only to the maintainers,
which is the right place for anything you would not want in a public issue.

This is one person's side project with no service behind it and no on-call
rotation. Expect a reply in days, not hours. There is no bounty.

If the report is about your own installation rather than about this code,
rotate first and report second: revoke the Deepgram and Anthropic keys in their
dashboards and delete the transcripts you would rather not keep. Neither needs
anybody else's cooperation, because there is no account and no server holding
anything on your behalf.

## Where the secrets live

Every credential this tool touches belongs to the person running it.

| Secret | Stored in | Scope |
| --- | --- | --- |
| Deepgram API key (terminal) | `~/.config/meeting-copilot/config.env`, or the `DEEPGRAM_API_KEY` environment variable | Streaming transcription on your Deepgram account |
| Anthropic API key (terminal, optional) | The same file, or `ANTHROPIC_API_KEY` | Drafts on your Anthropic account |
| Claude Code sign-in (terminal fallback) | Wherever the `claude` CLI keeps it; this tool never reads it | Whatever the CLI is signed in as |
| Deepgram and Anthropic keys (extension) | `chrome.storage.local` of the extension, unencrypted | The same, from the browser |

Make `config.env` mode 600. The install script does not do it for you.

The terminal app strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the
environment before it runs the `claude` CLI, so the CLI always uses its own
sign-in and an API key is never sent anywhere the API path would not send it.

## The browser key

The extension calls the Anthropic API directly from the side panel, with the
`anthropic-dangerous-direct-browser-access` header. That header exists because
a key in a browser is readable by anything that can read the browser profile:
another extension with the right permissions, a person at the keyboard, a
backup of the profile directory. The key is fine for personal use on your own
machine. Do not put it in a shared profile, and do not ship the extension with
a key in it.

## Transcripts

The terminal app writes `meeting-*.md` into the directory you launched it from
(falling back to your home directory, then the temp directory, if that is not
writable). The extension keeps the last fifty calls in `chrome.storage.local`.
Both are plain text, unencrypted, and readable by anything with access to your
files or your Chrome profile. Delete what you would rather not keep; the
extension has a Delete and a Clear all for exactly that.

## What leaves the machine

See `PRIVACY.md`. In short: audio goes to Deepgram when a key is set, prompts
(your context plus the transcript) go to the Anthropic API or through the
`claude` CLI, and nothing goes to the author. With no keys and Ollama installed
the terminal app runs fully offline.
