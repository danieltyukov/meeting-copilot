// End-to-end-ish test for the side panel's transcription rendering, with NO deps.
//
// It loads the REAL deepgram.js, names.js, history.js and sidepanel.js into a single
// vm context (the same way the browser shares one global across <script> tags), shims just the DOM
// and WebSocket, then pushes real Deepgram "Results" frames through the real
// parser into the real render(). This exercises the exact path that populates the
// live transcript, including diarization, which splits the far end into separate
// voices, and the echo guard that keeps your own words out of that bucket.
//
//   run:  node extension/test_render.cjs

const vm = require("vm");
const fs = require("fs");
const path = require("path");

const extDir = __dirname;

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log("  PASS  " + name); }
  else { console.log("  FAIL  " + name + (extra ? "  →  " + extra : "")); failures++; }
}

// ---- minimal DOM shim ----
// classList is backed by className, the way the browser's is, so a class added
// one way and checked the other agrees. Listeners are kept so a test can fire them.
function makeEl(id) {
  const el = {
    id, _text: "", _html: "", className: "", value: "", disabled: false, checked: false,
    scrollTop: 0, scrollHeight: 100, style: {}, dataset: {}, _on: {},
    addEventListener(type, fn) { (this._on[type] ||= []).push(fn); },
    querySelector() { return null; },
    get textContent() { return this._text; }, set textContent(v) { this._text = v; },
    // Like Chrome: replacing the content that holds the focused element blurs
    // it first, synchronously, while it is still in the page (see `focused`).
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      const t = this._focused;
      if (t) { this._focused = null; fire(this, "focusout", { target: t }); }
      this._html = v;
    },
  };
  const list = () => el.className.split(/\s+/).filter(Boolean);
  el.classList = {
    add(...c) { el.className = [...new Set([...list(), ...c])].join(" "); },
    remove(...c) { el.className = list().filter((x) => !c.includes(x)).join(" "); },
    toggle(c, force) {
      const on = force === undefined ? !list().includes(c) : !!force;
      on ? this.add(c) : this.remove(c);
      return on;
    },
    contains(c) { return list().includes(c); },
  };
  return el;
}
function fire(el, type, ev) {
  for (const fn of el._on[type] || []) fn(Object.assign({ preventDefault() {} }, ev));
}
// A stand-in for the element an event lands on, answering the closest() and
// classList questions the panel's delegated handlers ask.
function fakeTarget({ act, key, sid, cls, value, tag, forKey } = {}) {
  const classes = (cls || "").split(" ").filter(Boolean);
  const self = {
    tagName: tag || "SPAN", value: value || "", dataset: { act, key, for: forKey },
    classList: { contains: (c) => classes.includes(c) },
    closest(sel) {
      if (sel === "[data-act]") return act ? self : null;
      if (sel === "[data-key]") return key ? self : null;
      if (sel === ".sess") return sid ? { dataset: { id: sid } } : null;
      return null;
    },
  };
  return self;
}

// One side panel: its own DOM, storage and runtime, with the given scripts
// loaded into one vm context the way the browser shares one global across
// <script> tags. The gate starts hidden, as it does in sidepanel.html.
function makePanel(opts = {}) {
  const els = {};
  const docListeners = {};
  const store = Object.assign({}, opts.store);
  const runtimeSent = [];
  const runtimeListeners = [];
  // chrome.scripting: what the call page "shows" is panel.script.result; the
  // panel's 15 s timer is recorded rather than run.
  // `hold` leaves a read in flight until script.release(result) answers it.
  const script = { result: null, throws: false, hold: false, release: null };
  const scriptCalls = [];
  const intervals = [];
  const cleared = [];
  // `missing`: ids an older sidepanel.html does not have, for the stale-page case.
  const missing = new Set(opts.missing || []);
  const document = {
    getElementById: (id) => (missing.has(id) ? null : els[id] || (els[id] = makeEl(id))),
    addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
    createElement: () => makeEl("tmp"),
    body: { appendChild() {} },
  };
  document.getElementById("gate").className = "gate hidden";
  const chrome = {
    storage: { local: {
      async get(keys) {
        const o = {};
        const list = typeof keys === "string" ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
        list.forEach((k) => { if (k in store) o[k] = store[k]; });
        return o;
      },
      async set(obj) { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
    } },
    runtime: {
      async sendMessage(m) { runtimeSent.push(m); return { ok: false, error: "test" }; },
      onMessage: { addListener(fn) { runtimeListeners.push(fn); } },
    },
    scripting: {
      async executeScript(opts) {
        scriptCalls.push(opts);
        if (script.throws) throw new Error("Cannot access contents of the page.");
        if (script.hold) return new Promise((r) => { script.release = (result) => r([{ result }]); });
        return [{ result: script.result }];
      },
    },
  };
  const ctx = {
    document, chrome, WebSocket: FakeWS, console,
    JSON, URLSearchParams, TextDecoder, TextEncoder, setTimeout, clearTimeout,
    setInterval: (fn, ms) => intervals.push({ fn, ms }),
    clearInterval: (id) => cleared.push(id),
    navigator: { mediaDevices: { getUserMedia: async () => { throw new Error("no mic in test"); } } },
    fetch: async () => { throw new Error("no fetch in test"); },
    AbortController,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const f of opts.files || ["deepgram.js", "names.js", "roster.js", "history.js", "sidepanel.js"]) {
    vm.runInContext(fs.readFileSync(path.join(extDir, f), "utf8"), ctx, { filename: f });
  }
  const T = () => (els.transcript ? els.transcript._html : "");
  return { ctx, els, store, docListeners, runtimeSent, runtimeListeners, T, script, scriptCalls, intervals, cleared };
}
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

// ---- WebSocket shim (captures the last instance so we can feed it frames) ----
let lastWS = null;
class FakeWS {
  constructor(url, protocols) { this.url = url; this.protocols = protocols; this.readyState = 0; lastWS = this; }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}
FakeWS.OPEN = 1;

// ---- the panel under test: every script, as sidepanel.html loads them ----
const P = makePanel();
const { ctx, els, store, docListeners, runtimeSent, runtimeListeners, T } = P;

// ---- Deepgram frame builders ----
// spec: [[speakerIndex, "some words"], …] -> a diarized word list
function diarizedWords(spec) {
  const out = [];
  for (const [speaker, text] of spec) {
    for (const w of text.split(" ")) out.push({ word: w.toLowerCase(), punctuated_word: w, speaker });
  }
  return out;
}
function feed(ws, alt, opts = {}) {
  ws.onmessage({ data: JSON.stringify({
    type: "Results", is_final: !!opts.final, speech_final: !!opts.final,
    channel: { alternatives: [alt] },
  }) });
}
function connect(config, leg, panel = P) {
  const c = panel.ctx;
  const dg = c.openDeepgram(
    Object.assign({ key: "x", model: "nova-3", language: "en", sampleRate: 48000 }, config),
    { onOpen() {}, onPartial: (s) => c.setPartial(leg, s),
      onFinal: (s) => c.addFinal(leg, s), onError: (m) => { throw new Error(m); } }
  );
  const ws = lastWS;
  ws.readyState = 1;
  if (ws.onopen) ws.onopen();
  return { dg, ws };
}

console.log("\nsidepanel render(): e2e through the real Deepgram parser\n");

// 1) Loading the script already ran setState("idle") -> render(). It must not throw,
//    and must show the idle prompt. (Before the fix, render() referenced an
//    undeclared `recording` and threw a ReferenceError the moment it ran empty.)
check("loads + renders idle without throwing", /Press Start/.test(T()), T());

// 2) Recording with no audio yet: the listening placeholder must render (this is the
//    state the user was stuck in): no throw, shows mic + tab status honestly.
ctx.setState("recording");
check("recording+empty shows 'Listening…'", /Listening/.test(T()), T());
check("recording+empty reports mic off (no mic in test)", /mic off/.test(T()), T());
check("recording+empty reports waiting for the call tab", /waiting for the call/.test(T()), T());

// 3) Query string: diarization is on for the tab leg and off for the mic leg.
const me = connect({}, "me");
check("mic leg asks Deepgram for no diarization", !/diarize/.test(me.ws.url), me.ws.url);
const them = connect({ diarize: true }, "them");
check("tab leg asks Deepgram for diarize=true", /diarize=true/.test(them.ws.url), them.ws.url);

// 4) One voice on the call still reads as a plain "Speaker", with no numbering noise
//    on a 1:1, which is the common case.
feed(them.ws, { transcript: "tell me about", words: diarizedWords([[0, "tell me about"]]) });
check("interim frame shows a live partial", /Speaker:.*tell me about.*▌/s.test(T()), T());
feed(them.ws, { transcript: "tell me about yourself", words: diarizedWords([[0, "Tell me about yourself"]]) }, { final: true });
check("final frame commits an utterance", /class="spk-Speaker">Speaker:<\/span> Tell me about yourself/.test(T()), T());
check("partial cleared after final", !/▌/.test(T()), T());
check("single voice is not numbered", !/Speaker 1/.test(T()), T());

// 5) Your own leg is labelled Me and coloured separately.
// (Apostrophes render as &#39;: escapeHtml covers quotes as well as tags, because
// the same helper feeds value="…" attributes in the history list.)
ctx.addFinal("me", [{ speaker: null, text: "Sure, here's a quick summary." }]);
check("mic leg renders as Me", /spk-Me">Me:<\/span> Sure, here&#39;s a quick summary\./.test(T()), T());
check("latestQuestion skips my own line", ctx.latestQuestion() === "Tell me about yourself", ctx.latestQuestion());

// 6) A second far-end voice appears. Labels must upgrade to numbers RETROACTIVELY:
//    the earlier speaker-0 line is now "Speaker 1", not a stale "Speaker".
feed(them.ws, { transcript: "what about scaling", words: diarizedWords([[1, "What about scaling?"]]) }, { final: true });
check("second voice gets its own label", /Speaker 2:<\/span> What about scaling\?/.test(T()), T());
check("first voice renumbers retroactively", /Speaker 1:<\/span> Tell me about yourself/.test(T()), T());
check("distinct voices get distinct colours",
  /spk-Speaker">Speaker 1/.test(T()) && /spk-other">Speaker 2/.test(T()), T());

// 7) A speaker change INSIDE one Deepgram frame must split into two utterances,
//    not concatenate into one mislabelled blob.
feed(them.ws, { transcript: "and a third joins right here",
  words: diarizedWords([[2, "And a third joins"], [1, "right here."]]) }, { final: true });
check("mid-frame speaker change splits", /Speaker 3:<\/span> And a third joins/.test(T()), T());
check("…and the tail lands on the right voice", /Speaker 2:<\/span> right here\./.test(T()), T());

// 8) Echo guard. On speakers the mic re-hears the call, so the same line arrives on
//    both legs, so the second copy must be dropped rather than double-rendered.
ctx.addFinal("them", [{ speaker: 0, text: "How do you handle schema migrations in production?" }]);
ctx.addFinal("me", [{ speaker: null, text: "how do you handle schema migrations in production" }]);
check("mic echo of the call is dropped",
  (T().match(/schema migrations/g) || []).length === 1, T().match(/schema migrations/g));

// …and symmetrically: if your voice leaks into the tab stream, diarization would
// file you as a brand-new speaker on the call. That copy must be dropped too.
ctx.addFinal("me", [{ speaker: null, text: "I usually run expand and contract with a backfill step." }]);
ctx.addFinal("them", [{ speaker: 3, text: "I usually run expand and contract with a backfill step." }]);
check("my voice leaking into the tab stream is dropped",
  (T().match(/expand and contract/g) || []).length === 1, T().match(/expand and contract/g));
check("the leak did not invent a new speaker", !/Speaker 4/.test(T()), T());

// Short backchannels are NOT echo-suppressed: both sides genuinely say "yes".
ctx.addFinal("them", [{ speaker: 0, text: "Right, yes." }]);
ctx.addFinal("me", [{ speaker: null, text: "Right, yes." }]);
check("short backchannels survive on both legs",
  (T().match(/Right, yes\./g) || []).length === 2, T().match(/Right, yes\./g));

// 9) Claude's view of the conversation is labelled per voice and ordered.
check("transcriptText names each voice",
  /^Speaker 1: Tell me about yourself\nMe: Sure, here's a quick summary\.\nSpeaker 2: What about scaling\?/.test(ctx.transcriptText()),
  JSON.stringify(ctx.transcriptText()));
check("latestQuestion skips the backchannel for the real question",
  ctx.latestQuestion() === "How do you handle schema migrations in production?", ctx.latestQuestion());
check("lastLine is the newest line from anyone", ctx.lastLine() === "Right, yes.", ctx.lastLine());

// 10) HTML escaping (no injection from transcript text).
ctx.addFinal("them", [{ speaker: 0, text: "<script>alert(1)</script>" }]);
check("transcript escapes HTML", /&lt;script&gt;/.test(T()) && !/<script>alert/.test(T()), T());

// 11) Layout: the help/answer box must sit ABOVE the live transcript in the markup
//     (the panel is flex-column, so DOM order is visual order).
const html = fs.readFileSync(path.join(extDir, "sidepanel.html"), "utf8");
check("answer box is rendered above the transcript",
  html.indexOf('id="answer"') < html.indexOf('id="transcript"'),
  `answer@${html.indexOf('id="answer"')} transcript@${html.indexOf('id="transcript"')}`);

// 12) The live transcript is persisted as it happens. This is the wiring check:
//     a real Deepgram frame, through the real parser, must reach storage without
//     anyone pressing Stop. Closing the panel mid-call is the case that matters.
(async () => {
  // 0) The first-run notice: shown once, remembered, shown again only for a new
  //    version of it. It must keep saying what it says, too.
  {
    await tick();
    check("the notice shows on a first open", !els.gate.classList.contains("hidden"), els.gate.className);
    const said = () => els.status && els.status._text;
    const before = said();
    (docListeners.keydown || []).forEach((fn) => fn({ key: "h", target: { tagName: "BODY" }, preventDefault() {} }));
    check("h does nothing while the notice is up", said() === before, said());
    check("Continue waits for the consent box", els.ackBtn.disabled === true);
    fire(els.ackBox, "change", { target: { checked: true } });
    check("ticking it enables Continue", els.ackBtn.disabled === false);
    fire(els.ackBtn, "click", {});
    await tick();
    check("Continue closes the notice", els.gate.classList.contains("hidden"), els.gate.className);
    check("…and remembers it in storage, as this version", store.noticeAck === 2, JSON.stringify(store.noticeAck));

    const again = makePanel({ store: { noticeAck: 2 } });
    await tick();
    check("the next open skips it", again.els.gate.classList.contains("hidden"), again.els.gate.className);
    const bumped = makePanel({ store: { noticeAck: 1 } });
    await tick();
    check("someone who acknowledged the first notice sees it once more, since it now says more",
      !bumped.els.gate.classList.contains("hidden"), bumped.els.gate.className);

    fire(again.els.noticeShow, "click", {});
    check("Settings can bring it back", !again.els.gate.classList.contains("hidden"), again.els.gate.className);
    check("…with the box unticked again", again.els.ackBox.checked === false && again.els.ackBtn.disabled === true);

    const gateHtml = html.slice(html.indexOf('id="gate"'), html.indexOf("<header"));
    check("the notice asks for consent to transcribe", /consent to transcribe/i.test(gateHtml), gateHtml.slice(0, 200));
    check("the notice says it is not hidden from screen sharing", /not hidden from screen sharing/i.test(gateHtml.replace(/<\/?b>/g, "")));
    check("the notice says where audio and prompts go", /Deepgram/.test(gateHtml) && /Anthropic/.test(gateHtml));
    check("the notice says the call page's participant names are read, and nothing else",
      /reads the participant names the call page shows, to label voices\. Nothing else on the page is read\./.test(gateHtml), gateHtml.slice(0, 900));
    check("the gate starts hidden in the markup, so a returning user never sees it flash",
      /id="gate"[^>]*class="[^"]*hidden/.test(html) || /class="[^"]*hidden[^"]*"[^>]*id="gate"/.test(html));
  }

  ctx.historySetLimits({ flushMs: 1 });
  ctx.beginSession({ tabTitle: "Meet - abc-defg-hij" });
  const live = connect({ diarize: true }, "them");
  feed(live.ws, { transcript: "why do you want this role",
    words: diarizedWords([[0, "Why do you want this role?"]]) }, { final: true });
  await new Promise((r) => setTimeout(r, 20));

  let saved = await ctx.listSessions();
  check("a final utterance is persisted mid-call, before Stop",
    saved.length === 1 && saved[0].lines.some((l) => /Why do you want this role/.test(l.text)),
    JSON.stringify(saved.map((s) => s.lines.length)));

  // Partials must NOT be persisted: they are rewritten as Deepgram hears more.
  const before = saved[0].lines.length;
  feed(live.ws, { transcript: "and where do you", words: diarizedWords([[0, "and where do you"]]) });
  await new Promise((r) => setTimeout(r, 20));
  saved = await ctx.listSessions();
  check("partials are not persisted", saved[0].lines.length === before, JSON.stringify(saved[0].lines));

  // The record is the whole live transcript, not just what arrived after beginSession:
  // recordSession is handed the full array every time, so a session started late
  // still captures everything the panel is showing.
  const sealed = await ctx.endSession();
  check("Stop seals the whole live transcript",
    sealed.lines.length === saved[0].lines.length && /Why do you want this role/.test(sealed.lines.at(-1).text),
    JSON.stringify(sealed.lines.at(-1)));
  check("Stop names it from the first question asked of me", /^"Tell me about yourself/.test(sealed.title), sealed.title);
  check("…and tags it with where it was captured", / - Meet$/.test(sealed.title), sealed.title);

  // The history list itself renders: card, meta line, and the expanded transcript.
  await ctx.refreshHistory();
  const H = () => els.histList._html;
  check("the saved call shows up in the history list", /sess-title/.test(H()) && /Tell me about yourself/.test(H()), H().slice(0, 200));
  check("the card carries a meta line", /sess-meta">\d+ \w{3} \d{4}, \d{2}:\d{2}/.test(H()), H().slice(0, 300));
  check("the summary counts saved calls", els.histCount._text === " (1)", JSON.stringify(els.histCount._text));
  check("Clear all is offered once something is saved", !/hidden/.test(els.histClear.className), els.histClear.className);

  // 13) history.js absent: a side panel Chrome kept alive across an extension
  //     reload runs a document whose script list predates it. That threw
  //     "endSession is not defined" out of the Stop handler. The panel must
  //     instead keep transcribing and say plainly what is wrong.
  {
    let threw = null, P2 = null;
    try {
      P2 = makePanel({ files: ["deepgram.js", "sidepanel.js"] });   // names.js and history.js deliberately omitted
    } catch (e) { threw = e; }
    check("the panel still loads without history.js", !threw, threw && threw.message);
    if (!threw) {
      const { ctx: ctx2, els: els2 } = P2;
      ctx2.addFinal("them", [{ speaker: 0, text: "Does the transcript still work?" }]);
      check("…and the live transcript still renders",
        /Speaker:<\/span> Does the transcript still work\?/.test(els2.transcript._html), els2.transcript._html);
      await ctx2.stop();
      check("…and Stop reports the cause instead of throwing",
        /history\.js did not load/.test(els2.status._text), els2.status._text);
      await ctx2.refreshHistory();
      check("…and the history pane says why it is empty",
        /history\.js did not load/.test(els2.histList._html), els2.histList._html);
    }
  }

  // 14) The invariant that keeps the above from ever being the normal path.
  check("sidepanel.html loads history.js before sidepanel.js",
    html.indexOf('src="history.js"') > 0 && html.indexOf('src="history.js"') < html.indexOf('src="sidepanel.js"'),
    `history@${html.indexOf('src="history.js"')} sidepanel@${html.indexOf('src="sidepanel.js"')}`);
  check("sidepanel.html loads names.js before sidepanel.js",
    html.indexOf('src="names.js"') > 0 && html.indexOf('src="names.js"') < html.indexOf('src="sidepanel.js"'),
    `names@${html.indexOf('src="names.js"')} sidepanel@${html.indexOf('src="sidepanel.js"')}`);

  // …and names.js alone missing: voices stay numbered, hand naming still works.
  {
    const N0 = makePanel({ files: ["deepgram.js", "history.js", "sidepanel.js"], store: { noticeAck: 2 } });
    N0.ctx.addFinal("them", [{ speaker: 0, text: "Hi, I'm Sarah, can you hear me?" }]);
    check("without names.js an introduction leaves the voice numbered",
      /spk-Speaker">Speaker:<\/span> Hi, I&#39;m Sarah/.test(N0.T()), N0.T());
    fire(N0.els.people, "click", { target: fakeTarget({ act: "name", key: "int:0" }) });
    fire(N0.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value: "Sarah", tag: "INPUT" }) });
    check("…and naming it by hand still works", /Sarah:<\/span> Hi, I&#39;m Sarah/.test(N0.T()), N0.T());
    let rosterThrew = null;
    try { N0.ctx.watchRoster(3); await tick(); } catch (e) { rosterThrew = e; }
    check("…and without roster.js the call page is simply not read", !rosterThrew && N0.scriptCalls.length === 0,
      rosterThrew ? rosterThrew.message : String(N0.scriptCalls.length));
  }

  // 15) Talking points. A second drafting mode: first-person lines to carry the
  //     conversation on from the last thing said, not a reply to a question.
  {
    check("points mode has its own system rules",
      /TALKING POINTS/i.test(ctx.systemRules("points")) && ctx.systemRules("answer") !== ctx.systemRules("points"),
      ctx.systemRules("points").slice(0, 80));
    const ans = ctx.buildUserPrompt("Why Rust?", "", "answer");
    const pts = ctx.buildUserPrompt("We shipped v2.", "", "points");
    check("answer prompt asks for an answer to the latest question",
      /LATEST QUESTION/.test(ans) && /spoken answer:$/.test(ans.trim()), ans.slice(-120));
    check("points prompt continues from the last thing said",
      /continue from here/i.test(pts) && !/LATEST QUESTION/.test(pts) && /talking points:$/.test(pts.trim()), pts.slice(-160));
    check("points prompt with nothing said yet asks for openers",
      /not started/i.test(ctx.buildUserPrompt("", "", "points")), ctx.buildUserPrompt("", "", "points").slice(-200));
    check("one Help button, no second one to choose between",
      /id="helpBtn"/.test(html) && !/pointsBtn/.test(html), "helpBtn only");

    // The request itself: a fake streaming fetch records what was sent.
    const sent = [];
    function sseBody(text) {
      const lines = [
        `data: ${JSON.stringify({ type: "content_block_delta", delta: { type: "text_delta", text } })}\n`,
        `data: ${JSON.stringify({ type: "message_stop" })}\n`,
      ];
      let i = 0;
      return { getReader: () => ({ read: async () => (i < lines.length
        ? { done: false, value: new TextEncoder().encode(lines[i++]) } : { done: true }) }) };
    }
    ctx.fetch = async (url, init) => {
      const req = { url, body: JSON.parse(init.body), signal: init.signal };
      sent.push(req);
      await new Promise((r) => setTimeout(r, 5));
      return { ok: true, body: sseBody(req.body.system.includes("TALKING") ? "- I can walk through v2." : "Because it is fast.") };
    };
    vm.runInContext("settings.anthropicKey = 'sk-test'", ctx);
    // Top-level consts live in the scripts' shared lexical scope, not on ctx.
    const U = vm.runInContext("utterances", ctx);
    const say = (key, text) => U.push({ key, text, t: Date.now() });
    U.length = 0;
    say("int:0", "How do you handle schema migrations in production?");
    say("me", "Expand and contract, with a backfill.");
    say("int:0", "Right, yes.");

    // Decided from the transcript: a backchannel after my answer means "carry on".
    check("decideHelp: nothing asked since I spoke -> points from the last line",
      JSON.stringify(ctx.decideHelp()) === JSON.stringify(["points", "Right, yes."]), JSON.stringify(ctx.decideHelp()));
    await ctx.help();
    const last = sent[sent.length - 1];
    check("points request carries the points system prompt",
      last && /TALKING POINTS/i.test(last.body.system), last && last.body.system.slice(0, 60));
    check("points request continues from the last line",
      last && /LAST THING SAID/.test(last.body.messages[0].content) && /Right, yes\./.test(last.body.messages[0].content),
      last && last.body.messages[0].content.slice(-200));
    check("the box shows the points once streamed", /I can walk through v2\./.test(els.answer._html), els.answer._html);
    check("the box says what it continued from", /From: Right, yes\./.test(els.answer._html), els.answer._html);
    check("status says the points are ready", /Talking points ready/.test(els.status._text), els.status._text);

    say("int:0", "And how do you roll one back?");
    check("decideHelp: a question since I spoke -> answer it, lead-in included",
      JSON.stringify(ctx.decideHelp()) === JSON.stringify(["answer", "Right, yes. And how do you roll one back?"]), JSON.stringify(ctx.decideHelp()));
    await ctx.help();
    const autoReq = sent[sent.length - 1];
    check("Help answers the pending question on its own",
      !/TALKING/.test(autoReq.body.system) && /LATEST QUESTION[^]*roll one back/.test(autoReq.body.messages[0].content),
      autoReq.body.messages[0].content.slice(-160));
    U.pop();

    await ctx.help("answer");
    const ansReq = sent[sent.length - 1];
    check("answer request still targets the real question",
      /LATEST QUESTION[^]*schema migrations/.test(ansReq.body.messages[0].content), ansReq.body.messages[0].content.slice(-200));
    check("the box shows the answer with its question", /Q: How do you handle schema migrations[^]*Because it is fast\./.test(els.answer._html), els.answer._html);

    // Pressing twice must not interleave two streams: the first is aborted.
    const p1 = ctx.help("points");
    const p2 = ctx.help("answer");
    await Promise.all([p1, p2]);
    const [first, second] = sent.slice(-2);
    check("a newer request aborts the one in flight", first.signal.aborted && !second.signal.aborted,
      `first=${first.signal.aborted} second=${second.signal.aborted}`);
    check("only the newest draft is shown", /Because it is fast\./.test(els.answer._html) && !/walk through v2/.test(els.answer._html), els.answer._html);

    // Keyboard: h and t outside a text field; ignored while typing a note.
    const before = sent.length;
    const press = (key, tagName) => (docListeners.keydown || []).forEach((fn) => fn({ key, target: { tagName }, preventDefault() {} }));
    press("h", "BODY"); await new Promise((r) => setTimeout(r, 20));
    check("pressing h drafts, deciding from the transcript (points here)",
      sent.length === before + 1 && /TALKING/.test(sent[sent.length - 1].body.system), sent.length - before);
    press("t", "BODY"); await new Promise((r) => setTimeout(r, 20));
    check("t is not a shortcut any more", sent.length === before + 1, sent.length - before);
    press("h", "INPUT"); press("h", "TEXTAREA"); await new Promise((r) => setTimeout(r, 20));
    check("keys typed into a field are left alone", sent.length === before + 1, sent.length - before);

    // Nothing said yet: Help gives openers rather than complaining.
    U.length = 0;
    check("decideHelp: nothing yet -> openers", JSON.stringify(ctx.decideHelp()) === JSON.stringify(["points", ""]), JSON.stringify(ctx.decideHelp()));
    await ctx.help();
    check("Help with nothing captured drafts openers", /opening/i.test(els.answer._html) && /walk through v2/.test(els.answer._html), els.answer._html);
    await ctx.help("answer");
    check("a forced answer with nothing captured says so", /No question captured/.test(els.status._text), els.status._text);
  }

  // 16) The question picker, in isolation.
  {
    const U = vm.runInContext("utterances", ctx);
    const say = (key, text) => U.push({ key, text, t: Date.now() });
    U.length = 0;
    say("int:0", "So, one more thing.");
    say("int:0", "How did you test the migration path?");
    check("a lead-in and its question arrive together",
      ctx.latestQuestion() === "So, one more thing. How did you test the migration path?", ctx.latestQuestion());
    U.length = 0;
    say("int:0", "Tell me about the caching layer");
    say("me", "Sure.");
    check("a far-end line with no question mark is still the fallback",
      ctx.latestQuestion() === "Tell me about the caching layer", ctx.latestQuestion());
    U.length = 0;
    say("int:0", "Why Rust?");
    for (let i = 0; i < 12; i++) say("int:0", `statement number ${i}`);
    check("a stale question far back is not dug up", !/Why Rust/.test(ctx.latestQuestion()), ctx.latestQuestion());
    U.length = 0;
  }

  // 17) A toolbar click on the call tab joins it to a recording that is missing it.
  {
    const invoked = async (tabId) => {
      runtimeSent.length = 0;
      runtimeListeners.forEach((fn) => fn({ target: "sidepanel", cmd: "tabInvoked", tabId }));
      await new Promise((r) => setTimeout(r, 0));
      return runtimeSent.filter((m) => m.cmd === "getStreamId");
    };
    vm.runInContext("recording = true; hub = {}; tabAttached = false;", ctx);
    const asked = await invoked(42);
    check("an icon click while recording asks for that tab's stream",
      asked.length === 1 && asked[0].tabId === 42, JSON.stringify(asked));
    check("a refused capture after the click is reported", /Tab capture failed: test/.test(els.status._text), els.status._text);
    vm.runInContext("tabAttached = true;", ctx);
    check("an icon click with the tab already attached does nothing", (await invoked(42)).length === 0);
    vm.runInContext("recording = false; hub = null; tabAttached = false;", ctx);
    check("an icon click while stopped does nothing", (await invoked(42)).length === 0);

    // The shortcut is the no-mouse way to invoke Sparky on the call tab. The
    // refusal message names it, so the two must agree.
    const manifest = JSON.parse(fs.readFileSync(path.join(extDir, "manifest.json"), "utf8"));
    const keyCombo = manifest.commands && manifest.commands._execute_action
      && manifest.commands._execute_action.suggested_key.default;
    check("the toolbar action has a keyboard shortcut", keyCombo === "Alt+Shift+S", JSON.stringify(manifest.commands));
    ctx.onTabCaptureFailed(new Error("Extension has not been invoked for the current page (see activeTab permission)."));
    check("a refused capture says to click the icon or press the shortcut on the call tab",
      /Sparky icon/.test(els.status._text) && els.status._text.includes(keyCombo) && els.status.classList.contains("err"),
      els.status._text);
  }

  // 18) Speaker names. A fresh panel, so the numbered labels above stay as they were.
  {
    const N = makePanel({ store: { noticeAck: 2 } });
    const { ctx: c, els: e, T: NT } = N;
    await tick();
    c.historySetLimits({ flushMs: 1 });
    const sid = vm.runInContext("liveSessionId = beginSession({ tabTitle: 'Meet - Platform sync' }).id", c);
    c.setState("recording");
    const call = connect({ diarize: true }, "them", N);
    const people = () => e.people._html;
    const names = () => JSON.parse(JSON.stringify(vm.runInContext("({ names: speakerNames, sources: nameSources })", c)));

    // A partial intro can still change, so only the final names anyone.
    feed(call.ws, { transcript: "hi i'm sarah", words: diarizedWords([[0, "Hi, I'm Sarah"]]) });
    check("a partial introduction names nobody", /Speaker: Hi, I&#39;m Sarah ▌/.test(NT()) && !names().names["int:0"], NT());
    feed(call.ws, { transcript: "hi i'm sarah i lead the platform team",
      words: diarizedWords([[0, "Hi, I'm Sarah, I lead the platform team."]]) }, { final: true });
    check("a final introduction names the voice",
      /class="spk-Speaker">Sarah:<\/span> Hi, I&#39;m Sarah, I lead the platform team\./.test(NT()), NT());
    check("…marked as automatic", names().sources["int:0"] === "auto", JSON.stringify(names()));
    check("the People row lists her", /data-key="int:0"[^>]*>.*Sarah<\/button>/.test(people()), people());

    // A second voice speaks before it introduces itself, then does: every line it
    // already spoke picks up the name.
    feed(call.ws, { transcript: "thanks for having us", words: diarizedWords([[1, "Thanks for having us today."]]) }, { final: true });
    check("an unnamed second voice is numbered as before", /Speaker 2:<\/span> Thanks for having us today\./.test(NT()), NT());
    feed(call.ws, { transcript: "oh and i'm tom", words: diarizedWords([[1, "Oh, and I'm Tom, I run the data side."]]) }, { final: true });
    check("naming a voice relabels its earlier lines", /Tom:<\/span> Thanks for having us today\./.test(NT()) && !/Speaker 2/.test(NT()), NT());

    feed(call.ws, { transcript: "actually call me sally", words: diarizedWords([[0, "Actually, call me Sally."]]) }, { final: true });
    check("a later introduction does not rename a voice", /Sarah:<\/span> Actually, call me Sally\./.test(NT()) && !/Sally:/.test(NT()), NT());

    c.addFinal("me", [{ speaker: null, text: "Hi both, I'm Daniel." }]);
    check("my own introduction leaves me as Me", /spk-Me">Me:<\/span> Hi both, I&#39;m Daniel\./.test(NT()), NT());
    check("Me comes first in the People row", people().indexOf(">Me<") > 0 && people().indexOf(">Me<") < people().indexOf("Sarah"), people());

    // My name, said on the far end, is my voice leaking into the call audio.
    vm.runInContext("settings.myName = 'Daniel Tyukov'", c);
    feed(call.ws, { transcript: "hi i'm daniel can you hear me", words: diarizedWords([[2, "Hi, I'm Daniel, can you hear me?"]]) }, { final: true });
    check("a far-end intro with my name is ignored", /Speaker 3:<\/span> Hi, I&#39;m Daniel/.test(NT()) && !names().names["int:2"], NT());

    // Renaming by hand: the chip opens an input, Enter saves, and the typed name
    // outranks anything the voice says about itself afterwards.
    const enter = (value, host = e.people) => fire(host, "keydown",
      { key: "Enter", target: fakeTarget({ cls: "person-edit", value, tag: "INPUT" }) });
    fire(e.people, "click", { target: fakeTarget({ act: "name", key: "int:1" }) });
    check("clicking a chip opens its name input", /class="person-edit"[^>]*value="Tom"/.test(people()), people());
    feed(call.ws, { transcript: "and one more", words: diarizedWords([[3, "And one more thing."]]) });
    check("a voice heard while the input is open does not wipe it", /person-edit/.test(people()), people());
    enter("Thomas");
    await tick();
    check("Enter saves a typed name", /Thomas:<\/span> Thanks for having us today\./.test(NT()) && names().sources["int:1"] === "user", NT());
    feed(call.ws, { transcript: "i'm tommy", words: diarizedWords([[1, "I'm Tommy, by the way."]]) }, { final: true });
    check("a typed name beats a later introduction", /Thomas:<\/span> I&#39;m Tommy/.test(NT()) && !/Tommy:/.test(NT()), NT());

    fire(e.transcript, "click", { target: fakeTarget({ key: "int:2" }) });
    check("clicking a speaker label in the transcript opens that voice's input",
      /person-edit[^>]*placeholder="Speaker 3"/.test(people()), people());
    fire(e.people, "keydown", { key: "Escape", target: fakeTarget({ cls: "person-edit", value: "Nope", tag: "INPUT" }) });
    check("Escape cancels without renaming", !/person-edit/.test(people()) && /Speaker 3:<\/span>/.test(NT()), people());
    fire(e.transcript, "click", { target: fakeTarget({ key: "me" }) });
    check("Me cannot be renamed", !/person-edit/.test(people()), people());

    fire(e.people, "click", { target: fakeTarget({ act: "name", key: "int:1" }) });
    enter("   ");
    await tick();
    check("an empty name returns the voice to automatic",
      /Speaker 2:<\/span> Thanks for having us today\./.test(NT()) && !names().sources["int:1"], NT());
    feed(call.ws, { transcript: "sorry i'm tom becker", words: diarizedWords([[1, "Sorry, I'm Tom Becker from data."]]) }, { final: true });
    check("…so its next introduction names it again", /Tom Becker:<\/span> Thanks for having us today\./.test(NT()), NT());

    fire(e.people, "click", { target: fakeTarget({ act: "name", key: "int:2" }) });
    enter("Priya <b>");
    await tick();
    check("a typed name is escaped wherever it shows",
      /Priya &lt;b&gt;:<\/span>/.test(NT()) && !/Priya <b>/.test(NT()) && !/Priya <b>/.test(people()), NT());
    fire(e.people, "click", { target: fakeTarget({ act: "name", key: "int:2" }) });
    enter("Priya");
    await tick();

    // Claude reads the same labels, and knows who I am.
    check("the drafting transcript uses names", /^Sarah: Hi, I'm Sarah/.test(c.transcriptText()) && /\nTom Becker: /.test(c.transcriptText()),
      JSON.stringify(c.transcriptText().slice(0, 120)));
    const prompt = c.buildUserPrompt("Why?", "", "answer");
    check("the prompt says who I am, as the terminal app's does",
      /=== WHO I AM ===\nMy name is Daniel Tyukov\. Lines labelled "Me" are mine\./.test(prompt), prompt.slice(0, 160));
    check("…between my context and the conversation",
      prompt.indexOf("=== MY CONTEXT") < prompt.indexOf("=== WHO I AM") && prompt.indexOf("=== WHO I AM") < prompt.indexOf("=== CONVERSATION"));
    check("the rules explain name labels", /by name when known/.test(c.systemRules("answer")) && /by name when known/.test(c.systemRules("points")));
    vm.runInContext("settings.myName = ''", c);
    check("no name set, no WHO I AM block", !/WHO I AM/.test(c.buildUserPrompt("Why?", "", "answer")));

    // Names are written with the transcript, and the history view uses them.
    await tick();
    let [saved] = await c.listSessions();
    check("names are persisted with the session",
      saved && saved.names["int:0"] === "Sarah" && saved.names["int:1"] === "Tom Becker" && saved.names["int:2"] === "Priya",
      JSON.stringify(saved && saved.names));
    check("…with where each came from",
      saved && saved.nameSources["int:0"] === "auto" && saved.nameSources["int:2"] === "user", JSON.stringify(saved && saved.nameSources));

    await c.refreshHistory();
    fire(e.histList, "click", { target: fakeTarget({ act: "toggle", sid }) });
    const H = () => e.histList._html;
    check("an opened history entry labels lines by name", /Sarah:<\/span> Hi, I&#39;m Sarah/.test(H()), H().slice(0, 400));
    check("…and has its own People row", /class="people-row"/.test(H()) && /data-key="int:1"[^>]*>.*Tom Becker<\/button>/.test(H()), H().slice(0, 600));

    fire(e.histList, "click", { target: fakeTarget({ act: "name", key: "int:0", sid }) });
    check("a label in a history entry opens a name input there", /person-edit[^>]*value="Sarah"/.test(H()), H().slice(0, 600));
    e.histList._focused = fakeTarget({ cls: "person-edit", value: "Sar", tag: "INPUT", forKey: `${sid}|int:0` });
    await c.refreshHistory();                    // a retitle landing mid-edit redraws the list
    check("a redraw of the list while a name is typed there does not save or close it",
      /person-edit/.test(H()) && vm.runInContext("editing && editing.key", c) === "int:0" && !/Sar:<\/span>/.test(NT()), H().slice(0, 300));
    enter("Sarah Lin", e.histList);
    await tick(40);
    [saved] = await c.listSessions();
    check("renaming in history persists to storage", saved.names["int:0"] === "Sarah Lin" && saved.nameSources["int:0"] === "user",
      JSON.stringify(saved.names));
    check("…and relabels the live transcript of the same call", /Sarah Lin:<\/span> Hi, I&#39;m Sarah/.test(NT()), NT());
    check("…and survives the next live write", (c.addFinal("them", [{ speaker: 0, text: "Shall we start?" }]), await tick(),
      (await c.listSessions())[0].names["int:0"] === "Sarah Lin"));

    const md = c.sessionMarkdown((await c.listSessions())[0]);
    check("the Markdown lists the people", /^- People: Me, Sarah Lin, Tom Becker, Priya$/m.test(md), md.slice(0, 300));
    check("the Markdown bolds names", /\*\*Sarah Lin:\*\* Hi, I'm Sarah/.test(md), md);
    check("Copy uses names too", /\nTom Becker: Thanks for having us today\./.test(c.sessionText((await c.listSessions())[0])));
  }

  // 19) Names from the meeting: the roster. The call page says who is in the
  //     call; elimination names the one voice left for the one name left.
  {
    const finalFrom = (ws, speaker, text) => feed(ws, { transcript: text.toLowerCase(), words: diarizedWords([[speaker, text]]) }, { final: true });
    const who = (c) => JSON.parse(JSON.stringify(vm.runInContext("({ names: speakerNames, sources: nameSources })", c)));

    // A 1:1 call, read from the page at capture start.
    const R = makePanel({ store: { noticeAck: 2 } });
    await tick();
    R.ctx.setState("recording");
    const one = connect({ diarize: true }, "them", R);
    R.script.result = { names: ["Sarah Chen (Host)", "Test User", "You"], self: "Test User" };
    R.ctx.watchRoster(42);
    await tick();
    const call0 = R.scriptCalls[0];
    check("capture start reads the roster from the call tab",
      R.scriptCalls.length === 1 && call0.target.tabId === 42 && call0.func === R.ctx.readMeetingRoster, JSON.stringify(call0 && call0.target));
    check("…and reads it again every 15 s", R.intervals.some((i) => i.ms === 15000), JSON.stringify(R.intervals.map((i) => i.ms)));
    check("the page's label for me, and its roles, are left out", JSON.stringify(R.ctx.rosterNow()) === '["Sarah Chen"]',
      JSON.stringify(R.ctx.rosterNow()));
    check("someone in the call with no voice yet shows as expected",
      /class="person expected"[^>]*>Sarah Chen</.test(R.els.people._html), R.els.people._html);
    finalFrom(one.ws, 0, "Thanks for joining, shall we start?");
    check("1:1: the only far-end voice takes the only name, on its first final",
      /Sarah Chen:<\/span> Thanks for joining/.test(R.T()) && who(R.ctx).sources["int:0"] === "roster", R.T());
    check("…and is no longer listed as expected", !/person expected/.test(R.els.people._html), R.els.people._html);
    finalFrom(one.ws, 0, "Oh, I'm Priya, Sarah could not make it.");
    check("an introduction replaces an elimination name on its own voice",
      /Priya:<\/span> Thanks for joining/.test(R.T()) && who(R.ctx).sources["int:0"] === "auto", R.T());
    check("…and the roster name it freed is expected again", /person expected[^>]*>Sarah Chen</.test(R.els.people._html));

    R.script.throws = true;
    let threw = null;
    try { await R.ctx.readRoster(); } catch (e) { threw = e; }
    check("a page that cannot be read changes nothing", !threw && JSON.stringify(R.ctx.rosterNow()) === '["Sarah Chen"]');
    R.script.throws = false;
    R.script.result = { names: [], self: "" };
    await R.ctx.readRoster();
    check("…nor does one that shows nobody", JSON.stringify(R.ctx.rosterNow()) === '["Sarah Chen"]');
    await R.ctx.stop();
    check("Stop stops the roster reads", R.cleared.length >= 1, JSON.stringify(R.cleared));

    // Clearing a name elimination gave does not hand it straight back.
    const D = makePanel({ store: { noticeAck: 2 } });
    await tick();
    const dCall = connect({ diarize: true }, "them", D);
    D.script.result = { names: ["Sarah Chen"], self: "" };
    D.ctx.watchRoster(5);
    await tick();
    finalFrom(dCall.ws, 0, "Can everyone hear me?");
    fire(D.els.people, "click", { target: fakeTarget({ act: "name", key: "int:0" }) });
    check("a rename input offers the roster as suggestions",
      /class="person-edit"[^>]*list="rosterLive"/.test(D.els.people._html) && /<datalist id="rosterLive"><option value="Sarah Chen">/.test(D.els.people._html),
      D.els.people._html);
    fire(D.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value: "", tag: "INPUT" }) });
    await tick();
    finalFrom(dCall.ws, 0, "Hello?");
    check("clearing an elimination name keeps it off that voice", /spk-Speaker">Speaker:<\/span> Hello\?/.test(D.T()), D.T());
    const declined = () => vm.runInContext("[...declined]", D.ctx);
    const rename = (value) => {
      fire(D.els.people, "click", { target: fakeTarget({ act: "name", key: "int:0" }) });
      fire(D.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value, tag: "INPUT" }) });
    };
    rename("Bob");
    await tick();
    check("naming a declined voice lifts the decline", declined().length === 0, JSON.stringify(declined()));
    rename("");
    await tick();
    finalFrom(dCall.ws, 0, "Still there?");
    check("…so clearing a typed name leaves it fully automatic, elimination included",
      /Sarah Chen:<\/span> Still there\?/.test(D.T()) && who(D.ctx).sources["int:0"] === "roster", D.T());
    rename("");
    await tick();
    check("(setup) declined again", declined().join() === "int:0", JSON.stringify(declined()));
    finalFrom(dCall.ws, 0, "Sorry, I'm Sarah, I should have said.");
    check("an introduction still names a declined voice, and lifts the decline",
      /Sarah Chen:<\/span> Sorry, I&#39;m Sarah/.test(D.T()) && who(D.ctx).sources["int:0"] === "auto" && declined().length === 0, D.T());

    // A bigger call: intros are matched to the roster, the last voice is named
    // by elimination, and an intro that claims that name takes it back.
    const Q = makePanel({ store: { noticeAck: 2 } });
    await tick();
    Q.ctx.historySetLimits({ flushMs: 1 });
    vm.runInContext("liveSessionId = beginSession({ tabTitle: 'Meet' }).id", Q.ctx);
    const qCall = connect({ diarize: true }, "them", Q);
    Q.script.result = { names: ["Sarah Chen", "Marcus Lee"], self: "" };
    Q.ctx.watchRoster(7);
    await tick();
    finalFrom(qCall.ws, 0, "Hi, I'm Sarah, I lead the platform team.");
    check("an introduction is matched to the roster's full name",
      /Sarah Chen:<\/span> Hi, I&#39;m Sarah/.test(Q.T()) && who(Q.ctx).sources["int:0"] === "auto", Q.T());
    finalFrom(qCall.ws, 1, "Thanks, good to be here.");
    check("the last voice gets the last name", /Marcus Lee:<\/span> Thanks, good to be here\./.test(Q.T()) &&
      who(Q.ctx).sources["int:1"] === "roster", Q.T());
    finalFrom(qCall.ws, 2, "Hi all, I'm Marcus, sorry I'm late.");
    check("an intro claiming an elimination name takes it",
      /Marcus Lee:<\/span> Hi all, I&#39;m Marcus/.test(Q.T()) && who(Q.ctx).sources["int:2"] === "auto", Q.T());
    check("…and the voice that had it goes back to automatic",
      /Speaker 2:<\/span> Thanks, good to be here\./.test(Q.T()) && !who(Q.ctx).names["int:1"], Q.T());

    // A typed name outranks elimination the same way: typing the name another
    // voice was given frees that voice.
    fire(Q.els.people, "click", { target: fakeTarget({ act: "name", key: "int:1" }) });
    fire(Q.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value: "Wei", tag: "INPUT" }) });
    await tick();
    finalFrom(qCall.ws, 3, "Can I jump in here?");
    check("(setup) a fourth voice, unnamed", /Speaker 4:<\/span> Can I jump in here\?/.test(Q.T()), Q.T());
    const qBefore = who(Q.ctx);
    vm.runInContext("speakerNames['int:3'] = 'Marcus Lee'; nameSources['int:3'] = 'roster';", Q.ctx);
    fire(Q.els.people, "click", { target: fakeTarget({ act: "name", key: "int:2" }) });
    fire(Q.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value: "Marcus Lee", tag: "INPUT" }) });
    await tick();
    check("typing a name elimination gave another voice takes it back from that voice",
      who(Q.ctx).sources["int:2"] === "user" && !who(Q.ctx).names["int:3"] && qBefore.sources["int:1"] === "user", JSON.stringify(who(Q.ctx)));

    const qPrompt = Q.ctx.buildUserPrompt("Why?", "", "answer");
    check("the prompt names the people in the meeting",
      /^PEOPLE IN THIS MEETING: Sarah Chen, Marcus Lee$/m.test(qPrompt) && qPrompt.indexOf("PEOPLE IN") < qPrompt.indexOf("=== CONVERSATION"),
      qPrompt.slice(0, 200));
    await tick();
    const [qSaved] = await Q.ctx.listSessions();
    check("the session keeps its roster", qSaved && JSON.stringify(qSaved.roster) === '["Sarah Chen","Marcus Lee"]', JSON.stringify(qSaved && qSaved.roster));
    check("the Markdown People line is Me, the voices and the roster",
      /^- People: Me, Sarah Chen, Wei, Marcus Lee, Speaker 4$/m.test(Q.ctx.sessionMarkdown(qSaved)), Q.ctx.sessionMarkdown(qSaved).slice(0, 220));

    // Typed names, for when the page cannot be read.
    const W = makePanel({ store: { noticeAck: 2 } });
    await tick();
    check("no roster, no PEOPLE line", !/PEOPLE IN THIS MEETING/.test(W.ctx.buildUserPrompt("Why?", "", "answer")));
    check("the People row offers to add names before anyone speaks", /data-key="@roster"[^>]*>\+ Add names</.test(W.els.people._html), W.els.people._html);
    vm.runInContext("settings.myName = 'Daniel'", W.ctx);
    fire(W.els.people, "click", { target: fakeTarget({ key: "@roster" }) });
    check("Add names opens a comma separated input", /roster-edit[^>]*><input class="person-edit"/.test(W.els.people._html), W.els.people._html);
    fire(W.els.people, "keydown", { key: "Enter", target: fakeTarget({ cls: "person-edit", value: "Sarah Chen, Marcus Lee (Guest); Daniel Tyukov", tag: "INPUT" }) });
    await tick();
    check("typed names join the roster, tidied, without me", JSON.stringify(W.ctx.rosterNow()) === '["Sarah Chen","Marcus Lee"]',
      JSON.stringify(W.ctx.rosterNow()));
    check("…show as expected", (W.els.people._html.match(/person expected/g) || []).length === 2, W.els.people._html);
    check("…and reach the prompt", /^PEOPLE IN THIS MEETING: Sarah Chen, Marcus Lee$/m.test(W.ctx.buildUserPrompt("Why?", "", "answer")));
    fire(W.els.people, "click", { target: fakeTarget({ key: "@roster" }) });
    check("reopening shows what was typed", /value="Sarah Chen, Marcus Lee, Daniel Tyukov"/.test(W.els.people._html), W.els.people._html);
    fire(W.els.people, "keydown", { key: "Escape", target: fakeTarget({ cls: "person-edit", value: "x", tag: "INPUT" }) });
  }

  // 20) roster.js on its own: run where it runs, in a page, with nothing but
  //     `document` (it is serialized, so anything from outside would be missing).
  {
    // Just enough DOM for the selectors it uses: tag, .class, [attr], [attr=v], [attr*=v], and lists of them.
    function node(attrs, kids = [], text = "", tag = "div") {
      const n = {
        attrs, kids, text, tag,
        getAttribute: (a) => (a in attrs ? attrs[a] : null),
        hasAttribute: (a) => a in attrs,
        matches: (sel) => matches(n, sel),
        get textContent() { return [text, ...kids.map((k) => k.textContent)].filter(Boolean).join("\n"); },
        querySelectorAll(sel) {
          const out = [];
          const walk = (x) => { for (const k of x.kids) { if (matches(k, sel)) out.push(k); walk(k); } };
          walk(n);
          return out;
        },
        querySelector(sel) { return n.querySelectorAll(sel)[0] || null; },
      };
      return n;
    }
    function matches(n, sel) {
      return sel.split(",").some((one) => {
        one = one.trim();
        let m;
        if (/^[a-z][a-z0-9]*$/.test(one)) return n.tag === one;
        if ((m = one.match(/^\.([\w-]+)$/))) return (n.attrs.class || "").split(/\s+/).includes(m[1]);
        if ((m = one.match(/^\[([\w-]+)\]$/))) return m[1] in n.attrs;
        if ((m = one.match(/^\[([\w-]+)(\*?)=["']?([^"'\]]*)["']?\]$/))) {
          const v = n.attrs[m[1]];
          return v != null && (m[2] ? v.includes(m[3]) : v === m[3]);
        }
        throw new Error("selector not in the test DOM: " + one);
      });
    }
    const tile = (id, name, self) => node(Object.assign({ "data-participant-id": id }, self ? { "data-self-name": self } : {}),
      [node({ class: "notranslate" }, [], name), node({ class: "tile-badge" }, [], "Muted")]);
    const page = node({}, [
      tile("p0", "Test User", "Test User"),                              // the fake call page the e2e uses
      tile("p1", "Sarah Chen"), tile("p2", "Marcus Lee"), tile("p3", "Daniel Okafor"),
      node({ role: "list", "aria-label": "Participants" }, [
        node({ role: "listitem", "aria-label": "Wei Chen" }, [], "Wei Chen\nHost"),
        node({ role: "listitem", "aria-label": "Sarah Chen" }, [], "Sarah Chen"),
      ]),
      node({ role: "list", "aria-label": "Chat messages" }, [node({ role: "listitem" }, [], "Sarah Chen: shall we start?")]),
      node({ class: "participants-item__display-name" }, [], "Priya Raman"),
      node({ "data-tid": "roster-list" }, [node({}, [], "In this meeting (3)"),
        node({ "data-tid": "participant-item", title: "Anil Patel" }, [], "Anil Patel")]),
      node({ class: "chat" }, [], "Marcus: I think we should ship it"),
      // junk that sits where names do
      node({ "data-participant-id": "p4" }, [node({ class: "google-material-icons" }, [], "mic_off", "i"),
        node({}, [], "Wei Zhang"), node({}, [], "Muted")]),
      node({ role: "list", "aria-label": "People in the call" }, [
        node({ role: "listitem" }, [node({}, [node({}, [], "person_add", "i"), node({}, [], "Add people", "span")], "", "button")]),
        node({ role: "listitem" }, [node({}, [], "Copy joining info", "button")]),
        node({ role: "listitem" }, [node({}, [], "In the meeting", "h3")]),
        node({ role: "listitem", "aria-label": "Lena Ortiz" }, [node({}, [], "person", "i"), node({}, [], "Lena Ortiz", "span"),
          node({}, [node({}, [], "more_vert", "i")], "", "button")]),
      ]),
      node({ "data-tid": "roster-section-title" }, [], "Participants (4)", "h2"),
      node({ "data-tid": "participant-section" }, [], "In this meeting (3)"),
      node({ "data-tid": "roster-add-people" }, [], "Add people", "button"),
      node({ "data-tid": "participant-count" }, [], "4"),
    ]);
    const rctx = { document: page };
    vm.createContext(rctx);
    vm.runInContext(fs.readFileSync(path.join(extDir, "roster.js"), "utf8"), rctx, { filename: "roster.js" });
    let got = null, threw = null;
    try { got = rctx.readMeetingRoster(); } catch (e) { threw = e; }
    check("readMeetingRoster runs with nothing but the page's document", !threw, threw && threw.message);
    const names = (got && got.names) || [];
    check("Meet tiles give their names", ["Sarah Chen", "Marcus Lee", "Daniel Okafor"].every((n) => names.includes(n)), JSON.stringify(names));
    check("…and the self tile gives my own label, not a name", got && got.self === "Test User" && !names.includes("Test User"), JSON.stringify(got));
    check("a participants list gives its items by visible name", names.includes("Wei Chen") && !names.some((n) => /Host/.test(n)), JSON.stringify(names));
    check("Zoom and Teams name elements are read", names.includes("Priya Raman") && names.includes("Anil Patel"), JSON.stringify(names));
    check("each name once", names.filter((n) => n === "Sarah Chen").length === 1, JSON.stringify(names));
    check("nothing but names: no chat, no badges, no list headings",
      !names.some((n) => /ship it|shall we start|Muted|In this meeting/.test(n)), JSON.stringify(names));
    check("a Meet tile without a label gives its name, not its icon or status", names.includes("Wei Zhang") &&
      !names.some((n) => /mic_off|^Muted$/.test(n)), JSON.stringify(names));
    check("list items that are actions or headings are skipped",
      !names.some((n) => /Add people|Copy joining info|In the meeting|person_add/.test(n)), JSON.stringify(names));
    check("a participant row whose first text is an avatar icon still gives its name",
      names.includes("Lena Ortiz") && !names.some((n) => /^(person|more_vert)$/.test(n)), JSON.stringify(names));
    check("Teams headings, buttons, section titles and counts are skipped",
      !names.some((n) => /Participants|Add people|^4$/.test(n)), JSON.stringify(names));
    check("exactly the people, in page order",
      JSON.stringify(names) === JSON.stringify(["Sarah Chen", "Marcus Lee", "Daniel Okafor", "Wei Zhang", "Wei Chen", "Lena Ortiz", "Priya Raman", "Anil Patel"]),
      JSON.stringify(names));
    check("sidepanel.html loads roster.js before sidepanel.js",
      html.indexOf('src="roster.js"') > 0 && html.indexOf('src="roster.js"') < html.indexOf('src="sidepanel.js"'));
    const manifest = JSON.parse(fs.readFileSync(path.join(extDir, "manifest.json"), "utf8"));
    check("the manifest can read the invoked tab and nothing more",
      manifest.permissions.includes("activeTab") && manifest.permissions.includes("scripting") &&
      !(manifest.host_permissions || []).some((h) => /<all_urls>|meet|teams|zoom/.test(h)), JSON.stringify(manifest.permissions));
  }

  // 21) The name input closes the way people expect: clicking away saves it as
  //     Enter does, Escape stays a cancel, an IME's Enter is not a save, and
  //     moving to another name saves the first.
  {
    const B = makePanel({ store: { noticeAck: 2 } });
    await tick();
    B.ctx.addFinal("them", [{ speaker: 0, text: "Morning all." }]);
    B.ctx.addFinal("them", [{ speaker: 1, text: "Morning." }]);
    const openChip = (key) => fire(B.els.people, "click", { target: fakeTarget({ act: "name", key }) });
    const inputFor = (key, value) => fakeTarget({ cls: "person-edit", value, tag: "INPUT", forKey: "null|" + key });
    const people = () => B.els.people._html;

    openChip("int:0");
    fire(B.els.people, "focusout", { target: inputFor("int:0", "Sarah") });
    await tick();
    check("clicking away saves the name, as Enter does", /Sarah:<\/span> Morning all\./.test(B.T()) && !/person-edit/.test(people()), B.T());
    fire(B.els.people, "focusout", { target: inputFor("int:0", "Stale") });
    check("a blur from an input already closed changes nothing", /Sarah:<\/span>/.test(B.T()) && !/Stale/.test(B.T()), B.T());
    openChip("int:0");
    fire(B.els.people, "focusout", { target: Object.assign(inputFor("int:0", "Redrawn"), { isConnected: false }) });
    check("the blur Chrome sends as a redraw removes the open input does not save it",
      /person-edit/.test(people()) && !/Redrawn/.test(B.T()), people());
    fire(B.els.people, "keydown", { key: "Escape", target: inputFor("int:0", "Sarah") });

    openChip("int:1");
    fire(B.els.people, "keydown", { key: "Escape", target: inputFor("int:1", "Nope") });
    fire(B.els.people, "focusout", { target: inputFor("int:1", "Nope") });
    check("Escape stays a cancel even when a blur follows", !/Nope/.test(B.T()) && /Speaker 2:<\/span> Morning\./.test(B.T()), B.T());

    openChip("int:1");
    fire(B.els.people, "keydown", { key: "Enter", isComposing: true, target: inputFor("int:1", "To") });
    check("Enter while an IME is composing does not save", /person-edit/.test(people()) && /Speaker 2:<\/span>/.test(B.T()), people());

    fire(B.els.people, "input", { target: inputFor("int:1", "Tom") });
    const onName = { prevented: false };
    fire(B.els.people, "mousedown", { target: fakeTarget({ act: "name", key: "int:0" }), preventDefault() { onName.prevented = true; } });
    const onOther = { prevented: false };
    fire(B.els.transcript, "mousedown", { target: fakeTarget({}), preventDefault() { onOther.prevented = true; } });
    check("pressing another name keeps focus in the open input; pressing anything else does not",
      onName.prevented && !onOther.prevented);
    openChip("int:0");
    check("…and the click saves what was typed, then opens the other name",
      /Tom:<\/span> Morning\./.test(B.T()) && /person-edit[^>]*value="Sarah"/.test(people()), people());
    fire(B.els.people, "input", { target: inputFor("int:0", "Sarah L") });
    fire(B.els.transcript, "click", { target: fakeTarget({ key: "int:0" }) });
    check("clicking the label of the name being typed keeps what is typed",
      /person-edit[^>]*data-for="null\|int:0"/.test(people()) && vm.runInContext("editing.draft", B.ctx) === "Sarah L",
      vm.runInContext("JSON.stringify(editing)", B.ctx));
    fire(B.els.people, "keydown", { key: "Escape", target: inputFor("int:0", "Sarah L") });
  }

  // 22) A roster read still in flight when the call ends, or the next starts,
  //     must not put the old call's people into the new one.
  {
    const S = makePanel({ store: { noticeAck: 2 } });
    await tick();
    S.ctx.createAudioHub = async () => ({
      sampleRate: 48000, state: () => "running", usingWorklet: () => true, addSource: () => ({ stop() {} }), close() {},
    });
    vm.runInContext("settings.deepgramKey = 'dg_test'", S.ctx);
    S.script.hold = true;
    S.ctx.watchRoster(11);                       // reading the last call's tab...
    await S.ctx.start();                         // ...when the next call starts
    S.script.release({ names: ["Old Caller"], self: "" });
    await tick();
    check("a read that answers after Start is dropped", JSON.stringify(S.ctx.rosterNow()) === "[]", JSON.stringify(S.ctx.rosterNow()));
    S.ctx.watchRoster(12);
    await S.ctx.stop();
    S.script.release({ names: ["Late Answer"], self: "" });
    await tick();
    check("…and so is one that answers after Stop", JSON.stringify(S.ctx.rosterNow()) === "[]", JSON.stringify(S.ctx.rosterNow()));
    S.script.hold = false;
    S.script.result = { names: ["Sarah Chen"], self: "" };
    S.ctx.watchRoster(13);
    await tick();
    check("…while a read for the call in progress still lands", JSON.stringify(S.ctx.rosterNow()) === '["Sarah Chen"]',
      JSON.stringify(S.ctx.rosterNow()));
  }

  // 23) A panel kept open across the update runs this script against its old
  //     page: the elements added since must be optional, the level dots included.
  {
    let threw = null;
    try {
      const O = makePanel({ store: { noticeAck: 2 }, missing: ["lvlMe", "lvlThem", "people", "myName", "noticeShow"] });
      await tick();
      O.ctx.setState("recording");
      O.ctx.setLevel("me", true);
      O.ctx.addFinal("them", [{ speaker: 0, text: "Hi, I'm Sarah." }]);
      O.ctx.setState("stopped");
      check("…and still renders", /Sarah:<\/span> Hi, I&#39;m Sarah\./.test(O.T()), O.T());
    } catch (e) { threw = e; }
    check("an old page without the level dots, People row or new settings does not throw", !threw, threw && threw.message);
  }

  console.log("\n" + (failures ? `${failures} FAIL` : "all passed") + "\n");
  process.exit(failures ? 1 : 0);
})();
