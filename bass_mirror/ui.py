"""Tkinter front end for Bass Mirror."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .devices import (
    DEFAULT_CAPTURE_LABEL,
    AudioDevices,
    base_endpoint_name,
    list_wasapi_outputs,
    same_endpoint,
)
from .engine import Engine, EngineParams
from .settings import Settings, SettingsStore


class BassMirrorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Bass Mirror")
        self.root.minsize(660, 560)

        self.devices = AudioDevices()
        self.engine = Engine(self.devices)
        self.store = SettingsStore()
        self.settings = self.store.load()

        self._busy = False
        self._intent_running = False
        self._restart_pending = False
        self._closing = False
        self._loading = True
        self._save_job: str | None = None
        self._busy_label = "Starting…"
        self._error_after_stop = ""
        self._meter_db = -120.0
        self._meter_peak = -120.0
        self._output_names: list[str] = []

        self._build_ui()
        self._refresh_devices()
        self._load_settings()
        self._loading = False
        self._apply_engine_params()
        self._update_value_labels()
        self._update_button()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll)

    # ------------------------------------------------------------- construction

    def _build_ui(self) -> None:
        self.capture_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.cutoff_var = tk.DoubleVar(value=110.0)
        self.gain_var = tk.DoubleVar(value=1.0)
        self.delay_var = tk.DoubleVar(value=0.0)
        self.subsonic_var = tk.BooleanVar(value=True)

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20))
        style.configure("Sub.TLabel", foreground="#555555")
        style.configure("Hint.TLabel", foreground="#666666", font=("Segoe UI", 8))
        style.configure("Error.TLabel", foreground="#b00020")
        style.configure("Ok.TLabel", foreground="#166534")
        style.configure("Value.TLabel", font=("Consolas", 9))
        style.configure("Meter.TLabel", font=("Consolas", 9))

        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        ttk.Label(main, text="Bass Mirror", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            main,
            text="Headphones play the whole song. The speaker plays a mono, "
            "low-passed, delayed copy — so you hear it and feel the bass.",
            style="Sub.TLabel",
            wraplength=600,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        devices = ttk.LabelFrame(main, text=" Devices ", padding=10)
        devices.grid(row=2, column=0, sticky="ew")
        devices.columnconfigure(1, weight=1)

        ttk.Label(devices, text="Capture (headphones)").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.capture_combo = ttk.Combobox(
            devices, state="readonly", textvariable=self.capture_var
        )
        self.capture_combo.grid(row=0, column=1, sticky="ew")
        self.capture_combo.bind("<<ComboboxSelected>>", self._on_capture_selected)
        self.refresh_button = ttk.Button(
            devices, text="Refresh", width=9, command=self._refresh_devices
        )
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(devices, text="Output (speaker)").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.output_combo = ttk.Combobox(
            devices, state="readonly", textvariable=self.output_var
        )
        self.output_combo.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.output_combo.bind("<<ComboboxSelected>>", self._on_output_selected)

        ttk.Label(
            devices,
            text="Tip: set the headphones as the Windows default output (or pick "
            "their loopback above), then choose the speaker here.",
            style="Hint.TLabel",
            wraplength=600,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        controls = ttk.LabelFrame(main, text=" Bass ", padding=10)
        controls.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure(1, weight=1)

        self.cutoff_scale, self.cutoff_value = self._slider_row(
            controls, 0, "Crossover", self.cutoff_var, 60, 200, self._on_cutoff
        )
        self.gain_scale, self.gain_value = self._slider_row(
            controls, 1, "Bass gain", self.gain_var, 0.0, 3.0, self._on_gain
        )
        self.delay_scale, self.delay_value = self._slider_row(
            controls, 2, "Sync delay", self.delay_var, 0.0, 500.0, self._on_delay
        )

        self.subsonic_check = ttk.Checkbutton(
            controls,
            text="Subsonic protection (30 Hz high-pass)",
            variable=self.subsonic_var,
            command=self._on_subsonic,
        )
        self.subsonic_check.grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(
            actions, text="Start", width=12, command=self._toggle_running
        )
        self.start_button.grid(row=0, column=0, sticky="w")

        meter_frame = ttk.Frame(actions)
        meter_frame.grid(row=0, column=1, sticky="ew", padx=(16, 0))
        meter_frame.columnconfigure(0, weight=1)
        self.meter = tk.Canvas(
            meter_frame,
            height=18,
            background="#1f2937",
            highlightthickness=1,
            highlightbackground="#9ca3af",
        )
        self.meter.grid(row=0, column=0, sticky="ew")
        self.meter_fill = self.meter.create_rectangle(
            0, 0, 0, 18, fill="#22c55e", outline=""
        )
        self.meter_peak_line = self.meter.create_line(
            0, 0, 0, 18, fill="#f8fafc", width=2
        )
        self.meter_text = ttk.Label(
            meter_frame, text="  -∞ dB", width=9, style="Meter.TLabel"
        )
        self.meter_text.grid(row=0, column=1, sticky="w", padx=(6, 0))

        status_frame = ttk.Frame(main)
        status_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(1, weight=1)

        self.status_label = ttk.Label(
            status_frame,
            text="Ready.",
            style="Ok.TLabel",
            anchor="w",
            wraplength=600,
            justify="left",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.detail_label = ttk.Label(
            status_frame, text="", style="Hint.TLabel", anchor="w", justify="left"
        )
        self.detail_label.grid(row=1, column=0, sticky="new", pady=(4, 0))

    def _slider_row(self, parent, row, label, variable, lo, hi, command):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=6, padx=(0, 10)
        )
        scale = ttk.Scale(
            parent,
            from_=lo,
            to=hi,
            orient="horizontal",
            variable=variable,
            command=command,
        )
        scale.grid(row=row, column=1, sticky="ew")
        value = ttk.Label(parent, text="", width=9, anchor="e", style="Value.TLabel")
        value.grid(row=row, column=2, sticky="e", padx=(10, 0))
        return scale, value

    # ------------------------------------------------------------------ devices

    def _refresh_devices(self) -> None:
        if self.engine.snapshot()["state"] != "stopped":
            return
        try:
            loopbacks = self.devices.list_loopbacks()
            self._output_names = [d.name for d in list_wasapi_outputs()]
        except Exception as exc:
            self._set_status(f"Could not enumerate audio devices: {exc}", error=True)
            return

        capture_values = [DEFAULT_CAPTURE_LABEL] + [d.name for d in loopbacks]
        self.capture_combo.configure(values=capture_values)
        if self.capture_var.get() not in capture_values:
            self.capture_var.set(DEFAULT_CAPTURE_LABEL)

        self.output_combo.configure(values=self._output_names)
        if self.output_var.get() not in self._output_names:
            self.output_var.set(self._default_output_choice())
        if not self.output_var.get():
            self._set_status(
                "Choose the speaker you want to feel, then press Start.", error=False
            )

    def _default_output_choice(self) -> str:
        preferred = self.settings.output_device
        if preferred in self._output_names:
            return preferred
        try:
            default_loop = self.devices.default_loopback()
            default_base = base_endpoint_name(default_loop.name)
        except Exception:
            default_base = ""

        candidates = [
            name
            for name in self._output_names
            if base_endpoint_name(name) != default_base
        ]
        if not candidates:
            return ""

        def rank(name: str) -> int:
            low = name.lower()
            if low.startswith(("headphone", "headset")):
                return 0
            if low.startswith("speaker"):
                return 2
            return 1

        best = max(candidates, key=rank)
        return best if rank(best) > 0 else ""

    def _on_capture_selected(self, _event=None) -> None:
        self._schedule_save()

    def _on_output_selected(self, _event=None) -> None:
        self._schedule_save()

    # ------------------------------------------------------------------ sliders

    def _on_cutoff(self, _value=None) -> None:
        if self._loading:
            return
        value = int(round(self.cutoff_var.get()))
        self.cutoff_value.configure(text=f"{value} Hz")
        self.engine.set_params(cutoff_hz=float(value))
        self._schedule_save()

    def _on_gain(self, _value=None) -> None:
        if self._loading:
            return
        value = round(self.gain_var.get(), 2)
        self.gain_value.configure(text=f"{value:.2f}×")
        self.engine.set_params(gain=float(value))
        self._schedule_save()

    def _on_delay(self, _value=None) -> None:
        if self._loading:
            return
        value = int(round(self.delay_var.get()))
        self.delay_value.configure(text=f"{value} ms")
        self.engine.set_params(delay_ms=float(value))
        self._schedule_save()

    def _on_subsonic(self) -> None:
        if self._loading:
            return
        self.engine.set_params(subsonic=bool(self.subsonic_var.get()))
        self._schedule_save()

    def _update_value_labels(self) -> None:
        self.cutoff_value.configure(text=f"{int(round(self.cutoff_var.get()))} Hz")
        self.gain_value.configure(text=f"{self.gain_var.get():.2f}×")
        self.delay_value.configure(text=f"{int(round(self.delay_var.get()))} ms")

    # ---------------------------------------------------------------- settings

    def _load_settings(self) -> None:
        settings = self.settings
        self.cutoff_var.set(float(min(200, max(60, settings.crossover_hz))))
        self.gain_var.set(float(min(3.0, max(0.0, settings.gain))))
        self.delay_var.set(float(min(500.0, max(0.0, settings.delay_ms))))
        self.subsonic_var.set(bool(settings.subsonic))
        self.capture_var.set(settings.capture_label or DEFAULT_CAPTURE_LABEL)
        self.output_var.set(settings.output_device)
        if self.capture_var.get() not in self.capture_combo.cget("values"):
            self.capture_var.set(DEFAULT_CAPTURE_LABEL)
        if self.output_var.get() not in self._output_names:
            self.output_var.set(self._default_output_choice())

    def _current_settings(self) -> Settings:
        return Settings(
            capture_label=self.capture_var.get() or DEFAULT_CAPTURE_LABEL,
            output_device=self.output_var.get(),
            crossover_hz=round(float(self.cutoff_var.get()), 1),
            gain=round(float(self.gain_var.get()), 2),
            delay_ms=float(int(round(self.delay_var.get()))),
            subsonic=bool(self.subsonic_var.get()),
        )

    def _schedule_save(self) -> None:
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(700, self._save_now)

    def _save_now(self) -> None:
        self._save_job = None
        try:
            self.settings = self._current_settings()
            self.store.save(self.settings)
        except Exception:
            pass

    def _apply_engine_params(self) -> None:
        self.engine.set_params(
            cutoff_hz=float(int(round(self.cutoff_var.get()))),
            gain=float(round(self.gain_var.get(), 2)),
            delay_ms=float(int(round(self.delay_var.get()))),
            subsonic=bool(self.subsonic_var.get()),
        )

    # ------------------------------------------------------------------ engine

    def _toggle_running(self) -> None:
        if self._busy:
            return
        if self._intent_running or self.engine.snapshot()["state"] != "stopped":
            self._intent_running = False
            self._stop_async()
        else:
            self._start_async()

    def _start_async(self) -> None:
        if self._busy:
            return
        output_name = self.output_var.get()
        if not output_name:
            self._set_status("Choose an output speaker first.", error=True)
            return

        capture_label = self.capture_var.get() or DEFAULT_CAPTURE_LABEL
        capture_auto = capture_label == DEFAULT_CAPTURE_LABEL
        if not capture_auto and same_endpoint(capture_label, output_name):
            self._set_status(
                "Capture and speaker are the same device — that would feed the "
                "bass back into itself. Pick different devices.",
                error=True,
            )
            return

        self._save_now()
        self._apply_engine_params()
        self._error_after_stop = ""
        self._intent_running = True
        self._busy = True
        self._busy_label = "Starting…"
        self._set_status("Starting audio…")
        self._update_button()
        self._set_device_controls_enabled(False)

        capture_name = "" if capture_auto else capture_label
        params = EngineParams(
            cutoff_hz=float(int(round(self.cutoff_var.get()))),
            gain=float(round(self.gain_var.get(), 2)),
            delay_ms=float(int(round(self.delay_var.get()))),
            subsonic=bool(self.subsonic_var.get()),
        )

        # PyAudio must open and start the loopback stream on the same thread
        # that created its PyAudio instance.  That instance lives on the Tk
        # thread (device enumeration happens here too), so start synchronously.
        self.root.update_idletasks()
        try:
            self.engine.start(
                capture_auto=capture_auto,
                capture_name=capture_name,
                output_name=output_name,
                params=params,
            )
        except Exception as exc:
            self._busy = False
            self._intent_running = False
            self._restart_pending = False
            self._set_status(str(exc), error=True)
            self._set_device_controls_enabled(True)
            self._update_button()
            return

        self._busy = False
        self._intent_running = True
        self._set_status("Running — tune the sync delay by ear.")
        self._update_button()

    def _stop_async(self, status: str = "Stopping…") -> None:
        if self._busy:
            return
        self._busy = True
        self._busy_label = "Stopping…"
        self._update_button()
        self._set_status(status)
        self.root.update_idletasks()
        try:
            self.engine.stop()
        except Exception as exc:
            self._busy = False
            self._intent_running = False
            self._restart_pending = False
            self._set_status(str(exc), error=True)
            self._set_device_controls_enabled(True)
            self._update_button()
            return

        self._busy = False
        self._update_button()
        self._set_device_controls_enabled(True)
        if self._restart_pending and self._intent_running:
            self._restart_pending = False
            self.root.after(250, self._start_async)
        elif self._error_after_stop:
            self._set_status(self._error_after_stop, error=True)
            self._error_after_stop = ""
        else:
            self._set_status("Stopped.")

    def _restart_async(self, reason: str) -> None:
        self._restart_pending = True
        text = f"{reason} Reconnecting…"
        self._set_status(text)
        if not self._busy:
            self._stop_async(text)

    # -------------------------------------------------------------------- poll

    def _poll(self) -> None:
        if self._closing:
            return
        self._drain_engine_events()
        snapshot = self.engine.snapshot()
        self._update_meter(snapshot["level_db"])
        self._update_details(snapshot)
        self.root.after(80, self._poll)

    def _drain_engine_events(self) -> None:
        for kind, message in self.engine.drain_events():
            if kind == "device_changed":
                if self._intent_running:
                    self._restart_async(message)
            elif kind in ("device_lost", "stream_failed", "error"):
                self._intent_running = False
                self._restart_pending = False
                self._error_after_stop = message
                self._set_status(message, error=True)
                if not self._busy:
                    self._stop_async(message)
            else:
                self._set_status(message)

    # --------------------------------------------------------------------- ui

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.configure(
            text=text, style="Error.TLabel" if error else "Ok.TLabel"
        )

    def _update_button(self) -> None:
        state = self.engine.snapshot()["state"]
        if self._busy:
            self.start_button.configure(text=self._busy_label, state="disabled")
            return
        if self._intent_running or state in ("running", "starting"):
            self.start_button.configure(text="Stop", state="normal")
        else:
            self.start_button.configure(text="Start", state="normal")

    def _set_device_controls_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        self.capture_combo.configure(state=combo_state)
        self.output_combo.configure(state=combo_state)
        self.refresh_button.configure(state="normal" if enabled else "disabled")

    def _update_meter(self, level_db: float) -> None:
        if level_db > self._meter_db:
            self._meter_db = level_db
        else:
            self._meter_db += (level_db - self._meter_db) * 0.2
        if self._meter_db < -115:
            self._meter_db = -120.0

        self._meter_peak = max(self._meter_db, self._meter_peak - 0.6)

        width = max(1, self.meter.winfo_width())
        height = max(1, self.meter.winfo_height())
        fraction = 0.0 if self._meter_db <= -60 else (self._meter_db + 60) / 60.0
        fraction = min(1.0, max(0.0, fraction))
        peak_fraction = 0.0 if self._meter_peak <= -60 else (self._meter_peak + 60) / 60.0
        peak_fraction = min(1.0, max(0.0, peak_fraction))

        if fraction <= 0.72:
            color = "#22c55e"
        elif fraction <= 0.9:
            color = "#eab308"
        else:
            color = "#ef4444"
        self.meter.coords(self.meter_fill, 0, 0, width * fraction, height)
        self.meter.itemconfigure(self.meter_fill, fill=color)
        self.meter.coords(
            self.meter_peak_line,
            width * peak_fraction,
            0,
            width * peak_fraction,
            height,
        )
        if self._meter_db <= -60:
            text = "  -∞ dB"
        else:
            text = f"{self._meter_db:5.1f} dB"
        self.meter_text.configure(text=text)

    def _update_details(self, snapshot: dict) -> None:
        if snapshot["state"] == "running":
            fill = snapshot["fill_ms"]
            drift = snapshot["drift_ppm"]
            details = (
                f"Capture: {snapshot['capture']}\n"
                f"Output:  {snapshot['output']}\n"
                f"Buffer {fill:.0f} ms · drift {drift:+.0f} ppm · "
                f"underruns {snapshot['underruns']} · "
                f"device latency {snapshot['latency_ms']:.0f} ms"
            )
        elif snapshot["state"] == "stopped":
            details = (
                "Speaker path = ~50 ms buffer target + output latency + sync "
                "delay. Start with 0 ms delay and increase it until the kick "
                "and the bass lock together."
            )
        else:
            details = ""
        if snapshot["error"]:
            details = snapshot["error"] + "\n" + details
        self.detail_label.configure(text=details)

    # ------------------------------------------------------------------ close

    def _on_close(self) -> None:
        self._closing = True
        try:
            self._save_now()
        except Exception:
            pass
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            self.devices.close()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = BassMirrorApp(root)
    try:
        root.mainloop()
    finally:
        if not app._closing:
            app._on_close()
