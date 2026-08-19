"""
multi_pulse.py
==============

SPC_REP_STD_MULTI + Ext0 via hardware.AWG._spectrumAWG.Spectrum_AWG.

Each Ext0 edge advances to the next segment:
  segment 0 → tau[0], segment 1 → tau[1], ...

Requires DG645 (or similar) TTL → Spectrum Ext0 / Trg0.

For the PI scope figure pair:
  - main.py       → SINGLERESTART: 4 triggers, SAME waveform
  - multi_pulse.py → MULTI:         4 triggers, DIFFERENT waveforms (tau)
"""

from __future__ import annotations

import numpy as np

from hardware.AWG._spectrumAWG import Spectrum_AWG, _align32
from ps_calculations import Pulse_Shaper_Calculations

# ----- User knobs -----
SR_MSa = 1250          # MSa/s (M4i.6631 max)
F0_MHz = 100           # RF carrier (MHz); same units convention as main.py
T_us = 10              # segment duration (us) = one piece per Ext0
NUM_SEGMENTS = 4
# Double-pulse delays — experiment here (PI tau scan).
TAU_LIST = [0.0, 0.25, 0.5, 1.0]
assert len(TAU_LIST) == NUM_SEGMENTS


def _build_double_pulse_vt(
    sr_msa: float,
    record_length: int,
    f0_mhz: float,
    tau: float,
) -> np.ndarray:
    """Build one RF segment V(t) for a chosen double-pulse delay tau."""
    pulse = Pulse_Shaper_Calculations(sr_msa, record_length)
    pulse.set_carrier_freq_phi(f0_mhz)
    pulse.set_amp_control(1.0)
    # double_pulse(parent, R, w0, tau, phi)
    pulse.AmpFcn.double_pulse(pulse, 1.0, 0.0, tau, 0.0)
    return pulse.generate_waveform(randomize_phi=False)


def main() -> None:
    segment_rl = _align32(int(np.ceil(SR_MSa * T_us / 32) * 32))

    print(
        f"MULTI via Spectrum_AWG: {NUM_SEGMENTS} segments × {segment_rl} samples "
        f"(~{T_us} us @ {SR_MSa} MSa/s), taus={TAU_LIST}"
    )

    waveforms = [
        _build_double_pulse_vt(SR_MSa, segment_rl, F0_MHz, tau) for tau in TAU_LIST
    ]
    for i, tau in enumerate(TAU_LIST):
        print(f"  segment[{i}] ← double_pulse tau={tau:g}")

    # RECORD_LENGTH ctor arg is a placeholder; play_multi resizes for N×segment.
    awg = Spectrum_AWG(segment_rl, SR_MSa, voltage_max_mV=2000)
    awg.open_card()
    try:
        # setup_multi + fill all segments + DMA + START|ENABLE (wait_ready=False).
        awg.play_multi_voltage_arrays(
            waveforms,
            loops=0,
            auto_start=True,
            wait_ready=False,
            ext0_level_mV=1500,
        )
        print(
            "Armed MULTI — DG645 TTL → Trg0. "
            "Each edge plays the next tau. Ctrl+C to stop."
        )
        awg.run_until_interrupt()
    finally:
        awg.close_card()


if __name__ == "__main__":
    main()
