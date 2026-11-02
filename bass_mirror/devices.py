"""Windows device enumeration and the PyAudio loopback backend."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pyaudiowpatch as pyaudio
import sounddevice as sd

DEFAULT_CAPTURE_LABEL = "Auto (Windows default output)"


@dataclass(frozen=True)
class LoopbackDevice:
    index: int
    name: str
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class OutputDevice:
    index: int
    name: str
    sample_rate: int
    channels: int


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def list_wasapi_outputs() -> list[OutputDevice]:
    hostapis = sd.query_hostapis()
    wasapi = next(
        (i for i, api in enumerate(hostapis) if api["name"] == "Windows WASAPI"), None
    )
    if wasapi is None:
        raise RuntimeError("The Windows WASAPI host API is not available.")

    devices: list[OutputDevice] = []
    by_name: dict[str, OutputDevice] = {}
    for index, info in enumerate(sd.query_devices()):
        if info["hostapi"] != wasapi or info["max_output_channels"] < 1:
            continue
        name = str(info["name"])
        if name in by_name:
            continue
        device = OutputDevice(
            index=index,
            name=name,
            sample_rate=int(round(info["default_samplerate"])),
            channels=int(info["max_output_channels"]),
        )
        by_name[name] = device
        devices.append(device)
    return sorted(devices, key=lambda d: d.name.lower())


def find_output(name: str) -> OutputDevice:
    for device in list_wasapi_outputs():
        if device.name == name:
            return device
    raise RuntimeError(f'Output device "{name}" is no longer available.')


def wasapi_default_output() -> OutputDevice | None:
    hostapis = sd.query_hostapis()
    wasapi = next(
        (i for i, api in enumerate(hostapis) if api["name"] == "Windows WASAPI"), None
    )
    if wasapi is None:
        return None
    index = hostapis[wasapi]["default_output_device"]
    if index is None or index < 0:
        return None
    info = sd.query_devices(index)
    return OutputDevice(
        index=index,
        name=str(info["name"]),
        sample_rate=int(round(info["default_samplerate"])),
        channels=int(info["max_output_channels"]),
    )


def base_endpoint_name(name: str) -> str:
    suffix = " [loopback]"
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    return name.strip().lower()


def same_endpoint(loopback_name: str, output_name: str) -> bool:
    return base_endpoint_name(loopback_name) == base_endpoint_name(output_name)


class AudioDevices:
    """Owns the PyAudioWPatch instance used for loopback capture.

    PyAudio is kept alive across engine restarts.  All access is serialised
    because the watchdog enumerates devices while capture may be running.
    """

    def __init__(self) -> None:
        self._pa: pyaudio.PyAudio | None = None
        self._lock = threading.RLock()

    def _ensure(self) -> pyaudio.PyAudio:
        if self._pa is None:
            self._pa = pyaudio.PyAudio()
        return self._pa

    def list_loopbacks(self) -> list[LoopbackDevice]:
        with self._lock:
            pa = self._ensure()
            result: list[LoopbackDevice] = []
            for info in pa.get_loopback_device_info_generator():
                if not info.get("isLoopbackDevice"):
                    continue
                channels = int(info.get("maxInputChannels", 0) or 0)
                if channels < 1:
                    continue
                result.append(
                    LoopbackDevice(
                        index=int(info["index"]),
                        name=str(info["name"]),
                        sample_rate=int(round(info["defaultSampleRate"])),
                        channels=channels,
                    )
                )
            return sorted(result, key=lambda d: d.name.lower())

    def default_loopback(self) -> LoopbackDevice:
        with self._lock:
            pa = self._ensure()
            info = pa.get_default_wasapi_loopback()
            if not info:
                raise RuntimeError(
                    "Windows has no default output device, so there is nothing to capture."
                )
            return LoopbackDevice(
                index=int(info["index"]),
                name=str(info["name"]),
                sample_rate=int(round(info["defaultSampleRate"])),
                channels=int(info["maxInputChannels"]),
            )

    def find_loopback(self, name: str, auto: bool) -> LoopbackDevice:
        if auto:
            return self.default_loopback()
        for device in self.list_loopbacks():
            if device.name == name:
                return device
        raise RuntimeError(f'Capture device "{name}" is no longer available.')

    def open_capture(
        self,
        device: LoopbackDevice,
        sample_rate: int,
        frames_per_buffer: int,
        callback,
    ):
        """Open a shared-mode loopback stream.

        Returns ``(stream, is_float, rate, channels)``.  The native mix
        format is tried first, then a couple of common fallbacks.
        """
        with self._lock:
            pa = self._ensure()
            rates = _dedupe([int(sample_rate), 48000, 44100])
            channels_list = _dedupe([int(device.channels), 2, 1])
            formats = [(pyaudio.paFloat32, True), (pyaudio.paInt16, False)]
            errors: list[str] = []
            for rate in rates:
                for channels in channels_list:
                    if channels < 1:
                        continue
                    for fmt, is_float in formats:
                        try:
                            stream = pa.open(
                                format=fmt,
                                channels=channels,
                                rate=rate,
                                frames_per_buffer=frames_per_buffer,
                                input=True,
                                input_device_index=device.index,
                                stream_callback=callback,
                                start=False,
                            )
                            return stream, is_float, rate, channels
                        except Exception as exc:  # PortAudio raises OSError
                            errors.append(
                                f"{rate} Hz/{channels} ch/"
                                f"{'float32' if is_float else 'int16'}: {exc}"
                            )
            detail = "; ".join(errors[:2] + errors[-2:]) if len(errors) > 4 else "; ".join(errors)
            raise RuntimeError(
                f'Could not open loopback capture on "{device.name}". {detail}'
            )

    def close(self) -> None:
        with self._lock:
            if self._pa is not None:
                try:
                    self._pa.terminate()
                finally:
                    self._pa = None
