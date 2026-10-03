"""Audio capture and utterance segmentation.

Capture is done by piping raw 16 kHz mono PCM out of ``ffmpeg``. The same code
path serves a live microphone and a pre-recorded file, which is what lets the
whole pipeline be exercised headlessly (feed a WAV, get the same events the mic
would produce). Only the ffmpeg input differs per desktop: PulseAudio on Linux
(PipeWire serves the same API), AVFoundation on macOS, DirectShow on Windows.

The segmenter turns the continuous frame stream into discrete *utterances* using
a simple adaptive energy VAD: it accumulates speech and flushes a chunk once it
sees a long enough trailing silence. Per-utterance chunks are the natural unit
for transcription and speaker clustering alike.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .exe import find_executable

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * 2  # int16


class AudioError(RuntimeError):
    pass


def _ffmpeg() -> str:
    path = find_executable("ffmpeg")
    if path is None:
        raise AudioError("ffmpeg not found on PATH")
    return path


class _FfmpegSource:
    """Base: read raw s16le mono 16k frames from an ffmpeg stdout pipe."""

    def __init__(self, input_args: list[str] | None = None) -> None:
        self._input_args = input_args or []
        self._proc: subprocess.Popen | None = None

    def _inputs(self) -> list[str]:
        return self._input_args

    def command(self) -> list[str]:
        return [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            *self._inputs(),
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ]

    def __enter__(self) -> "_FfmpegSource":
        cmd = self.command()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=FRAME_BYTES * 4,
            )
        except OSError as exc:
            raise AudioError(f"could not run ffmpeg: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def frames(self) -> Iterator[np.ndarray]:
        if self._proc is None or self._proc.stdout is None:
            raise AudioError("source not started; use as a context manager")
        stdout = self._proc.stdout
        while True:
            buf = stdout.read(FRAME_BYTES)
            if not buf or len(buf) < FRAME_BYTES:
                break
            yield np.frombuffer(buf, dtype=np.int16)
        # Surface a startup failure (e.g. no such device) as a clear error.
        ret = self._proc.poll()
        if ret not in (None, 0):
            err = b""
            if self._proc.stderr is not None:
                err = self._proc.stderr.read() or b""
            msg = err.decode("utf-8", "replace").strip()
            raise AudioError(msg or f"ffmpeg exited with code {ret}")


class MicSource(_FfmpegSource):
    """Live microphone. ``device`` picks one by the name ``list_mics`` gives;
    without it the system default is used."""

    def __init__(self, device: str | None = None) -> None:
        super().__init__()
        self.device = device

    def _inputs(self) -> list[str]:
        # Resolved on start rather than here: finding a Windows mic runs
        # ffmpeg, and a failure is then reported like any other capture error.
        return mic_input_args(self.device)


class FileSource(_FfmpegSource):
    """Decode any audio file to the canonical format.

    ``realtime=True`` makes ffmpeg emit samples at playback speed so the file
    behaves like a live mic (useful for demos); tests leave it off for speed.
    """

    def __init__(self, path: str, realtime: bool = False) -> None:
        args = ["-re", "-i", path] if realtime else ["-i", path]
        super().__init__(args)


def _desktop(platform: str) -> str:
    return platform if platform in ("darwin", "win32") else "linux"


def mic_input_args(device: str | None = None, platform: str = sys.platform) -> list[str]:
    """The ffmpeg input flags for a microphone on this desktop."""
    desktop = _desktop(platform)
    if desktop == "darwin":
        return ["-f", "avfoundation", "-i", f":{device or 'default'}"]
    if desktop == "win32":
        # DirectShow has no "default" device, so take the first microphone.
        name = device or next((m.name for m in list_mics(platform)), None)
        if not name:
            raise AudioError("no microphone found. Plug one in, or pick one with --mic "
                             "(meeting-copilot --list-mics shows them)")
        # DirectShow hands audio over in 500 ms blocks unless told otherwise.
        return ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={name}"]
    return ["-f", "pulse", "-i", device or "default"]


@dataclass
class Mic:
    name: str             # what --mic takes
    label: str = ""       # the system's description, when it differs from the name
    default: bool = False


_LIST_ARGS = {
    "linux": ["-sources", "pulse"],
    "darwin": ["-f", "avfoundation", "-list_devices", "true", "-i", ""],
    "win32": ["-list_devices", "true", "-f", "dshow", "-i", "dummy"],
}


def list_mics(platform: str = sys.platform) -> list[Mic]:
    """The audio inputs ffmpeg can open here, the one used by default marked."""
    desktop = _desktop(platform)
    try:
        # The device lists go to stderr, and the macOS and Windows ones exit
        # non-zero by design (there is no real input to open).
        proc = subprocess.run([_ffmpeg(), "-hide_banner", *_LIST_ARGS[desktop]],
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=15)
    except OSError as exc:
        raise AudioError(f"could not run ffmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg took too long to list the audio devices") from exc
    parse = {"linux": parse_pulse_sources, "darwin": parse_avfoundation_devices,
             "win32": parse_dshow_devices}[desktop]
    return parse(proc.stdout + proc.stderr)


_PULSE_LINE = re.compile(r"^(?P<mark>[* ]) (?P<name>\S+)(?: \[(?P<label>.*?)\])?")


def parse_pulse_sources(text: str) -> list[Mic]:
    """``ffmpeg -sources pulse``: one source per line, the default starred."""
    mics = []
    for line in text.splitlines():
        m = _PULSE_LINE.match(line)
        if m:
            mics.append(Mic(m["name"], m["label"] or "", m["mark"] == "*"))
    return mics


_AVF_LINE = re.compile(r"\]\s\[(?P<index>\d+)\] (?P<name>.+?)(?:\s+\[uid:.*)?$")


def parse_avfoundation_devices(text: str) -> list[Mic]:
    """``-f avfoundation -list_devices true``: video devices, then audio ones."""
    mics, audio = [], False
    for line in text.splitlines():
        if "AVFoundation audio devices" in line:
            audio = True
        elif "AVFoundation video devices" in line:
            audio = False
        elif audio and (m := _AVF_LINE.search(line)):
            mics.append(Mic(m["name"].strip(), f"index {m['index']}"))
    return mics


_DSHOW_LINE = re.compile(r'\]\s*"(?P<name>[^"]+)"\s*(?:\((?P<kinds>[^)]*)\))?\s*$')


def parse_dshow_devices(text: str) -> list[Mic]:
    """``-list_devices true -f dshow``. ffmpeg 5 and later tag each device
    ``(audio)`` or ``(video)``; older builds list them under section headings."""
    mics, section = [], None
    for line in text.splitlines():
        low = line.lower()
        if "directshow audio devices" in low:
            section = "audio"
        elif "directshow video devices" in low:
            section = "video"
        elif "alternative name" not in low and (m := _DSHOW_LINE.search(line)):
            kinds = m["kinds"]
            if (kinds is not None and "audio" in kinds) or (kinds is None and section == "audio"):
                mics.append(Mic(m["name"]))
    if mics:
        mics[0].default = True       # what mic_input_args falls back to
    return mics


def _rms(frame_f32: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame_f32 * frame_f32)) + 1e-12)


def frame_rms(frame_int16: np.ndarray) -> float:
    """Level of one int16 frame on a 0..1 scale. Cheap enough for every frame."""
    f = frame_int16.astype(np.float32)
    return float(np.sqrt(np.mean(f * f))) / 32768.0


class LevelGate:
    """Per-frame levels in, a throttled "someone is speaking" flag out.

    Drives the mic dot. It turns on above ``on`` and off only after ``hold_ms``
    below ``off`` (hysteresis, so the dot does not flicker between words), and a
    change is reported at most once per ``interval_ms``. Time is counted in
    frames, so it needs no clock. ``update`` returns the new state when it
    should be reported, else None.
    """

    def __init__(self, on: float = 0.012, off: float = 0.006, hold_ms: int = 300,
                 interval_ms: int = 200) -> None:
        self.on, self.off = on, off
        self.hold_frames = max(1, hold_ms // FRAME_MS)
        self.interval_frames = max(1, interval_ms // FRAME_MS)
        self.state = False           # last reported
        self._want = False
        self._quiet = 0              # frames below `off` while on
        self._since = self.interval_frames  # frames since the last report

    def update(self, level: float) -> bool | None:
        self._since += 1
        if level >= self.on:
            self._want, self._quiet = True, 0
        elif level < self.off:
            if self._want:
                self._quiet += 1
                if self._quiet >= self.hold_frames:
                    self._want, self._quiet = False, 0
        else:
            self._quiet = 0          # between the thresholds: keep what we have
        if self._want != self.state and self._since >= self.interval_frames:
            self.state, self._since = self._want, 0
            return self.state
        return None


class UtteranceSegmenter:
    """Group frames into utterances separated by silence.

    Maintains an adaptive noise floor so it works in both quiet and noisy rooms.
    ``process`` returns a float32 mono chunk when an utterance completes,
    otherwise ``None``. Call ``flush`` at the end to emit any trailing speech.
    """

    def __init__(
        self,
        silence_hangover_ms: int = 700,
        min_utterance_ms: int = 350,
        max_utterance_ms: int = 14000,
        pre_roll_ms: int = 150,
        energy_factor: float = 2.6,
        abs_floor: float = 0.006,
    ) -> None:
        self.hangover_frames = silence_hangover_ms // FRAME_MS
        self.min_frames = min_utterance_ms // FRAME_MS
        self.max_frames = max_utterance_ms // FRAME_MS
        self.energy_factor = energy_factor
        self.abs_floor = abs_floor
        self._pre_roll: deque[np.ndarray] = deque(maxlen=max(1, pre_roll_ms // FRAME_MS))
        self._noise = abs_floor
        self._buf: list[np.ndarray] = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_frames = 0

    def _threshold(self) -> float:
        return max(self.abs_floor, self._noise * self.energy_factor)

    def process(self, frame_int16: np.ndarray) -> np.ndarray | None:
        frame = frame_int16.astype(np.float32) / 32768.0
        level = _rms(frame)
        voiced = level > self._threshold()

        if not self._in_speech:
            self._pre_roll.append(frame)
            if voiced:
                # Onset: seed with pre-roll so we don't clip the first word.
                self._in_speech = True
                self._buf = list(self._pre_roll)
                self._speech_frames = 1
                self._silence_run = 0
            else:
                # Track ambient noise only while idle.
                self._noise = 0.97 * self._noise + 0.03 * level
            return None

        # In speech.
        self._buf.append(frame)
        if voiced:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        if self._silence_run >= self.hangover_frames or len(self._buf) >= self.max_frames:
            return self._finish()
        return None

    def _finish(self) -> np.ndarray | None:
        buf, speech = self._buf, self._speech_frames
        self._buf = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_frames = 0
        self._pre_roll.clear()
        if speech < self.min_frames:
            return None  # too short, likely a cough/click
        return np.concatenate(buf).astype(np.float32)

    def flush(self) -> np.ndarray | None:
        if self._in_speech:
            return self._finish()
        return None
