# Bass Mirror: build spec

Saved from the original brief. The working implementation is documented in the
top-level `README.md`; this file is the design record and the v2 wish list.

**Goal:** the headphones play the full song, while the speaker plays only the
low frequencies (a mono, filtered, delayed copy), so you hear the song and feel
the bass.

## 1. Signal flow

```
App audio -> Windows default output (headphones, full range)
                |
        WASAPI loopback capture (same stream)
                |
   Stereo -> mono sum -> subsonic high-pass (~30 Hz)
                |
   Low-pass crossover (~110 Hz, Linkwitz-Riley 4th order)
                |
        Gain -> soft limiter -> delay line
                |
   Ring buffer (+ drift correction) -> speaker output (WASAPI)
```

The headphones are never processed, so their path adds no latency and needs no
virtual audio cable. Nahimic isn't involved; the app replaces its sharing
feature.

## 2. Components

1. **Capture thread:** opens a WASAPI loopback capture on the headphone render
   device, in shared mode, using the device's mix format (usually 48 kHz,
   32-bit float, stereo). It pushes frames into a ring buffer.
2. **DSP stage:** runs on the capture thread or a separate worker.
   - Mono sum: `(L + R) / 2`.
   - Subsonic high-pass: 2nd-order Butterworth at 25-35 Hz, to protect the
     speaker from inaudible energy.
   - Low-pass crossover: a Linkwitz-Riley 4th-order low-pass at 80-150 Hz is
     two identical 2nd-order Butterworth low-passes in series (cascaded
     biquads, Q = 0.707). Make the cutoff adjustable and recompute the
     coefficients when the slider moves.
   - Gain: a linear multiplier, 0 to about 3x.
   - Soft limiter: `tanh`-style or a simple look-ahead limiter, so loud kicks
     don't clip the speaker.
3. **Delay line:** a circular buffer holding `delay_ms * sampleRate / 1000`
   samples. Range 0-500 ms, adjustable live.
4. **Render thread:** opens the speaker device with WASAPI, shared mode,
   event-driven, and pulls from the ring buffer. If the speaker's sample rate
   differs from the capture rate, resample.
5. **Drift corrector:** capture and render run on different clocks. Keep the
   ring buffer near a target fill level (say 40-60 ms). If it grows or shrinks,
   nudge the resampling ratio by about +/-0.05-0.1%. Don't drop samples
   abruptly, because you'd hear it as clicks.
6. **UI:** device pickers, sliders, and a start/stop button (see section 5).

## 3. Sync, the part that decides whether it feels good

The speaker's bass must arrive at the same time as the headphones' sound.

```
speaker_delay = headphone_latency - speaker_latency   (must be >= 0)
```

- A delay line can only add delay, so this works when the speaker path is
  faster than the headphone path.
- A wired speaker with Bluetooth headphones is the ideal case. Bluetooth adds
  roughly 40-250 ms depending on the codec.
- If the speaker is the slower device (both Bluetooth, for example), the fix is
  to delay the headphones instead. That requires the virtual-cable route, so
  treat it as a v2 feature.
- **Tuning by ear:** play a song with a sharp kick, then adjust the delay until
  the thump and the sound line up. Low frequencies are forgiving, since
  10-20 ms off is barely noticeable and 50 ms or more feels wrong.
- **Optional auto-calibration (v2):** play a click through each path, record it
  with a microphone, and measure the difference.
- **Video:** the player compensates for one device's latency only, so lip-sync
  may be off even if your two audio paths agree.

## 4. Edge cases to handle

- **Silence:** WASAPI loopback delivers no packets when nothing is playing, so
  the render side must output silence instead of underrunning and glitching.
- **Default device changes:** if the headphones disconnect or the default
  switches, the loopback stream dies. Listen for device notifications
  (`IMMNotificationClient`) and reopen the capture.
- **Sample rate differences:** the capture and speaker rates may differ (44.1
  vs 48 kHz), so resample as needed.
- **Exclusive-mode apps and protected content:** exclusive-mode apps bypass the
  mixer, and DRM-protected streams may not loopback at all.
- **Speaker protection:** keep a hard gain ceiling and the subsonic high-pass on
  by default. Small speakers can't reproduce much below 60-80 Hz, and pushing
  them there only causes distortion.
- **Volume tracking:** loopback follows the Windows volume, so the bass rises
  and falls with the headphones automatically.
- **Buffer sizes:** use small buffers (10-20 ms) on the capture and render
  sides, but don't go so low that you get dropouts. Bluetooth output has its
  own fixed latency you can't reduce.

## 5. UI

- Capture device (headphones), shown as a read-only default or a picker
- Output device (speaker) picker
- Crossover cutoff slider (60-200 Hz), default 110
- Bass gain slider
- Delay slider (0-500 ms)
- Subsonic filter toggle
- Start/stop button and a level meter for the bass signal
- Optional: save settings per device pair, and run in the system tray

## 6. Tech choices

| Option | Libraries | Good for |
|---|---|---|
| Python prototype | `pyaudiowpatch` (loopback), `sounddevice` (output), `scipy.signal` (`butter`, `sosfilt`) | Proving the idea in an afternoon |
| C# desktop app | NAudio (`WasapiLoopbackCapture`, `WasapiOut`, `BufferedWaveProvider`, `BiQuadFilter`) | Easiest route to a real Windows app with a UI |
| Rust or C++ | `cpal`, or raw WASAPI | Lowest latency and tightest control |

For a Python prototype, use `sosfilt` with state kept between blocks (`zi`),
otherwise you get clicks at every block boundary.

## 7. Milestones

1. Capture loopback and write 10 seconds to a WAV to confirm you're getting the
   stream.
2. Low-pass it and play it to the speaker with no delay or drift handling.
3. Add the delay line and the delay slider, then tune it by ear.
4. Add the ring buffer, drift correction, and silence handling, and run it for
   an hour to check for crackle or sync creep.
5. Add the UI, device change handling, and the limiter.
6. v2: auto-calibration and the virtual-cable mode for a slower speaker.

## 8. Quick test before you code

Voicemeeter Banana can route one input to multiple outputs with per-output EQ
and delay, so you can confirm the idea feels good with your actual hardware
before writing anything.
