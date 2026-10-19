// Shared audio hub. ONE AudioContext, resumed up front under the Start click's
// user gesture (so it actually runs — AudioContext.resume() hangs/stays suspended
// without a gesture, which is why offscreen capture produced no audio).
//
// Primary capture path is an AudioWorkletNode (no deprecation warning). If the
// worklet module fails to load for any reason, we fall back to ScriptProcessor so
// capture still works — only then does Chrome log the harmless deprecation note.

function floatTo16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function workletUrl() {
  return (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.getURL)
    ? chrome.runtime.getURL("pcm-worklet.js")   // same-origin in the extension
    : "pcm-worklet.js";                          // relative (harness)
}

async function createAudioHub() {
  const ctx = new AudioContext();
  await ctx.resume(); // called while the Start gesture is still active

  let useWorklet = false;
  try {
    await ctx.audioWorklet.addModule(workletUrl());
    useWorklet = true;
  } catch (e) {
    console.warn("[copilot] AudioWorklet unavailable — falling back to ScriptProcessor:", e);
  }

  // A throttled boolean "is this source loud right now?" for the level dots.
  function levelGate(opts) {
    let ts = 0;
    return (rms) => {
      if (!opts.onLevel) return;
      const now = Date.now();
      if (now - ts > 200) { ts = now; opts.onLevel(rms > 0.008); }
    };
  }

  function addViaWorklet(src, onPcm, opts) {
    const node = new AudioWorkletNode(ctx, "pcm-worklet");
    src.connect(node);
    node.connect(ctx.destination); // outputs silence, but keeps the node pulling
    const gate = levelGate(opts);
    node.port.onmessage = (e) => { onPcm(e.data.pcm); gate(e.data.rms); };
    return () => { try { node.port.onmessage = null; node.disconnect(); } catch {} };
  }

  function addViaScriptProcessor(src, onPcm, opts) {
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    src.connect(proc);
    proc.connect(ctx.destination); // ScriptProcessor only runs when connected (outputs silence)
    const gate = levelGate(opts);
    proc.onaudioprocess = (ev) => {
      const f = ev.inputBuffer.getChannelData(0);
      onPcm(floatTo16(f).buffer);
      let sum = 0;
      for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
      gate(Math.sqrt(sum / f.length));
    };
    return () => { try { proc.disconnect(); } catch {} };
  }

  return {
    sampleRate: ctx.sampleRate,
    state: () => ctx.state,
    usingWorklet: () => useWorklet,
    // A Reconnect click is a fresh user gesture, so a context Chrome suspended
    // mid-call can run again.
    resume: () => ctx.resume().catch(() => {}),

    // opts: { playback: bool (route to speakers — true for tab so you still hear
    //         the call, false for mic so you don't echo), onLevel: (active)=>void }
    addSource(mediaStream, onPcm, opts = {}) {
      const src = ctx.createMediaStreamSource(mediaStream);
      if (opts.playback) src.connect(ctx.destination); // keep the call audible

      const stopNode = useWorklet
        ? addViaWorklet(src, onPcm, opts)
        : addViaScriptProcessor(src, onPcm, opts);

      return {
        stop() {
          stopNode();
          try { src.disconnect(); } catch {}
          try { mediaStream.getTracks().forEach((t) => t.stop()); } catch {}
        },
      };
    },

    close() { try { ctx.close(); } catch {} },
  };
}
