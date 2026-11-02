"""Small JSON-backed settings store."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .devices import DEFAULT_CAPTURE_LABEL


@dataclass
class Settings:
    capture_label: str = DEFAULT_CAPTURE_LABEL
    output_device: str = ""
    crossover_hz: float = 110.0
    gain: float = 1.0
    delay_ms: float = 0.0
    subsonic: bool = True


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "BassMirror" / "settings.json"


def _number(value, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(value)))
    except (TypeError, ValueError):
        return default


def _boolean(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()

    def load(self) -> Settings:
        defaults = Settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults

        capture = raw.get("capture_label", defaults.capture_label)
        output = raw.get("output_device", defaults.output_device)
        return Settings(
            capture_label=str(capture) if capture else defaults.capture_label,
            output_device=str(output) if output else "",
            crossover_hz=_number(raw.get("crossover_hz"), defaults.crossover_hz, 60, 200),
            gain=_number(raw.get("gain"), defaults.gain, 0.0, 3.0),
            delay_ms=_number(raw.get("delay_ms"), defaults.delay_ms, 0.0, 500.0),
            subsonic=_boolean(raw.get("subsonic"), defaults.subsonic),
        )

    def save(self, settings: Settings) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(asdict(settings), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            # Settings are a convenience; never take the audio engine down.
            pass
