"""Diagnostics: list devices or record WASAPI loopback to a WAV file.

Milestone 1 from the build brief.  Run from the project root:

    .venv\\Scripts\\python tools\\capture_probe.py --list
    .venv\\Scripts\\python tools\\capture_probe.py --seconds 10
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bass_mirror.devices import AudioDevices, list_wasapi_outputs  # noqa: E402


def show_devices(devices: AudioDevices) -> None:
    print("Loopback capture devices:")
    for device in devices.list_loopbacks():
        print(
            f"  {device.name}\n"
            f"    index {device.index}, {device.sample_rate} Hz, "
            f"{device.channels} ch"
        )
    try:
        default = devices.default_loopback()
        print(f"  default: {default.name}")
    except Exception as exc:
        print(f"  default: unavailable ({exc})")

    print("\nWASAPI speaker outputs:")
    for output in list_wasapi_outputs():
        print(
            f"  {output.name}\n"
            f"    index {output.index}, {output.sample_rate} Hz, "
            f"{output.channels} ch"
        )


def record(
    devices: AudioDevices,
    device_name: str | None,
    seconds: float,
    output_path: Path,
) -> None:
    device = (
        devices.default_loopback()
        if not device_name
        else devices.find_loopback(device_name, False)
    )
    chunks: list[bytes] = []

    def callback(in_data, frame_count, time_info, status):
        if in_data:
            chunks.append(bytes(in_data))
        return (None, 0)

    stream, is_float, rate, channels = devices.open_capture(
        device, device.sample_rate, max(128, int(device.sample_rate * 0.01)), callback
    )
    print(f"Recording {seconds:.1f} s from {device.name} ({rate} Hz, {channels} ch)…")
    stream.start_stream()
    try:
        time.sleep(seconds)
    finally:
        stream.stop_stream()
        stream.close()

    raw = b"".join(chunks)
    if is_float:
        floats = np.frombuffer(raw, dtype=np.float32)
        pcm = (np.clip(floats, -1.0, 1.0) * 32767.0).astype(np.int16)
    else:
        pcm = np.frombuffer(raw, dtype=np.int16)

    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())

    duration = pcm.size / max(1, channels) / rate
    print(f"Wrote {output_path} ({duration:.2f} s, {pcm.nbytes} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list devices and exit")
    parser.add_argument(
        "--device", help="loopback device name (default: Windows default output)"
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output", default="loopback_probe.wav")
    args = parser.parse_args()

    devices = AudioDevices()
    try:
        if args.list:
            show_devices(devices)
            return
        record(devices, args.device, args.seconds, Path(args.output))
    finally:
        devices.close()


if __name__ == "__main__":
    main()
