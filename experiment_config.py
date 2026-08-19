"""
experiment_config.py
====================

Loads and validates YAML experiment definitions.

WHY THIS MODULE EXISTS
----------------------
The PI's workflow needs many knobs (waveforms, repeats, trigger mode, averaging).
Putting them in a YAML file keeps experiment logic out of hard-coded scripts and
matches how LabVIEW stores parameters in text files / front-panel settings.

DESIGN CHOICES
--------------
1. Dataclasses instead of raw dicts
   - Every field is typed and documented in one place.
   - Invalid configs fail early with clear errors before touching hardware.

2. Minimal YAML schema
   - We only model what the MVP needs; FROG / closed-loop can be added later.

3. No hidden defaults in loader for critical hardware values
   - AWG sample rate and segment length must be explicit in YAML.
   - Small defaults only for optional fields (gap, loops).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


TriggerMode = Literal["software", "external"]
PlaybackMode = Literal["concatenated", "per_block"]
AwgBackend = Literal["dummy", "spectrum"]
SpectrometerBackend = Literal["dummy", "nio_qmini"]


@dataclass
class AwgConfig:
    """
    Parameters for the Spectrum arbitrary waveform generator (AWG).

    The AWG outputs RF voltage V(t) that drives the AOM on the pulse shaper.
    Times here are ACOUSTIC/RF (microseconds), not optical femtoseconds.
    """

    sampling_rate_MSa_s: float
    segment_duration_us: float
    voltage_max_mV: int
    backend: AwgBackend = "dummy"
    trigger_mode: TriggerMode = "external"
    inter_segment_gap_us: float = 0.0
    playback_mode: PlaybackMode = "per_block"
    concatenated_loops: int = 1
    # Per Trg0 MEMSIZE replays (main.py LOOPS). For per_block: 0 → schedule.repeats.
    # Must resolve to >= 1 before play — never free-run 0 when syncing to Ext0.
    acquire_loops: int = 0
    # Spectrum device path when multiple cards installed (Windows: /dev/spcm0, spcm1, ...)
    device: str = "/dev/spcm0"

    def record_length_samples(self) -> int:
        """
        Number of DAC samples in ONE waveform segment.

        LOGIC:
        - RL = sampling_rate * duration  (both in consistent units)
        - SR is MSa/s, T is µs → (MSa/s)*(µs) = samples (1e6 and 1e-6 cancel).

        CARD CONSTRAINT:
        M4i.66xx cards require RL to be a multiple of 32 (DMA block size).
        Round UP to nearest 32 so we never undershoot segment duration.
        """
        import math

        raw_rl = self.sampling_rate_MSa_s * self.segment_duration_us
        return int(math.ceil(raw_rl / 32) * 32)

    def gap_samples(self) -> int:
        """Convert inter-segment gap from microseconds to sample count."""
        if self.inter_segment_gap_us <= 0:
            return 0
        import math

        raw = self.sampling_rate_MSa_s * self.inter_segment_gap_us
        return int(math.ceil(raw / 32) * 32)


@dataclass
class CalibrationConfig:
    """Polynomial coefficients for mapping acoustic time t → optical frequency ω."""

    coefficients: list[float] = field(default_factory=lambda: [0.0, 1.0, 0.0])


@dataclass
class PhaseMaskConfig:
    """Phase mask φ(ω) specification — one mask type per waveform."""

    type: str
    w0: float = 0.0
    phi_n: list[float] = field(default_factory=list)


@dataclass
class AmplitudeMaskConfig:
    """Amplitude mask M(ω) specification."""

    type: str
    R: float = 1.0
    w0: float = 0.0
    w0_list: list[float] = field(default_factory=list)
    tau: float = 0.0
    phi: float = 0.0
    FWHM_w: float = 1.0


@dataclass
class WaveformConfig:
    """One distinct shaped pulse definition (one entry in the waveform library)."""

    id: str
    carrier_freq_MHz: float
    amp_control: float = 1.0
    randomize_carrier_phase: bool = False
    phase_mask: PhaseMaskConfig = field(default_factory=lambda: PhaseMaskConfig(type="constant"))
    amplitude_mask: AmplitudeMaskConfig = field(
        default_factory=lambda: AmplitudeMaskConfig(type="constant")
    )


@dataclass
class ScheduleEntry:
    """One schedule row: play waveform_id repeats times before next entry."""

    waveform_id: str
    repeats: int


@dataclass
class SpectrometerConfig:
    """Spectrometer settings — PI averages over many triggers."""

    backend: SpectrometerBackend = "dummy"
    frames_to_average: int = 50
    integration_time_ms: float = 10.0
    # Wait after AWG start before first frame (ms). Tunable; no fixed lab value.
    settle_delay_ms: float = 10.0
    device_index: int = 0
    serial_number: str | None = None


@dataclass
class ExperimentConfig:
    """Top-level container for everything needed to run one experiment."""

    name: str
    output_dir: str
    awg: AwgConfig
    calibration: CalibrationConfig
    waveforms: list[WaveformConfig]
    schedule: list[ScheduleEntry]
    spectrometer: SpectrometerConfig

    def waveform_by_id(self, wf_id: str) -> WaveformConfig:
        """Look up waveform by id; raises KeyError if schedule references unknown id."""
        for wf in self.waveforms:
            if wf.id == wf_id:
                return wf
        known = [w.id for w in self.waveforms]
        raise KeyError(
            f"Schedule references unknown waveform_id '{wf_id}'. Known ids: {known}"
        )


def _parse_phase_mask(raw: dict[str, Any]) -> PhaseMaskConfig:
    return PhaseMaskConfig(
        type=str(raw.get("type", "constant")),
        w0=float(raw.get("w0", 0.0)),
        phi_n=list(raw.get("phi_n", [])),
    )


def _parse_amplitude_mask(raw: dict[str, Any]) -> AmplitudeMaskConfig:
    return AmplitudeMaskConfig(
        type=str(raw.get("type", "constant")),
        R=float(raw.get("R", 1.0)),
        w0=float(raw.get("w0", 0.0)),
        w0_list=list(raw.get("w0_list", [])),
        tau=float(raw.get("tau", 0.0)),
        phi=float(raw.get("phi", 0.0)),
        FWHM_w=float(raw.get("FWHM_w", 1.0)),
    )


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Load experiment YAML and return validated ExperimentConfig."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Experiment config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or "experiment" not in raw:
        raise ValueError("Config must contain top-level 'experiment' section")

    exp_meta = raw["experiment"]
    awg_raw = raw.get("awg", {})
    cal_raw = raw.get("calibration", {})
    spec_raw = raw.get("spectrometer", {})

    awg = AwgConfig(
        sampling_rate_MSa_s=float(awg_raw["sampling_rate_MSa_s"]),
        segment_duration_us=float(awg_raw["segment_duration_us"]),
        voltage_max_mV=int(awg_raw["voltage_max_mV"]),
        backend=str(awg_raw.get("backend", "dummy")),  # type: ignore[arg-type]
        trigger_mode=str(awg_raw.get("trigger_mode", "external")),  # type: ignore
        inter_segment_gap_us=float(awg_raw.get("inter_segment_gap_us", 0.0)),
        playback_mode=str(awg_raw.get("playback_mode", "per_block")),  # type: ignore
        concatenated_loops=int(awg_raw.get("concatenated_loops", 1)),
        acquire_loops=int(awg_raw.get("acquire_loops", 0)),
        device=str(awg_raw.get("device", "/dev/spcm0")),
    )

    calibration = CalibrationConfig(coefficients=list(cal_raw.get("coefficients", [0, 1, 0])))

    waveforms: list[WaveformConfig] = []
    for wf_raw in raw.get("waveforms", []):
        waveforms.append(
            WaveformConfig(
                id=str(wf_raw["id"]),
                carrier_freq_MHz=float(wf_raw["carrier_freq_MHz"]),
                amp_control=float(wf_raw.get("amp_control", 1.0)),
                randomize_carrier_phase=bool(wf_raw.get("randomize_carrier_phase", False)),
                phase_mask=_parse_phase_mask(wf_raw.get("phase_mask", {"type": "constant"})),
                amplitude_mask=_parse_amplitude_mask(
                    wf_raw.get("amplitude_mask", {"type": "constant"})
                ),
            )
        )

    schedule: list[ScheduleEntry] = []
    for entry in raw.get("schedule", []):
        schedule.append(
            ScheduleEntry(
                waveform_id=str(entry["waveform_id"]),
                repeats=int(entry["repeats"]),
            )
        )

    serial = spec_raw.get("serial_number")
    spectrometer = SpectrometerConfig(
        backend=str(spec_raw.get("backend", "dummy")),  # type: ignore
        frames_to_average=int(spec_raw.get("frames_to_average", 50)),
        integration_time_ms=float(spec_raw.get("integration_time_ms", 10.0)),
        settle_delay_ms=float(spec_raw.get("settle_delay_ms", 10.0)),
        device_index=int(spec_raw.get("device_index", 0)),
        serial_number=str(serial) if serial else None,
    )

    config = ExperimentConfig(
        name=str(exp_meta.get("name", "unnamed")),
        output_dir=str(exp_meta.get("output_dir", "data/experiments")),
        awg=awg,
        calibration=calibration,
        waveforms=waveforms,
        schedule=schedule,
        spectrometer=spectrometer,
    )

    _validate_config(config)
    return config


def _validate_config(config: ExperimentConfig) -> None:
    """Cross-field validation after parsing."""
    if not config.waveforms:
        raise ValueError("Config must define at least one waveform")

    if not config.schedule:
        raise ValueError("Config must define a non-empty schedule")

    ids = [w.id for w in config.waveforms]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate waveform ids in config: {ids}")

    for entry in config.schedule:
        if entry.repeats < 1:
            raise ValueError(
                f"Schedule entry for '{entry.waveform_id}' must have repeats >= 1"
            )
        config.waveform_by_id(entry.waveform_id)

    for wf in config.waveforms:
        if not (0.0 <= wf.amp_control <= 1.0):
            raise ValueError(
                f"Waveform '{wf.id}': amp_control must be in [0, 1], got {wf.amp_control}"
            )

    if config.spectrometer.frames_to_average < 1:
        raise ValueError("spectrometer.frames_to_average must be >= 1")

    if config.spectrometer.device_index < 0:
        raise ValueError("spectrometer.device_index must be >= 0")
