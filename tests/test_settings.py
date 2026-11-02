from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bass_mirror.settings import Settings, SettingsStore


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.store = SettingsStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_file_gives_defaults(self) -> None:
        settings = self.store.load()
        self.assertEqual(settings.crossover_hz, 110.0)
        self.assertTrue(settings.subsonic)

    def test_bad_json_gives_defaults(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load(), Settings())

    def test_values_are_clamped(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "crossover_hz": 999,
                    "gain": -3,
                    "delay_ms": 9000,
                    "subsonic": "false",
                }
            ),
            encoding="utf-8",
        )
        settings = self.store.load()
        self.assertEqual(settings.crossover_hz, 200)
        self.assertEqual(settings.gain, 0.0)
        self.assertEqual(settings.delay_ms, 500.0)
        self.assertFalse(settings.subsonic)

    def test_round_trip(self) -> None:
        original = Settings(
            capture_label="Headphones (X) [Loopback]",
            output_device="Speakers (Y)",
            crossover_hz=95,
            gain=1.75,
            delay_ms=120,
            subsonic=False,
        )
        self.store.save(original)
        self.assertEqual(self.store.load(), original)


if __name__ == "__main__":
    unittest.main()
