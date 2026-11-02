# Bass Mirror

Play the full song through your headphones and send a **mono, low-passed,
delayed copy** to a speaker, so you hear the music and feel the bass.

Windows only. The headphones are never processed, so the headphone path adds
no latency and needs no virtual audio cable.

```
app audio -> Windows default output (headphones, full range)
                |
        WASAPI loopback capture
                |
   stereo -> mono -> 30 Hz high-pass (optional)
                |
   110 Hz Linkwitz-Riley 4th-order low-pass
                |
      gain -> soft limiter -> delay (0-500 ms)
                |
   ring buffer + drift correction -> speaker (WASAPI shared mode)
```

## Requirements

* Windows 10 or 11
* Python 3.10+ (the included virtual environment uses 3.13)
* Two output devices: the headphones you listen to and a speaker (or a
  subwoofer) for the bass

## Install and run

```bat
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Or double-click `run.bat` after the virtual environment is set up.

## Using it

1. **Set the headphones as the Windows default output.** The app captures the
   default output's WASAPI loopback. If your music goes somewhere else, choose
   that device's loopback in **Capture** instead.
2. **Choose the speaker** in **Output**. Do not choose the same endpoint you
   are capturing; the app blocks that to avoid a feedback loop.
3. Start some music, press **Start**, and raise **Bass gain**.
4. Tune the **sync delay** by ear with a track that has a sharp kick. Increase
   it until the thump and the headphones line up. Low frequencies are
   forgiving: 10-20 ms is barely noticeable, 50 ms feels wrong.
5. Set **Crossover** around 80-150 Hz. Higher sends more of the kick to the
   speaker; lower keeps it cleaner. Start at 110 Hz.
6. Leave **Subsonic protection** on unless you have a reason not to. It removes
   energy below 30 Hz that small speakers cannot reproduce.

Settings are saved to `%APPDATA%\BassMirror\settings.json` and restored on the
next launch.

The app follows Windows default-output changes while running and reconnects
automatically. Click **Refresh** after plugging in a new device while stopped.

## Sync, in detail

The speaker copy must arrive at about the same time as the headphone sound:

```
added_delay = headphone_latency - speaker_latency
```

A delay line can only add delay, so this works when the speaker path is faster
than the headphone path. A wired speaker with Bluetooth headphones is the
ideal case; Bluetooth adds roughly 40-250 ms depending on the codec. Both
Bluetooth is usually the wrong way around and would require delaying the
headphones through a virtual cable, which is not implemented.

The app adds a fixed ~50 ms internal buffer plus the output device's own
latency (shown in the status line) on top of the **Sync delay** slider. Tune
with the slider; the displayed numbers are for reference, not calibration.

Video lip-sync is a separate problem: the player compensates for one device's
latency only, so audio and video may disagree even when the two audio paths
agree.

## Implementation notes

| File | Purpose |
|---|---|
| `bass_mirror/dsp.py` | Mono sum, LR4 crossover, subsonic HPF, gain ramp, soft limiter, delay line, ring buffer |
| `bass_mirror/devices.py` | WASAPI output enumeration, PyAudioWPatch loopback capture |
| `bass_mirror/engine.py` | Capture thread, DSP worker, render callback, drift controller, watchdog |
| `bass_mirror/ui.py` | Tkinter UI, level meter, start/stop, live controls |
| `bass_mirror/settings.py` | JSON settings |
| `docs/build-spec.md` | The original build brief |

Details that matter:

* **Loopback capture** uses `pyaudiowpatch` in shared mode at the endpoint's mix
  format (usually 48 kHz float32 stereo). The first two channels are summed to
  mono.
* **Linkwitz-Riley 4th order** is two identical 2nd-order Butterworth low-passes
  (Q = 0.707) in series. The crossover is computed with `scipy.signal.butter`
  and run with `sosfilt`, keeping `zi` between blocks.
* **Click-free cutoff changes:** the new filter runs in parallel for 40 ms and
  is cross-faded in. Rapid slider drags are coalesced.
* **Delay changes:** small changes glide; jumps larger than 20 ms cross-fade
  between the old and new taps.
* **Drift correction:** a 50 ms target fill, nudged by at most +/-0.1% in the
  resampling ratio. A 1 s ring buffer absorbs jitter; if a long stall leaves
  more than 120 ms queued, the app trims the stale audio rather than keeping a
  permanent sync offset.
* **Silence:** WASAPI loopback sends no packets when nothing plays. The DSP
  worker detects the gap and clears the delay/filter history, so a new song
  does not replay the tail of the last one. The renderer outputs silence and
  primes the buffer again.
* **Device changes:** a watchdog polls the default loopback and the selected
  endpoints every 1.5 s. The UI stops and restarts the engine when needed.
* **Speaker protection:** 30 Hz high-pass on by default, gain capped at 3x, and
  a soft limiter that is linear below 0.7 and asymptotically bounded at 1.0.
* **Thread affinity:** the PyAudio instance is created on the Tk main thread, so
  stream open/start/stop also run there. Starting a stream from a worker thread
  can fail with `OSError: [Errno -9999] Unanticipated host error`. Device
  enumeration from the watchdog thread is safe.

## Diagnostics

List every loopback and WASAPI output the app can see:

```bat
.venv\Scripts\python tools\capture_probe.py --list
```

Record the current default output to a WAV (milestone 1 from the brief) to
check that loopback is delivering samples:

```bat
.venv\Scripts\python tools\capture_probe.py --seconds 10 --output probe.wav
```

## Tests

```bat
.venv\Scripts\python -m unittest discover -s tests -t . -v
```

The tests cover mono downmix, filter response, subsonic attenuation, click-free
cutoff changes, gain ramps, limiting, delay timing, live delay changes, ring
buffer wrap/underrun, and variable-rate reads.

## Known limitations

* Exclusive-mode players bypass the Windows mixer and cannot be loopback
  captured. DRM-protected audio may also be silent.
* Auto-calibration with a microphone is not implemented; tune by ear.
* There is no virtual-cable mode for delaying the headphones. If the speaker is
  slower than the headphones, this app cannot fix the sync.
* Settings are global, not saved per device pair. System-tray mode is not
  implemented.
* Python and Tkinter add some jitter; 10 ms blocks and a 50 ms target buffer are
  a practical compromise. If you hear crackle, close CPU-heavy apps or raise
  the buffer in `engine.py` (`TARGET_FILL_S`).

## Troubleshooting

**No bass / level meter stays at -inf**
The capture is probably the wrong endpoint. Set the headphones as the Windows
default output, or select the matching `[Loopback]` device in **Capture**.

**Crackle or dropouts**
Bluetooth output has fixed latency and limited bandwidth. Keep the speaker
close to the PC, stop other audio apps, and avoid 5 ms buffers. The app already
uses 10 ms blocks.

**Feedback or a rising rumble**
Capture and speaker are the same physical endpoint, or a mirroring/virtual
device routes the bass back into the capture. Pick a different speaker.

**Music plays on both devices at full range**
This app does not reroute the main stream. It only adds the bass copy. If
Nahimic or another mixer mirrors the full mix to the speaker, disable that
mirroring or select a different capture endpoint.
