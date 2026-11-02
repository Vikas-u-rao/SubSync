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
)


def tone_block(freq: float, sr: int, frames: int, amplitude: float, phase: float):
    t = (np.arange(frames) + phase) / sr
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


class PipelineTests(unittest.TestCase):
    sr = 48000

    def test_bass_reaches_the_ring_after_filter_and_delay(self) -> None:
        filters = FilterBank(self.sr, cutoff_hz=110, subsonic=True)
        gain = SmoothedGain(1.0)
        delay = DelayLine(self.sr, delay_ms=100)
        ring = RingBuffer(self.sr)
        target = int(0.05 * self.sr)

        primed = False
        output = []
        phase = 0.0
        for _ in range(60):  # 600 ms
            block = tone_block(60, self.sr, 480, 0.5, phase)
            phase += 480
            y = filters.process(block)
            y = gain.process(y)
            y = soft_limit(y)
            y = delay.process(y)
            ring.write(y)

            if not primed:
                if ring.fill_samples() < target:
                    output.append(np.zeros(480, dtype=np.float32))
                    continue
                primed = True
            chunk, ok = ring.read_resampled(480, 1.0)
            self.assertTrue(ok)
            output.append(chunk)

        result = np.concatenate(output)
        tail = result[-int(0.2 * self.sr) :]
        self.assertGreater(float(np.sqrt(np.mean(tail**2))), 0.1)
        self.assertLessEqual(float(np.max(np.abs(result))), 1.0)

    def test_gain_and_limiter_keep_a_hot_signal_bounded(self) -> None:
        filters = FilterBank(self.sr, cutoff_hz=110, subsonic=True)
        gain = SmoothedGain(3.0)
        delay = DelayLine(self.sr, delay_ms=0)
        ring = RingBuffer(self.sr)
        target = int(0.05 * self.sr)

        primed = False
        output = []
        phase = 0.0
        for _ in range(60):
            block = tone_block(60, self.sr, 480, 0.9, phase)
            phase += 480
            y = filters.process(block)
            y = gain.process(y)
            y = soft_limit(y)
            y = delay.process(y)
            ring.write(y)
            if not primed:
                if ring.fill_samples() < target:
                    continue
                primed = True
            chunk, ok = ring.read_resampled(480, 1.0)
            self.assertTrue(ok)
            output.append(chunk)

        result = np.concatenate(output)
        tail = result[-int(0.2 * self.sr) :]
        self.assertLessEqual(float(np.max(np.abs(tail))), 1.0)
        self.assertGreater(float(np.sqrt(np.mean(tail**2))), 0.2)


if __name__ == "__main__":
    unittest.main()
