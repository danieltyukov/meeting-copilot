import wave

import numpy as np
import pytest

from meeting_copilot import audio
from meeting_copilot.audio import (FRAME_SAMPLES, AudioError, FileSource, LevelGate, MicSource,
                                   UtteranceSegmenter, frame_rms, mic_input_args,
                                   parse_avfoundation_devices, parse_dshow_devices,
                                   parse_pulse_sources)

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


# -- the microphone on each desktop ---------------------------------------------
PULSE = """Auto-detected sources for pulse:
* alsa_input.usb-046d_Brio_105-02.mono-fallback [Brio 105 Mono] (none)
  alsa_output.pci-0000_00_1f.3.analog-stereo.monitor [Monitor of Built-in Audio] (none)
"""

AVFOUNDATION = """[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera  [uid:0x1420000005ac8600]
[AVFoundation indev @ 0x7f8] [1] Capture screen 0
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Pro Microphone  [uid:BuiltInMicrophoneDevice]
[AVFoundation indev @ 0x7f8] [1] BlackHole 2ch
: Input/output error
"""

DSHOW = """[dshow @ 000001f0] "Integrated Camera" (video)
[dshow @ 000001f0]   Alternative name "@device_pnp_\\\\?\\usb#vid_04f2"
[dshow @ 000001f0] "Microphone Array (Realtek(R) Audio)" (audio)
[dshow @ 000001f0]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{1}"
[dshow @ 000001f0] "Mikrofon (USB Audio Device)" (audio)
dummy: Immediate exit requested
"""

DSHOW_OLD = """[dshow @ 0000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000]  "Integrated Camera"
[dshow @ 0000]     Alternative name "@device_pnp_usb"
[dshow @ 0000] DirectShow audio devices
[dshow @ 0000]  "Microphone (Realtek High Definition Audio)"
[dshow @ 0000]     Alternative name "@device_cm_wave"
"""


def test_parse_pulse_sources_marks_the_default():
    mics = parse_pulse_sources(PULSE)
    assert [m.name for m in mics] == ["alsa_input.usb-046d_Brio_105-02.mono-fallback",
                                      "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"]
    assert mics[0].label == "Brio 105 Mono" and mics[0].default and not mics[1].default


def test_parse_avfoundation_takes_audio_devices_only():
    mics = parse_avfoundation_devices(AVFOUNDATION)
    assert [m.name for m in mics] == ["MacBook Pro Microphone", "BlackHole 2ch"]
    assert mics[0].label == "index 0"


def test_parse_dshow_both_formats():
    assert [m.name for m in parse_dshow_devices(DSHOW)] == [
        "Microphone Array (Realtek(R) Audio)", "Mikrofon (USB Audio Device)"]
    assert [m.name for m in parse_dshow_devices(DSHOW_OLD)] == [
        "Microphone (Realtek High Definition Audio)"]
    assert parse_dshow_devices(DSHOW)[0].default


def test_mic_input_per_desktop(monkeypatch):
    assert mic_input_args(None, "linux") == ["-f", "pulse", "-i", "default"]
    assert mic_input_args("alsa_input.x", "linux") == ["-f", "pulse", "-i", "alsa_input.x"]
    assert mic_input_args(None, "darwin") == ["-f", "avfoundation", "-i", ":default"]
    assert mic_input_args("BlackHole 2ch", "darwin")[-1] == ":BlackHole 2ch"

    monkeypatch.setattr(audio, "list_mics", lambda platform: parse_dshow_devices(DSHOW))
    win = mic_input_args(None, "win32")
    assert win[:2] == ["-f", "dshow"] and win[-1] == "audio=Microphone Array (Realtek(R) Audio)"
    assert mic_input_args("Mikrofon (USB Audio Device)", "win32")[-1] == \
        "audio=Mikrofon (USB Audio Device)"


def test_no_windows_mic_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(audio, "list_mics", lambda platform: [])
    with pytest.raises(AudioError, match="--list-mics"):
        mic_input_args(None, "win32")


def test_mic_source_resolves_its_input_on_start(monkeypatch):
    calls = []
    monkeypatch.setattr(audio, "mic_input_args", lambda device: calls.append(device) or ["-i", "x"])
    src = MicSource("Brio")
    assert calls == []                      # nothing runs until the meeting starts
    assert src.command()[4:6] == ["-i", "x"] and calls == ["Brio"]
