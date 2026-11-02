from __future__ import annotations

import math
import unittest

import numpy as np

from bass_mirror.dsp import (
    DelayLine,
    FilterBank,
    RingBuffer,
    SmoothedGain,
    soft_limit,
    to_mono,
)


def sine(freq: float, seconds: float, sr: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def run_filter(bank: FilterBank, signal: np.ndarray, block: int = 480) -> np.ndarray:
    parts = []
    for start in range(0, signal.size, block):
        parts.append(bank.process(signal[start : start + block]))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def rms(signal: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))


class MonoSumTests(unittest.TestCase):
    def test_stereo_sum(self) -> None:
        data = np.array([0.5, 0.1, -0.5, 0.3], dtype=np.float32)
        mono = to_mono(data, 2)
        np.testing.assert_allclose(mono, [0.3, -0.1], atol=1e-7)

    def test_mono_passthrough(self) -> None:
        data = np.array([0.1, -0.2], dtype=np.float32)
        np.testing.assert_allclose(to_mono(data, 1), data)


class FilterTests(unittest.TestCase):
    sr = 48000

    def test_low_pass_keeps_bass_and_rejects_mids(self) -> None:
        bank = FilterBank(self.sr, cutoff_hz=110, subsonic=True)
        bass = run_filter(bank, sine(50, 1.5, self.sr))
        self.assertGreater(rms(bass[24000:]), 0.25)

        mids = run_filter(FilterBank(self.sr, 110, True), sine(1000, 1.5, self.sr))
        self.assertLess(rms(mids[24000:]), 0.01)

    def test_subsonic_toggle_removes_rumble(self) -> None:
        with_hpf = run_filter(FilterBank(self.sr, 200, True), sine(15, 1.5, self.sr))
        without = run_filter(FilterBank(self.sr, 200, False), sine(15, 1.5, self.sr))
        self.assertLess(rms(with_hpf[24000:]), rms(without[24000:]) * 0.5)

    def test_cutoff_change_is_smooth(self) -> None:
        bank = FilterBank(self.sr, cutoff_hz=80, subsonic=True)
        signal = sine(60, 1.2, self.sr)
        block = 480
        output = []
        for start in range(0, signal.size, block):
            if start == 24000:
                bank.set_params(160, True)
            output.append(bank.process(signal[start : start + block]))
        out = np.concatenate(output)
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertLess(float(np.max(np.abs(np.diff(out)))), 0.02)

    def test_near_nyquist_cutoff_is_clamped(self) -> None:
        bank = FilterBank(8000, cutoff_hz=200, subsonic=True)
        out = bank.process(np.zeros(480, dtype=np.float32))
        self.assertEqual(out.size, 480)


class GainAndLimiterTests(unittest.TestCase):
    def test_gain_ramps_to_target(self) -> None:
        gain = SmoothedGain(1.0)
        gain.target = 2.0
        out = gain.process(np.ones(100, dtype=np.float32))
        self.assertAlmostEqual(float(out[0]), 1.0, places=4)
        self.assertAlmostEqual(float(out[-1]), 2.0, places=4)
        self.assertAlmostEqual(gain.current, 2.0, places=6)

    def test_soft_limiter_bounds_without_touching_quiet_audio(self) -> None:
        data = np.array([-10.0, -0.5, 0.0, 0.5, 10.0], dtype=np.float32)
        out = soft_limit(data)
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)
        self.assertAlmostEqual(float(out[1]), -0.5, places=6)
        self.assertAlmostEqual(float(out[3]), 0.5, places=6)
        self.assertAlmostEqual(float(out[0]), -float(out[4]), places=6)


class DelayTests(unittest.TestCase):
    sr = 48000

    def test_impulse_arrives_at_requested_delay(self) -> None:
        delay = DelayLine(self.sr, max_delay_ms=500, delay_ms=100)
        signal = np.zeros(5000, dtype=np.float32)
        signal[0] = 1.0
        out = np.concatenate(
            [delay.process(signal[i : i + 480]) for i in range(0, signal.size, 480)]
        )
        peak = int(np.argmax(np.abs(out)))
        self.assertEqual(peak, 4800)
        self.assertAlmostEqual(float(out[peak]), 1.0, places=5)

    def test_zero_delay_is_passthrough(self) -> None:
        delay = DelayLine(self.sr, delay_ms=0)
        signal = sine(100, 0.1, self.sr)
        out = np.concatenate(
            [delay.process(signal[i : i + 480]) for i in range(0, signal.size, 480)]
        )
        np.testing.assert_allclose(out, signal, atol=1e-6)

    def test_large_live_change_does_not_click(self) -> None:
        delay = DelayLine(self.sr, delay_ms=0)
        warmup = np.full(self.sr // 2, 0.5, dtype=np.float32)
        for start in range(0, warmup.size, 480):
            delay.process(warmup[start : start + 480])
        delay.set_delay_ms(100)
        out = np.concatenate(
            [delay.process(np.full(480, 0.5, dtype=np.float32)) for _ in range(50)]
        )
        np.testing.assert_allclose(out, 0.5, atol=2e-3)


class RingBufferTests(unittest.TestCase):
    sr = 48000

    def test_identity_read(self) -> None:
        ring = RingBuffer(self.sr)
        ring.write(np.arange(1000, dtype=np.float32))
        out, ok = ring.read_resampled(100, 1.0)
        self.assertTrue(ok)
        np.testing.assert_allclose(out, np.arange(100, dtype=np.float32), atol=1e-6)
        self.assertAlmostEqual(ring.fill_samples(), 900.0, places=3)

    def test_underrun_returns_silence_and_resets(self) -> None:
        ring = RingBuffer(self.sr)
        ring.write(np.arange(100, dtype=np.float32))
        out, ok = ring.read_resampled(200, 1.0)
        self.assertFalse(ok)
        self.assertFalse(np.any(out))
        self.assertEqual(ring.fill_samples(), 0.0)

    def test_variable_rate_interpolates(self) -> None:
        ring = RingBuffer(self.sr)
        source = np.arange(2000, dtype=np.float32)
        ring.write(source)
        step = 48000.0 / 44100.0
        out, ok = ring.read_resampled(441, step)
        self.assertTrue(ok)
        expected = np.interp(
            np.arange(441) * step, np.arange(2000), source
        ).astype(np.float32)
        np.testing.assert_allclose(out, expected, atol=1e-2)
        self.assertAlmostEqual(ring.fill_samples(), 2000 - 441 * step, places=2)

    def test_wrap_around(self) -> None:
        ring = RingBuffer(1000, capacity_ms=10)  # 10 samples
        ring.write(np.arange(8, dtype=np.float32))
        _, ok = ring.read_resampled(6, 1.0)
        self.assertTrue(ok)
        ring.write(np.array([100, 101, 102, 103], dtype=np.float32))
        out, ok = ring.read_resampled(4, 1.0)
        self.assertTrue(ok)
        np.testing.assert_allclose(out, [6, 7, 100, 101], atol=1e-6)


if __name__ == "__main__":
    unittest.main()
