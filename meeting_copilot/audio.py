"""Audio capture and utterance segmentation.

Capture is done by piping raw 16 kHz mono PCM out of ``ffmpeg``. The same code
path serves a live microphone and a pre-recorded file, which is what lets the
whole pipeline be exercised headlessly (feed a WAV, get the same events the mic
would produce).

The segmenter turns the continuous frame stream into discrete *utterances* using
a simple adaptive energy VAD: it accumulates speech and flushes a chunk once it
sees a long enough trailing silence. Per-utterance chunks are the natural unit
for transcription and speaker clustering alike.
"""

from __future__ import annotations

import subprocess
from collections import deque
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * 2  # int16


class AudioError(RuntimeError):
    pass


class _FfmpegSource:
    """Base: read raw s16le mono 16k frames from an ffmpeg stdout pipe."""

    def __init__(self, input_args: list[str]) -> None:
        self._cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *input_args,
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ]
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "_FfmpegSource":
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=FRAME_BYTES * 4,
            )
        except FileNotFoundError as exc:
            raise AudioError("ffmpeg not found on PATH") from exc
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
    """Live microphone via PulseAudio/PipeWire (default source)."""

    def __init__(self, device: str = "default", backend: str = "pulse") -> None:
        super().__init__(["-f", backend, "-i", device])


class FileSource(_FfmpegSource):
    """Decode any audio file to the canonical format.

    ``realtime=True`` makes ffmpeg emit samples at playback speed so the file
    behaves like a live mic (useful for demos); tests leave it off for speed.
    """

    def __init__(self, path: str, realtime: bool = False) -> None:
        args = ["-re", "-i", path] if realtime else ["-i", path]
        super().__init__(args)


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
