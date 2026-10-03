import wave

import numpy as np

from meeting_copilot.audio import (FRAME_SAMPLES, FileSource, LevelGate, UtteranceSegmenter,
                                  frame_rms)

SR = 16000


def tone_frame(freq=200.0, amp=0.15, idx=0):
    t = (np.arange(FRAME_SAMPLES) + idx * FRAME_SAMPLES) / SR
    return (np.sin(2 * np.pi * freq * t) * amp * 32768).astype(np.int16)


def silence_frame():
    return np.zeros(FRAME_SAMPLES, dtype=np.int16)


def test_segmenter_emits_one_utterance():
    seg = UtteranceSegmenter()
    out = []
    # ambient silence, then ~1.2s of tone, then enough silence to flush
    for _ in range(20):
        seg.process(silence_frame())
    for i in range(40):
        r = seg.process(tone_frame(idx=i))
        if r is not None:
            out.append(r)
    for _ in range(30):
        r = seg.process(silence_frame())
        if r is not None:
            out.append(r)
    assert len(out) == 1
    # roughly the spoken length (pre-roll + ~40 frames), in samples
    assert out[0].shape[0] > 20 * FRAME_SAMPLES


def test_segmenter_drops_too_short():
    seg = UtteranceSegmenter()
    for _ in range(20):
        seg.process(silence_frame())
    for i in range(4):  # ~120ms < min utterance
        seg.process(tone_frame(idx=i))
    out = [seg.process(silence_frame()) for _ in range(30)]
    assert all(o is None for o in out)


def test_flush_emits_pending():
    seg = UtteranceSegmenter()
    for _ in range(20):
        seg.process(silence_frame())
    for i in range(40):
        seg.process(tone_frame(idx=i))
    tail = seg.flush()
    assert tail is not None


def test_file_source_decodes_wav(tmp_path):
    # write 1s of 16k mono audio and read it back through the ffmpeg pipe
    path = tmp_path / "tone.wav"
    samples = (np.sin(2 * np.pi * 200 * np.arange(SR) / SR) * 0.2 * 32768).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(samples.tobytes())

    with FileSource(str(path)) as src:
        frames = list(src.frames())
    assert len(frames) >= 25  # ~33 frames of 30ms in 1s
    assert frames[0].shape[0] == FRAME_SAMPLES


# -- the level gate behind the mic dot ------------------------------------------
def test_frame_rms_scale():
    assert frame_rms(np.zeros(480, dtype=np.int16)) == 0.0
    full = np.full(480, 16384, dtype=np.int16)
    assert abs(frame_rms(full) - 0.5) < 1e-6


def _feed(gate, levels):
    return [(i, r) for i, lvl in enumerate(levels) if (r := gate.update(lvl)) is not None]


def test_level_gate_turns_on_at_once_and_off_after_the_hold():
    gate = LevelGate(on=0.02, off=0.01, hold_ms=300, interval_ms=0)
    out = _feed(gate, [0.0] * 5 + [0.05] * 3 + [0.0] * 20)
    assert out[0] == (5, True)
    assert out[1] == (5 + 3 + 10 - 1, False)     # 10 quiet frames of 30 ms
    assert len(out) == 2


def test_level_gate_hysteresis_keeps_it_on_between_thresholds():
    gate = LevelGate(on=0.02, off=0.01, hold_ms=90, interval_ms=0)
    out = _feed(gate, [0.05] + [0.015] * 50)     # soft speech: below on, above off
    assert out == [(0, True)]


def test_level_gate_ignores_short_dips():
    gate = LevelGate(on=0.02, off=0.01, hold_ms=300, interval_ms=0)
    out = _feed(gate, [0.05, 0.0, 0.0, 0.05, 0.0, 0.0, 0.05])
    assert out == [(0, True)]


def test_level_gate_throttles_reports():
    gate = LevelGate(on=0.02, off=0.01, hold_ms=30, interval_ms=300)
    out = _feed(gate, [0.05, 0.0, 0.0] * 10)     # would flip every few frames
    assert len(out) <= 4
    assert all(b - a >= 10 for (a, _), (b, _) in zip(out, out[1:]))
