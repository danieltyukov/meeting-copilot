// Deepgram streaming client (used in the side panel). One connection per capture
// leg: the microphone (always you) and the meeting tab (everyone else). The tab
// leg runs with diarize=true so Deepgram splits the far end into distinct
// speakers; the mic leg doesn't need it, since that leg is you by definition.
//
// Handlers receive SEGMENTS — [{ speaker, text }] — not a flat string, because a
// single Deepgram frame can span a speaker change. `speaker` is Deepgram's
// integer index, or null when diarization is off.

// Group consecutive words by speaker. Falls back to the flat transcript when a
// frame carries no word list.
function dgSegments(alt) {
  const words = alt.words || [];
  if (!words.length) {
    const t = (alt.transcript || "").trim();
    return t ? [{ speaker: null, text: t }] : [];
  }
  const segs = [];
  for (const w of words) {
    const text = (w.punctuated_word || w.word || "").trim();
    if (!text) continue;
    const speaker = typeof w.speaker === "number" ? w.speaker : null;
    const last = segs[segs.length - 1];
    if (last && last.speaker === speaker) last.text += " " + text;
    else segs.push({ speaker, text });
  }
  return segs;
}

// Concatenate two segment lists, merging across the seam when the speaker matches.
function dgJoin(a, b) {
  const out = a.map((s) => ({ speaker: s.speaker, text: s.text }));
  for (const s of b) {
    const last = out[out.length - 1];
    if (last && last.speaker === s.speaker) last.text += " " + s.text;
    else out.push({ speaker: s.speaker, text: s.text });
  }
  return out;
}

// A stream can end under a call that is still going. Deepgram closes one it has
// had no audio from for about 12 s (a stalled uplink does that) with a clean
// close frame, code 1011, which raises no error event; a network drop ends one
// too, and a dead link can also stay "open" and simply stop answering. So this
// client keeps the stream up for as long as audio comes in:
//   - the newest audio is held in a ring, back to the last final result;
//   - when the stream ends or stalls, the next audio frame opens a new one, which
//     is first sent everything since that last final result. Words already final
//     are kept, the unfinished tail is heard again, and nothing is doubled;
//   - retries are spaced out, and a stream that never once opened (a wrong key)
//     gives up after a few tries.
// All of it runs off incoming audio frames, not timers: Chrome throttles timers
// in a panel it considers hidden, and with no audio there is nothing to send.
const DG_RING_SECONDS = 30;                   // audio held for a new stream, at most
const DG_STALL_MS = 10000;                    // Deepgram answers about once a second while audio flows
const DG_PAUSE_MS = 2000;                     // no audio for this long: the capture paused, not Deepgram
const DG_RETRY_MS = [0, 500, 1000, 2000, 4000, 8000];   // wait before each try in a row; the last repeats
const DG_FIRST_TRIES = 3;                     // tries before giving up on a stream that never opened

// Why a stream ended, for the status line. 1006 is the browser's code for a
// connection that went away without a close frame.
function dgCloseReason(ev) {
  const code = ev && ev.code;
  if (!code || code === 1006) return "connection lost";
  const reason = String((ev && ev.reason) || "").replace(/\s*See https?:\S+\s*$/i, "").replace(/[.\s]+$/, "").slice(0, 120);
  return "code " + code + (reason ? ": " + reason : "");
}

function openDeepgram(config, h) {
  const rate = config.sampleRate || 16000;
  const bytesPerSec = rate * 2;               // 16-bit mono
  const params = new URLSearchParams({
    model: config.model || "nova-3",
    encoding: "linear16", sample_rate: String(rate), channels: "1",
    smart_format: "true", interim_results: "true", utterance_end_ms: "1000",
  });
  if (config.language) params.set("language", config.language);
  if (config.diarize) params.set("diarize", "true");
  const url = `wss://api.deepgram.com/v1/listen?${params}`;

  let ws = null;
  let live = false;       // ws is open and taking audio
  let ended = false;      // close() was called, or the stream never opened and gave up
  let everOpen = false;
  let fails = 0;          // streams in a row that ended without a reply
  let retryAt = 0;        // while down: when the next try is due
  let cur = [];           // segments committed so far within the current utterance
  // Positions are byte offsets into everything captured since openDeepgram().
  const ring = [];        // [{ at, buf }], oldest first
  let ringBytes = 0;
  let captured = 0;
  let base = 0;           // where the open stream's time 0 falls
  let finalAt = 0;        // results are final up to here
  let heardAt = 0;        // when the stream last said anything: opening counts, and so does being asked for
  let sentAt = 0;         // when audio last came in

  const flush = () => {
    const out = cur.filter((s) => s.text.trim());
    cur = [];
    if (out.length && h.onFinal) h.onFinal(out);
  };

  function onMessage(ev) {
    heardAt = Date.now();
    fails = 0;
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === "UtteranceEnd") return flush();
    if (d.type !== "Results") return;
    // Final results tile the stream with no gaps, so the end of the newest one
    // is how far the audio is settled.
    if (d.is_final && typeof d.start === "number" && typeof d.duration === "number") {
      finalAt = Math.max(finalAt, base + Math.round((d.start + d.duration) * rate) * 2);
    }
    const alt = d.channel && d.channel.alternatives && d.channel.alternatives[0];
    if (!alt) return;
    const segs = dgSegments(alt);
    if (d.is_final) {
      if (segs.length) { cur = dgJoin(cur, segs); h.onPartial && h.onPartial(cur); }
      if (d.speech_final) flush();
    } else if (segs.length) {
      h.onPartial && h.onPartial(dgJoin(cur, segs));
    }
  }

  function connect() {
    // Browsers can't set WS headers → Deepgram auth via the subprotocol.
    const sock = new WebSocket(url, ["token", config.key]);
    sock.binaryType = "arraybuffer";
    ws = sock;
    heardAt = Date.now();
    sock.onopen = () => {
      if (sock !== ws || ended) return;
      const resumed = everOpen;
      // Its time 0: where the last stream's final results ended, or the oldest
      // audio still held when the gap outlasted the ring.
      const from = Math.max(finalAt, ring.length ? ring[0].at : captured);
      const lostMs = Math.round((from - finalAt) / bytesPerSec * 1000);
      const heldMs = Math.round((captured - from) / bytesPerSec * 1000);
      base = finalAt = from;
      for (const c of ring) {
        const skip = from - c.at;
        if (skip < c.buf.byteLength) sock.send(skip > 0 ? c.buf.slice(skip) : c.buf);
      }
      live = everOpen = true;
      heardAt = sentAt = Date.now();
      // heldMs: audio the new stream starts behind by. Deepgram works through it
      // only a little faster than real time, so lines trail for a while.
      if (h.onOpen) h.onOpen({ resumed, lostMs, heldMs });
    };
    sock.onmessage = (ev) => { if (sock === ws) onMessage(ev); };
    // No onerror: a close always follows an error, and says more.
    sock.onclose = (ev) => {
      if (sock !== ws) return;
      if (ended) { if (h.onClose) h.onClose(); return; }
      down(dgCloseReason(ev));
    };
  }

  // The stream ended, or stopped answering, and nobody asked it to.
  function down(why) {
    const sock = ws;
    ws = null;
    live = false;
    if (sock) {
      sock.onopen = sock.onmessage = sock.onclose = null;
      try { sock.close(); } catch {}
    }
    flush();                                   // what was final stays
    if (h.onPartial) h.onPartial([]);          // what was not is heard again by the next stream
    const wait = DG_RETRY_MS[Math.min(fails, DG_RETRY_MS.length - 1)];
    fails++;
    if (!everOpen && fails >= DG_FIRST_TRIES) {
      ended = true;
      if (h.onError) h.onError("Deepgram connection error (check the key / network).");
      return;
    }
    retryAt = Date.now() + wait;
    if (h.onDown) h.onDown(why, { wasOpen: everOpen });
  }

  connect();

  return {
    sendPcm(buf) {
      if (ended) return;
      ring.push({ at: captured, buf });
      captured += buf.byteLength;
      ringBytes += buf.byteLength;
      // Keep back to the last final result, and never more than the ring.
      while (ring.length > 1 && (ring[0].at + ring[0].buf.byteLength <= finalAt || ringBytes > DG_RING_SECONDS * bytesPerSec)) {
        ringBytes -= ring.shift().buf.byteLength;
      }
      const now = Date.now();
      if (now - sentAt > DG_PAUSE_MS) heardAt = now;   // audio is only now flowing again: start the clock over
      sentAt = now;
      if (!ws) {
        if (now >= retryAt) connect();
      } else if (now - heardAt > DG_STALL_MS) {
        // Open and silent, or still connecting: either way the link is dead.
        down(`no reply for ${DG_STALL_MS / 1000} s`);
      } else if (live) {
        ws.send(buf);
      }
    },
    // Seconds of audio Chrome has taken but not yet got onto the wire: it grows
    // when the uplink is slower than the audio.
    behindSeconds() { return live ? Math.round((ws.bufferedAmount || 0) / bytesPerSec) : 0; },
    close() {
      ended = true;
      live = false;
      if (!ws) return;
      try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "CloseStream" })); } catch {}
      try { ws.close(); } catch {}
    },
  };
}
