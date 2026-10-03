"""Read a speaker's name from the way they introduce themselves.

People nearly always say who they are at the start of a meeting ("Hi, I'm
Sarah"), so the transcript already holds the names. ``detect_name`` finds one
in a single finished line, or returns None.

The rule is shared with ``extension/names.js`` and both are held to
``tests/name_cases.json``, so change them together. The full rule is in
docs/superpowers/specs/2026-10-03-speaker-names-design.md; in short, the cues
are tried in this order and the first one that yields a name wins:

1. ``my name is NAME`` / ``my name's NAME``, anywhere
2. ``call me NAME``, anywhere
3. ``I'm NAME`` / ``I am NAME`` at a clause start, optionally after fillers
4. ``this is NAME`` after an opening greeting, or followed by here/speaking/from
5. ``NAME here`` / ``NAME speaking`` at the start, optionally after a greeting
"""

from __future__ import annotations

import re
import unicodedata

# Words that are never a name, compared in lower case with a straight
# apostrophe. A capitalised word in a name slot is usually a sentence start, a
# nationality, a day, a month or a product, and those are what this list holds.
STOPWORDS = frozenset("""
    a about above after again against all also am an and any anybody anyone
    anything are as at be because been before being below between both but by
    can could did do does doing down during each either else even ever every
    everybody everyone everything few for from further had has have having he
    her here hers herself him himself his how i if in into is it its itself just
    let me mine more most much must my myself neither never no nobody none nor
    not nothing now of off often on once one only or other our ours ourselves out
    over own people perhaps please quite rather really same she should so some
    somebody someone something such than that the their theirs them themselves
    then there these they this those though through thus to too under until up
    upon us very was we well were what whatever when where whether which while
    who whom whose why with within without would yes yet you your yours yourself
    yourselves
    i'm i've i'll i'd we're we've we'll we'd you're you've you'll you'd they're
    they've they'll they'd can't don't won't isn't aren't wasn't weren't didn't
    doesn't haven't hasn't couldn't shouldn't wouldn't
    hi hello hey hiya morning afternoon evening okay ok oh um uh yeah yep nope
    thanks thank welcome actually basically speaking guys folks team speaker
    sorry happy sure glad fine good great excited afraid back busy ready new late
    early tired done right alright interested curious confused aware available
    responsible honest serious kidding joking delighted pleased thrilled grateful
    lucky nervous worried calm free still quiet loud noisy cold hot warm nice
    lovely crowded empty safe sitting standing working living staying coming
    going moving waiting looking stuck born raised
    generally honestly technically strictly roughly broadly frankly personally
    practically relatively realistically ideally essentially literally simply
    american british english scottish welsh irish canadian australian dutch german
    french spanish italian portuguese brazilian mexican argentinian colombian
    russian ukrainian polish czech slovak hungarian romanian bulgarian greek
    turkish swedish norwegian danish finnish icelandic swiss austrian belgian
    indian pakistani chinese japanese korean vietnamese thai filipino indonesian
    malaysian israeli egyptian moroccan nigerian kenyan african european asian
    arab arabic persian iranian latvian lithuanian estonian croatian serbian
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october november
    december
    google meet teams zoom microsoft slack skype webex apple amazon aws azure
    facebook meta whatsapp linkedin youtube github gitlab jira confluence notion
    figma discord outlook gmail chrome firefox safari windows linux android iphone
    openai chatgpt gemini copilot deepgram salesforce hubspot zendesk dropbox
    spotify netflix uber airbnb tesla nvidia intel ibm oracle adobe twitter
    dr doctor professor prof mr mrs ms miss
""".split())

_APOS = "['’]"
_FILLER = r"(?:and|so|well|yeah|yes|oh|um|uh|okay|ok|hi|hello|hey|actually)"
_GREETING = (r"(?:hi|hello|hey|hiya|good\s+(?:morning|afternoon|evening))"
             r"(?:\s+(?:everyone|all|there|folks))?")

# Each cue regex ends where the name should begin. The cue words match in any
# case; the name itself must be capitalised, which _name_at checks.
_MY_NAME = re.compile(rf"\bmy\s+name(?:\s+is|{_APOS}s)\s+", re.IGNORECASE)
_CALL_ME = re.compile(r"\bcall\s+me\s+", re.IGNORECASE)
_I_AM = re.compile(rf"(?:^|(?<=[.!?,;:]))\s*(?:{_FILLER}\s+)*i(?:{_APOS}m|\s+am)\s+",
                   re.IGNORECASE)
_GREETED_THIS_IS = re.compile(rf"^\s*{_GREETING}[\s,.!]+this\s+is\s+", re.IGNORECASE)
_THIS_IS = re.compile(r"\bthis\s+is\s+", re.IGNORECASE)
_GREETED_START = re.compile(rf"^\s*{_GREETING}[\s,.!]+", re.IGNORECASE)
_LEADING_SPACE = re.compile(r"^\s*")
_FOLLOW_THIS_IS = re.compile(r"\s+(?:here|speaking|from)\b", re.IGNORECASE)
_FOLLOW_START = re.compile(r"\s+(?:here|speaking)\b", re.IGNORECASE)

# Kept as spoken, trailing dot and all: "Dr. Patel", "Mr Smith".
_TITLE = re.compile(r"((?:Dr|Doctor|Professor|Prof|Mrs|Mr|Ms|Miss)\.?)\s+")
_SPACE = re.compile(r"\s+")


def _word_at(text: str, i: int) -> str | None:
    """The name word starting at ``i``: an uppercase letter, then letters (any
    script), apostrophes or hyphens, two characters at least."""
    if i >= len(text) or not text[i].isupper():
        return None
    j = i + 1
    while j < len(text) and (text[j].isalpha() or text[j] in "'’-"):
        j += 1
    return text[i:j] if j - i >= 2 else None


def _stopped(word: str) -> bool:
    return word.lower().replace("’", "'") in STOPWORDS


def _possessive(word: str) -> bool:
    return word.lower().endswith(("'s", "’s"))


def _name_at(text: str, i: int) -> tuple[str, int] | None:
    """Read a name starting at ``i``: an optional title, then one or two name
    words separated only by whitespace. Returns ``(name, end)``, or None when
    what is there is not a name."""
    title = ""
    m = _TITLE.match(text, i)
    if m and _word_at(text, m.end()):
        title, i = m.group(1), m.end()
    first = _word_at(text, i)
    if first is None:
        return None
    words, end = [first], i + len(first)
    gap = _SPACE.match(text, end)
    second = _word_at(text, gap.end()) if gap else None
    if second is not None:
        words.append(second)
    if any(_possessive(w) for w in words) or _stopped(first):
        return None
    if second is not None:
        if _stopped(second):
            words.pop()                  # "Tom From design": keep the first word
        else:
            end = gap.end() + len(second)
    return " ".join(([title] if title else []) + words), end


def detect_name(text: str) -> str | None:
    """The name a speaker gives for themselves in ``text``, or None."""
    text = unicodedata.normalize("NFC", text or "")
    if not text:
        return None
    # 1 and 2: unambiguous wherever they appear.
    for cue in (_MY_NAME, _CALL_ME):
        for m in cue.finditer(text):
            hit = _name_at(text, m.end())
            if hit:
                return hit[0]
    # 3: "I'm X" only opens a clause; "When I'm Deploying" is not an intro.
    for m in _I_AM.finditer(text):
        hit = _name_at(text, m.end())
        if hit:
            return hit[0]
    # 4: "this is X" needs a greeting before it or here/speaking/from after it,
    # so "This is Kubernetes" stays a sentence about Kubernetes.
    m = _GREETED_THIS_IS.match(text)
    if m:
        hit = _name_at(text, m.end())
        if hit:
            return hit[0]
    for m in _THIS_IS.finditer(text):
        hit = _name_at(text, m.end())
        if hit and _FOLLOW_THIS_IS.match(text, hit[1]):
            return hit[0]
    # 5: "Sarah here" / "Tom speaking" at the very start, maybe after a greeting.
    starts = [m.end() for m in (_GREETED_START.match(text),) if m]
    starts.append(_LEADING_SPACE.match(text).end())
    for start in starts:
        hit = _name_at(text, start)
        if hit and _FOLLOW_START.match(text, hit[1]):
            return hit[0]
    return None


def _name_key(name: str) -> list[str]:
    words = [w.lower() for w in name.replace(".", " ").split()]
    while words and words[0] in ("dr", "doctor", "professor", "prof", "mr", "mrs", "ms", "miss"):
        words.pop(0)
    return words


def same_name(a: str, b: str) -> bool:
    """Whether two names are the same person, loosely: "Daniel" matches
    "Daniel Tyukov" and "dr. patel" matches "Patel", but "Dan" is not "Daniel"."""
    ka, kb = _name_key(a), _name_key(b)
    n = min(len(ka), len(kb))
    return n > 0 and ka[:n] == kb[:n]
