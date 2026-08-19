"""
waveform_builder.py
===================

Builds a single RF waveform V(t) from a WaveformConfig + AWG timing parameters.

CONNECTION TO PHYSICS (PI / PDF)
--------------------------------
- Masks are defined in optical frequency ω (stored as self.wt after calibration).
- Phase φ(ω) and amplitude M(ω) combine into V(t) on the acoustic time axis.
- double_pulse encodes delay τ in the mask (PI: delay is mask-accurate).

WHY WRAP ps_calculations
------------------------
Existing class mirrors LabVIEW / PI math — we map config → PhiFcn / AmpFcn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ps_calculations import Pulse_Shaper_Calculations

if TYPE_CHECKING:
    from experiment_config import AwgConfig, CalibrationConfig, WaveformConfig


def build_single_waveform(
    wf_config: "WaveformConfig",
    awg_config: "AwgConfig",
    cal_config: "CalibrationConfig",
) -> tuple[np.ndarray, Pulse_Shaper_Calculations]:
    """
    Compute normalized V(t) for one waveform definition.

    Returns Vt array and Pulse_Shaper_Calculations instance (for debug plots).
    """
    # SR stays in MSa/s to match Pulse_Shaper_Calculations and legacy main.py
    sr = awg_config.sampling_rate_MSa_s
    # RL is segment length in samples (one AWG "chunk" before concatenation)
    rl = awg_config.record_length_samples()

    # New calculator per waveform — prevents mask state leaking between library entries
    pulse = Pulse_Shaper_Calculations(sr, rl)

    # Polynomial t→ω: maps DAC time axis to optical frequency coordinate on shaper
    pulse.calibration(list(cal_config.coefficients))

    # RF carrier f0 — the tone sent to the AOM (MHz in acoustic time)
    pulse.set_carrier_freq_phi(wf_config.carrier_freq_MHz)

    # Spectral phase φ(ω) and amplitude M(ω) from YAML mask definitions
    _apply_phase_mask(pulse, wf_config)
    _apply_amplitude_mask(pulse, wf_config)

    # Scalar 0–1 scale applied before DAC full-scale in AWG driver
    pulse.set_amp_control(wf_config.amp_control)

    # Combine into real-valued RF voltage trace; optional global phase randomization
    vt = pulse.generate_waveform(randomize_phi=wf_config.randomize_carrier_phase)
    return np.asarray(vt, dtype=np.float64), pulse


def _apply_phase_mask(pulse: Pulse_Shaper_Calculations, wf_config: "WaveformConfig") -> None:
    pm = wf_config.phase_mask
    mask_type = pm.type.lower()

    if mask_type == "constant":
        pulse.PhiFcn.constant(pulse)
        return

    if mask_type == "taylor_series":
        if not pm.phi_n:
            raise ValueError(
                f"Waveform '{wf_config.id}': taylor_series phase_mask requires phi_n list"
            )
        pulse.PhiFcn.taylor_series(pulse, pm.w0, pm.phi_n)
        return

    raise ValueError(
        f"Waveform '{wf_config.id}': unknown phase_mask.type '{pm.type}'. "
        "Supported: constant, taylor_series"
    )


def _apply_amplitude_mask(
    pulse: Pulse_Shaper_Calculations, wf_config: "WaveformConfig"
) -> None:
    am = wf_config.amplitude_mask
    mask_type = am.type.lower()
    wf_id = wf_config.id

    if mask_type == "constant":
        pulse.AmpFcn.constant(pulse)
        return
    if mask_type == "gaussian":
        pulse.AmpFcn.gaussian(pulse, am.w0, am.FWHM_w)
        return
    if mask_type == "delayed_pulse":
        pulse.AmpFcn.delayed_pulse(pulse, am.w0, am.tau, am.phi)
        return
    if mask_type == "double_pulse":
        pulse.AmpFcn.double_pulse(pulse, am.R, am.w0, am.tau, am.phi)
        return
    if mask_type == "multi_gaussian":
        if not am.w0_list:
            raise ValueError(f"Waveform '{wf_id}': multi_gaussian requires w0_list")
        pulse.AmpFcn.multi_gaussian(pulse, am.w0_list, am.FWHM_w)
        return

    raise ValueError(
        f"Waveform '{wf_id}': unknown amplitude_mask.type '{am.type}'. "
        "Supported: constant, gaussian, delayed_pulse, double_pulse, multi_gaussian"
    )


def build_waveform_library(
    waveforms: list["WaveformConfig"],
    awg_config: "AwgConfig",
    cal_config: "CalibrationConfig",
) -> dict[str, np.ndarray]:
    """
    Pre-compute V(t) for every waveform (page 4 New target — before hardware I/O).
    """
    library: dict[str, np.ndarray] = {}
    for wf_cfg in waveforms:
        vt, _ = build_single_waveform(wf_cfg, awg_config, cal_config)
        library[wf_cfg.id] = vt
    return library
