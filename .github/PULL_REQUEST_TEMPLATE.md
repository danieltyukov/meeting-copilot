## What this changes

<!-- What the change is and why it is right. One or two paragraphs. -->

## How it was checked

<!--
CI runs these on every push. A note here is about what CI cannot see: a real
call, a phone on speakers, a dropped network, a long meeting.

  .venv/bin/python -m pytest
  node extension/test_render.cjs
  node extension/test_history.cjs
-->

## Notes

<!--
Anything a reviewer would otherwise have to work out: a prompt change and how
the drafts read afterwards, a new permission in the manifest, a new dependency
and why it earns its place.

Two things the review will check for, because both are easy to break by
accident: the question picker and the prompt builder exist twice (session.py
and assistant.py on one side, sidepanel.js on the other) and must stay in
step; and nothing in the extension may hide the panel from screen sharing.

No emojis, and no em or en dashes as punctuation, in code, comments,
documentation or the commit message.
-->
