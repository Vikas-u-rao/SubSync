"""DSP building blocks for Bass Mirror.

Everything here is mono, float32 in the range [-1, 1], at the capture
device's sample rate.  The DSP thread owns these objects; only the ring
buffer is shared with the render callback.
"""

from __future__ import annotations

import math
import threading

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

SUBSONIC_HZ = 30.0
MIN_CUTOFF_HZ = 60.0
MAX_CUTOFF_HZ = 200.0


def to_mono(samples: np.ndarray, channels: int) -> np.ndarray:
    """Downmix interleaved float32 samples to mono.

    The first two channels are used when more than two are present; the
    loopback of a stereo endpoint normally delivers exactly two.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if channels <= 1:
        return samples.reshape(-1)
    frames = samples.reshape(-1, channels)
    if channels >= 2:
        return frames[:, :2].mean(axis=1, dtype=np.float32)
    return frames.mean(axis=1, dtype=np.float32)


def design_filters(
    sample_rate: float, cutoff_hz: float, subsonic: bool = True
) -> np.ndarray:
    """Return an SOS cascade: optional 30 Hz HPF + LR4 low-pass.

    A Linkwitz-Riley 4th order low-pass is two identical 2nd order
    Butterworth sections (Q = 0.707) in series.
    """
    sr = float(sample_rate)
    cutoff = float(np.clip(cutoff_hz, MIN_CUTOFF_HZ, min(MAX_CUTOFF_HZ, sr * 0.45)))
    sections = []
    if subsonic:
        sections.append(butter(2, SUBSONIC_HZ, btype="highpass", fs=sr, output="sos"))
    low = butter(2, cutoff, btype="lowpass", fs=sr, output="sos")
    sections.append(low)
    sections.append(low.copy())
    return np.vstack(sections).astype(np.float64)


class FilterBank:
    """Adjustable crossover with click-free coefficient changes.

    A parameter change starts a second filter bank in parallel.  Both are
    run for ``fade_ms`` and cross-faded, then the new bank takes over.
    Small changes are therefore inaudible; rapid slider drags coalesce.
    """

    def __init__(
        self,
        sample_rate: float,
        cutoff_hz: float = 110.0,
        subsonic: bool = True,
        fade_ms: float = 40.0,
    ) -> None:
        self.sample_rate = float(sample_rate)
        self.fade_samples = max(1, int(self.sample_rate * fade_ms / 1000.0))
        self._cutoff = float(cutoff_hz)
        self._subsonic = bool(subsonic)
        self._sos = design_filters(self.sample_rate, self._cutoff, self._subsonic)
        self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)
        self._alt_sos: np.ndarray | None = None
        self._alt_zi: np.ndarray | None = None
        self._alt_cutoff = self._cutoff
        self._alt_subsonic = self._subsonic
        self._fade_pos = self.fade_samples
        self._pending: tuple[float, bool] | None = None
        self._last_sample = 0.0

    @property
    def cutoff_hz(self) -> float:
        return self._cutoff

    @property
    def subsonic(self) -> bool:
        return self._subsonic

    def set_params(self, cutoff_hz: float, subsonic: bool) -> None:
        cutoff = float(np.clip(cutoff_hz, MIN_CUTOFF_HZ, MAX_CUTOFF_HZ))
        sub = bool(subsonic)
        if self._alt_sos is not None:
            if cutoff == self._alt_cutoff and sub == self._alt_subsonic:
                self._pending = None
            else:
                # Includes dragging back to the old value: finish this fade,
                # then fade home again.
                self._pending = (cutoff, sub)
        elif cutoff == self._cutoff and sub == self._subsonic:
            self._pending = None
        else:
            self._pending = (cutoff, sub)

    def reset(self) -> None:
        """Clear filter history after a long gap in the input."""
        self._zi.fill(0.0)
        self._alt_sos = None
        self._alt_zi = None
        self._fade_pos = self.fade_samples
        self._last_sample = 0.0

    def _begin_fade(self, cutoff: float, subsonic: bool) -> None:
        self._alt_cutoff = cutoff
        self._alt_subsonic = subsonic
        self._alt_sos = design_filters(self.sample_rate, cutoff, subsonic)
        self._alt_zi = sosfilt_zi(self._alt_sos) * self._last_sample
        self._fade_pos = 0

    def _commit_alt(self) -> None:
        self._cutoff = self._alt_cutoff
        self._subsonic = self._alt_subsonic
        self._sos = self._alt_sos
        self._zi = self._alt_zi
        self._alt_sos = None
        self._alt_zi = None
        self._fade_pos = self.fade_samples

    def process(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        if self._alt_sos is None and self._pending is not None:
            cutoff, sub = self._pending
            self._pending = None
            if (cutoff, sub) != (self._cutoff, self._subsonic):
                self._begin_fade(cutoff, sub)

        main, self._zi = sosfilt(self._sos, x, zi=self._zi)
        if self._alt_sos is None:
            out = main
        else:
            alt, self._alt_zi = sosfilt(self._alt_sos, x, zi=self._alt_zi)
            n = x.size
            weights = np.ones(n, dtype=np.float64)
            remaining = self.fade_samples - self._fade_pos
            if remaining > 0:
                m = min(n, remaining)
                start = self._fade_pos / self.fade_samples
                stop = (self._fade_pos + m) / self.fade_samples
                weights[:m] = np.linspace(start, stop, m, endpoint=False)
            out = main + (alt - main) * weights
            self._fade_pos += n
            if self._fade_pos >= self.fade_samples:
                self._commit_alt()

        self._last_sample = float(x[-1])
        return out.astype(np.float32)


class SmoothedGain:
    """Linear gain with a per-block ramp to avoid zipper noise."""

    def __init__(self, gain: float = 1.0) -> None:
        self.current = float(gain)
        self.target = float(gain)

    def reset(self, gain: float | None = None) -> None:
        if gain is not None:
            self.current = float(gain)
            self.target = float(gain)
        else:
            self.current = self.target

    def process(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return x
        if abs(self.target - self.current) < 1e-6:
            return x * np.float32(self.current)
        ramp = np.linspace(self.current, self.target, x.size, dtype=np.float32)
        self.current = self.target
        return x * ramp


def soft_limit(samples: np.ndarray, threshold: float = 0.7) -> np.ndarray:
    """Transparent below the threshold, tanh-shaped above it, max 1.0."""
    x = np.asarray(samples, dtype=np.float32)
    mag = np.abs(x)
    over = mag > threshold
    if not np.any(over):
        return np.clip(x, -1.0, 1.0)
    out = x.copy()
    headroom = 1.0 - threshold
    shaped = threshold + headroom * np.tanh((mag[over] - threshold) / headroom)
    out[over] = np.sign(x[over]) * shaped
    return np.clip(out, -1.0, 1.0)


class DelayLine:
    """Fractional circular delay line, 0..500 ms, safe to change live.

    Small changes glide (a short, inaudible pitch bend).  Jumps larger than
    20 ms cross-fade between the old and new taps so there is no click.
    """

    def __init__(
        self,
        sample_rate: float,
        max_delay_ms: float = 500.0,
        delay_ms: float = 0.0,
        fade_ms: float = 20.0,
    ) -> None:
        self.sample_rate = float(sample_rate)
        self.max_delay_samples = int(self.sample_rate * max_delay_ms / 1000.0)
        self.capacity = self.max_delay_samples + int(self.sample_rate * 0.05) + 8
        self._buffer = np.zeros(self.capacity, dtype=np.float32)
        self._write = 0
        self._delay = float(
            np.clip(delay_ms, 0.0, max_delay_ms) * self.sample_rate / 1000.0
        )
        self._target = self._delay
        self._fade_len = max(1, int(self.sample_rate * fade_ms / 1000.0))
        self._fade_pos = self._fade_len
        self._fade_to = self._delay
        self._glide_tau = 0.15

    @property
    def delay_ms(self) -> float:
        return self._delay * 1000.0 / self.sample_rate

    def set_delay_ms(self, delay_ms: float) -> None:
        value = float(np.clip(delay_ms, 0.0, 500.0))
        self._target = value * self.sample_rate / 1000.0

    def reset(self) -> None:
        self._buffer.fill(0.0)
        self._write = 0
        self._fade_pos = self._fade_len
        self._fade_to = self._delay

    def _read_tap(self, base: np.ndarray, delay: float | np.ndarray) -> np.ndarray:
        pos = np.mod(base - delay, self.capacity)
        i0 = np.floor(pos).astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        i0 %= self.capacity
        i1 = (i0 + 1) % self.capacity
        return self._buffer[i0] * (1.0 - frac) + self._buffer[i1] * frac

    def process(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = x.size
        if n == 0:
            return x
        if n > self.capacity:
            x = x[-self.capacity :]
            n = x.size

        idx = (self._write + np.arange(n)) % self.capacity
        self._buffer[idx] = x
        self._write = (self._write + n) % self.capacity
        base = (self._write - n + np.arange(n)) % self.capacity

        out = np.empty(n, dtype=np.float32)

        if self._fade_pos < self._fade_len:
            remaining = self._fade_len - self._fade_pos
            m = min(n, remaining)
            start = self._fade_pos / self._fade_len
            stop = (self._fade_pos + m) / self._fade_len
            weight = np.linspace(start, stop, m, endpoint=False, dtype=np.float32)
            old = self._read_tap(base[:m], self._delay)
            new = self._read_tap(base[:m], self._fade_to)
            out[:m] = old * (1.0 - weight) + new * weight
            if n > m:
                out[m:] = self._read_tap(base[m:], self._fade_to)
            self._fade_pos += n
            if self._fade_pos >= self._fade_len:
                self._delay = self._fade_to
                self._fade_pos = self._fade_len
            return out

        diff = self._target - self._delay
        if abs(diff) > self.sample_rate * 0.02:
            self._fade_to = self._target
            self._fade_pos = 0
            weight = np.linspace(
                0.0,
                min(n, self._fade_len) / self._fade_len,
                min(n, self._fade_len),
                endpoint=False,
                dtype=np.float32,
            )
            m = min(n, self._fade_len)
            old = self._read_tap(base[:m], self._delay)
            new = self._read_tap(base[:m], self._fade_to)
            out[:m] = old * (1.0 - weight) + new * weight
            if n > m:
                out[m:] = self._read_tap(base[m:], self._fade_to)
            self._fade_pos += n
            if self._fade_pos >= self._fade_len:
                self._delay = self._fade_to
                self._fade_pos = self._fade_len
            return out

        if abs(diff) > 1e-9:
            old = self._delay
            alpha = 1.0 - math.exp(-n / (self.sample_rate * self._glide_tau))
            new = old + diff * alpha
            delays = np.linspace(old, new, n, endpoint=True, dtype=np.float64)
            out[:] = self._read_tap(base, delays)
            self._delay = new
            return out

        out[:] = self._read_tap(base, self._delay)
        return out


class RingBuffer:
    """Thread-safe mono FIFO with a variable-rate read for resampling.

    The writer (DSP thread) and reader (PortAudio callback) share a lock.
    Reads interpolate linearly, which is enough for a clock-nudge of at
    most +/-0.1%.
    """

    def __init__(self, sample_rate: float, capacity_ms: float = 1000.0) -> None:
        self.sample_rate = float(sample_rate)
        self.capacity = max(1024, int(self.sample_rate * capacity_ms / 1000.0))
        self._buffer = np.zeros(self.capacity, dtype=np.float32)
        self._write = 0
        self._read = 0.0
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._buffer.fill(0.0)
            self._write = 0
            self._read = 0.0

    def fill_samples(self) -> float:
        with self._lock:
            return max(0.0, self._write - self._read)

    def fill_seconds(self) -> float:
        return self.fill_samples() / self.sample_rate

    def write(self, samples: np.ndarray) -> None:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = x.size
        if n == 0:
            return
        with self._lock:
            if n >= self.capacity:
                x = x[-self.capacity :]
                n = x.size
            idx = (self._write + np.arange(n)) % self.capacity
            self._buffer[idx] = x
            self._write += n
            if self._write - self._read > self.capacity:
                self._read = float(self._write - self.capacity)

    def align_fill(self, seconds: float) -> None:
        """Drop or pad history so the buffer holds roughly ``seconds``."""
        with self._lock:
            self._read = float(max(0, self._write - int(seconds * self.sample_rate)))

    def read_resampled(
        self, frames: int, step: float
    ) -> tuple[np.ndarray, bool]:
        """Return ``frames`` output samples, consuming ``frames * step``.

        Returns ``(samples, ok)``.  On underrun the read pointer is reset to
        the write position and a block of silence is returned.
        """
        if frames <= 0:
            return np.zeros(0, dtype=np.float32), True
        step = max(0.05, float(step))
        with self._lock:
            start = int(math.floor(self._read))
            span = (frames - 1) * step if frames > 1 else 0.0
            count = int(math.ceil(span)) + 2
            if start + count > self._write:
                self._read = float(self._write)
                return np.zeros(frames, dtype=np.float32), False

            idx = (start + np.arange(count)) % self.capacity
            src = self._buffer[idx]
            local = (self._read - start) + np.arange(frames) * step
            i0 = local.astype(np.int64)
            frac = (local - i0).astype(np.float32)
            out = src[i0] * (1.0 - frac) + src[i0 + 1] * frac
            self._read += frames * step
            return out.astype(np.float32), True
