# Speaker names instead of "Speaker 1, 2"

Date: 2026-10-03. Applies to the terminal app and the extension alike.

## Problem

Both front ends label every voice that is not you by number: `Speaker A` /
`Speaker B` in the terminal app, `Speaker 1` / `Speaker 2` in the panel. People
nearly always say who they are at the start of a meeting, so the transcript
already holds the names; we just do not read them. A named transcript is easier
to follow live, and it lets a draft address people properly ("Good question,
Sarah").

## Rule (one rule, two implementations)

`detect_name(text)` in `meeting_copilot/names.py` and `detectName(text)` in
`extension/names.js` return a display name, or nothing. Both are held to
`tests/name_cases.json`, which pytest and `node extension/test_names.cjs` both
read, so the two cannot drift.

A name word starts with an uppercase letter and continues with letters
(any script), apostrophes or hyphens; at least two characters. A name is one
or two such words separated only by whitespace (punctuation ends it), with an
optional title in front (`Dr`, `Doctor`, `Professor`, `Prof`, `Mr`, `Mrs`,
`Ms`, `Miss`, each with or without a trailing dot, kept in the name as
spoken: `Dr. Patel`, `Mr. Smith`). A word in the stoplist (lowercase compare: function
words, adjectives like sorry/happy/sure, nationalities, weekdays, months, app
and company names such as google/teams/zoom, "everyone") is never a name; a
stopped second word is dropped and the first kept. A word ending in `'s` or
`’s` (possessive) rejects the match.

Cues, tried in this order, first accepted hit wins (a match whose name is
rejected does not end the scan, so "I'm Dutch, I'm Jan." gives Jan).
Contractions and words that commonly precede `here` or `speaking` (same,
generally, honestly, ...) are in the stoplist too:

1. `my name is NAME`, `my name's NAME`: anywhere.
2. `call me NAME`: anywhere.
3. `I'm NAME`, `I am NAME`: only at a clause start (start of text, or after
   `.` `!` `?` `,` `;` `:`), optionally after fillers (and, so, well, yeah,
   yes, oh, um, uh, okay, ok, hi, hello, hey, actually).
4. `this is NAME`: only when a greeting comes first (hi, hello, hey, hiya,
   good morning/afternoon/evening, optionally followed by everyone / all /
   there / folks), or when `here`, `speaking` or `from` follows the name.
5. `NAME here`, `NAME speaking`: at the start of the text, optionally after a
   greeting.

## Where it applies

- Only to final utterances, never to partials.
- Only to voices that are not you. In the panel that is every tab-leg key
  (`int:N`). In the terminal app it is every label except the one marked as
  me and `?`.
- First confident detection wins for a voice; later intros do not rename it.
- A name typed by the user always wins and is never overwritten by detection.
  Clearing a typed name returns the voice to automatic.
- Optional "your name" (terminal: `--me NAME` or `MY_NAME=` in config.env;
  panel: a Settings field). In the terminal app, when a voice introduces
  itself with your name and no voice is marked as you yet, that voice is
  marked as you rather than named, which settles the hardest part of
  in-person diarization without pressing `m`. In the panel, a far-end intro
  with your name is ignored (it is your own voice leaking into the call
  audio). Your name also goes into the drafting prompt so drafts know who
  "I" am.

## Display

- Label of a voice: `Me` for you; its name when known; otherwise the numbered
  label exactly as before (`Speaker`, `Speaker 1`... in the panel,
  `Speaker A`... in the terminal app).
- Labels are computed at render time, so naming a voice relabels every line
  it already spoke.
- Saved transcripts store the names (panel: `names` and `nameSources` maps in
  the session record, keyed like `ordinals`; terminal app: in the Markdown
  export, plus a `Participants` line in the header block).
- The drafting prompts say other voices are labelled by name when known,
  otherwise by number.

## Renaming by hand

- Panel: a "People" row of chips, one per voice heard (Me first). Clicking a
  chip, or a speaker label in the transcript, opens an inline input: Enter
  saves, Escape cancels, an empty value returns the voice to automatic.
  Renaming works while recording and in a reopened history entry.
- Terminal app: `n` then the voice's letter, then type the name; Enter saves,
  Esc cancels, empty returns to automatic.

## Names from the meeting (the roster)

Introductions are one source. The meeting itself is the other: an online call
shows who is in it, and an in-person meeting usually has a calendar invite. Both
front ends keep a roster, a list of display names of the other people, and use
it alongside introductions. The shared rules live in
`meeting_copilot/roster.py` and `extension/names.js`, held to
`tests/roster_cases.json` by pytest and `node extension/test_names.cjs`.

Where the roster comes from:

- Panel: the call page. After you invoke Sparky on the call tab (icon or
  Alt+Shift+S), `activeTab` lets the panel run one read-only function on that
  tab with `chrome.scripting.executeScript`; it returns the participant names
  Meet, Teams or Zoom show (participant tiles, the people panel, ARIA list
  items in a list labelled participants/people/attendees). It runs at capture
  start and every 15 s while recording, reads nothing but names, and fails
  silently (the roster just stays as it was). Plus names you type into the
  People row ("who is on the call"), for when the page cannot be read.
- Terminal app: `--people "Sarah Chen, Marcus Lee"`, `--invite FILE.ics`, or,
  with neither, the single `*.ics` file at the root of the launch directory if
  there is exactly one (ATTENDEE and ORGANIZER `CN=` values, with iCalendar
  line unfolding and quoted or escaped values handled).

Rules:

- `clean_roster_name(raw)`: first line only, whitespace collapsed; drop a
  trailing role in parentheses or after a comma (Host, Co-host, Guest,
  External, Presenting, Organizer, Organiser, Presenter, Me, You); reject the
  self markers (`You`, anything with `(You)` or `(me)`), bare role words,
  `Presentation`, anything with `@`, and anything without a letter. Also
  reject the meeting apps' own UI text (`Add people`, `Participants`,
  `People`, `In this meeting`, `Contributors`, with or without a count in
  parentheses), icon ligature names (anything containing `_`, such as
  `mic_off`), and anything longer than 60 characters. Drop a trailing
  professional suffix (`PhD`, `MD`, `MBA`, `Esq`, each with or without a
  trailing dot) after a comma or in parentheses, like a role, and reject one
  on its own, like a bare role word (so `--people "Anil Patel, PhD"` does not
  invent a person called PhD).
- `roster_others(my_name, roster)`: cleaned, de-duplicated case-insensitively
  (first spelling wins), minus every entry that is your name (full match, or
  first-word match when your name is a single word).
- `match_roster(name, roster)`: compare case-insensitively and ignoring
  diacritics. A full-name match, or a first-name match against exactly one
  roster entry, returns that roster entry; otherwise the name unchanged. So an
  intro "I'm Sarah" labels the voice "Sarah Chen".
- `eliminate(voices, names, roster)`: returns new assignments. Only when the
  number of distinct other voices heard equals the number of roster names, and
  exactly one voice is unnamed and exactly one roster name is unused (a voice's
  name "uses" a roster entry when `match_roster` maps it there), the unnamed
  voice gets the unused name. This names the other side of a 1:1 call as soon
  as they speak, and the last voice in a bigger call. It never guesses between
  two candidates.
- Priority of a voice's name: typed by you, then an introduction, then roster
  elimination. An introduction replaces a roster-elimination name on its own
  voice, and if it claims a name another voice got by elimination, that other
  voice goes back to automatic.
- Known limit: elimination trusts diarization's voice count. If one person
  is split into two voices while another person on the roster stays silent,
  the counts still match and the split half gets the silent person's name.
  The harm is bounded: the label is marked as coming from the roster, an
  introduction replaces it, and clearing it keeps elimination off that voice
  until it is named some other way.
- Typing the name a voice already has (for example, confirming a roster
  guess) makes it a typed name, so later introductions cannot take it.
- "Which voice is me" is per meeting in the terminal app: speaker letters
  restart with every stream, so `me` resets on start and is re-marked by your
  introduction or `m`.
- In the terminal app, elimination needs to know which voice is you (`me` is
  set), since the room microphone hears you too; the "other voices" are the
  labels heard minus yours and `?`. The panel's far-end voices never include
  you.
- The roster goes into the drafting prompt ("PEOPLE IN THIS MEETING: ...") and
  into the saved transcript's participants list, and its names are offered as
  suggestions when renaming a voice by hand (panel: input suggestions; terminal
  app: numbered picks after `n` and the letter).

## Out of scope

Reading who is speaking from the meeting page (speaking indicators, captions).
Meet and Teams obfuscate and churn that markup, and aligning it in time with
diarized audio is fragile. The roster only answers "who is here", and
introductions plus elimination turn that into "who is speaking".
