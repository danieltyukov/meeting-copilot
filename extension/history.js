// Transcript history. The side panel is an ordinary page that Chrome may tear
// down at any moment, so a call is written to chrome.storage.local WHILE it
// happens (throttled) rather than at Stop: forgetting to press Stop, or closing
// the panel mid-call, must not cost you the transcript.
//
// A record stores the raw speaker KEY per line plus snapshots of the ordinal
// map and the names map, never the rendered label: labels upgrade
// retroactively ("Speaker" becomes "Speaker 1" the moment a second voice is
// heard, and "Sarah" the moment she introduces herself), so freezing them at
// write time would bake in whatever was true halfway through the call.
//
// Loaded before sidepanel.js, which shares this file's speakerLabel/speakerClass
// so the live view and the history view can't drift apart.

const HISTORY_KEY = "sessions";
const historyLimits = { cap: 50, flushMs: 2000 };
const SPK_CLASSES = ["spk-Speaker", "spk-other", "spk-int3", "spk-int4"];
const TITLE_RULES = `Give this meeting transcript a short title: 3-7 words, no quotes, \
no trailing period, no preamble. Name the topic and the kind of conversation. Reply with the title only.`;

// Call sites where the tab title is worth keeping in a session name.
const APPS = [
  [/google meet|(^|\W)meet(\W|$)/i, "Meet"],
  [/microsoft teams|(^|\W)teams(\W|$)/i, "Teams"],
  [/zoom/i, "Zoom"],
  [/webex/i, "Webex"],
  [/youtube/i, "YouTube"],
];

function historySetLimits(patch) { Object.assign(historyLimits, patch); }

// Quotes are escaped too, not just tags: heuristic titles are literally quoted
// transcript text, and they get interpolated into a value="…" attribute by the
// rename box. Escaping only <>& there would let a spoken line close the attribute.
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

// ---- labels (shared with the live transcript) ----
// `names` is optional: records saved before speakers had names carry none, and
// an unnamed voice keeps the numbered label exactly as before.
function speakerLabel(key, ordinals, names) {
  if (key === "me") return "Me";
  if (names && names[key]) return names[key];
  const o = ordinals || {};
  if (Object.keys(o).length <= 1) return "Speaker";
  return "Speaker " + (o[key] || Object.keys(o).length);
}
function speakerClass(key, ordinals) {
  if (key === "me") return "spk-Me";
  const n = (ordinals && ordinals[key]) || 1;
  return SPK_CLASSES[(n - 1) % SPK_CLASSES.length];
}

// Every voice with at least one line, Me first, then the far end in
// first-heard order. A voice only ever seen in a partial is not listed.
function speakersHeard(lines, ordinals) {
  const o = ordinals || {};
  const spoke = new Set((lines || []).map((l) => l.key));
  const far = Object.keys(o).filter((k) => spoke.has(k)).sort((a, b) => o[a] - o[b]);
  return spoke.has("me") ? ["me", ...far] : far;
}

// A name typed by hand. It always wins and detection never replaces it; an
// empty one hands the voice back to automatic naming. "Me" is never renamed.
function nameVoice(names, sources, key, typed) {
  if (key === "me") return;
  const clean = String(typed || "").replace(/\s+/g, " ").trim().slice(0, 40);
  if (clean) { names[key] = clean; sources[key] = "user"; }
  else { delete names[key]; delete sources[key]; }
}

// ---- storage ----
let current = null;      // the call in progress, or the last one until Start replaces it
let flushTimer = null;
let writeQueue = Promise.resolve();

// Writes are serialized: a throttled flush and a rename can otherwise
// read-modify-write the same array concurrently and lose one of the two.
function enqueue(fn) {
  const next = writeQueue.then(fn, fn);
  writeQueue = next.then(() => {}, () => {});
  return next;
}

async function readSessions() {
  const got = await chrome.storage.local.get(HISTORY_KEY);
  const all = got && got[HISTORY_KEY];
  return Array.isArray(all) ? all : [];
}
async function writeSessions(all) {
  const kept = all.slice(0, historyLimits.cap);
  try {
    await chrome.storage.local.set({ [HISTORY_KEY]: kept });
  } catch (e) {
    // Out of quota: drop the oldest half rather than lose the call in progress.
    await chrome.storage.local.set({ [HISTORY_KEY]: kept.slice(0, Math.max(1, kept.length >> 1)) });
  }
}

function listSessions() { return enqueue(readSessions); }

function beginSession(opts) {
  const startedAt = (opts && opts.startedAt) || Date.now();
  current = {
    id: "s" + startedAt + "-" + Math.random().toString(36).slice(2, 7),
    startedAt, endedAt: null, title: "", titleSource: "heuristic",
    tabTitle: (opts && opts.tabTitle) || "", ordinals: {}, names: {}, nameSources: {}, roster: [], lines: [],
  };
  return current;
}

// Which tab we ended up capturing is only known once tabCapture resolves, a beat
// after the session starts.
function noteSessionSource(tabTitle) {
  if (current && tabTitle) current.tabTitle = tabTitle;
}

// The whole transcript is re-sent on every call; it is a few KB of text, and
// diffing it would buy nothing but a chance to persist a wrong line.
// `nameSources` says, per named voice, "user" (typed), "auto" (from an
// introduction) or "roster" (by elimination); `roster` is who else was in the call.
function recordSession(lines, ordinals, names, nameSources, roster) {
  if (!current) return;
  current.lines = lines.map((u) => ({ key: u.key, text: u.text, t: u.t }));
  current.ordinals = Object.assign({}, ordinals);
  current.names = Object.assign({}, names);
  current.nameSources = Object.assign({}, nameSources);
  current.roster = [...(roster || [])];
  if (flushTimer) return;                       // a write is already due
  flushTimer = setTimeout(() => { flushTimer = null; flushSession(); }, historyLimits.flushMs);
}

function flushSession() {
  if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
  if (!current || !current.lines.length) return Promise.resolve(null);
  const snapshot = JSON.parse(JSON.stringify(current));
  return enqueue(async () => {
    const all = await readSessions();
    const i = all.findIndex((s) => s.id === snapshot.id);
    if (i >= 0) all[i] = snapshot; else all.unshift(snapshot);
    await writeSessions(all);
    return snapshot;
  });
}

// Seals the call: stamps the end, names it, writes it. Returns null for a
// session nobody spoke in, since a stray Start/Stop shouldn't litter the list.
// `current` stays put so a final that lands after Stop still joins this record.
async function endSession(opts) {
  if (!current) return null;
  current.endedAt = Math.max((opts && opts.endedAt) || Date.now(), current.startedAt);
  if (!current.lines.length) { current = null; return null; }
  if (!current.title) current.title = heuristicTitle(current);
  const saved = await flushSession();
  return saved;
}

function renameSession(id, title, source) {
  const clean = String(title || "").replace(/\s+/g, " ").trim().slice(0, 100);
  if (!clean) return Promise.resolve(null);
  return enqueue(async () => {
    const all = await readSessions();
    const s = all.find((x) => x.id === id);
    if (!s) return null;
    s.title = clean;
    s.titleSource = source || "user";
    if (current && current.id === id) { current.title = clean; current.titleSource = s.titleSource; }
    await writeSessions(all);
    return s;
  });
}

// Names a voice in a saved call, as typed in its People row.
function renameSpeaker(id, key, typed) {
  return enqueue(async () => {
    const all = await readSessions();
    const s = all.find((x) => x.id === id);
    if (!s) return null;
    s.names = s.names || {};
    s.nameSources = s.nameSources || {};
    nameVoice(s.names, s.nameSources, key, typed);
    if (current && current.id === id) {
      current.names = Object.assign({}, s.names);
      current.nameSources = Object.assign({}, s.nameSources);
    }
    await writeSessions(all);
    return s;
  });
}

function deleteSession(id) {
  if (current && current.id === id) current = null;
  return enqueue(async () => {
    await writeSessions((await readSessions()).filter((s) => s.id !== id));
  });
}

function clearSessions() {
  current = null;
  return enqueue(() => writeSessions([]));
}

// ---- naming ----
function appSuffix(tabTitle) {
  for (const [re, name] of APPS) if (re.test(tabTitle || "")) return name;
  return "";
}
function clip(text, n) {
  if (text.length <= n) return text;
  const cut = text.slice(0, n);
  const sp = cut.lastIndexOf(" ");
  return (sp > n / 2 ? cut.slice(0, sp) : cut).replace(/[,;:.\s]+$/, "") + "…";
}
// Quoting the first thing asked of me is a poor summary but an honest one: it is
// always recognisable, and it never claims to be more than the transcript's own
// words. autoTitleSession replaces it when a key is available.
function heuristicTitle(s) {
  const line = s.lines.find((l) => l.key !== "me") || s.lines[0];
  const suffix = appSuffix(s.tabTitle);
  if (!line) return suffix || "Untitled session";
  return '"' + clip(line.text.replace(/\s+/g, " ").trim(), 60) + '"' + (suffix ? " - " + suffix : "");
}

async function autoTitleSession(id, apiKey, model) {
  if (!apiKey) return null;
  const all = await listSessions();
  const s = all.find((x) => x.id === id);
  if (!s || !s.lines.length) return null;
  try {
    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: model || "claude-haiku-4-5",
        max_tokens: 32,
        system: TITLE_RULES,
        messages: [{ role: "user", content: sessionTranscript(s).slice(0, 4000) }],
      }),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const text = (data.content || [])
      .filter((b) => b.type === "text").map((b) => b.text).join("")
      .replace(/^[\s"'`]+|[\s"'`.]+$/g, "");
    if (!text) return null;
    await renameSession(id, text, "claude");
    return text;
  } catch {
    return null;                                 // a title is never worth an error toast
  }
}

// ---- reading a saved session ----
function sessionLines(s) {
  return (s.lines || []).map((l) => ({
    key: l.key, text: l.text, t: l.t,
    label: speakerLabel(l.key, s.ordinals, s.names),
    cls: speakerClass(l.key, s.ordinals),
    html: escapeHtml(l.text),
  }));
}
function sessionTitleHtml(s) { return escapeHtml(s.title || "Untitled session"); }
function sessionVoices(s) { return Object.keys(s.ordinals || {}).length; }
// Me, every far-end voice heard, and anyone on the roster no voice was named
// for (they were in the call, whether or not they spoke). Uses names.js's
// roster matching when it is loaded, so "Sarah" accounts for "Sarah Chen".
function sessionPeople(s) {
  const far = speakersHeard(s.lines, s.ordinals).filter((k) => k !== "me");
  const named = far.map((k) => (s.names || {})[k]).filter(Boolean);
  const roster = s.roster || [];
  const left = typeof unusedRoster === "function" ? unusedRoster(named, roster)
    : roster.filter((r) => !named.some((n) => n.toLowerCase() === r.toLowerCase()));
  return ["Me", ...far.map((k) => speakerLabel(k, s.ordinals, s.names)), ...left];
}
function sessionTranscript(s) { return sessionLines(s).map((l) => l.label + ": " + l.text).join("\n"); }

function pad2(n) { return (n < 10 ? "0" : "") + n; }
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function formatWhen(ts) {
  const d = new Date(ts);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function formatDuration(ms) {
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return sec + " sec";
  if (sec < 3600) return Math.round(sec / 60) + " min";
  return Math.floor(sec / 3600) + " h " + Math.round((sec % 3600) / 60) + " min";
}
function sessionMeta(s) {
  const voices = sessionVoices(s);
  const lines = (s.lines || []).length;
  return [
    formatWhen(s.startedAt),
    formatDuration((s.endedAt || s.startedAt) - s.startedAt),
    lines + (lines === 1 ? " line" : " lines"),
    voices + (voices === 1 ? " voice" : " voices"),
  ].join(" · ");
}

function sessionText(s) {
  return s.title + "\n" + sessionMeta(s) + "\n\n" + sessionTranscript(s) + "\n";
}
function sessionMarkdown(s) {
  const head = [
    "# " + s.title, "",
    "- Date: " + formatWhen(s.startedAt),
    "- Duration: " + formatDuration((s.endedAt || s.startedAt) - s.startedAt),
    "- Source: " + (s.tabTitle || "unknown"),
    "- People: " + sessionPeople(s).join(", "),
    "- Voices: " + sessionVoices(s),
    "",
  ].join("\n");
  return head + "\n" + sessionLines(s).map((l) => `**${l.label}:** ${l.text}`).join("\n\n") + "\n";
}
function sessionFilename(s) {
  const d = new Date(s.startedAt);
  const date = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  const slug = (s.title || "session").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").slice(0, 48).replace(/-+$/, "") || "session";
  return `sparky/${date}-${slug}.md`;
}
