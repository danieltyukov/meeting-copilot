// Render docs/extension.png from the real side panel, mid-call.
//
// The panel's own HTML, CSS and scripts run in headless Chrome with a stand-in
// for the chrome.* extension APIs; a short script then drives the panel's real
// functions (setState, addFinal, watchRoster, ...) into a call with named
// voices, someone on the roster who has not spoken, and a drafted answer. So
// the picture can never drift from what the panel draws.
//
//     node tools/render_extension_shot.mjs [--scheme dark|light] [--out PATH]
//
// Needs Node 22+ (global WebSocket) and Chrome or Chromium; set CHROME to the
// binary if it is not on PATH. Headless only: it never opens a window.

import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WIDTH = 380;                     // the README shows it at this width, 1:1
const HEIGHT = 840;

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const scheme = opt("--scheme", "dark");
const out = path.resolve(opt("--out", path.join(ROOT, "docs", "extension.png")));

// ---- what the panel sees ----
// chrome.storage holds the welcome notice as read, keys, Your name and three
// saved calls; chrome.scripting answers like a Meet tab would.
const STUB = `
(() => {
  const H = 3600e3, now = Date.now();
  const old = (id, title, ago, n) => ({
    id, title, titleSource: "claude", tabTitle: "Meet", startedAt: now - ago, endedAt: now - ago + 40 * 60e3,
    ordinals: { "int:0": 1 }, names: {}, nameSources: {}, roster: [],
    lines: Array.from({ length: n }, (_, i) => ({ key: i % 2 ? "me" : "int:0", text: "Line " + i, t: now - ago })),
  });
  const store = {
    noticeAck: 2, deepgramKey: "dg_demo", anthropicKey: "sk-ant-demo", model: "sonnet", language: "en", myName: "Daniel",
    sessions: [old("s1", "Backend sync: queue retries", 26 * H, 40), old("s2", "Platform interview, round one", 74 * H, 60),
      old("s3", "Design review: ingest service", 170 * H, 30)],
  };
  const copy = (v) => JSON.parse(JSON.stringify(v));
  const chrome = {
    storage: { local: {
      async get(keys) {
        const list = typeof keys === "string" ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys || {});
        const o = {}; for (const k of list) if (k in store) o[k] = copy(store[k]); return o;
      },
      async set(obj) { Object.assign(store, copy(obj)); },
    } },
    runtime: { sendMessage: async () => ({ ok: false, error: "stub" }), onMessage: { addListener() {} }, getURL: (p) => p },
    scripting: {
      executeScript: async () => [{ result: { names: ["Sarah Chen (Host)", "Tom Becker", "Priya Raman", "Daniel"], self: "Daniel" } }],
    },
    tabs: { create() {} }, downloads: { download() {} },
  };
  Object.defineProperty(window, "chrome", { value: chrome, configurable: true, writable: true });
})();
`;

const DEMO = `
(async () => {
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  await pause(80);
  setState("recording");
  watchRoster(1);                                   // the call tab: Sarah Chen, Tom Becker, Priya Raman
  await pause(30);
  addFinal("them", [{ speaker: 0, text: "Hi, I'm Sarah, I lead the platform team. Thanks for making the time." }]);
  addFinal("them", [{ speaker: 1, text: "And I'm Tom, I look after the data pipeline." }]);
  addFinal("me", [{ speaker: null, text: "Great to meet you both." }]);
  const q = "So, how would you run a schema migration on a table that never stops taking writes?";
  addFinal("them", [{ speaker: 0, text: q }]);
  setPartial("them", [{ speaker: 1, text: "And what happens to reads while" }]);
  const a = "I'd use expand and contract. First I add the new column next to the old one and have the app write to both. " +
    "Then I backfill in small batches, watching replication lag, and switch reads over once the two agree.\\n\\n" +
    "The old column goes in a later deploy, so every step can be rolled back on its own.";
  $("answer").innerHTML = '<span class="q">' + escapeHtml("Q: " + q) + "</span>" + escapeHtml(a);
  $("copyBtn").classList.remove("hidden");
  setLevel("them", true);
  status("Capturing: Meet - qpr-wxyz-abc");
  $("diag").textContent = "ctx:running@48000Hz worklet · you 1842f dg ok · them 2310f dg ok";
  await refreshHistory();
  document.body.dataset.ready = "1";
})();
`;

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  for (const name of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
    try { return execFileSync("which", [name], { encoding: "utf8" }).trim(); } catch {}
  }
  throw new Error("Chrome not found: set CHROME to its binary.");
}

// A copy of extension/ with the stub loaded first and the demo last.
function stage(dir) {
  fs.cpSync(path.join(ROOT, "extension"), dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "stub.js"), STUB);
  fs.writeFileSync(path.join(dir, "demo.js"), DEMO);
  const html = fs.readFileSync(path.join(dir, "sidepanel.html"), "utf8")
    .replace('<script src="audio.js"></script>', '<script src="stub.js"></script>\n  <script src="audio.js"></script>')
    .replace('<script src="sidepanel.js"></script>', '<script src="sidepanel.js"></script>\n  <script src="demo.js"></script>');
  fs.writeFileSync(path.join(dir, "shot.html"), html);
  return path.join(dir, "shot.html");
}

async function main() {
  if (typeof WebSocket !== "function") throw new Error("Needs Node 22 or later (global WebSocket).");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sparky-shot-"));
  const page = stage(path.join(tmp, "ext"));
  const env = { ...process.env };
  delete env.DISPLAY;                              // headless: never touch a real desktop
  delete env.WAYLAND_DISPLAY;
  const chrome = spawn(findChrome(), [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${path.join(tmp, "profile")}`,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--allow-file-access-from-files",
    "about:blank",
  ], { env, stdio: ["ignore", "ignore", "pipe"] });
  try {
    const port = await new Promise((resolve, reject) => {
      let err = "";
      const timer = setTimeout(() => reject(new Error("Chrome did not start:\n" + err)), 15000);
      chrome.stderr.on("data", (d) => {
        err += d;
        const m = /DevTools listening on ws:\/\/[^:]+:(\d+)\//.exec(err);
        if (m) { clearTimeout(timer); resolve(m[1]); }
      });
      chrome.on("exit", (code) => reject(new Error(`Chrome exited (${code}):\n${err}`)));
    });
    const target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page");
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((r, j) => { ws.addEventListener("open", r, { once: true }); ws.addEventListener("error", j, { once: true }); });
    let id = 0;
    const waiting = new Map();
    ws.addEventListener("message", (e) => {
      const m = JSON.parse(e.data);
      if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
    });
    const send = (method, params = {}) => new Promise((r) => { const i = ++id; waiting.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
    const evaluate = async (expression) => (await send("Runtime.evaluate", { expression, returnByValue: true })).result?.result?.value;

    await send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });
    await send("Emulation.setEmulatedMedia", { features: [
      { name: "prefers-color-scheme", value: scheme }, { name: "prefers-reduced-motion", value: "reduce" },
    ] });
    await send("Runtime.enable");
    await send("Page.navigate", { url: "file://" + page });
    let ready = false;
    for (let i = 0; i < 200 && !ready; i++) {
      ready = await evaluate("document.body && document.body.dataset.ready === '1'");
      if (!ready) await new Promise((r) => setTimeout(r, 50));
    }
    if (!ready) throw new Error("The panel did not reach the demo state.");
    await new Promise((r) => setTimeout(r, 300));    // fonts and the pinned transcript settle
    const shot = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(out, Buffer.from(shot.result.data, "base64"));
    ws.close();
    console.log(`wrote ${path.relative(process.cwd(), out)} (${WIDTH}x${HEIGHT}, ${scheme})`);
  } finally {
    // Chrome keeps writing its profile for a moment after the kill.
    const gone = new Promise((r) => (chrome.exitCode !== null ? r() : chrome.once("exit", r)));
    chrome.kill();
    await gone;
    fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
