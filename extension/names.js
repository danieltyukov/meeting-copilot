// Speaker names. Two sources, one file:
//   • introductions: detectName(text) returns the name a line introduces
//     ("Hi, I'm Sarah, I lead the platform team." -> "Sarah"), or null, and
//     sameName(a, b) says whether two names are the same person, loosely;
//   • the meeting itself (the roster: names the call page shows, or typed):
//     cleanRosterName, rosterOthers, matchRoster and eliminate, at the bottom.
//
// The rules are docs/superpowers/specs/2026-10-03-speaker-names-design.md. The
// terminal app implements them in meeting_copilot/names.py and roster.py, held
// to tests/name_cases.json and tests/roster_cases.json like this file is, so the
// panel and the terminal name the same voices the same way. Change them
// together, the stoplist and the role words included: they are the same lists.
//
// Loaded before sidepanel.js; pure, no DOM, no chrome.* calls.

// Words that are never a name, compared in lower case with a straight
// apostrophe. A capitalised word in a name slot is usually a sentence start, a
// nationality, a day, a month or a product, and those are what this list holds.
const NAME_STOP = new Set(`
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
`.split(/\s+/).filter(Boolean));

const APOS = "['’]";
const FILLER = "(?:and|so|well|yeah|yes|oh|um|uh|okay|ok|hi|hello|hey|actually)";
const GREETING = "(?:hi|hello|hey|hiya|good\\s+(?:morning|afternoon|evening))(?:\\s+(?:everyone|all|there|folks))?";

// Each cue ends where the name should begin. The cue words match in any case;
// the name itself must be capitalised, which wordAt checks.
const CUE_MY_NAME = new RegExp(`\\bmy\\s+name(?:\\s+is|${APOS}s)\\s+`, "giu");
const CUE_CALL_ME = /\bcall\s+me\s+/giu;
const CUE_I_AM = new RegExp(`(?:^|(?<=[.!?,;:]))\\s*(?:${FILLER}\\s+)*i(?:${APOS}m|\\s+am)\\s+`, "giu");
const CUE_GREETED_THIS_IS = new RegExp(`^\\s*${GREETING}[\\s,.!]+this\\s+is\\s+`, "iu");
const CUE_THIS_IS = /\bthis\s+is\s+/giu;
const GREETED_START = new RegExp(`^\\s*${GREETING}[\\s,.!]+`, "iu");
// A follower is a whole word: Python's \b, which counts any letter or digit.
const FOLLOW_THIS_IS = /\s+(?:here|speaking|from)(?![\p{L}\p{N}_])/iuy;
const FOLLOW_START = /\s+(?:here|speaking)(?![\p{L}\p{N}_])/iuy;
// Kept as spoken, trailing dot and all: "Dr. Patel", "Mr Smith".
const TITLE = /((?:Dr|Doctor|Professor|Prof|Mrs|Mr|Ms|Miss)\.?)\s+/y;
const SPACE = /\s+/y;
// Case-sensitive on purpose: with the `i` flag, \p{Lu} would match lowercase too.
const NAME_WORD = /\p{Lu}[\p{L}'’-]*/uy;

function sticky(re, text, i) {
  re.lastIndex = i;
  return re.exec(text);
}

// The name word starting at `i`: an uppercase letter, then letters (any
// script), apostrophes or hyphens, two characters at least.
function wordAt(text, i) {
  const m = sticky(NAME_WORD, text, i);
  return m && m[0].length >= 2 ? m[0] : null;
}
function isStopWord(w) { return NAME_STOP.has(w.toLowerCase().replace(/’/g, "'")); }
function isPossessive(w) { return /['’]s$/i.test(w); }

// Reads a name starting at `i`: an optional title, then one or two name words
// separated only by whitespace. Returns [name, end], or null when what is
// there is not a name. A possessive word ("Sarah's manager") or a stopped
// first word rejects it; a stopped second word ("Tom From design") is dropped.
function nameAt(text, i) {
  let title = "";
  const t = sticky(TITLE, text, i);
  if (t && wordAt(text, t.index + t[0].length)) { title = t[1]; i = t.index + t[0].length; }
  const first = wordAt(text, i);
  if (!first) return null;
  const words = [first];
  let end = i + first.length;
  const gap = sticky(SPACE, text, end);
  const second = gap ? wordAt(text, end + gap[0].length) : null;
  if (second) words.push(second);
  if (words.some(isPossessive) || isStopWord(first)) return null;
  if (second) {
    if (isStopWord(second)) words.pop();
    else end += gap[0].length + second.length;
  }
  return [(title ? [title, ...words] : words).join(" "), end];
}

function firstHit(cue, text, accept) {
  cue.lastIndex = 0;
  for (const m of text.matchAll(cue)) {
    const hit = nameAt(text, m.index + m[0].length);
    if (hit && (!accept || accept(hit))) return hit[0];
  }
  return null;
}
function followedBy(re, text, at) {
  re.lastIndex = at;
  return re.test(text);
}

// The cues, in order; the first that yields a name wins.
function detectName(text) {
  const s = String(text || "").normalize("NFC");
  if (!s) return null;
  // 1 and 2: unambiguous wherever they appear.
  const plain = firstHit(CUE_MY_NAME, s) || firstHit(CUE_CALL_ME, s);
  if (plain) return plain;
  // 3: "I'm X" only opens a clause; "When I'm Deploying" is not an intro.
  const iAm = firstHit(CUE_I_AM, s);
  if (iAm) return iAm;
  // 4: "this is X" needs a greeting before it or here/speaking/from after it,
  // so "This is Kubernetes" stays a sentence about Kubernetes.
  const greeted = CUE_GREETED_THIS_IS.exec(s);
  const hello = greeted && nameAt(s, greeted[0].length);
  if (hello) return hello[0];
  const thisIs = firstHit(CUE_THIS_IS, s, (hit) => followedBy(FOLLOW_THIS_IS, s, hit[1]));
  if (thisIs) return thisIs;
  // 5: "Sarah here" / "Tom speaking" at the very start, maybe after a greeting.
  const starts = [];
  const lead = GREETED_START.exec(s);
  if (lead) starts.push(lead[0].length);
  starts.push(s.length - s.trimStart().length);
  for (const at of starts) {
    const hit = nameAt(s, at);
    if (hit && followedBy(FOLLOW_START, s, hit[1])) return hit[0];
  }
  return null;
}

// Whether two names are the same person, loosely: "Daniel" matches "Daniel
// Tyukov" and "dr. patel" matches "Patel", but "Dan" is not "Daniel".
const NAME_TITLES = new Set(["dr", "doctor", "professor", "prof", "mr", "mrs", "ms", "miss"]);
function personWords(name) {
  const words = String(name || "").replace(/\./g, " ").split(/\s+/).filter(Boolean).map((w) => w.toLowerCase());
  while (words.length && NAME_TITLES.has(words[0])) words.shift();
  return words;
}
function sameName(a, b) {
  const ka = personWords(a), kb = personWords(b);
  const n = Math.min(ka.length, kb.length);
  return n > 0 && ka.slice(0, n).every((w, i) => w === kb[i]);
}

// ---- the roster: who is in the meeting ----

const ROSTER_ROLES = ["host", "co-host", "guest", "external", "presenting", "organizer", "organiser",
  "presenter", "me", "you"];
const ROLE_ALT = ROSTER_ROLES.map((r) => r.replace(/-/g, "\\-")).join("|");
const TRAILING_ROLE = new RegExp(`\\s*(?:\\(\\s*(?:${ROLE_ALT})\\s*\\)|,\\s*(?:${ROLE_ALT}))$`, "i");
const SELF_MARK = /\(\s*(?:you|me)\s*\)/i;
// Professional suffixes go the way roles do, dot or not: "Sarah Chen, PhD",
// "Anil Patel (MD)", "John Smith, Esq.". One on its own is no one's name.
const DEGREE = "(?:phd|md|mba|esq)\\.?";
const TRAILING_SUFFIX = new RegExp(`\\s*(?:\\(\\s*${DEGREE}\\s*\\)|,\\s*${DEGREE})$`, "i");
const BARE_DEGREE = /^(?:phd|md|mba|esq)\.*$/i;   // any trailing dots, as roster.py's rstrip(".")
// The meeting apps' own labels, which sit where names do.
const UI_TEXT = /^(?:add people|participants|people|in this meeting|contributors)(?:\s*\(\d+\))?$/i;
const MAX_NAME = 60;
const LINE_BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/;

// Compare form of a name: lower case, no diacritics, single spaces.
function nameKey(name) {
  return String(name || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase()
    .split(/\s+/).filter(Boolean).join(" ");
}

// A display name as the meeting shows it, tidied, or null when it is not
// someone else's name (you, a role word, an address, a phone number, the app's
// own labels, an icon's ligature name like "mic_off").
function cleanRosterName(raw) {
  const name0 = String(raw || "").split(LINE_BREAK)[0].split(/\s+/).filter(Boolean).join(" ");
  if (SELF_MARK.test(name0)) return null;
  const name = name0.replace(TRAILING_ROLE, "").trim().replace(TRAILING_SUFFIX, "").trim();
  const low = name.toLowerCase();
  if (!name || ROSTER_ROLES.includes(low) || BARE_DEGREE.test(name) || low === "presentation" || name.includes("@") || name.includes("_")
      || UI_TEXT.test(name) || [...name].length > MAX_NAME || !/\p{L}/u.test(name)) {
    return null;
  }
  return name;
}

// The roster cleaned, without duplicates (the first spelling wins) and without
// me: a full match of my name, or its first word when my name is a single word.
function rosterOthers(myName, roster) {
  const mine = nameKey(myName).split(" ").filter(Boolean);
  const out = [];
  const seen = new Set();
  for (const raw of roster || []) {
    const name = cleanRosterName(raw);
    if (name === null) continue;
    const key = nameKey(name);
    if (seen.has(key)) continue;
    seen.add(key);
    const words = key.split(" ");
    const isMe = mine.length && (words.join(" ") === mine.join(" ") || (mine.length === 1 && words[0] === mine[0]));
    if (!isMe) out.push(name);
  }
  return out;
}

// The roster entry a name refers to: a full-name match, or a one-word first
// name that matches exactly one entry. Otherwise the name as given.
function matchRoster(name, roster) {
  const key = nameKey(name);
  if (!key) return name;
  for (const entry of roster || []) if (nameKey(entry) === key) return entry;
  if (!key.includes(" ")) {
    const hits = (roster || []).filter((e) => nameKey(e).split(" ")[0] === key);
    if (hits.length === 1) return hits[0];
  }
  return name;
}

// Roster entries that none of `names` refers to.
function unusedRoster(names, roster) {
  const used = new Set(names.filter(Boolean).map((n) => matchRoster(n, roster)));
  return roster.filter((r) => !used.has(r));
}

// Name the one voice left by the one name left: only when there are as many
// other voices as people on the roster, exactly one voice is unnamed and
// exactly one roster name is unused. It never guesses between two. Returns the
// new assignments ({} or one).
function eliminate(voices, names, roster) {
  const vs = [...new Set(voices || [])];
  const rs = [...(roster || [])];
  const has = (v) => Boolean(names && names[v]);
  if (!rs.length || vs.length !== rs.length) return {};
  const unnamed = vs.filter((v) => !has(v));
  const unused = unusedRoster(vs.filter(has).map((v) => names[v]), rs);
  return unnamed.length === 1 && unused.length === 1 ? { [unnamed[0]]: unused[0] } : {};
}
