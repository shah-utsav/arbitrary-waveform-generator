"""
pulse_shaper / main.py
======================

How to run (from this folder):
    python main.py

Edit the knobs in "Set User Parameters". Pulse math lives in ps_calculations.py
(leave that file alone — same as Optical-Pulse-Shaping).
"""

import sys
import math
import numpy as np

from ps_calculations import Pulse_Shaper_Calculations

# =====================================================================
# Set User Parameters  (lab "front panel" — same idea as Optical-Pulse-Shaping)
# =====================================================================

USE_DUMMY = False          # True = no card (plots / dry-run only)

T = 10                     # (us) RF burst length inside one shot
SR = 1250                  # (MSa/s)  M4i.6631-X8 max = 1250
F0 = 100                   # (MHz) carrier
VOLTAGE_MAX_MV = 2000      # ±mV into 50 Ω

# Trigger / playback
#   TRIGGER_MODE = "external" → each shot starts on Ext0 (DG645 or photodiode)
#   TRIGGER_MODE = "internal" → see INTERNAL_SHOT_TRIGGER
TRIGGER_MODE = "external"

# Only used when TRIGGER_MODE == "internal":
#   "ext0"     — INTPLL sample clock + Ext0 shot start (desk default; no walk)
#   "free_run" — no Ext0; free-run [tip?][RF|idle] on the sample clock
INTERNAL_SHOT_TRIGGER = "ext0"

PLAYBACK = "multi"        # "single" | "multi"
LOOPS = 0                  # 0 = until Ctrl+C (Ext0 modes)
EXT0_LEVEL_MV = 1500       # Ext0 threshold (mV), TTL ~1.5 V
TAU_LIST = [0.0, 0.25, 0.5, 1.0]  # multi only: one segment per tau

# free_run only
INTERNAL_PERIOD_MS = 1.0   # full period on the sample clock
CH0_SCOPE_SYNC_TIP = True  # short full-scale tip before RF (scope Normal on Ch0)

# Must be a multiple of 32 (M4i block size)
RL = int(np.ceil(SR * T / 32) * 32)


# =====================================================================
# Pick dummy vs real card
# =====================================================================
if USE_DUMMY:
    from hardware.AWG._dummyAWG import Dummy_AWG as AWG
    print("Using Dummy_AWG (no hardware).\n")
else:
    from hardware.AWG._spectrumAWG import Spectrum_AWG as AWG
    print("Using Spectrum_AWG (real card).\n")


def build_one_waveform(record_length, tau=None):
    """Build one V(t) with Pulse_Shaper_Calculations (PI's math)."""
    pulse = Pulse_Shaper_Calculations(SR, record_length)

    # --- Calibration (default [0, 1, 0] inside ps_calculations) ---
    # pulse.calibration([5.4127, 0.0098, 0.1608])

    # --- Phase shaping (pick at most one mask) ---
    pulse.set_carrier_freq_phi(F0)
    # pulse.PhiFcn.constant(pulse)
    # pulse.PhiFcn.taylor_series(pulse, 0, [0, 0, 2 * math.pi * 10])
    # pulse.PhiFcn.taylor_series(pulse, 6, [0, 0, -100, -1000])
    # pulse.set_phi_eq()

    # --- Amplitude shaping (pick at most one mask) ---
    pulse.set_amp_control(1)
    # pulse.AmpFcn.constant(pulse)
    # pulse.AmpFcn.multi_gaussian(pulse, [2, 4, 6], 1)
    if tau is not None:
        pulse.AmpFcn.double_pulse(pulse, 1, 0, tau, 0)
    # else:
    #     pulse.AmpFcn.double_pulse(pulse, 1, 0, 1, 0)
    # pulse.set_amp_eq()

    # Fixed phase when free-running (helps Ch0 tip / envelope triggering).
    free = TRIGGER_MODE == "internal" and INTERNAL_SHOT_TRIGGER == "free_run"
    pulse.generate_waveform(randomize_phi=not free)
    return pulse


def card_trigger_mode():
    """Map front-panel knobs → Spectrum_AWG trigger_mode string."""
    if TRIGGER_MODE == "external":
        return "external"
    if TRIGGER_MODE == "internal" and INTERNAL_SHOT_TRIGGER == "ext0":
        return "external"  # INTPLL clock + Ext0 shots (same lock as external)
    return "internal"      # free_run


def arm_and_load(awg, waveforms, trigger, playback, nseg=1):
    """setup → allocate → load → write → output (one place for single & multi)."""
    kwargs = dict(
        trigger_mode=trigger,
        playback=playback,
        loops=LOOPS if trigger == "external" else 0,
        ext0_level_mV=EXT0_LEVEL_MV,
    )
    if playback == "multi":
        kwargs.update(num_segments=nseg, segment_samples=RL)

    awg.setup_card(**kwargs)
    awg.allocate_buffer()
    if playback == "multi":
        awg.load_waveforms_in_buffer(waveforms)
    else:
        awg.load_waveform_in_buffer(waveforms[0])
    awg.write_waveform_to_card()
    awg.output_waveform(wait_ready=False)


# =====================================================================
# Start AWG
# =====================================================================
if __name__ == "__main__":
    card_trig = card_trigger_mode()

    awg = AWG(RL, SR, voltage_max_mV=VOLTAGE_MAX_MV)
    if not awg.open_card():
        print("Stopping: the AWG was not opened. See the message above.")
        sys.exit(1)

    try:
        if PLAYBACK == "multi":
            # Multi needs Ext0 edges to advance segments.
            pulses = [build_one_waveform(RL, tau=tau) for tau in TAU_LIST]
            waveforms = [p.Vt for p in pulses]
            nseg = len(waveforms)
            print(f"PLAYBACK=multi  {nseg} segments  taus={TAU_LIST}")

            awg.reconfigure_for_sequence(RL * nseg)
            arm_and_load(awg, waveforms, trigger="external", playback="multi", nseg=nseg)
            pulses[0].plot_pulse_shaper_results("time")

        else:
            pulse = build_one_waveform(RL, tau=None)

            if card_trig == "internal":
                # True free-run: [tip?][RF|idle] on sample clock, no Ext0.
                awg.scope_lock_pulse = CH0_SCOPE_SYNC_TIP
                awg.envelope_burst = True
                period = awg.build_internal_period(
                    pulse.Vt, period_s=INTERNAL_PERIOD_MS / 1000.0
                )
                awg.reconfigure_for_sequence(period.size)
                arm_and_load(awg, [period], trigger="internal", playback="single")

                tip_n = getattr(awg, "_scope_lock_samples", 0)
                tip_us = 1e6 * tip_n / awg.SR if tip_n else 0.0
                hz = awg.SR / float(awg.RECORD_LENGTH)
                print(
                    f"\n*** FREE-RUN INTERNAL armed ***  {hz:.6f} Hz\n"
                    f"  Ch0: [{tip_us:.2f} us tip][RF {T} us][idle]\n"
                    "  Scope: leave DG645; Normal / Rising on Ch0 tip.\n"
                    "  If the scope stays on the DG645, the pulse WILL walk.\n"
                )
            else:
                # Ext0 shots + INTPLL sample clock (locked like "external").
                awg.scope_lock_pulse = False
                awg.envelope_burst = False
                arm_and_load(awg, [pulse.Vt], trigger="external", playback="single")

                if TRIGGER_MODE == "internal":
                    print(
                        "\n*** INTERNAL (INTPLL) + Ext0 shot trigger ***\n"
                        "  DG645 TTL → AWG Trg0/Ext0. Scope on DG645 — no walk.\n"
                        "  For no-DG645 free-run: INTERNAL_SHOT_TRIGGER = \"free_run\".\n"
                    )
                else:
                    print("\n*** EXTERNAL Ext0 mode armed ***\n")

            pulse.plot_pulse_shaper_results("time")
            # pulse.plot_pulse_shaper_results("freq")

        print("Plot closed — card running until Ctrl+C.")
        awg.run_until_interrupt()

    finally:
        awg.close_card()
        sys.exit(0)
