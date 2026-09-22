// End-to-end-ish test for the side panel's transcription rendering, with NO deps.
//
// It loads the REAL deepgram.js + sidepanel.js into a single vm context (the same
// way the browser shares one global across two <script> tags), shims just the DOM
// and WebSocket, then pushes real Deepgram "Results" frames through the real
// parser into the real render(). This exercises the exact path that populates the
// live transcript — including diarization, which splits the far end into separate
// voices, and the echo guard that keeps your own words out of that bucket.
//
//   run:  node extension/test_render.cjs

const vm = require("vm");
const fs = require("fs");
const path = require("path");

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log("  PASS  " + name); }
  else { console.log("  FAIL  " + name + (extra ? "  →  " + extra : "")); failures++; }
}

// ---- minimal DOM shim ----
function makeEl(id) {
  return {
    id, _text: "", _html: "", className: "", value: "", disabled: false,
    scrollTop: 0, scrollHeight: 100, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    get textContent() { return this._text; }, set textContent(v) { this._text = v; },
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; },
  };
}
const els = {};
const docListeners = {};
const document = {
  getElementById: (id) => els[id] || (els[id] = makeEl(id)),
  addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
  createElement: () => makeEl("tmp"),
  body: { appendChild() {} },
};

// ---- chrome shim ----
const store = {};
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
  runtime: { async sendMessage() { return { ok: false, error: "test" }; } },
};

// ---- WebSocket shim (captures the last instance so we can feed it frames) ----
let lastWS = null;
class FakeWS {
  constructor(url, protocols) { this.url = url; this.protocols = protocols; this.readyState = 0; lastWS = this; }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}
FakeWS.OPEN = 1;

// ---- build context = browser global shared by both scripts ----
const ctx = {
  document, chrome, WebSocket: FakeWS, console,
  JSON, URLSearchParams, TextDecoder, TextEncoder, setTimeout, clearTimeout,
  navigator: { mediaDevices: { getUserMedia: async () => { throw new Error("no mic in test"); } } },
  fetch: async () => { throw new Error("no fetch in test"); },
  AbortController,
};
vm.createContext(ctx);

const extDir = __dirname;
for (const f of ["deepgram.js", "history.js", "sidepanel.js"]) {
  vm.runInContext(fs.readFileSync(path.join(extDir, f), "utf8"), ctx, { filename: f });
}

const T = () => els.transcript ? els.transcript._html : "";

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
function connect(config, leg) {
  const dg = ctx.openDeepgram(
    Object.assign({ key: "x", model: "nova-3", language: "en", sampleRate: 48000 }, config),
    { onOpen() {}, onPartial: (s) => ctx.setPartial(leg, s),
      onFinal: (s) => ctx.addFinal(leg, s), onError: (m) => { throw new Error(m); } }
  );
  const ws = lastWS;
  ws.readyState = 1;
  if (ws.onopen) ws.onopen();
  return { dg, ws };
}

console.log("\nsidepanel render() — e2e through the real Deepgram parser\n");

// 1) Loading the script already ran setState("idle") -> render(). It must not throw,
//    and must show the idle prompt. (Before the fix, render() referenced an
//    undeclared `recording` and threw a ReferenceError the moment it ran empty.)
check("loads + renders idle without throwing", /Press Start/.test(T()), T());

// 2) Recording with no audio yet: the listening placeholder must render (this is the
//    state the user was stuck in) — no throw, shows mic + tab status honestly.
ctx.setState("recording");
check("recording+empty shows 'Listening…'", /Listening/.test(T()), T());
check("recording+empty reports mic off (no mic in test)", /mic off/.test(T()), T());
check("recording+empty reports waiting for the call tab", /waiting for the call/.test(T()), T());

// 3) Query string: diarization is on for the tab leg and off for the mic leg.
const me = connect({}, "me");
check("mic leg asks Deepgram for no diarization", !/diarize/.test(me.ws.url), me.ws.url);
const them = connect({ diarize: true }, "them");
check("tab leg asks Deepgram for diarize=true", /diarize=true/.test(them.ws.url), them.ws.url);

// 4) One voice on the call still reads as a plain "Speaker" — no numbering noise
//    on a 1:1, which is the common case.
feed(them.ws, { transcript: "tell me about", words: diarizedWords([[0, "tell me about"]]) });
check("interim frame shows a live partial", /Speaker:.*tell me about.*▌/s.test(T()), T());
feed(them.ws, { transcript: "tell me about yourself", words: diarizedWords([[0, "Tell me about yourself"]]) }, { final: true });
check("final frame commits an utterance", /class="spk-Speaker">Speaker:<\/span> Tell me about yourself/.test(T()), T());
check("partial cleared after final", !/▌/.test(T()), T());
check("single voice is not numbered", !/Speaker 1/.test(T()), T());

// 5) Your own leg is labelled Me and coloured separately.
// (Apostrophes render as &#39; — escapeHtml covers quotes as well as tags, because
// the same helper feeds value="…" attributes in the history list.)
ctx.addFinal("me", [{ speaker: null, text: "Sure, here's a quick summary." }]);
check("mic leg renders as Me", /spk-Me">Me:<\/span> Sure, here&#39;s a quick summary\./.test(T()), T());
check("latestQuestion skips my own line", ctx.latestQuestion() === "Tell me about yourself", ctx.latestQuestion());

// 6) A second far-end voice appears. Labels must upgrade to numbers RETROACTIVELY —
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
//    both legs — the second copy must be dropped rather than double-rendered.
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

// Short backchannels are NOT echo-suppressed — both sides genuinely say "yes".
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
//     anyone pressing Stop — closing the panel mid-call is the case that matters.
(async () => {
  ctx.historySetLimits({ flushMs: 1 });
  ctx.beginSession({ tabTitle: "Meet — abc-defg-hij" });
  const live = connect({ diarize: true }, "them");
  feed(live.ws, { transcript: "why do you want this role",
    words: diarizedWords([[0, "Why do you want this role?"]]) }, { final: true });
  await new Promise((r) => setTimeout(r, 20));

  let saved = await ctx.listSessions();
  check("a final utterance is persisted mid-call, before Stop",
    saved.length === 1 && saved[0].lines.some((l) => /Why do you want this role/.test(l.text)),
    JSON.stringify(saved.map((s) => s.lines.length)));

  // Partials must NOT be persisted — they are rewritten as Deepgram hears more.
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

  // 13) history.js absent — a side panel Chrome kept alive across an extension
  //     reload runs a document whose script list predates it. That threw
  //     "endSession is not defined" out of the Stop handler. The panel must
  //     instead keep transcribing and say plainly what is wrong.
  {
    const els2 = {};
    const doc2 = {
      getElementById: (id) => els2[id] || (els2[id] = makeEl(id)),
      addEventListener() {},
      createElement: () => makeEl("tmp"),
      body: { appendChild() {} },
    };
    const ctx2 = {
      document: doc2, chrome, WebSocket: FakeWS, console,
      JSON, URLSearchParams, TextDecoder, TextEncoder, setTimeout, clearTimeout,
      navigator: { mediaDevices: { getUserMedia: async () => { throw new Error("no mic"); } } },
      fetch: async () => { throw new Error("no fetch in test"); },
      AbortController,
    };
    ctx2.globalThis = ctx2;
    vm.createContext(ctx2);
    let threw = null;
    try {
      for (const f of ["deepgram.js", "sidepanel.js"]) {       // history.js deliberately omitted
        vm.runInContext(fs.readFileSync(path.join(extDir, f), "utf8"), ctx2, { filename: f });
      }
    } catch (e) { threw = e; }
    check("the panel still loads without history.js", !threw, threw && threw.message);
    if (!threw) {
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

  console.log("\n" + (failures ? `${failures} FAIL` : "all passed") + "\n");
  process.exit(failures ? 1 : 0);
})();
