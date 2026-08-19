"""
pulse_shaper / main.py
======================

Talk to a Spectrum M4i.6631-X8 AWG, or run the same steps with Dummy_AWG
when the card is not connected.

This file is meant to be *the* place you edit day to day — same idea as
Optical-Pulse-Shaping/main.py: knobs at the top, pulse math in the middle,
hardware steps at the bottom.

How to run (from this folder):
    python main.py

-------------------------------------------------
What the knobs mean
-------------------------------------------------
USE_DUMMY
    True  → Dummy_AWG. No card, no driver. Plots still work.
    False → Spectrum_AWG. Needs the M4i and the Spectrum driver.

TRIGGER_MODE
    "internal"  AWG sample clock (INTPLL). No SRS delay generator, no photodiode.
                Memory is RF + idle zeros so you see *pulses*, not a continuous wave.
    "external"  Ext0 / Trg0. Office: SRS DG645 TTL. Lab: photodiode.

PLAYBACK
    "single"  one waveform. External: that waveform on every trigger.
              Internal: that waveform + zeros, repeating on the sample clock.
    "multi"   several waveforms (see TAU_LIST).
              External: next waveform on each trigger (Spectrum MULTI mode).
              Internal: all waveforms concatenated, repeating on the sample clock.

Pulse shaping (T, SR, F0, masks) is the PI's Pulse_Shaper_Calculations class,
copied as-is into ps_calculations.py.
"""

import sys
import math
import numpy as np

from ps_calculations import Pulse_Shaper_Calculations

# =====================================================================
# Set User Parameters  (edit these — this is the lab "front panel")
# =====================================================================

# True = no hardware. False = real M4i.6631-X8.
USE_DUMMY = False

T = 10       # (us) RF burst length inside one shot
SR = 1250    # (MSa/s) sampling rate  — M4i.6631-X8 max is 1250
F0 = 100     # (MHz) rf / carrier frequency
VOLTAGE_MAX_MV = 2000  # analog range into 50 Ω, millivolts (±80 … ±2000)

# "internal"  = built-in sample clock (no delay generator)
# "external"  = SRS delay generator or photodiode on Ext0 / Trg0
TRIGGER_MODE = "internal"

# "single" = same waveform every shot
# "multi"  = different waveforms (one per tau in TAU_LIST)
PLAYBACK = "single"

# Internal only: full period of one replay, in milliseconds.
# Example: 10 us of RF every 1 ms → T=10 and INTERNAL_PERIOD_MS=1.0
INTERNAL_PERIOD_MS = 1.0

# External only: SPC_LOOPS. 0 = keep accepting triggers until Ctrl+C.
LOOPS = 0

# External trigger threshold on Ext0 (TTL is typically ~1.5–2.5 V).
EXT0_LEVEL_MV = 1500

# Multi only: one double-pulse waveform per delay tau (same convention as OPS).
# Uncomment / edit the list. Ignored when PLAYBACK = "single".
TAU_LIST = [0.0, 0.25, 0.5, 1.0]

# M4i memory length must be a multiple of 32 samples.
RL = int(np.ceil(SR * T / 32) * 32)  # samples in one RF burst


# =====================================================================
# Pick dummy vs real card  (same methods either way)
# =====================================================================
if USE_DUMMY:
    from hardware.AWG._dummyAWG import Dummy_AWG as AWG
    print("Using Dummy_AWG (no hardware).\n")
else:
    # Importing Spectrum_AWG loads the Spectrum DLL. If the DLL is missing
    # the wrapper prints a short diagnosis instead of a ctypes traceback.
    from hardware.AWG._spectrumAWG import Spectrum_AWG as AWG
    print("Using Spectrum_AWG (real card).\n")


def build_one_waveform(record_length, tau=None):
    """
    Build one V(t) with Pulse_Shaper_Calculations.

    This block is the Optical-Pulse-Shaping/main.py workflow:
    calibration → phase mask (pick one) → amplitude mask (pick one) → generate.

    For PLAYBACK = "multi", tau is passed into AmpFcn.double_pulse.
    For PLAYBACK = "single", uncomment the masks you want, same as the PI's file.
    """
    pulse = Pulse_Shaper_Calculations(SR, record_length)

    # --- Calibration (default [0, 1, 0] is set inside the class) ---
    # pulse.calibration([5.4127, 0.0098, 0.1608])  # real calibration

    # --- Phase Shaping ---
    pulse.set_carrier_freq_phi(F0)
    ## Phase mask — uncomment at most one
    # pulse.PhiFcn.constant(pulse)
    # pulse.PhiFcn.taylor_series(pulse, 0, [0, 0, 2 * math.pi * 10])
    # pulse.PhiFcn.taylor_series(pulse, 6, [0, 0, -100, -1000])
    # pulse.set_phi_eq()  # not implemented yet in ps_calculations.py

    # --- Amplitude Shaping ---
    pulse.set_amp_control(1)
    ## Amplitude mask — uncomment at most one (double_pulse is used automatically in multi)
    # pulse.AmpFcn.constant(pulse)
    # pulse.AmpFcn.multi_gaussian(pulse, [2, 4, 6], 1)
    if tau is not None:
        # R, w0, tau, phi  — same call as Optical-Pulse-Shaping / OPS
        pulse.AmpFcn.double_pulse(pulse, 1, 0, tau, 0)
    # else:
    #     pulse.AmpFcn.double_pulse(pulse, 1, 0, 1, 0)
    # pulse.set_amp_eq()  # not implemented yet

    pulse.generate_waveform(randomize_phi=True)
    return pulse


# =====================================================================
# Start AWG
# =====================================================================
if __name__ == "__main__":
    awg = AWG(RL, SR, voltage_max_mV=VOLTAGE_MAX_MV)

    opened = awg.open_card()
    if not opened:
        print("Stopping: the AWG was not opened. See the message above.")
        sys.exit(1)

    try:
        if PLAYBACK == "multi":
            # Several waveforms, same length. Each tau → one segment.
            pulses = [build_one_waveform(RL, tau=tau) for tau in TAU_LIST]
            waveforms = [p.Vt for p in pulses]
            nseg = len(waveforms)

            print(f"PLAYBACK=multi  {nseg} segments  taus={TAU_LIST}")

            awg.reconfigure_for_sequence(RL * nseg)
            awg.setup_card(
                trigger_mode=TRIGGER_MODE,
                playback="multi",
                loops=LOOPS,
                num_segments=nseg,
                segment_samples=RL,
                ext0_level_mV=EXT0_LEVEL_MV,
            )
            awg.allocate_buffer()
            awg.load_waveforms_in_buffer(waveforms)
            awg.write_waveform_to_card()

            # Plot the first segment so the usual 2×2 figure still appears.
            pulses[0].plot_pulse_shaper_results("time")
            # pulses[0].plot_pulse_shaper_results("freq")

        else:
            # One waveform — Optical-Pulse-Shaping/main.py path.
            pulse = build_one_waveform(RL, tau=None)

            if TRIGGER_MODE == "internal":
                # Pad RF with zeros so free-run on the sample clock is pulsed, not CW.
                period_s = INTERNAL_PERIOD_MS / 1000.0
                period = awg.build_internal_period(pulse.Vt, period_s=period_s)
                awg.reconfigure_for_sequence(period.size)
                awg.setup_card(
                    trigger_mode="internal",
                    playback="single",
                    loops=0,
                )
                awg.allocate_buffer()
                awg.load_waveform_in_buffer(period)
            else:
                awg.setup_card(
                    trigger_mode="external",
                    playback="single",
                    loops=LOOPS,
                    ext0_level_mV=EXT0_LEVEL_MV,
                )
                awg.allocate_buffer()
                awg.load_waveform_in_buffer(pulse.Vt)

            awg.write_waveform_to_card()
            pulse.plot_pulse_shaper_results("time")
            # pulse.plot_pulse_shaper_results("freq")

        print("Plot closed — card running until Ctrl+C.")
        awg.run_until_interrupt()

    finally:
        awg.close_card()
        sys.exit(0)
