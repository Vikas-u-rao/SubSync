"""Capture -> DSP -> speaker engine."""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass, replace

import numpy as np
import pyaudiowpatch as pyaudio
import sounddevice as sd

from .devices import (
    AudioDevices,
    OutputDevice,
    find_output,
    list_wasapi_outputs,
    same_endpoint,
)
from .dsp import (
    DelayLine,
    FilterBank,
    RingBuffer,
    SmoothedGain,
    soft_limit,
    to_mono,
)


@dataclass(frozen=True)
class EngineParams:
    cutoff_hz: float = 110.0
    gain: float = 1.0
    delay_ms: float = 0.0
    subsonic: bool = True


class Engine:
    TARGET_FILL_S = 0.050
    HIGH_FILL_S = 0.120

    def __init__(self, devices: AudioDevices) -> None:
        self._devices = devices
        self._lock = threading.RLock()
        self._params_lock = threading.Lock()
        self._params = EngineParams()
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()

        self._state = "stopped"
        self._error = ""
        self._capture_name = ""
        self._output_name = ""
        self._capture_auto = True

        self._stop_event = threading.Event()
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._pa_stream = None
        self._out_stream = None
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

        self._ring: RingBuffer | None = None
        self._filters: FilterBank | None = None
        self._delay: DelayLine | None = None
        self._gain = SmoothedGain(1.0)

        self._cap_sr = 48000
        self._out_sr = 48000
        self._out_channels = 2
        self._capture_is_float = True

        self._priming = True
        self._fade = 0.0
        self._drift = 1.0
        self._meter_db = -120.0
        self._underruns = 0
        self._trims = 0
        self._fill_ms = 0.0
        self._out_latency = 0.0

    # ------------------------------------------------------------------ API

    def set_params(self, **changes) -> None:
        with self._params_lock:
            self._params = replace(self._params, **changes)

    def get_params(self) -> EngineParams:
        with self._params_lock:
            return self._params

    def drain_events(self) -> list[tuple[str, str]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def snapshot(self) -> dict:
        ring = self._ring
        with self._lock:
            state = self._state
            error = self._error
        return {
            "state": state,
            "error": error,
            "capture": self._capture_name,
            "output": self._output_name,
            "fill_ms": self._fill_ms if ring is not None else 0.0,
            "drift_ppm": (self._drift - 1.0) * 1e6,
            "underruns": self._underruns,
            "trims": self._trims,
            "level_db": self._meter_db,
            "latency_ms": self._out_latency * 1000.0,
            "priming": self._priming,
        }

    def start(
        self,
        *,
        capture_auto: bool,
        capture_name: str,
        output_name: str,
        params: EngineParams,
    ) -> None:
        with self._lock:
            if self._state != "stopped":
                raise RuntimeError("The engine is already running or stopping.")
            self._state = "starting"
            self._error = ""

        self._stop_event = threading.Event()
        self._queue = queue.Queue(maxsize=64)
        self._params = params
        self._priming = True
        self._fade = 0.0
        self._drift = 1.0
        self._meter_db = -120.0
        self._underruns = 0
        self._trims = 0
        self._fill_ms = 0.0
        self._out_latency = 0.0
        self._capture_auto = capture_auto

        try:
            capture = (
                self._devices.default_loopback()
                if capture_auto
                else self._devices.find_loopback(capture_name, False)
            )
            output = find_output(output_name)
            if same_endpoint(capture.name, output.name):
                raise RuntimeError(
                    "Capture and speaker are the same device. Choose the "
                    "headphones as capture and a different device as output."
                )

            self._capture_name = capture.name
            self._output_name = output.name
            self._capture_is_float = True

            self._pa_stream, is_float, cap_sr, cap_ch = self._devices.open_capture(
                capture,
                capture.sample_rate,
                max(128, int(capture.sample_rate * 0.01)),
                self._capture_callback,
            )
            self._capture_is_float = is_float
            self._cap_sr = float(cap_sr)
            self._cap_channels = int(cap_ch)

            self._ring = RingBuffer(self._cap_sr, capacity_ms=1000.0)
            self._filters = FilterBank(
                self._cap_sr, params.cutoff_hz, params.subsonic
            )
            self._delay = DelayLine(
                self._cap_sr, max_delay_ms=500.0, delay_ms=params.delay_ms
            )
            self._gain = SmoothedGain(params.gain)

            self._worker = threading.Thread(
                target=self._dsp_loop, name="bass-mirror-dsp", daemon=True
            )
            self._worker.start()
            self._pa_stream.start_stream()

            self._out_stream, out_sr, out_ch = self._open_output_stream(output)
            self._out_sr = float(out_sr)
            self._out_channels = int(out_ch)
            self._out_latency = float(self._out_stream.latency)
            # The capture may have run for a moment before the speaker
            # opened.  Keep the intended latency instead of starting with
            # whatever happened to accumulate.
            self._ring.align_fill(self.TARGET_FILL_S)
            self._priming = True
            self._out_stream.start()

            self._watchdog = threading.Thread(
                target=self._watchdog_loop, name="bass-mirror-watchdog", daemon=True
            )
            self._watchdog.start()

            with self._lock:
                self._state = "running"
        except Exception:
            self._shutdown_streams()
            with self._lock:
                self._state = "stopped"
            raise

    def stop(self) -> None:
        with self._lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
        self._shutdown_streams()
        with self._lock:
            self._state = "stopped"

    # ------------------------------------------------------------- capture

    def _capture_callback(self, in_data, frame_count, time_info, status):
        if self._stop_event.is_set() or in_data is None:
            return (None, pyaudio.paContinue)
        try:
            if self._capture_is_float:
                raw = np.frombuffer(in_data, dtype=np.float32)
            else:
                raw = (
                    np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
                    / 32768.0
                )
            mono = to_mono(raw, self._cap_channels)
            if mono.size:
                try:
                    self._queue.put_nowait(mono)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._queue.put_nowait(mono)
                    except queue.Full:
                        pass
        except Exception as exc:
            self._fail(f"Capture callback failed: {exc}")
        return (None, pyaudio.paContinue)

    # ----------------------------------------------------------------- DSP

    def _dsp_loop(self) -> None:
        last_block = time.monotonic()
        idle = False
        while not self._stop_event.is_set():
            try:
                block = self._queue.get(timeout=0.1)
            except queue.Empty:
                if not idle and (time.monotonic() - last_block) > 0.25:
                    # No loopback packets means nothing is playing. Clear the
                    # delay line and filter history so a new song does not
                    # start with a replay of the previous one.
                    if self._filters is not None:
                        self._filters.reset()
                    if self._delay is not None:
                        self._delay.reset()
                    self._gain.reset()
                    idle = True
                continue

            last_block = time.monotonic()
            idle = False
            blocks = [block]
            for _ in range(7):
                try:
                    blocks.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            params = self.get_params()
            if self._filters is None or self._delay is None or self._ring is None:
                continue
            self._filters.set_params(params.cutoff_hz, params.subsonic)
            self._gain.target = max(0.0, min(3.0, params.gain))
            self._delay.set_delay_ms(params.delay_ms)

            for item in blocks:
                if item is None or item.size == 0:
                    continue
                y = self._filters.process(item)
                y = self._gain.process(y)
                y = soft_limit(y)
                y = self._delay.process(y)
                self._ring.write(y)
                if y.size:
                    peak = float(np.max(np.abs(y)))
                    db = 20.0 * math.log10(max(peak, 1e-7))
                    db = max(-120.0, min(0.0, db))
                    if db >= self._meter_db:
                        self._meter_db = db
                    else:
                        self._meter_db += (db - self._meter_db) * 0.25

    # ---------------------------------------------------------------- render

    def _open_output_stream(self, output: OutputDevice):
        rates = []
        for rate in (output.sample_rate, 48000, 44100):
            if rate not in rates:
                rates.append(rate)
        channels = 2 if output.channels >= 2 else 1
        last_error: Exception | None = None
        for rate in rates:
            stream = None
            try:
                extra = (
                    sd.WasapiSettings(exclusive=False)
                    if hasattr(sd, "WasapiSettings")
                    else None
                )
                stream = sd.OutputStream(
                    device=output.index,
                    samplerate=rate,
                    channels=channels,
                    dtype="float32",
                    blocksize=max(64, int(rate * 0.01)),
                    latency="low",
                    extra_settings=extra,
                    callback=self._render_callback,
                )
                return stream, rate, channels
            except Exception as exc:
                last_error = exc
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        raise RuntimeError(
            f'Could not open speaker "{output.name}": {last_error}'
        )

    def _render_callback(self, outdata, frames, time_info, status):
        ring = self._ring
        if self._stop_event.is_set() or ring is None:
            outdata.fill(0)
            return
        try:
            fill = ring.fill_seconds()
            if fill > self.HIGH_FILL_S:
                ring.align_fill(self.TARGET_FILL_S)
                self._priming = True
                self._fade = 0.0
                self._trims += 1
                self._fill_ms = self.TARGET_FILL_S * 1000.0
                outdata.fill(0)
                return

            if self._priming:
                if fill < self.TARGET_FILL_S:
                    self._fill_ms = fill * 1000.0
                    outdata.fill(0)
                    return
                self._priming = False

            step = (self._cap_sr / self._out_sr) * self._drift
            out, ok = ring.read_resampled(frames, step)
            if not ok:
                self._underruns += 1
                self._priming = True
                self._fade = 0.0
                self._fill_ms = 0.0
                outdata.fill(0)
                return

            if self._fade < 1.0:
                self._fade = min(
                    1.0, self._fade + frames / (0.008 * self._out_sr)
                )
                out *= np.float32(self._fade)

            fill = ring.fill_seconds()
            error = (fill - self.TARGET_FILL_S) / self.TARGET_FILL_S
            correction = 1.0 + float(np.clip(error, -1.0, 1.0)) * 0.001
            self._drift += 0.05 * (correction - self._drift)
            self._drift = min(1.001, max(0.999, self._drift))
            self._fill_ms = fill * 1000.0

            if self._out_channels == 1:
                outdata[:, 0] = out
            else:
                outdata[:] = out[:, None]
        except Exception as exc:
            outdata.fill(0)
            self._fail(f"Render callback failed: {exc}")

    # -------------------------------------------------------------- watchdog

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(1.5):
            try:
                cap = self._pa_stream
                if cap is not None and not cap.is_active():
                    self._events.put(
                        ("stream_failed", "The capture stream stopped unexpectedly.")
                    )
                    return
                out = self._out_stream
                if out is not None and not out.active:
                    self._events.put(
                        ("stream_failed", "The speaker stream stopped unexpectedly.")
                    )
                    return

                if self._capture_auto:
                    try:
                        current = self._devices.default_loopback()
                    except Exception:
                        self._events.put(
                            ("device_lost", "Windows has no default output device.")
                        )
                        return
                    if current.name != self._capture_name:
                        self._events.put(
                            (
                                "device_changed",
                                f"Windows default output changed to {current.name}.",
                            )
                        )
                        return
                else:
                    names = {d.name for d in self._devices.list_loopbacks()}
                    if self._capture_name not in names:
                        self._events.put(
                            (
                                "device_lost",
                                f'Capture device "{self._capture_name}" disconnected.',
                            )
                        )
                        return

                output_names = {d.name for d in list_wasapi_outputs()}
                if self._output_name not in output_names:
                    self._events.put(
                        (
                            "device_lost",
                            f'Speaker "{self._output_name}" disconnected.',
                        )
                    )
                    return
            except Exception:
                # Device enumeration can fail transiently while Windows
                # reconfigures the audio graph.  Try again next tick.
                continue

    # --------------------------------------------------------------- cleanup

    def _shutdown_streams(self) -> None:
        self._stop_event.set()
        # Let the watchdog finish before the streams it inspects disappear.
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.join(timeout=2.0)

        out, self._out_stream = self._out_stream, None
        cap, self._pa_stream = self._pa_stream, None
        if out is not None:
            try:
                out.stop()
            except Exception:
                pass
            try:
                out.close()
            except Exception:
                pass
        if cap is not None:
            try:
                cap.stop_stream()
            except Exception:
                pass
            try:
                cap.close()
            except Exception:
                pass

        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=2.0)

        self._ring = None
        self._filters = None
        self._delay = None
        self._priming = True
        self._fade = 0.0
        self._fill_ms = 0.0
        self._meter_db = -120.0

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._error:
                return
            self._error = message
        self._events.put(("error", message))
