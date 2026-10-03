// Tests for the transcript history store, with NO deps, same vm approach as
// test_render.cjs. history.js is loaded into a context with a chrome.storage
// shim, so these exercise the real persistence path (including the write queue,
// the prune, and the label snapshot) rather than a mock of it.
//
//   run:  node extension/test_history.cjs

const vm = require("vm");
const fs = require("fs");
const path = require("path");

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log("  PASS  " + name); }
  else { console.log("  FAIL  " + name + (extra ? "  →  " + extra : "")); failures++; }
}

// ---- chrome.storage.local shim ----
// set() deep-clones like the real structured-clone boundary does, so a bug that
// stores a live reference and mutates it later shows up here instead of in Chrome.
function makeChrome() {
  const store = {};
  const local = {
    async get(keys) {
      if (typeof keys === "string") return keys in store ? { [keys]: store[keys] } : {};
      const out = {};
      if (Array.isArray(keys)) {
        for (const k of keys) if (k in store) out[k] = store[k];
        return out;
      }
      for (const [k, dflt] of Object.entries(keys)) out[k] = k in store ? store[k] : dflt;
      return out;
    },
    async set(obj) { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
    async remove(key) { delete store[key]; },
  };
  return { store, chrome: { storage: { local } } };
}

// `withNames` loads names.js first, as the panel does; history.js uses its
// roster matching when it is there and a plain comparison when it is not.
function load(fetchImpl, withNames) {
  const { store, chrome } = makeChrome();
  const ctx = {
    chrome, console, JSON, Date, Object, Array, Math, String, Number,
    setTimeout, clearTimeout, TextEncoder, TextDecoder,
    fetch: fetchImpl || (async () => { throw new Error("no fetch in test"); }),
  };
  vm.createContext(ctx);
  for (const f of withNames ? ["names.js", "history.js"] : ["history.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, f), "utf8"), ctx, { filename: f });
  }
  return { ctx, store };
}

const tick = () => new Promise((r) => setTimeout(r, 20));
const T = (h, m) => new Date(2026, 7, 14, h, m, 0).getTime();   // 14 Aug 2026, local

async function main() {
  console.log("\nhistory.js: transcript persistence\n");

  // ---- 1) labels are pure and derived from a stored ordinal map ----
  {
    const { ctx } = load();
    check("my own leg is Me", ctx.speakerLabel("me", {}) === "Me");
    check("a lone far-end voice is not numbered",
      ctx.speakerLabel("int:0", { "int:0": 1 }) === "Speaker",
      ctx.speakerLabel("int:0", { "int:0": 1 }));
    const two = { "int:0": 1, "int:1": 2 };
    check("two voices are numbered in first-heard order",
      ctx.speakerLabel("int:0", two) === "Speaker 1" && ctx.speakerLabel("int:1", two) === "Speaker 2");
    check("distinct voices get distinct colours",
      ctx.speakerClass("int:0", two) !== ctx.speakerClass("int:1", two));
    check("my colour is my own", ctx.speakerClass("me", two) === "spk-Me");
  }

  // ---- 2) a recorded session round-trips through storage ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet - abc-defg-hij", startedAt: T(14, 32) });
    ctx.recordSession(
      [{ key: "int:0", text: "Tell me about yourself.", t: T(14, 33) },
       { key: "me", text: "Sure, here's a quick summary.", t: T(14, 33) }],
      { "int:0": 1 }
    );
    await ctx.flushSession();
    const saved = await ctx.listSessions();
    check("one session is stored", saved.length === 1, JSON.stringify(saved));
    check("both lines survive", saved[0].lines.length === 2, JSON.stringify(saved[0].lines));
    check("the raw speaker key is stored, not the rendered label",
      saved[0].lines[0].key === "int:0", JSON.stringify(saved[0].lines[0]));

    // Late-arriving finals (Deepgram can flush one as the socket closes) must not
    // be dropped just because Stop already ran.
    await ctx.endSession();
    ctx.recordSession(
      [{ key: "int:0", text: "Tell me about yourself.", t: T(14, 33) },
       { key: "me", text: "Sure, here's a quick summary.", t: T(14, 33) },
       { key: "int:0", text: "Thanks, we'll be in touch.", t: T(14, 50) }],
      { "int:0": 1 }
    );
    await ctx.flushSession();
    const after = await ctx.listSessions();
    check("a final arriving after Stop still lands", after[0].lines.length === 3, JSON.stringify(after[0].lines));
    check("it updates the same record, not a second one", after.length === 1, String(after.length));
  }

  // ---- 3) the debounced write happens on its own ----
  {
    const { ctx } = load();
    ctx.historySetLimits({ flushMs: 1 });
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(9, 0) });
    ctx.recordSession([{ key: "int:0", text: "Hello there.", t: T(9, 1) }], { "int:0": 1 });
    check("nothing is written synchronously", (await ctx.listSessions()).length === 0);
    await tick();
    check("the debounce flushes without an explicit call", (await ctx.listSessions()).length === 1);
  }

  // ---- 4) an empty session is not worth keeping ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(10, 0) });
    const sealed = await ctx.endSession();
    check("Start → Stop with nothing said saves nothing", sealed === null, JSON.stringify(sealed));
    check("…and leaves the list empty", (await ctx.listSessions()).length === 0);
  }

  // ---- 5) titles ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet - abc-defg-hij", startedAt: T(14, 32) });
    ctx.recordSession(
      [{ key: "me", text: "Hi, thanks for having me.", t: T(14, 32) },
       { key: "int:0", text: "Tell me about yourself.", t: T(14, 33) }],
      { "int:0": 1 }
    );
    const sealed = await ctx.endSession();
    check("the title quotes the first question asked of me, not my own opener",
      sealed.title === '"Tell me about yourself." - Meet', sealed.title);
    check("the title is marked as a guess", sealed.titleSource === "heuristic", sealed.titleSource);
    check("Stop stamps the end time", sealed.endedAt >= sealed.startedAt, JSON.stringify(sealed));
  }
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Weekly sync | Microsoft Teams", startedAt: T(11, 0) });
    const long = "So walk me through how you would design a rate limiter that works across a fleet of stateless API servers";
    ctx.recordSession([{ key: "int:0", text: long, t: T(11, 1) }], { "int:0": 1 });
    const sealed = await ctx.endSession();
    check("a long question is cut on a word boundary with an ellipsis",
      /^"So walk me through how you would design a rate limiter[^"]*…" - Teams$/.test(sealed.title), sealed.title);
    check("…and stays short enough for the list", sealed.title.length <= 80, String(sealed.title.length));
  }
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Some Random Page", startedAt: T(12, 0) });
    ctx.recordSession([{ key: "me", text: "Testing one two three.", t: T(12, 1) }], {});
    const sealed = await ctx.endSession();
    check("with no far-end line my own first line is used",
      sealed.title === '"Testing one two three."', sealed.title);
  }

  // ---- 6) a saved session relabels from its own snapshot ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(13, 0) });
    ctx.recordSession(
      [{ key: "int:0", text: "First question.", t: T(13, 1) },
       { key: "int:1", text: "And a follow-up.", t: T(13, 2) },
       { key: "me", text: "Sure.", t: T(13, 3) }],
      { "int:0": 1, "int:1": 2 }
    );
    await ctx.endSession();
    const [s] = await ctx.listSessions();
    const lines = ctx.sessionLines(s);
    check("a two-voice session reopens with numbered labels",
      lines[0].label === "Speaker 1" && lines[1].label === "Speaker 2" && lines[2].label === "Me",
      JSON.stringify(lines.map((l) => l.label)));
    check("voice count is reported", ctx.sessionMeta(s).includes("2 voices"), ctx.sessionMeta(s));
    check("meta carries the date and the line count",
      /14 Aug 2026, \d{2}:\d{2}/.test(ctx.sessionMeta(s)) && /3 lines/.test(ctx.sessionMeta(s)),
      ctx.sessionMeta(s));
  }

  // ---- 7) exports ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(15, 0) });
    ctx.recordSession(
      [{ key: "int:0", text: "How do you handle schema migrations?", t: T(15, 1) },
       { key: "me", text: "Expand and contract, with a backfill step.", t: T(15, 2) }],
      { "int:0": 1 }
    );
    const s = await ctx.endSession();
    const txt = ctx.sessionText(s);
    check("copy text labels every line",
      /Speaker: How do you handle schema migrations\?\nMe: Expand and contract, with a backfill step\./.test(txt), txt);
    check("copy text opens with the title", txt.startsWith(s.title), txt.slice(0, 60));

    const md = ctx.sessionMarkdown(s);
    check("markdown has the title as an h1", md.startsWith("# " + s.title + "\n"), md.slice(0, 80));
    check("markdown bolds each speaker", /\*\*Speaker:\*\* How do you handle/.test(md), md);
    check("markdown records where it came from", /Source: Meet/.test(md), md);

    const name = ctx.sessionFilename(s);
    check("the filename is dated, slugged and safe",
      /^sparky\/2026-08-14-[a-z0-9-]+\.md$/.test(name), name);
  }

  // ---- 8) the list is capped, newest first ----
  {
    const { ctx } = load();
    ctx.historySetLimits({ cap: 2 });
    for (let i = 1; i <= 3; i++) {
      ctx.beginSession({ tabTitle: "Meet", startedAt: T(8, i) });
      ctx.recordSession([{ key: "int:0", text: "Session " + i + " speaking.", t: T(8, i) }], { "int:0": 1 });
      await ctx.endSession();
    }
    const saved = await ctx.listSessions();
    check("the cap prunes the oldest", saved.length === 2, String(saved.length));
    check("newest is first", /Session 3/.test(saved[0].lines[0].text), JSON.stringify(saved.map((s) => s.lines[0].text)));
    check("the survivor is the second-newest, not the first", /Session 2/.test(saved[1].lines[0].text),
      JSON.stringify(saved.map((s) => s.lines[0].text)));
  }

  // ---- 9) rename / delete / clear ----
  {
    const { ctx } = load();
    for (let i = 1; i <= 2; i++) {
      ctx.beginSession({ tabTitle: "Meet", startedAt: T(7, i) });
      ctx.recordSession([{ key: "int:0", text: "Session " + i + " speaking.", t: T(7, i) }], { "int:0": 1 });
      await ctx.endSession();
    }
    let saved = await ctx.listSessions();
    const id = saved[0].id;
    await ctx.renameSession(id, "  Backend sync  ");
    saved = await ctx.listSessions();
    check("rename trims and sticks", saved[0].title === "Backend sync", saved[0].title);
    check("a hand-typed title is not a guess any more", saved[0].titleSource === "user", saved[0].titleSource);

    await ctx.deleteSession(id);
    saved = await ctx.listSessions();
    check("delete removes exactly one", saved.length === 1 && saved[0].id !== id, JSON.stringify(saved.map((s) => s.id)));

    await ctx.clearSessions();
    check("clear empties the list", (await ctx.listSessions()).length === 0);
  }

  // ---- 10) the Claude retitle is an upgrade, never a requirement ----
  {
    let seen = null;
    const { ctx } = load(async (url, init) => {
      seen = { url, body: JSON.parse(init.body), headers: init.headers };
      return { ok: true, async json() { return { content: [{ type: "text", text: "Backend sync: schema migrations\n" }] }; } };
    });
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(16, 0) });
    ctx.recordSession([{ key: "int:0", text: "How do you handle schema migrations?", t: T(16, 1) }], { "int:0": 1 });
    const s = await ctx.endSession();
    const title = await ctx.autoTitleSession(s.id, "sk-ant-test");
    check("Claude's title replaces the quoted guess", title === "Backend sync: schema migrations", title);
    const [stored] = await ctx.listSessions();
    check("…and is persisted", stored.title === "Backend sync: schema migrations", stored.title);
    check("…and marked as machine-written", stored.titleSource === "claude", stored.titleSource);
    check("titling uses the cheap model", seen.body.model === "claude-haiku-4-5", seen.body.model);
    check("titling asks for the browser-access header",
      seen.headers["anthropic-dangerous-direct-browser-access"] === "true", JSON.stringify(seen.headers));
  }
  {
    const { ctx } = load(async () => ({ ok: false, status: 401, async text() { return "bad key"; } }));
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(17, 0) });
    ctx.recordSession([{ key: "int:0", text: "Tell me about yourself.", t: T(17, 1) }], { "int:0": 1 });
    const s = await ctx.endSession();
    const title = await ctx.autoTitleSession(s.id, "sk-ant-bad");
    check("a failed retitle is silent", title === null, String(title));
    const [stored] = await ctx.listSessions();
    check("…and leaves the heuristic title in place", stored.title === s.title, stored.title);
  }
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(18, 0) });
    ctx.recordSession([{ key: "int:0", text: "Tell me about yourself.", t: T(18, 1) }], { "int:0": 1 });
    const s = await ctx.endSession();
    check("no key means no network call at all", (await ctx.autoTitleSession(s.id, "")) === null);
  }

  // ---- 11) stored transcripts are inert data ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(19, 0) });
    ctx.recordSession([{ key: "int:0", text: "<script>alert(1)</script>", t: T(19, 1) }], { "int:0": 1 });
    const s = await ctx.endSession();
    check("the title of a hostile line is escaped for display",
      !/<script>/.test(ctx.sessionTitleHtml(s)) && /&lt;script&gt;/.test(ctx.sessionTitleHtml(s)),
      ctx.sessionTitleHtml(s));
    check("rendered lines are escaped too",
      /&lt;script&gt;/.test(ctx.sessionLines(s)[0].html) && !/<script>/.test(ctx.sessionLines(s)[0].html),
      ctx.sessionLines(s)[0].html);
    check("the raw text is kept verbatim for copy/export",
      ctx.sessionLines(s)[0].text === "<script>alert(1)</script>", ctx.sessionLines(s)[0].text);
  }
  {
    // Every heuristic title is quoted transcript text, and the rename box puts it
    // inside value="…", so quotes must not survive escaping either.
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(20, 0) });
    ctx.recordSession([{ key: "int:0", text: 'so " onfocus=alert(1) x="', t: T(20, 1) }], { "int:0": 1 });
    const s = await ctx.endSession();
    check("a spoken quote cannot close an HTML attribute",
      !/"/.test(ctx.sessionTitleHtml(s)) && /&quot;/.test(ctx.sessionTitleHtml(s)), ctx.sessionTitleHtml(s));
  }

  // ---- 12) names: labels, typed names, who was heard ----
  {
    const { ctx } = load();
    const two = { "int:0": 1, "int:1": 2 };
    check("a named voice is labelled by its name", ctx.speakerLabel("int:0", two, { "int:0": "Sarah" }) === "Sarah");
    check("an unnamed voice beside it keeps its number",
      ctx.speakerLabel("int:1", two, { "int:0": "Sarah" }) === "Speaker 2", ctx.speakerLabel("int:1", two, { "int:0": "Sarah" }));
    check("a lone unnamed voice is still plain Speaker", ctx.speakerLabel("int:0", { "int:0": 1 }, {}) === "Speaker");
    check("Me is always Me", ctx.speakerLabel("me", two, { me: "Daniel" }) === "Me");
    check("callers without a names map get the old labels", ctx.speakerLabel("int:0", two) === "Speaker 1");

    const names = {}, src = {};
    ctx.nameVoice(names, src, "int:0", "  Sarah   Lin ");
    check("a typed name is tidied and marked as yours", names["int:0"] === "Sarah Lin" && src["int:0"] === "user", JSON.stringify([names, src]));
    ctx.nameVoice(names, src, "int:0", "  ");
    check("an empty name hands the voice back to automatic", !("int:0" in names) && !("int:0" in src), JSON.stringify([names, src]));
    ctx.nameVoice(names, src, "me", "Daniel");
    check("Me cannot be given a name", !("me" in names), JSON.stringify(names));

    const heard = ctx.speakersHeard(
      [{ key: "int:1", text: "a" }, { key: "me", text: "b" }, { key: "int:0", text: "c" }],
      { "int:0": 1, "int:1": 2, "int:2": 3 });
    check("voices heard: Me first, then first-heard order, partial-only voices left out",
      JSON.stringify(heard) === JSON.stringify(["me", "int:0", "int:1"]), JSON.stringify(heard));
  }

  // ---- 13) names persist, and every view of a saved call uses them ----
  {
    const { ctx } = load();
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(21, 0) });
    const lines = [
      { key: "int:0", text: "Hi, I'm Sarah, I lead the platform team.", t: T(21, 1) },
      { key: "int:1", text: "Thanks for having us.", t: T(21, 1) },
      { key: "me", text: "Great to meet you both.", t: T(21, 2) },
    ];
    ctx.recordSession(lines, { "int:0": 1, "int:1": 2 }, { "int:0": "Sarah" }, { "int:0": "auto" });
    const s = await ctx.endSession();
    let [stored] = await ctx.listSessions();
    check("names are stored with the session", stored.names["int:0"] === "Sarah", JSON.stringify(stored.names));
    check("…and where they came from", stored.nameSources["int:0"] === "auto", JSON.stringify(stored.nameSources));
    check("a reopened session labels by name",
      JSON.stringify(ctx.sessionLines(stored).map((l) => l.label)) === JSON.stringify(["Sarah", "Speaker 2", "Me"]),
      JSON.stringify(ctx.sessionLines(stored).map((l) => l.label)));
    check("Copy uses names", /\nSarah: Hi, I'm Sarah/.test(ctx.sessionText(stored)), ctx.sessionText(stored));
    let md = ctx.sessionMarkdown(stored);
    check("the Markdown header lists the people", /^- People: Me, Sarah, Speaker 2$/m.test(md), md.slice(0, 260));
    check("the Markdown bolds names", /\*\*Sarah:\*\* Hi, I'm Sarah/.test(md), md);

    await ctx.renameSpeaker(s.id, "int:1", "Tom");
    [stored] = await ctx.listSessions();
    check("renaming a voice in a saved call persists", stored.names["int:1"] === "Tom" && stored.nameSources["int:1"] === "user",
      JSON.stringify(stored));
    md = ctx.sessionMarkdown(stored);
    check("…and the export follows", /^- People: Me, Sarah, Tom$/m.test(md) && /\*\*Tom:\*\* Thanks for having us\./.test(md), md);
    await ctx.flushSession();
    [stored] = await ctx.listSessions();
    check("…and the call still in memory took it too, so its next write keeps it", stored.names["int:1"] === "Tom",
      JSON.stringify(stored.names));
    await ctx.renameSpeaker(s.id, "int:1", "");
    [stored] = await ctx.listSessions();
    check("an empty rename returns the saved voice to its number",
      !stored.names["int:1"] && !stored.nameSources["int:1"] && ctx.sessionLines(stored)[1].label === "Speaker 2",
      JSON.stringify(stored.names));
    check("renaming a session that is gone is a no-op", (await ctx.renameSpeaker("nope", "int:0", "X")) === null);
  }

  // ---- 14) a session saved before speakers had names ----
  {
    const { ctx, store } = load();
    store.sessions = [{
      id: "s1", startedAt: T(22, 0), endedAt: T(22, 5), title: "Old call", titleSource: "user", tabTitle: "Meet",
      ordinals: { "int:0": 1, "int:1": 2 },
      lines: [
        { key: "int:0", text: "First question.", t: T(22, 1) },
        { key: "int:1", text: "A follow-up.", t: T(22, 2) },
        { key: "me", text: "Sure.", t: T(22, 3) },
      ],
    }];
    const [old] = await ctx.listSessions();
    check("an old session still reopens with numbered labels",
      JSON.stringify(ctx.sessionLines(old).map((l) => l.label)) === JSON.stringify(["Speaker 1", "Speaker 2", "Me"]),
      JSON.stringify(ctx.sessionLines(old).map((l) => l.label)));
    check("…and exports, listing its people by number",
      /^- People: Me, Speaker 1, Speaker 2$/m.test(ctx.sessionMarkdown(old)), ctx.sessionMarkdown(old).slice(0, 220));
    check("…and copies", /Speaker 1: First question\.\nSpeaker 2: A follow-up\.\nMe: Sure\./.test(ctx.sessionText(old)), ctx.sessionText(old));
    await ctx.renameSpeaker("s1", "int:0", "Sarah");
    const [named] = await ctx.listSessions();
    check("…and can be given names afterwards", ctx.sessionLines(named)[0].label === "Sarah" && named.nameSources["int:0"] === "user",
      JSON.stringify(named.names));
  }

  // ---- 15) the roster: who was in the call, spoken or not ----
  for (const withNames of [true, false]) {
    const { ctx } = load(null, withNames);
    const how = withNames ? "" : " (without names.js)";
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(23, 0) });
    ctx.recordSession(
      [{ key: "int:0", text: "Hi, I'm Sarah.", t: T(23, 1) }, { key: "int:1", text: "Hello.", t: T(23, 2) }],
      { "int:0": 1, "int:1": 2 }, { "int:0": "Sarah Chen" }, { "int:0": "auto" }, ["Sarah Chen", "Marcus Lee", "Anil Patel"]);
    const s = await ctx.endSession();
    const [stored] = await ctx.listSessions();
    check("the roster is stored with the session" + how,
      JSON.stringify(stored.roster) === '["Sarah Chen","Marcus Lee","Anil Patel"]', JSON.stringify(stored.roster));
    check("People is Me, every voice, then whoever on the roster no voice was named for" + how,
      /^- People: Me, Sarah Chen, Speaker 2, Marcus Lee, Anil Patel$/m.test(ctx.sessionMarkdown(s)), ctx.sessionMarkdown(s).slice(0, 220));
  }
  {
    const { ctx } = load(null, true);
    ctx.beginSession({ tabTitle: "Meet", startedAt: T(23, 30) });
    ctx.recordSession([{ key: "int:0", text: "Hi, I'm Sarah.", t: T(23, 31) }],
      { "int:0": 1 }, { "int:0": "Sarah" }, { "int:0": "user" }, ["Sarah Chen"]);
    const s = await ctx.endSession();
    check("a short name still accounts for its roster entry", /^- People: Me, Sarah$/m.test(ctx.sessionMarkdown(s)),
      ctx.sessionMarkdown(s).slice(0, 200));
  }
  {
    const { ctx, store } = load(null, true);
    store.sessions = [{ id: "s9", startedAt: T(9, 0), endedAt: T(9, 1), title: "Old", tabTitle: "Meet",
      ordinals: { "int:0": 1 }, lines: [{ key: "int:0", text: "Hello.", t: T(9, 0) }] }];
    const [old] = await ctx.listSessions();
    check("a session from before the roster has People without one", /^- People: Me, Speaker$/m.test(ctx.sessionMarkdown(old)),
      ctx.sessionMarkdown(old).slice(0, 200));
  }

  console.log("\n" + (failures ? `${failures} FAIL` : "all passed") + "\n");
  process.exit(failures ? 1 : 0);
}

main();
