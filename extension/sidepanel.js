// Side-panel UI + capture. One AudioContext (resumed under the Start gesture)
// taps two legs:
//   • your microphone -> "Me"
//   • the meeting tab  -> the far end, split by Deepgram diarization into
//     "Speaker 1", "Speaker 2", … (plain "Speaker" while there's one), each
//     relabelled with a name once that voice introduces itself, or once it is
//     the one voice left for the one name left on the call's roster (names.js,
//     roster.js)
// Each leg streams to its own Deepgram connection; drafts come from Claude in
// one of two modes, decided from the transcript on each press of Help: an
// "answer" to a question just put to you, or "points" (first-person talking
// points that carry the conversation on from the last thing said).

const API_MODEL_IDS = { haiku: "claude-haiku-4-5", sonnet: "claude-sonnet-5-5", opus: "claude-opus-5-5" };
// Per-model request settings, all for time to the first word, as in the terminal
// app. Sonnet 5.5 rejects disabled thinking; between_tools is its thinking-off
// setting. Opus 5.5 always thinks, so it runs at low effort with token headroom
// for the thinking, which counts toward max_tokens.
const API_MODEL_OPTIONS = {
  "claude-sonnet-5-5": { thinking: { type: "between_tools" } },
  "claude-opus-5-5": { output_config: { effort: "low" } },
};
const THINKING_HEADROOM = { "claude-opus-5-5": 4096 };

const ANSWER_RULES = `You are my real-time meeting copilot. Read my context and the live transcript, then \
draft MY answer to the other person's latest question.
Rules:
- First person, the words I'd say out loud. No preamble, no meta-commentary.
- Concise and natural: speakable in about 20-40 seconds.
- Be concrete and specific to my context when relevant.
- Several people may be on the call. My lines are labelled "Me". Other voices are labelled \
by name when known (address them by it when that is natural), otherwise "Speaker 1", \
"Speaker 2" and so on.
- If the message has a line starting with "MY EXTRA INSTRUCTION:", follow it closely.`;

const POINTS_RULES = `You are my real-time meeting copilot. Read my context and the live transcript, then \
give me TALKING POINTS I can use to carry the conversation forward from exactly where it is now.
Rules:
- 3 to 5 points. Each is ONE line I can say out loud as-is, in the FIRST PERSON, starting with "- ". \
Nothing else: no preamble, no headings, no meta-commentary.
- Pick up from the last thing said. Build on it, add something concrete from my context the other \
side has not heard yet, or steer toward what I want to cover.
- Make at least one point a question I can ask them, so the conversation keeps moving.
- Be specific to my context; no generic filler.
- If the conversation has not started yet, give me points to open with.
- Several people may be on the call. My lines are labelled "Me". Other voices are labelled \
by name when known (address them by it when that is natural), otherwise "Speaker 1", \
"Speaker 2" and so on.
- If the message has a line starting with "MY EXTRA INSTRUCTION:", follow it closely.`;

function systemRules(mode) { return mode === "points" ? POINTS_RULES : ANSWER_RULES; }

const $ = (id) => document.getElementById(id);
const settings = { deepgramKey: "", anthropicKey: "", model: "sonnet", language: "en", context: "", myName: "" };

// Everything this panel borrows from history.js. A side panel that Chrome kept
// alive across an extension reload still runs the document it was opened with, so
// a page from before history.js existed loads the new sidepanel.js against the old
// script list, which surfaced as "endSession is not defined" thrown from a click
// handler, pointing at entirely the wrong file. Check once, say so plainly, and
// keep transcription working without history rather than dying on Stop.
const HISTORY_API = [
  "beginSession", "noteSessionSource", "recordSession", "flushSession", "endSession", "autoTitleSession",
  "listSessions", "renameSession", "renameSpeaker", "deleteSession", "clearSessions",
  "sessionLines", "sessionText", "sessionMarkdown", "sessionFilename", "sessionMeta",
  "sessionTitleHtml", "speakerLabel", "speakerClass", "speakersHeard", "nameVoice", "escapeHtml",
];
const historyMissing = HISTORY_API.filter((fn) => typeof globalThis[fn] !== "function");
const historyReady = historyMissing.length === 0;
const HISTORY_BROKEN = "History is unavailable: history.js did not load. Close the side panel and reopen it (Chrome keeps the old page alive across an extension reload).";

if (!historyReady) {
  // Stand-ins for the display and naming helpers, so a missing history.js costs
  // you the history pane and nothing else: the live transcript still renders,
  // labels, and takes names.
  globalThis.escapeHtml ||= (s) => String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.speakerLabel ||= (key, ord, names) => (key === "me" ? "Me" : (names && names[key])
    || "Speaker" + (Object.keys(ord || {}).length > 1 ? " " + (ord[key] || 1) : ""));
  globalThis.speakerClass ||= (key) => (key === "me" ? "spk-Me" : "spk-Speaker");
  globalThis.speakersHeard ||= (lines, ord) => [...new Set(lines.map((l) => l.key))]
    .sort((a, b) => (a === "me" ? -1 : b === "me" ? 1 : (ord[a] || 0) - (ord[b] || 0)));
  globalThis.nameVoice ||= (names, sources, key, typed) => {
    const clean = String(typed || "").replace(/\s+/g, " ").trim().slice(0, 40);
    if (key === "me") return;
    if (clean) { names[key] = clean; sources[key] = "user"; } else { delete names[key]; delete sources[key]; }
  };
  console.error("Sparky: history.js missing:", historyMissing.join(", "));
}
// names.js came later still, so a panel kept open across the update can lack it
// too. Voices then stay numbered until you name them by hand.
const NAMES_API = ["detectName", "sameName", "cleanRosterName", "rosterOthers", "matchRoster",
  "unusedRoster", "eliminate", "nameKey"];
if (NAMES_API.some((fn) => typeof globalThis[fn] !== "function")) {
  Object.assign(globalThis, {
    detectName: () => null, sameName: () => false, cleanRosterName: (s) => String(s || "").trim() || null,
    rosterOthers: (me, list) => [...(list || [])], matchRoster: (n) => n, unusedRoster: (n, list) => [...list],
    eliminate: () => ({}), nameKey: (s) => String(s || "").toLowerCase(),
  });
  console.error("Sparky: names.js missing, voices are not named from introductions or the roster");
}

const utterances = [];                        // { key, text, t }
const partials = { me: [], them: [] }; // capture leg -> [{ speaker, text }]
const speakerOrdinals = {};                   // "int:N" -> 1,2,3… in first-heard order
const speakerNames = {};                      // "int:N" -> display name, keyed like speakerOrdinals
const nameSources = {};                       // "int:N" -> "user" (typed) | "auto" (an intro) | "roster" (elimination)
const declined = new Set();                   // voices whose roster name you cleared: elimination leaves them be
// Who else is in the call: names the call page shows, and names you typed into
// the People row. `self` is the page's own label for you.
const roster = { page: [], self: "", typed: [] };
let callTabId = null;                         // the tab being transcribed, which the roster is read from
let rosterTimer = null;
let rosterRound = 0;                          // bumped whenever reads stop: a read still in flight then is stale
const ROSTER_EVERY_MS = 15000;
const ROSTER_KEY = "@roster";                 // the People row's "who is on the call" input
let liveSessionId = null;                     // the history record this call is written to
let hub = null;
let sources = [];                             // [{ stop() }]
let recording = false;                        // drives the "listening…" placeholder
let micAttached = false;
let tabAttached = false;
const frames = { me: 0, them: 0 };     // keyed by LEG, not by speaker
const dgOpen = { me: false, them: false };
const micState = { ok: false, msg: "" };      // mic is best-effort; tab audio is primary

function updateDiag() {
  const st = hub ? hub.state() : "-";
  const rate = hub ? hub.sampleRate : "?";
  const cap = hub && hub.usingWorklet ? (hub.usingWorklet() ? "worklet" : "scriptproc") : "-";
  $("diag").textContent =
    `ctx:${st}@${rate}Hz ${cap} · you ${frames.me}f ${dgOpen.me ? "dg ok" : "dg .."} · ` +
    `them ${frames.them}f ${dgOpen.them ? "dg ok" : "dg .."}`;
}

// ---- speaker identity ----
// A capture leg plus Deepgram's speaker index resolve to a stable key. Labels are
// derived at render time, so a 1:1 call reads "Speaker", every line upgrades to
// numbered labels the moment a second voice is heard, and to a name the moment
// that voice is named. speakerLabel/speakerClass live in history.js so a reopened
// transcript labels itself exactly like the live one.
function speakerKey(leg, speaker) {
  if (leg === "me") return "me";
  return "int:" + (typeof speaker === "number" ? speaker : 0);
}
function registerSpeaker(key) {
  if (key !== "me" && !(key in speakerOrdinals)) speakerOrdinals[key] = Object.keys(speakerOrdinals).length + 1;
}
function labelFor(key) { return speakerLabel(key, speakerOrdinals, speakerNames); }
function cls(key) { return speakerClass(key, speakerOrdinals); }

// ---- names ----
// A voice's name comes, in order of trust, from you (typed), from its own
// introduction, or from the roster by elimination. An introduction is read once
// per voice, matched to the roster ("I'm Sarah" -> "Sarah Chen"), and replaces
// a name elimination gave the same voice. A name typed or introduced that
// elimination gave another voice is taken back: that voice goes back to
// automatic. An intro carrying your own name is your voice leaking into the
// call audio, so it names nobody.
function isMine(name) { return sameName(name, settings.myName) || sameName(name, roster.self); }
function reclaim(key, name) {
  const want = nameKey(matchRoster(name, rosterNow()));
  for (const k of Object.keys(speakerNames)) {
    if (k !== key && nameSources[k] === "roster" && nameKey(speakerNames[k]) === want) {
      delete speakerNames[k];
      delete nameSources[k];
    }
  }
}
function autoName(key, text) {
  if (key === "me") return;
  if (speakerNames[key] && nameSources[key] !== "roster") return;
  const said = detectName(text);
  if (!said || isMine(said)) return;
  const name = matchRoster(said, rosterNow());
  reclaim(key, name);
  speakerNames[key] = name;
  nameSources[key] = "auto";
  declined.delete(key);
}

// The other people in the call: the page's names plus yours typed, minus you
// (the page's label for you, and Your name).
function rosterNow() {
  return rosterOthers(settings.myName, rosterOthers(roster.self, [...roster.page, ...roster.typed]));
}
// Elimination: once every other voice but one is accounted for and one name is
// left, they go together. It never guesses between two (names.js eliminate).
function applyRoster() {
  const voices = speakersHeard(utterances, speakerOrdinals).filter((k) => k !== "me");
  for (const [key, name] of Object.entries(eliminate(voices, speakerNames, rosterNow()))) {
    if (declined.has(key)) continue;
    speakerNames[key] = name;
    nameSources[key] = "roster";
  }
}
function persist() {
  if (historyReady) recordSession(utterances, speakerOrdinals, speakerNames, nameSources, rosterNow());
}

// ---- echo guard ----
// On speakers the mic re-hears the call, and any leak of your voice into the tab
// stream would surface as a brand-new speaker on the call. Both show up as one line
// arriving on both legs within a beat, so drop whichever copy lands second.
const ECHO_WINDOW_MS = 6000;
const ECHO_SIMILARITY = 0.6;
function wordSet(s) {
  return new Set(s.toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean));
}
function similarity(a, b) {
  if (!a.size || !b.size) return 0;
  let shared = 0;
  for (const w of a) if (b.has(w)) shared++;
  return shared / (a.size + b.size - shared);
}
function isEcho(key, text, now) {
  const set = wordSet(text);
  if (set.size < 3) return false;               // keep short backchannels ("yes", "exactly")
  const mine = key === "me";
  for (let i = utterances.length - 1; i >= 0; i--) {
    const u = utterances[i];
    if (now - u.t > ECHO_WINDOW_MS) break;
    if ((u.key === "me") === mine) continue;     // only compare across legs
    if (similarity(set, wordSet(u.text)) >= ECHO_SIMILARITY) return true;
  }
  return false;
}

// ---- settings ----
async function loadSettings() {
  Object.assign(settings, await chrome.storage.local.get(Object.keys(settings)));
  $("deepgramKey").value = settings.deepgramKey || "";
  $("anthropicKey").value = settings.anthropicKey || "";
  $("model").value = settings.model || "sonnet";
  $("language").value = settings.language || "en";
  $("context").value = settings.context || "";
  if ($("myName")) $("myName").value = settings.myName || "";
}
async function saveSettings() {
  Object.assign(settings, {
    deepgramKey: $("deepgramKey").value.trim(),
    anthropicKey: $("anthropicKey").value.trim(),
    model: $("model").value,
    language: $("language").value.trim() || "en",
    context: $("context").value,
    myName: $("myName") ? $("myName").value.replace(/\s+/g, " ").trim() : settings.myName,
  });
  await chrome.storage.local.set(settings);
  rosterChanged();                             // Your name is never on the roster
  $("saveMsg").textContent = "Saved.";
  setTimeout(() => ($("saveMsg").textContent = ""), 1500);
}

// ---- first-run notice ----
// Shown until it is acknowledged once, then remembered. Raising NOTICE_VERSION
// shows it to everyone again, which is what to do when what it says changes.
// 2: it now says the call page's participant names are read.
const NOTICE_VERSION = 2;
async function initNotice() {
  let seen = null;
  try { seen = (await chrome.storage.local.get("noticeAck")).noticeAck; } catch {}
  if (seen === NOTICE_VERSION) $("gate").classList.add("hidden");
  else showNotice();
}
function showNotice() {
  $("ackBox").checked = false;
  $("ackBtn").disabled = true;
  $("gate").classList.remove("hidden");
  if ($("ackBox").focus) $("ackBox").focus();
}
async function ackNotice() {
  $("gate").classList.add("hidden");
  try { await chrome.storage.local.set({ noticeAck: NOTICE_VERSION }); } catch {}
}
function noticeOpen() { return !$("gate").classList.contains("hidden"); }

// ---- transcript ----
function render() {
  renderPeople();
  const box = $("transcript");
  if (!utterances.length && !partials.me.length && !partials.them.length) {
    if (recording) {
      const them = frames.them > 0
        ? `<b>them</b> hearing the call (${frames.them} frames)`
        : `<b>them</b> waiting for the call tab: is audio actually playing in it?`;
      const you = micState.ok
        ? `<b>you</b> mic live (${frames.me} frames)`
        : `<b>you</b> mic off: ${escapeHtml(micState.msg || "not granted")}`;
      box.innerHTML =
        `<div class="muted">Listening…<br>${them}<br>${you}<br>` +
        `Text appears the moment someone <b>speaks</b>. Voices on the call are ` +
        `separated automatically; your own voice needs the mic.</div>`;
    } else {
      box.innerHTML = '<div class="muted">Press Start to transcribe the call. Each voice gets its own label, then a name: from an introduction, or from who the call page shows.</div>';
    }
    return;
  }
  const rows = utterances.map((u) =>
    `<div class="line"><span data-key="${escapeHtml(u.key)}" class="${cls(u.key)}">${escapeHtml(labelFor(u.key))}:</span> ${escapeHtml(u.text)}</div>`);
  for (const leg of ["them", "me"]) {
    for (const seg of partials[leg]) {
      if (!seg.text.trim()) continue;
      rows.push(`<div class="line partial">${escapeHtml(labelFor(speakerKey(leg, seg.speaker)))}: ${escapeHtml(seg.text)} ▌</div>`);
    }
  }
  box.innerHTML = rows.join("");
  box.scrollTop = box.scrollHeight;
}
function addFinal(leg, segments) {
  const now = Date.now();
  for (const seg of segments) {
    const text = seg.text.trim();
    if (!text) continue;
    const key = speakerKey(leg, seg.speaker);
    if (isEcho(key, text, now)) continue;
    registerSpeaker(key);
    utterances.push({ key, text, t: now });
    autoName(key, text);                       // finals only: a partial intro can still change
  }
  partials[leg] = [];
  applyRoster();
  persist();                                   // finals only: partials are guesses
  render();
}
function setPartial(leg, segments) {
  partials[leg] = segments;
  for (const seg of segments) registerSpeaker(speakerKey(leg, seg.speaker));
  render();
}
function setLevel(leg, active) {
  const dot = $(leg === "me" ? "lvlMe" : "lvlThem");   // optional, like every element a stale page may lack
  if (dot) dot.className = "lvldot " + (active ? "on" : "off");
}

// ---- people ----
// One chip per voice heard, Me first, then faint chips for roster names no
// voice has yet. A chip, or a speaker label in a transcript, opens a name input
// in place of the chip, with the roster as suggestions. `editing.sid` says whose
// names those are: null for the live call, or a saved session's id. The live
// row ends in "Add names", for typing who is on the call when the page can't be read.
let editing = null;                           // { sid, key, draft } while a name input is open
let peopleShown = "";
let redrawing = false;                        // true while a redraw replaces the elements, an open input with them

// Chrome blurs a focused input as a redraw removes it, synchronously, while it
// is still in the page. That blur is the redraw, not you leaving the input.
function redraw(el, html) {
  redrawing = true;
  try { el.innerHTML = html; } finally { redrawing = false; }
}

const NAME_SOURCE = {
  user: "Named by you", auto: "Named from their introduction", roster: "Named from who is in the call",
};
function peopleHtml(keys, ordinals, names, nameSrc, sid, people) {
  const open = editing && editing.sid === sid ? editing.key : null;
  const listId = sid === null ? "rosterLive" : `roster-${sid}`;
  const suggest = people.length ? ` list="${escapeHtml(listId)}"` : "";
  const chips = keys.map((key) => {
    const label = escapeHtml(speakerLabel(key, ordinals, names));
    const swatch = `<span class="swatch ${speakerClass(key, ordinals)}"></span>`;
    if (key === "me") return `<span class="person me" title="Your microphone">${swatch}${label}</span>`;
    const auto = escapeHtml(speakerLabel(key, ordinals, null));
    if (key === open) {
      return `<span class="person editing">${swatch}<input class="person-edit" type="text" data-for="${escapeHtml(sid + "|" + key)}" ` +
        `value="${escapeHtml(names[key] || "")}" placeholder="${auto}" aria-label="Name for ${auto}"${suggest} ` +
        `maxlength="40" spellcheck="false" autocomplete="off"></span>`;
    }
    const how = NAME_SOURCE[nameSrc[key]] || "Not named yet";
    return `<button class="person" data-act="name" data-key="${escapeHtml(key)}" title="${how}. Click to rename.">${swatch}${label}</button>`;
  });
  const far = keys.filter((k) => k !== "me");
  for (const name of unusedRoster(far.map((k) => names[k]).filter(Boolean), people)) {
    chips.push(`<span class="person expected" title="In the call, not matched to a voice yet">${escapeHtml(name)}</span>`);
  }
  if (sid === null) {
    chips.push(open === ROSTER_KEY
      ? `<span class="person editing roster-edit"><input class="person-edit" type="text" data-for="null|${ROSTER_KEY}" ` +
        `value="${escapeHtml(roster.typed.join(", "))}" placeholder="Sarah Chen, Marcus Lee" ` +
        `aria-label="Who is on the call, comma separated" spellcheck="false" autocomplete="off"></span>`
      : `<button class="person add" data-key="${ROSTER_KEY}" title="Type who is on the call, for when the call page cannot be read">+ Add names</button>`);
  }
  const hint = open === ROSTER_KEY
    ? '<div class="people-hint">Names, comma separated. Enter saves, Esc cancels. Names the call page shows are added on their own.</div>'
    : open ? '<div class="people-hint">Enter saves, Esc cancels. Leave it empty to go back to the automatic label.</div>' : "";
  const options = people.length
    ? `<datalist id="${escapeHtml(listId)}">${people.map((n) => `<option value="${escapeHtml(n)}"></option>`).join("")}</datalist>` : "";
  return `<div class="people-row">${chips.join("")}</div>${hint}${options}`;
}

// Rewritten only when it changes: render() runs on every partial, and a fresh
// innerHTML would drop keyboard focus from a chip several times a second. An
// open input is left alone entirely, along with whatever is typed in it.
function renderPeople(force) {
  const box = $("people");
  if (!box) return;
  if (!force && editing && editing.sid === null) return;
  const keys = speakersHeard(utterances, speakerOrdinals);
  const html = peopleHtml(keys, speakerOrdinals, speakerNames, nameSources, null, rosterNow());
  if (html === peopleShown && !force) return;
  peopleShown = html;
  redraw(box, html);
}

// What an input opens with: the voice's current name, or the typed roster.
function draftFor(sid, key) {
  if (key === ROSTER_KEY) return roster.typed.join(", ");
  if (sid === null) return speakerNames[key] || "";
  const s = historySessions.find((x) => x.id === sid);
  return (s && s.names && s.names[key]) || "";
}
// Opening a name while another is open saves the other first, as leaving it would.
function startNaming(sid, key) {
  if (!key || key === "me" || (key === ROSTER_KEY && sid !== null)) return;
  const host = sid === null ? $("people") : $("histList");
  const find = () => (host && host.querySelector ? host.querySelector(".person-edit") : null);
  if (editing && editing.sid === sid && editing.key === key) {   // already open: keep what is typed
    const input = find();
    if (input) input.focus();
    return;
  }
  if (editing) commitNaming(editing.draft);
  editing = { sid, key, draft: draftFor(sid, key) };
  renderPeople(true);
  if (historyReady) renderHistory();
  const input = find();
  if (input) { input.focus(); input.select(); }
}
// Keyboard focus goes back to the chip the input stood in for.
function refocusChip(ed) {
  const host = ed.sid === null ? $("people") : $("histList");
  const scope = ed.sid === null ? "" : `.sess[data-id="${ed.sid}"] `;
  const chip = host && host.querySelector ? host.querySelector(`${scope}button[data-key="${ed.key}"]`) : null;
  if (chip) chip.focus();
}
function cancelNaming() {
  const was = editing;
  editing = null;
  renderPeople(true);
  if (historyReady && was && was.sid !== null) renderHistory();
  if (was) refocusChip(was);
}
async function commitNaming(typed) {
  const ed = editing;
  if (!ed) return;
  editing = null;
  if (ed.key === ROSTER_KEY) {
    roster.typed = rosterOthers("", String(typed || "").split(/[,;\n]/));
  } else if (ed.sid === null || ed.sid === liveSessionId) {
    // Clearing a name elimination gave means "not them": do not hand it straight back.
    const clearing = !String(typed || "").trim();
    if (clearing && nameSources[ed.key] === "roster") declined.add(ed.key);
    if (!clearing) { declined.delete(ed.key); reclaim(ed.key, typed); }
    nameVoice(speakerNames, nameSources, ed.key, typed);
  }
  if (ed.sid === null || ed.sid === liveSessionId) {
    applyRoster();
    render();
  }
  if (historyReady) {
    if (ed.sid === null) {
      persist();
      await flushSession();
    } else {
      await renameSpeaker(ed.sid, ed.key, typed);
    }
    await refreshHistory();
  }
  if (!editing) refocusChip(ed);              // unless another input opened meanwhile
}
// Shared by the live People row and the history list. `isOpen` says whether an
// event comes from the input that is open right now, not one already replaced.
function isOpen(el) {
  return Boolean(editing && el && el.classList && el.classList.contains("person-edit")
    && el.dataset && el.dataset.for === `${editing.sid}|${editing.key}`);
}
// True when it handled the key. Enter while an IME is composing picks a
// character, not the name.
function onNameKey(e) {
  if (!e.target || !e.target.classList || !e.target.classList.contains("person-edit")) return false;
  if (e.key === "Enter") {
    if (e.isComposing || e.keyCode === 229) return true;
    e.preventDefault();
    commitNaming(e.target.value);
  } else if (e.key === "Escape") { e.preventDefault(); cancelNaming(); }
  return true;
}
function onNameInput(e) { if (isOpen(e.target)) editing.draft = e.target.value; }
// Clicking or tabbing away saves, as Enter does: an input left open would hold
// the People row still. A blur from a redraw (see redraw) is not leaving it.
function onNameBlur(e) {
  if (redrawing || (e.target && e.target.isConnected === false)) return;
  if (isOpen(e.target)) commitNaming(e.target.value);
}
// While a name is open, pressing on another name keeps focus in the input, so
// the press is not turned into a blur that redraws the row under the pointer
// and loses the click. The click then saves this name and opens that one.
function onNamePress(e) {
  if (!editing || !e.target || !e.target.closest) return;
  if (e.target.closest("[data-key]") && !e.target.closest(".person-edit")) e.preventDefault();
}
function onPeopleClick(e) {
  const chip = e.target && e.target.closest && e.target.closest("[data-key]");
  if (chip) startNaming(null, chip.dataset.key);
}
function onTranscriptClick(e) {
  const label = e.target && e.target.closest && e.target.closest("[data-key]");
  if (label) startNaming(null, label.dataset.key);
}

// ---- the roster, read from the call page ----
// roster.js's readMeetingRoster runs in the call tab, which activeTab allows
// since Sparky was invoked there. At capture start, then every 15 s while
// recording. A read that fails or finds nobody changes nothing; names found are
// added to what was read before, because Meet only keeps the tiles in view in
// the page and someone who has left was still in this call.
async function readRoster() {
  if (callTabId == null || typeof readMeetingRoster !== "function" || !chrome.scripting) return;
  const round = rosterRound;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: callTabId }, func: readMeetingRoster });
    // Stop, or Start of the next call, came first: these names belong to a call that is over.
    if (round !== rosterRound) return;
    const got = res && res.result;
    if (!got || !Array.isArray(got.names) || !got.names.length) return;
    roster.page = rosterOthers("", [...roster.page, ...got.names]);
    if (got.self) roster.self = String(got.self);
    rosterChanged();
  } catch {}
}
function rosterChanged() {
  applyRoster();
  persist();
  render();
}
function watchRoster(tabId) {
  unwatchRoster();
  callTabId = tabId;
  readRoster();
  rosterTimer = setInterval(readRoster, ROSTER_EVERY_MS);
}
function unwatchRoster() {
  if (rosterTimer) clearInterval(rosterTimer);
  rosterTimer = null;
  rosterRound++;
}

// ---- what to draft from ----
// A far-end line that reads as a question: ends in "?" or opens the way a spoken
// question does. Used to skip backchannels ("Right, yes.") when picking what to
// answer. Mirrors meeting_copilot/session.py so both UIs pick the same line.
const QUESTION_RE = /\?\s*$|^(?:so[,\s]+)?(?:and[,\s]+)?(?:who|what|when|where|why|how|which|whose|can|could|would|should|do|does|did|is|are|was|were|will|have|has|tell me|walk me|talk me|describe|explain)\b/i;
const QUESTION_LOOKBACK = 8;   // far-end lines back a question is still worth answering
const RUN_MAX = 3;             // a lead-in plus the question itself

// The far-end line worth answering right now: the newest run of lines from one
// voice (joined, so a lead-in and its question arrive together), unless that run
// is only a backchannel and a real question sits a few lines back.
function latestQuestion() {
  if (!utterances.length) return null;
  const far = [];
  for (let i = 0; i < utterances.length; i++) if (utterances[i].key !== "me") far.push(i);
  if (!far.length) return utterances[utterances.length - 1].text;
  const run = [far[far.length - 1]];
  for (let j = far.length - 2; j >= 0 && run.length < RUN_MAX; j--) {
    const i = far[j];
    if (utterances[i].key !== utterances[run[0]].key || i !== run[0] - 1) break;  // my reply sits between
    run.unshift(i);
  }
  const joined = run.map((i) => utterances[i].text).join(" ");
  if (run.some((i) => QUESTION_RE.test(utterances[i].text))) return joined;
  const older = far.slice(Math.max(0, far.length - QUESTION_LOOKBACK), far.length - run.length);
  for (let j = older.length - 1; j >= 0; j--) {
    if (QUESTION_RE.test(utterances[older[j]].text)) return utterances[older[j]].text;
  }
  return joined;
}
// The newest line from anyone: where the conversation is right now.
function lastLine() { return utterances.length ? utterances[utterances.length - 1].text : null; }

// What one press of Help should draft, from the transcript alone: ["answer", q]
// when the far end has put a question-shaped line to me since I last spoke,
// otherwise ["points", last line] (I just spoke, they only acknowledged, or
// nothing has been said yet). Mirrors Session.decide_help() in the terminal app.
function decideHelp() {
  const since = [];
  for (let i = utterances.length - 1; i >= 0 && since.length < QUESTION_LOOKBACK; i--) {
    if (utterances[i].key === "me") break;
    since.push(utterances[i]);
  }
  if (since.some((u) => QUESTION_RE.test(u.text))) return ["answer", latestQuestion() || ""];
  return ["points", lastLine() || ""];
}
function transcriptText() { return utterances.map((u) => `${labelFor(u.key)}: ${u.text}`).join("\n"); }

// ---- capture ----
function setState(state) {
  recording = state === "recording";
  $("state").textContent = state;
  $("dot").className = "dot " + state;
  $("startBtn").disabled = state === "recording";
  $("stopBtn").disabled = state !== "recording";
  if (state !== "recording") { setLevel("me", false); setLevel("them", false); }
  render();   // reflect listening / idle placeholder immediately
}

function attach(leg, stream, opts) {
  let dg;
  const tap = hub.addSource(stream, (buf) => {
    if (!dg) return;
    dg.sendPcm(buf);
    frames[leg]++;
    if (frames[leg] % 10 === 0) {
      updateDiag();
      // keep the "listening…" frame counts live until real text arrives
      if (!utterances.length && !partials.me.length && !partials.them.length) render();
    }
  }, { playback: opts.playback, onLevel: (a) => setLevel(leg, a) });
  dg = openDeepgram(
    { key: settings.deepgramKey, model: "nova-3", language: settings.language,
      sampleRate: hub.sampleRate, diarize: opts.diarize },
    { onOpen: () => { dgOpen[leg] = true; updateDiag(); },
      onPartial: (segs) => setPartial(leg, segs), onFinal: (segs) => addFinal(leg, segs),
      onError: (m) => status(leg + ": " + m, true) }
  );
  sources.push({ stop() { try { tap.stop(); } catch {} try { dg.close(); } catch {} } });
}

async function micPermissionState() {
  try { return (await navigator.permissions.query({ name: "microphone" })).state; }
  catch { return "unknown"; }
}

// ---- the call tab ----
// Chrome lets tabCapture take a tab only after the extension was invoked on it:
// a click on the Sparky toolbar icon, or its shortcut (Alt+Shift+S, from the
// manifest's _execute_action), while that tab is in front. Nothing inside this
// panel counts, and no host permission (<all_urls> included) replaces it. The
// background reports each invocation, so a recording that is still missing the
// call picks it up right then, with no Stop and Start.

// The meeting tab -> the far end, diarized into separate voices.
async function attachTab(tabId) {
  const resp = await chrome.runtime.sendMessage({ target: "background", cmd: "getStreamId", tabId });
  if (!resp || !resp.ok) throw new Error(resp?.error || "unknown");
  const tab = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: resp.streamId } },
  });
  attach("them", tab, { playback: true, diarize: true });  // playback so you still hear the call
  tabAttached = true;
  if (historyReady) noteSessionSource(resp.tabTitle);
  if (resp.tabId != null) watchRoster(resp.tabId);
  status("Capturing: " + (resp.tabTitle || "active tab"));
}
function onTabCaptureFailed(e) {
  const msg = String(e.message || e);
  if (/not been invoked|activeTab/i.test(msg)) {
    status("Chrome will not let Sparky hear this tab yet. On the call tab, click the Sparky "
      + "icon in the toolbar (under the puzzle piece if it is not pinned), or press Alt+Shift+S. "
      + "The call joins this recording by itself, no restart needed.", true);
  } else {
    status("Tab capture failed: " + msg, true);
  }
}
let tabAttaching = false;
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target !== "sidepanel" || msg.cmd !== "tabInvoked") return;
  if (!recording || !hub || tabAttached || tabAttaching) return;
  tabAttaching = true;
  attachTab(msg.tabId).then(render, onTabCaptureFailed).finally(() => { tabAttaching = false; });
});

// echoCancellation earns its keep when you're on speakers: without it the mic
// re-hears the call and every line from the far end lands twice.
async function attachMic() {
  if (micAttached) return true;
  micState.ok = false; micState.msg = "";
  try {
    const mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    attach("me", mic, { playback: false, diarize: false });
    micAttached = true;
    micState.ok = true;
    $("micBtn").classList.add("hidden");
    return true;
  } catch (e) {
    // Chrome won't show the mic prompt in a side panel, so point at the helper.
    micState.msg = e.name === "NotAllowedError"
      ? "press Fix microphone access below"
      : (e.message || String(e));
    $("micBtn").classList.remove("hidden");
    return false;
  }
}

async function start() {
  if (!settings.deepgramKey) return status("Add your Deepgram key in Settings first.", true);
  setState("recording");
  try {
    hub = await createAudioHub();            // resume() runs here, under the Start gesture
  } catch (e) {
    setState("stopped");
    return status("Audio init failed: " + e.message, true);
  }
  // A new capture session is a new speaker-index space: carrying the old maps over
  // would silently pin a fresh voice to the previous call's label or name.
  // Names typed before a call are for that call; names typed during the last one are not.
  if (utterances.length) roster.typed = [];
  utterances.length = 0;
  partials.me = []; partials.them = [];
  for (const map of [speakerOrdinals, speakerNames, nameSources]) for (const k of Object.keys(map)) delete map[k];
  declined.clear();
  roster.page = []; roster.self = "";
  unwatchRoster();
  callTabId = null;
  editing = null;
  liveSessionId = historyReady ? beginSession({}).id : null;   // the tab title arrives below, once capture starts
  micAttached = false;
  tabAttached = false;
  frames.me = 0; frames.them = 0;
  dgOpen.me = false; dgOpen.them = false;
  updateDiag();

  // your mic -> "Me" (best-effort; the far end is the tab, below)
  if (!(await attachMic())) {
    status("Mic off: " + micState.msg + ". The call tab is still transcribed.", true);
  }
  render();

  try {
    await attachTab();
  } catch (e) {
    onTabCaptureFailed(e);
  }
}

// Teardown is synchronous: the audio must stop the instant you click. Sealing the
// transcript happens after, and the optional Claude retitle after that.
async function stop() {
  unwatchRoster();
  sources.forEach((s) => { try { s.stop(); } catch {} });
  sources = [];
  try { hub && hub.close(); } catch {}
  hub = null;
  micAttached = false;
  tabAttached = false;
  setState("stopped");
  updateDiag();

  if (!historyReady) return status(HISTORY_BROKEN, true);
  const saved = await endSession();
  if (!saved) return;                      // nobody spoke, nothing worth keeping
  await refreshHistory();
  status("Saved to history: " + saved.title);
  const better = await autoTitleSession(saved.id, settings.anthropicKey, API_MODEL_IDS.haiku);
  if (better) { await refreshHistory(); status("Saved to history: " + better); }
}

// ---- history ----
// Saved calls, newest first. Destructive actions arm on the first click and fire
// on the second: window.confirm() blocks the whole side panel, and these
// transcripts are the one thing here you can't get back.
let historySessions = [];
const expanded = new Set();
let renamingId = null;
let armedId = null;                            // id awaiting a confirming second click ("all" = clear)

async function refreshHistory() {
  if (!historyReady) {
    $("histList").innerHTML = `<div class="muted">${escapeHtml(HISTORY_BROKEN)}</div>`;
    return;
  }
  historySessions = await listSessions();
  renderHistory();
}
function sessionCard(s) {
  const open = expanded.has(s.id);
  const head = renamingId === s.id
    ? `<input class="sess-rename" value="${escapeHtml(s.title)}" placeholder="Name this call" aria-label="Call title">`
    : `<button class="sess-title" data-act="toggle" aria-expanded="${open}"><span class="caret"></span>${sessionTitleHtml(s)}</button>`;
  const names = s.names || {};
  const body = open
    ? `<div class="sess-body">${peopleHtml(speakersHeard(s.lines, s.ordinals), s.ordinals, names, s.nameSources || {}, s.id, s.roster || [])}` +
      sessionLines(s).map((l) => {
        const act = l.key === "me" ? "" : ` data-act="name"`;
        return `<div class="line"><span${act} data-key="${escapeHtml(l.key)}" class="${l.cls}">${escapeHtml(l.label)}:</span> ${l.html}</div>`;
      }).join("") + `</div>`
    : "";
  return `<div class="sess" data-id="${escapeHtml(s.id)}">${head}
    <div class="sess-meta">${escapeHtml(sessionMeta(s))}</div>
    <div class="sess-btns">
      <button class="chip" data-act="copy">Copy</button>
      <button class="chip" data-act="download">Download</button>
      <button class="chip" data-act="rename">Rename</button>
      <button class="chip danger" data-act="delete">${armedId === s.id ? "Delete, sure?" : "Delete"}</button>
    </div>${body}</div>`;
}
function renderHistory() {
  // A name half typed into a saved call survives a re-render: a retitle can land
  // a few seconds after Stop, right while you are naming someone.
  const list = $("histList");
  const typing = editing && editing.sid !== null && list.querySelector ? list.querySelector(".person-edit") : null;
  const keep = typing && typing.dataset.for === `${editing.sid}|${editing.key}`
    ? { value: typing.value, focused: document.activeElement === typing } : null;
  $("histCount").textContent = historySessions.length ? ` (${historySessions.length})` : "";
  $("histClear").textContent = armedId === "all" ? "Delete every transcript, sure?" : "Clear all";
  $("histClear").className = "chip danger" + (historySessions.length ? "" : " hidden");
  redraw(list, historySessions.length
    ? historySessions.map(sessionCard).join("")
    : '<div class="muted">Nothing saved yet. Every call you Start is kept here automatically.</div>');
  const input = keep && list.querySelector(".person-edit");
  if (input) { input.value = keep.value; if (keep.focused) input.focus(); }
}

// Side panels can lose document focus, which makes the async clipboard reject.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch {}
    ta.remove();
    return ok;
  }
}

async function onHistoryClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const card = e.target.closest(".sess");
  const s = historySessions.find((x) => x.id === (card && card.dataset.id));
  if (!s) return;
  if (act !== "delete") armedId = null;
  if (act !== "rename") renamingId = null;
  if (act !== "name" && editing && editing.sid !== null) editing = null;

  if (act === "toggle") {
    expanded.has(s.id) ? expanded.delete(s.id) : expanded.add(s.id);
    renderHistory();
  } else if (act === "name") {
    startNaming(s.id, btn.dataset.key);
  } else if (act === "copy") {
    status((await copyText(sessionText(s))) ? "Transcript copied." : "Copy failed. Expand it and select the text.");
    renderHistory();
  } else if (act === "download") {
    const url = URL.createObjectURL(new Blob([sessionMarkdown(s)], { type: "text/markdown" }));
    chrome.downloads.download({ url, filename: sessionFilename(s), saveAs: false }, () => {
      if (chrome.runtime.lastError) status("Download failed: " + chrome.runtime.lastError.message, true);
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    });
    renderHistory();
  } else if (act === "rename") {
    renamingId = s.id;
    renderHistory();
    const input = $("histList").querySelector(".sess-rename");
    if (input) { input.focus(); input.select(); }
  } else if (act === "delete") {
    if (armedId !== s.id) { armedId = s.id; renderHistory(); return; }
    armedId = null;
    await deleteSession(s.id);
    expanded.delete(s.id);
    await refreshHistory();
  }
}
async function onHistoryKey(e) {
  if (onNameKey(e)) return;
  if (!e.target.classList || !e.target.classList.contains("sess-rename")) return;
  if (e.key === "Enter") {
    const id = renamingId;
    renamingId = null;
    await renameSession(id, e.target.value);
    await refreshHistory();
  } else if (e.key === "Escape") {
    renamingId = null;
    renderHistory();
  }
}
async function onClearHistory() {
  if (armedId !== "all") { armedId = "all"; renderHistory(); return; }
  armedId = null;
  await clearSessions();
  expanded.clear();
  await refreshHistory();
}

// ---- drafting (Anthropic streaming) ----
// `anchor` is the latest question in answer mode and the last thing anyone said
// in points mode (empty before the conversation starts).
function buildUserPrompt(anchor, note, mode) {
  const points = mode === "points";
  const me = (settings.myName || "").trim();
  const people = rosterNow();
  const parts = [
    `=== MY CONTEXT ===\n${(settings.context || "(none)").trim()}`,
    ...(me ? [`=== WHO I AM ===\nMy name is ${me}. Lines labelled "Me" are mine.`] : []),
    ...(people.length ? [`PEOPLE IN THIS MEETING: ${people.join(", ")}`] : []),
    `=== CONVERSATION SO FAR ===\n${transcriptText() || "(nothing yet)"}`,
    points
      ? `=== LAST THING SAID (continue from here) ===\n${anchor || "(the conversation has not started yet: give me points to open with)"}`
      : `=== LATEST QUESTION (answer this) ===\n${anchor}`,
  ];
  if (note.trim()) parts.push(`MY EXTRA INSTRUCTION: ${note.trim()}`);
  parts.push(points ? "Now write my talking points:" : "Now write my spoken answer:");
  return parts.join("\n\n");
}

// One draft at a time: a second press while one streams aborts the first, so
// two answers never interleave in the box. The draft that finished is kept for
// the Copy chip.
let draftAbort = null;
let lastDraft = "";
async function help(mode = "auto") {
  let anchor;
  if (mode === "auto") [mode, anchor] = decideHelp();
  else anchor = mode === "points" ? (lastLine() || "") : latestQuestion();
  const points = mode === "points";
  if (!points && !anchor) return status("No question captured yet.", true);
  if (!settings.anthropicKey) return status("Add your Anthropic key in Settings first.", true);
  if (draftAbort) draftAbort.abort();
  const ctl = new AbortController();
  draftAbort = ctl;
  const note = $("note").value;
  $("note").value = "";
  const box = $("answer");
  const head = points ? `From: ${anchor || "(opening the conversation)"}` : `Q: ${anchor}`;
  const show = (body) => { box.innerHTML = `<span class="q">${escapeHtml(head)}</span>${body}`; };
  box.className = "answer thinking";
  show("Drafting…");
  $("copyBtn").classList.add("hidden");
  const model = API_MODEL_IDS[settings.model] || settings.model;
  try {
    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      signal: ctl.signal,
      headers: {
        "x-api-key": settings.anthropicKey,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model, max_tokens: 512 + (THINKING_HEADROOM[model] || 0), system: systemRules(mode),
        messages: [{ role: "user", content: buildUserPrompt(anchor, note, mode) }],
        stream: true, ...API_MODEL_OPTIONS[model],
      }),
    });
    if (!resp.ok) throw new Error(resp.status + ": " + (await resp.text()).slice(0, 200));
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", acc = "", refused = false;
    box.className = "answer";
    while (true) {
      const { done, value } = await reader.read();
      if (done || ctl.signal.aborted) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let ev; try { ev = JSON.parse(data); } catch { continue; }
        if (ev.type === "content_block_delta" && ev.delta?.type === "text_delta") {
          acc += ev.delta.text;
          show(escapeHtml(acc));
        } else if (ev.type === "message_delta" && ev.delta?.stop_reason === "refusal") {
          refused = true;
        }
      }
    }
    if (ctl.signal.aborted) return;            // superseded by a newer press
    if (refused && !acc) throw new Error("Claude declined to draft this one. Press Help again, or pick another model in Settings.");
    lastDraft = acc;
    if (acc) $("copyBtn").classList.remove("hidden");
    status(points ? "Talking points ready. Pick one and say it." : "Answer ready. Read it out.");
  } catch (e) {
    if (ctl.signal.aborted) return;
    box.className = "answer";
    box.innerHTML = `<span class="muted">Draft failed: ${escapeHtml(String(e.message || e))}</span>`;
  } finally {
    if (draftAbort === ctl) draftAbort = null;
  }
}
async function copyDraft() {
  if (!lastDraft) return;
  status((await copyText(lastDraft)) ? "Draft copied." : "Copy failed. Select the text instead.");
}

// h drafts without leaving the call to click, as in the terminal app. Keys
// typed into the note, the context box or a settings field are left alone, and
// so is everything while the first-run notice is up.
function onShortcut(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = ((e.target && e.target.tagName) || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || (e.target && e.target.isContentEditable)) return;
  if (noticeOpen()) return;
  if (e.key === "h") { e.preventDefault(); help(); }
}

function status(msg, isErr) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
}

// The side panel can't raise the mic prompt, so mic.html does it in a real tab.
// Once the grant exists we bind to the LIVE hub: demanding a Stop and Start
// round trip was the dead end that left the "you" leg at 0 frames.
async function onMicBtn() {
  if (recording && hub && !micAttached && (await micPermissionState()) === "granted") {
    if (await attachMic()) {
      status("Mic live. Your voice is labelled Me from here on.");
      render();
      return;
    }
  }
  chrome.tabs.create({ url: chrome.runtime.getURL("mic.html") });
  status("Granted the mic in that tab? Come back and press Fix microphone access again.");
}

// ---- wire up ----
// Elements added after 0.4 are optional, for the same reason as HISTORY_API: a
// panel kept open across an update runs the new script against the old page.
function on(id, type, fn) { const el = $(id); if (el) el.addEventListener(type, fn); }
on("ackBox", "change", (e) => ($("ackBtn").disabled = !e.target.checked));
on("ackBtn", "click", ackNotice);
on("noticeShow", "click", showNotice);
on("startBtn", "click", start);
on("stopBtn", "click", stop);
on("helpBtn", "click", () => help());
on("copyBtn", "click", copyDraft);
document.addEventListener("keydown", onShortcut);
on("micBtn", "click", onMicBtn);
on("saveBtn", "click", saveSettings);
on("context", "change", saveSettings);
on("myName", "change", saveSettings);
on("people", "click", onPeopleClick);
on("people", "keydown", onNameKey);
on("transcript", "click", onTranscriptClick);
on("histList", "click", onHistoryClick);
on("histList", "keydown", onHistoryKey);
for (const id of ["people", "histList"]) {
  on(id, "input", onNameInput);
  on(id, "focusout", onNameBlur);
}
for (const id of ["people", "transcript", "histList"]) on(id, "mousedown", onNamePress);
on("histClear", "click", onClearHistory);
on("histBox", "toggle", () => { if ($("histBox").open) refreshHistory(); });

// The well gives up height when a draft lands above it; keep its newest line in view.
if (typeof ResizeObserver === "function") {
  new ResizeObserver(() => { const box = $("transcript"); box.scrollTop = box.scrollHeight; }).observe($("transcript"));
}

initNotice();
loadSettings();
setState("idle");
refreshHistory();
