#!/usr/bin/env node
// Real-browser e2e for the extension: real Chrome, real tab capture, real
// Deepgram, real Anthropic. Not run in CI (it needs keys, Xvfb, xdotool and the
// chrome-dev daemon from this machine's setup); run it before a release.
//
//   node tools/e2e_extension.mjs
//
// What it does: builds the test meeting (tools/e2e_audio.sh), serves a page that
// plays it on a loop, starts an isolated dev Chrome on its own Xvfb display (a
// fresh profile and control port, so it never touches your browser or desktop),
// loads extension/ unpacked, presses Alt+Shift+S on the call tab (the shortcut
// counts as invoking Sparky there, which tabCapture requires), then drives
// Start, Help and Stop in the side panel and checks that the voices were named
// from their introductions completed by the call page's participant tiles, that
// a draft streamed, and that the saved call kept the names. Keys come from the environment or ~/.config/meeting-copilot/config.env.
//
// Env: E2E_DIR (work dir), E2E_DISPLAY (default :78), E2E_LISTEN_MS (default 45000),
// E2E_KEEP=1 leaves Chrome and Xvfb running for a look.

import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WORK = resolve(process.env.E2E_DIR || join(tmpdir(), "sparky-e2e"));
const DISPLAY = process.env.E2E_DISPLAY || ":78";
const CTL = Number(process.env.E2E_CTL_PORT || 9331);
const HTTP = Number(process.env.E2E_HTTP_PORT || 8765);
const LISTEN_MS = Number(process.env.E2E_LISTEN_MS || 45000);
const DAEMON = "/usr/local/lib/chrome-dev/daemon.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(name, cond, extra) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + name + (cond || !extra ? "" : "  ->  " + extra));
  if (!cond) failures++;
}

function keyFromConfig(name) {
  if (process.env[name]) return process.env[name];
  try {
    const env = readFileSync(join(homedir(), ".config/meeting-copilot/config.env"), "utf8");
    const m = env.match(new RegExp("^" + name + "=(.*)$", "m"));
    return m ? m[1].trim() : "";
  } catch { return ""; }
}

// ---- the call: a page that plays the meeting once Sparky is listening ----
// Playback starts from 0 only after capture is up (playFrom0), never on a loop:
// diarization is steadier when a stream opens on the first voice rather than
// mid-sentence, and the run is the same every time.
// Participant tiles in Google Meet's shape (data-participant-id, and the own
// tile marked with data-self-name), so the roster reader has a page to read.
const ROSTER = ["Sarah Chen", "Marcus Lee", "Daniel Okafor"];
const tile = (id, name, self) => `<div data-participant-id="${id}"${self ? ` data-self-name="${name}"` : ""}
  style="display:inline-block;margin:6px;padding:40px 16px 8px;background:#3c4043;border-radius:8px">
  <span class="notranslate">${name}</span></div>`;
const callHtml = (title, audio, names) => `<!doctype html><html><head><meta charset="utf-8">
<title>${title} - Meet</title></head>
<body style="font:16px system-ui;background:#202124;color:#e8eaed;padding:24px">
<h1>${title} (e2e test call)</h1>
<div>${tile("p0", "Test User", true)}${names.map((n, i) => tile("p" + (i + 1), n)).join("")}</div>
<audio id="a" src="${audio}" controls></audio>
</body></html>`;

function serve() {
  const types = { ".html": "text/html", ".mp3": "audio/mpeg", ".wav": "audio/wav" };
  const server = createServer((req, res) => {
    const file = join(WORK, decodeURIComponent(new URL(req.url, "http://x").pathname));
    if (!file.startsWith(WORK) || !existsSync(file)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "content-type": types[extname(file)] || "application/octet-stream" });
    res.end(readFileSync(file));
  });
  return new Promise((r) => server.listen(HTTP, "127.0.0.1", () => r(server)));
}

// ---- isolated dev Chrome ----
const children = [];
async function startXvfb() {
  const lock = `/tmp/.X${DISPLAY.slice(1)}-lock`;
  if (existsSync(lock)) return;
  children.push(spawn("Xvfb", [DISPLAY, "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
    { stdio: "ignore" }));
  for (let i = 0; i < 40 && !existsSync(lock); i++) await sleep(100);
}

async function api(path, body) {
  const res = await fetch(`http://127.0.0.1:${CTL}${path}`, {
    method: body === undefined ? "GET" : "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const cdp = (method, params, sessionId) => api("/cdp", { method, params, sessionId });

async function startChrome() {
  const state = join(WORK, "state");
  const profile = join(WORK, "profile");
  rmSync(profile, { recursive: true, force: true });
  mkdirSync(state, { recursive: true });
  // On a Wayland desktop Chrome picks Wayland from WAYLAND_DISPLAY and opens on
  // the real screen, whatever DISPLAY says. Take it away and force X11, so the
  // only screen this Chrome can reach is the Xvfb one.
  const env = { ...process.env, DISPLAY, XDG_SESSION_TYPE: "x11", CHROME_DEV_STATE_DIR: state,
    CHROME_DEV_PROFILE: profile, CHROME_DEV_CTL_PORT: String(CTL) };
  delete env.WAYLAND_DISPLAY;
  children.push(spawn(process.execPath, [DAEMON,
    "--ozone-platform=x11",
    "--autoplay-policy=no-user-gesture-required",
    "--window-position=0,0", "--window-size=1500,950"], { env, stdio: "ignore" }));
  for (let i = 0; i < 60; i++) {
    try { await api("/status"); return; } catch { await sleep(500); }
  }
  throw new Error("dev Chrome did not come up; see " + join(state, "daemon.log"));
}

// Evaluate in a page target, as if the user did it (userGesture), returning by value.
async function evalIn(sessionId, expression) {
  const res = await cdp("Runtime.evaluate",
    { expression, returnByValue: true, awaitPromise: true, userGesture: true }, sessionId);
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description || "evaluate failed");
  return res.result?.value;
}

// Start the call's audio from the top, in the call tab itself.
async function playFrom0(targetId) {
  const { sessionId } = await cdp("Target.attachToTarget", { targetId, flatten: true });
  await evalIn(sessionId, "(() => { const a = document.getElementById('a'); a.currentTime = 0; return a.play().then(() => true); })()");
}

// Wait until the panel reports it is capturing the call tab.
async function untilCapturing(panel) {
  for (let i = 0; i < 40; i++) {
    const st = await evalIn(panel, "document.getElementById('status').innerText");
    if (/^Capturing/.test(st)) return true;
    await sleep(250);
  }
  return false;
}

async function main() {
  mkdirSync(WORK, { recursive: true });
  execFileSync(join(ROOT, "tools/e2e_audio.sh"), [WORK], { stdio: "inherit" });
  writeFileSync(join(WORK, "call.html"), callHtml("Design review", "meeting.mp3", ROSTER));
  // A 1:1 where the other person never says their name: only the page knows it.
  writeFileSync(join(WORK, "call2.html"), callHtml("One to one", "farend.mp3", ["Sarah Chen"]));
  const server = await serve();

  const deepgramKey = keyFromConfig("DEEPGRAM_API_KEY");
  const anthropicKey = keyFromConfig("ANTHROPIC_API_KEY");
  if (!deepgramKey || !anthropicKey) throw new Error("both DEEPGRAM_API_KEY and ANTHROPIC_API_KEY are needed");

  await startXvfb();
  await startChrome();
  const { id } = await api("/load", { path: join(ROOT, "extension") });
  console.log("extension loaded:", id);

  // An extension page to reach chrome.storage and chrome.commands from. The
  // service worker would do, but MV3 starts it lazily, so it may not be running.
  const { targetId: extPage } = await api("/open", { url: `chrome-extension://${id}/sidepanel.html` });
  const { sessionId: ext } = await cdp("Target.attachToTarget", { targetId: extPage, flatten: true });
  await sleep(500);

  // Settings as a returning user would have them: keys, a fast model, the
  // welcome notice already acknowledged (at whatever version the panel is on).
  // No "your name", so every voice gets named.
  const noticeAck = await evalIn(ext, "typeof NOTICE_VERSION === 'number' ? NOTICE_VERSION : 1");
  await evalIn(ext, `chrome.storage.local.set(${JSON.stringify({
    deepgramKey, anthropicKey, model: "haiku", language: "en",
    context: "I built the migration service: expand and contract migrations, backfills in batches, rehearsed in CI on a copy of production data.",
    noticeAck,
  })}).then(() => true)`);
  const shortcut = await evalIn(ext,
    "chrome.commands.getAll().then((c) => (c.find((x) => x.name === '_execute_action') || {}).shortcut || '')");
  check("the toolbar action has a keyboard shortcut", !!shortcut, JSON.stringify(shortcut));
  await cdp("Target.closeTarget", { targetId: extPage });

  // Open the call and let it start playing.
  const { targetId: callTarget } = await api("/open", { url: `http://127.0.0.1:${HTTP}/call.html` });
  await cdp("Target.activateTarget", { targetId: callTarget });
  await sleep(2500);

  // Invoke Sparky on the call tab with the shortcut, on our own display.
  const xenv = { ...process.env, DISPLAY };
  const pressShortcut = () => {
    const wid = execFileSync("xdotool", ["search", "--onlyvisible", "--class", "google-chrome"], { env: xenv })
      .toString().trim().split("\n").pop();
    execFileSync("xdotool", ["windowfocus", "--sync", wid], { env: xenv });
    execFileSync("xdotool", ["key", "--clearmodifiers", "alt+shift+s"], { env: xenv });
  };
  pressShortcut();

  let panel = null;
  for (let i = 0; i < 40 && !panel; i++) {
    const { targetInfos } = await cdp("Target.getTargets");
    panel = targetInfos.find((t) => t.url.startsWith(`chrome-extension://${id}/sidepanel.html`));
    if (!panel) await sleep(250);
  }
  check("the shortcut opened the side panel", !!panel);
  if (!panel) throw new Error("no side panel target");
  const { sessionId } = await cdp("Target.attachToTarget", { targetId: panel.targetId, flatten: true });
  await sleep(800);

  const noticeShown = await evalIn(sessionId,
    "(() => { const g = document.getElementById('gate'); return !!g && getComputedStyle(g).display !== 'none'; })()");
  check("an acknowledged notice does not come back", !noticeShown);

  await evalIn(sessionId, "document.getElementById('startBtn').click(), true");
  check("Start captures the call tab", await untilCapturing(sessionId));
  await sleep(1500);                                  // let both Deepgram streams open
  await playFrom0(callTarget);
  const t0 = Date.now();
  let transcript = "";
  while (Date.now() - t0 < LISTEN_MS) {
    await sleep(5000);
    transcript = await evalIn(sessionId, "document.getElementById('transcript').innerText");
    const status = await evalIn(sessionId, "document.getElementById('status').innerText");
    console.log(`  .. ${Math.round((Date.now() - t0) / 1000)}s  ${transcript.split("\n").filter(Boolean).length} lines  status: ${status.slice(0, 90)}`);
    if (ROSTER.every((n) => transcript.includes(n + ":")) && /schema migration/i.test(transcript)) break;
  }
  console.log("\n--- live transcript ---\n" + transcript + "\n-----------------------");
  // Each intro gives a first name; the call page's roster turns it into the full one.
  check("'I'm Sarah' plus the roster gives Sarah Chen", /(^|\n)Sarah Chen:/.test(transcript));
  check("'this is Marcus from' plus the roster gives Marcus Lee", /(^|\n)Marcus Lee:/.test(transcript));
  check("'I'm Daniel' plus the roster gives Daniel Okafor", /(^|\n)Daniel Okafor:/.test(transcript));
  check("your own tile on the call page is not a voice name", !/Test User/.test(transcript));
  check("no voice is left as a numbered speaker", !/Speaker( \d)?:/.test(transcript), transcript);

  const people = await evalIn(sessionId,
    "(document.getElementById('people') || { innerText: '' }).innerText");
  check("the People row lists the named voices", ROSTER.every((n) => people.includes(n)), JSON.stringify(people));
  check("the People row leaves your own tile out", !/Test User/.test(people), JSON.stringify(people));

  await evalIn(sessionId, "document.getElementById('helpBtn').click(), true");
  let answer = "";
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    // Finished when the Copy chip shows, which happens once the stream ends.
    answer = await evalIn(sessionId,
      "(() => document.getElementById('copyBtn').classList.contains('hidden') ? '' : document.getElementById('answer').innerText)()");
    if (answer) break;
  }
  console.log("\n--- draft ---\n" + answer + "\n-------------");
  check("Help drafted an answer to the migration question",
    /schema migration/i.test(answer) && answer.length > 80, answer.slice(0, 200));

  const shot = await cdp("Page.captureScreenshot", { format: "png" }, sessionId);
  writeFileSync(join(WORK, "panel.png"), Buffer.from(shot.data, "base64"));
  try { execFileSync("import", ["-display", DISPLAY, "-window", "root", join(WORK, "window.png")]); } catch {}

  await evalIn(sessionId, "document.getElementById('stopBtn').click(), true");
  await sleep(4000);
  const saved = await evalIn(sessionId,
    "chrome.storage.local.get('sessions').then((s) => (s.sessions || [])[0] || null)");
  const names = Object.values((saved && saved.names) || {});
  check("the saved call keeps the names", ROSTER.every((n) => names.includes(n)), JSON.stringify(saved && saved.names));
  check("the saved call keeps the roster", ROSTER.every((n) => ((saved && saved.roster) || []).includes(n)), JSON.stringify(saved && saved.roster));
  console.log("\nsaved call:", saved && saved.title, "| names:", JSON.stringify(saved && saved.names));

  // ---- a 1:1, named from the call page alone ----
  console.log("\n== a 1:1 where the other person never says their name");
  const { targetId: call2 } = await api("/open", { url: `http://127.0.0.1:${HTTP}/call2.html` });
  await cdp("Target.activateTarget", { targetId: call2 });
  await sleep(2500);
  pressShortcut();                                  // invoke Sparky on this tab too
  await sleep(1000);
  await evalIn(sessionId, "document.getElementById('startBtn').click(), true");
  check("Start captures the 1:1 tab", await untilCapturing(sessionId));
  await sleep(1500);
  await playFrom0(call2);
  const t1 = Date.now();
  let one = "";
  while (Date.now() - t1 < 40000) {
    await sleep(5000);
    one = await evalIn(sessionId, "document.getElementById('transcript').innerText");
    if ((one.match(/(^|\n)Sarah Chen:/g) || []).length >= 2) break;
  }
  console.log("--- live transcript ---\n" + one + "\n-----------------------");
  check("the only other person on the page names the only voice",
    (one.match(/(^|\n)Sarah Chen:/g) || []).length >= 2 && !/Speaker( \d)?:/.test(one), one);
  check("the 1:1 has no introduction to go on", !/I'm Sarah|this is Sarah/i.test(one), one);
  await evalIn(sessionId, "document.getElementById('stopBtn').click(), true");
  await sleep(2000);
  console.log("screenshots:", join(WORK, "panel.png"), join(WORK, "window.png"));

  server.close();
}

main()
  .catch((e) => { console.error("e2e error:", e.message); failures++; })
  .finally(async () => {
    if (!process.env.E2E_KEEP) {
      try { await api("/quit", {}); } catch {}
      await sleep(500);
      for (const c of children) { try { c.kill(); } catch {} }
    }
    console.log("\n" + (failures ? `${failures} FAIL` : "all passed") + "\n");
    process.exit(failures ? 1 : 0);
  });
