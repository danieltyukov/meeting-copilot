// Side-panel UI + capture. One AudioContext (resumed under the Start gesture)
// taps two legs:
//   • your microphone -> "Me"
//   • the meeting tab  -> the far end, split by Deepgram diarization into
//     "Speaker 1", "Speaker 2", … (plain "Speaker" while there's one)
// Each leg streams to its own Deepgram connection; drafts come from Claude in
// one of two modes, decided from the transcript on each press of Help: an
// "answer" to a question just put to you, or "points" (first-person talking
// points that carry the conversation on from the last thing said).

const API_MODEL_IDS = { haiku: "claude-haiku-4-5", sonnet: "claude-sonnet-4-6", opus: "claude-opus-4-8" };

const ANSWER_RULES = `You are my real-time meeting copilot. Read my context and the live transcript, then \
draft MY answer to the other person's latest question.
Rules:
- First person, the words I'd say out loud. No preamble, no meta-commentary.
- Concise and natural: speakable in about 20-40 seconds.
- Be concrete and specific to my context when relevant.
- Several people may be on the call; "Speaker 1/2/..." are different voices.
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
- Several people may be on the call; "Speaker 1/2/..." are different voices.
- If the message has a line starting with "MY EXTRA INSTRUCTION:", follow it closely.`;

function systemRules(mode) { return mode === "points" ? POINTS_RULES : ANSWER_RULES; }

const $ = (id) => document.getElementById(id);
const settings = { deepgramKey: "", anthropicKey: "", model: "sonnet", language: "en", context: "" };

// Everything this panel borrows from history.js. A side panel that Chrome kept
// alive across an extension reload still runs the document it was opened with, so
// a page from before history.js existed loads the new sidepanel.js against the old
// script list — which surfaced as "endSession is not defined" thrown from a click
// handler, pointing at entirely the wrong file. Check once, say so plainly, and
// keep transcription working without history rather than dying on Stop.
const HISTORY_API = [
  "beginSession", "noteSessionSource", "recordSession", "endSession", "autoTitleSession",
  "listSessions", "renameSession", "deleteSession", "clearSessions",
  "sessionLines", "sessionText", "sessionMarkdown", "sessionFilename", "sessionMeta",
  "sessionTitleHtml", "speakerLabel", "speakerClass", "escapeHtml",
];
const historyMissing = HISTORY_API.filter((fn) => typeof globalThis[fn] !== "function");
const historyReady = historyMissing.length === 0;
const HISTORY_BROKEN = "History is unavailable — history.js did not load. Close the side panel and reopen it (Chrome keeps the old page alive across an extension reload).";

if (!historyReady) {
  // Stand-ins for the three display helpers, so a missing history.js costs you the
  // history pane and nothing else — the live transcript still renders and labels.
  globalThis.escapeHtml ||= (s) => String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.speakerLabel ||= (key, ord) => (key === "me" ? "Me"
    : "Speaker" + (Object.keys(ord || {}).length > 1 ? " " + (ord[key] || 1) : ""));
  globalThis.speakerClass ||= (key) => (key === "me" ? "spk-Me" : "spk-Speaker");
  console.error("Sparky: history.js missing —", historyMissing.join(", "));
}

const utterances = [];                        // { key, text, t }
const partials = { me: [], them: [] }; // capture leg -> [{ speaker, text }]
const speakerOrdinals = {};                   // "int:N" -> 1,2,3… in first-heard order
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
// derived at render time, so a 1:1 call reads "Speaker" and every line upgrades
// to numbered labels the moment a second voice is heard. speakerLabel/speakerClass
// live in history.js so a reopened transcript labels itself exactly like the live one.
function speakerKey(leg, speaker) {
  if (leg === "me") return "me";
  return "int:" + (typeof speaker === "number" ? speaker : 0);
}
function registerSpeaker(key) {
  if (key !== "me" && !(key in speakerOrdinals)) speakerOrdinals[key] = Object.keys(speakerOrdinals).length + 1;
}
function labelFor(key) { return speakerLabel(key, speakerOrdinals); }
function cls(key) { return speakerClass(key, speakerOrdinals); }

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
}
async function saveSettings() {
  Object.assign(settings, {
    deepgramKey: $("deepgramKey").value.trim(),
    anthropicKey: $("anthropicKey").value.trim(),
    model: $("model").value,
    language: $("language").value.trim() || "en",
    context: $("context").value,
  });
  await chrome.storage.local.set(settings);
  $("saveMsg").textContent = "Saved.";
  setTimeout(() => ($("saveMsg").textContent = ""), 1500);
}

// ---- transcript ----
function render() {
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
      box.innerHTML = '<div class="muted">Press Start. The call audio is transcribed here, labelled automatically.</div>';
    }
    return;
  }
  const rows = utterances.map(
    (u) => `<div class="line"><span class="${cls(u.key)}">${labelFor(u.key)}:</span> ${escapeHtml(u.text)}</div>`
  );
  for (const leg of ["them", "me"]) {
    for (const seg of partials[leg]) {
      if (!seg.text.trim()) continue;
      rows.push(`<div class="line partial">${labelFor(speakerKey(leg, seg.speaker))}: ${escapeHtml(seg.text)} ▌</div>`);
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
  }
  partials[leg] = [];
  if (historyReady) recordSession(utterances, speakerOrdinals);  // finals only — partials are guesses
  render();
}
function setPartial(leg, segments) {
  partials[leg] = segments;
  for (const seg of segments) registerSpeaker(speakerKey(leg, seg.speaker));
  render();
}
function setLevel(leg, active) {
  (leg === "me" ? $("lvlMe") : $("lvlThem")).className = "lvldot " + (active ? "on" : "off");
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

// ---- access to the call tab ----
// tabCapture only works on a tab the extension was "invoked" on (a click on the
// toolbar icon while that tab is in front) unless it holds all-sites host
// access. The ritual is easy to miss mid-call, so Start asks for the access
// once, and a button offers it again if the capture still fails.
const ALL_SITES = { origins: ["<all_urls>"] };
async function hasTabAccess() {
  try { return await chrome.permissions.contains(ALL_SITES); } catch { return false; }
}
async function requestTabAccess() {
  try { return await chrome.permissions.request(ALL_SITES); } catch { return false; }
}

// The meeting tab -> the far end, diarized into separate voices.
async function attachTab() {
  const resp = await chrome.runtime.sendMessage({ target: "background", cmd: "getStreamId" });
  if (!resp || !resp.ok) throw new Error(resp?.error || "unknown");
  const tab = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: resp.streamId } },
  });
  attach("them", tab, { playback: true, diarize: true });  // playback so you still hear the call
  tabAttached = true;
  $("accessBtn").classList.add("hidden");
  if (historyReady) noteSessionSource(resp.tabTitle);
  status("Capturing: " + (resp.tabTitle || "active tab"));
}
function onTabCaptureFailed(e) {
  const msg = String(e.message || e);
  if (/not been invoked|activeTab/i.test(msg)) {
    $("accessBtn").classList.remove("hidden");
    status("Chrome will not capture this tab yet. Press \"Allow capturing the call tab\" below "
      + "(or click the Sparky toolbar icon while the call tab is in front, then Start).", true);
  } else {
    status("Tab capture failed: " + msg, true);
  }
}
// The click on this button is the user gesture Chrome wants for the permission
// prompt. Once granted, the tab joins the recording already in progress.
async function onAccessBtn() {
  const ok = await requestTabAccess();
  if (!ok) {
    return status("Access not granted. Alternative: click the Sparky toolbar icon while the call "
      + "tab is in front, then press Start.", true);
  }
  if (recording && hub && !tabAttached) {
    try { await attachTab(); render(); } catch (e) { onTabCaptureFailed(e); }
  } else {
    $("accessBtn").classList.add("hidden");
    status("Access granted. Press Start.");
  }
}

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
  // First, while the Start click still counts as a gesture: one-time access to
  // capture whichever tab the call is in.
  if (!(await hasTabAccess())) await requestTabAccess();
  setState("recording");
  try {
    hub = await createAudioHub();            // resume() runs here, under the Start gesture
  } catch (e) {
    setState("stopped");
    return status("Audio init failed: " + e.message, true);
  }
  // A new capture session is a new speaker-index space — carrying the old map over
  // would silently pin a fresh voice to the previous call's label.
  utterances.length = 0;
  partials.me = []; partials.them = [];
  for (const k of Object.keys(speakerOrdinals)) delete speakerOrdinals[k];
  if (historyReady) beginSession({});        // the tab title arrives below, once capture starts
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

// Teardown is synchronous — the audio must stop the instant you click. Sealing the
// transcript happens after, and the optional Claude retitle after that.
async function stop() {
  sources.forEach((s) => { try { s.stop(); } catch {} });
  sources = [];
  try { hub && hub.close(); } catch {}
  hub = null;
  micAttached = false;
  tabAttached = false;
  $("accessBtn").classList.add("hidden");
  setState("stopped");
  updateDiag();

  if (!historyReady) return status(HISTORY_BROKEN, true);
  const saved = await endSession();
  if (!saved) return;                      // nobody spoke — nothing worth keeping
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
    ? `<input class="sess-rename" value="${escapeHtml(s.title)}" placeholder="Name this call">`
    : `<button class="sess-title" data-act="toggle">${open ? "▾" : "▸"} ${sessionTitleHtml(s)}</button>`;
  const body = open
    ? `<div class="sess-body">${sessionLines(s)
        .map((l) => `<div class="line"><span class="${l.cls}">${l.label}:</span> ${l.html}</div>`).join("")}</div>`
    : "";
  return `<div class="sess" data-id="${s.id}">${head}
    <div class="sess-meta">${escapeHtml(sessionMeta(s))}</div>
    <div class="sess-btns">
      <button class="chip" data-act="copy">Copy</button>
      <button class="chip" data-act="download">Download</button>
      <button class="chip" data-act="rename">Rename</button>
      <button class="chip danger" data-act="delete">${armedId === s.id ? "Delete, sure?" : "Delete"}</button>
    </div>${body}</div>`;
}
function renderHistory() {
  $("histCount").textContent = historySessions.length ? ` (${historySessions.length})` : "";
  $("histClear").textContent = armedId === "all" ? "Delete every transcript, sure?" : "Clear all";
  $("histClear").className = "chip danger" + (historySessions.length ? "" : " hidden");
  $("histList").innerHTML = historySessions.length
    ? historySessions.map(sessionCard).join("")
    : '<div class="muted">Nothing saved yet. Every call you Start is kept here automatically.</div>';
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

  if (act === "toggle") {
    expanded.has(s.id) ? expanded.delete(s.id) : expanded.add(s.id);
    renderHistory();
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
  const parts = [
    `=== MY CONTEXT ===\n${(settings.context || "(none)").trim()}`,
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
        model: API_MODEL_IDS[settings.model] || settings.model,
        max_tokens: 512, system: systemRules(mode),
        messages: [{ role: "user", content: buildUserPrompt(anchor, note, mode) }],
        stream: true,
      }),
    });
    if (!resp.ok) throw new Error(resp.status + ": " + (await resp.text()).slice(0, 200));
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", acc = "";
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
        }
      }
    }
    if (ctl.signal.aborted) return;            // superseded by a newer press
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
// typed into the note, the context box or a settings field are left alone.
function onShortcut(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = ((e.target && e.target.tagName) || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || (e.target && e.target.isContentEditable)) return;
  if (e.key === "h") { e.preventDefault(); help(); }
}

function status(msg, isErr) {
  const el = $("status");
  el.textContent = msg;
  el.style.color = isErr ? "var(--red)" : "var(--muted)";
}

// The side panel can't raise the mic prompt, so mic.html does it in a real tab.
// Once the grant exists we bind to the LIVE hub — demanding a Stop → Start round
// trip was the dead end that left the "you" leg at 0 frames.
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
$("ackBox").addEventListener("change", (e) => ($("ackBtn").disabled = !e.target.checked));
$("ackBtn").addEventListener("click", () => $("gate").classList.add("hidden"));
$("startBtn").addEventListener("click", start);
$("stopBtn").addEventListener("click", stop);
$("helpBtn").addEventListener("click", () => help());
$("copyBtn").addEventListener("click", copyDraft);
document.addEventListener("keydown", onShortcut);
$("micBtn").addEventListener("click", onMicBtn);
$("accessBtn").addEventListener("click", onAccessBtn);
$("saveBtn").addEventListener("click", saveSettings);
$("context").addEventListener("change", saveSettings);
$("histList").addEventListener("click", onHistoryClick);
$("histList").addEventListener("keydown", onHistoryKey);
$("histClear").addEventListener("click", onClearHistory);
$("histBox").addEventListener("toggle", () => { if ($("histBox").open) refreshHistory(); });

loadSettings();
setState("idle");
refreshHistory();
