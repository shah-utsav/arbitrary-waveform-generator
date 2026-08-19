"""
multi_pulse_test.py
===================

PURPOSE
-------
Play several different optical-pulse-shaping RF waveforms on a Spectrum M4i AWG,
one waveform per external trigger edge, using Multiple Replay mode.

WHY "MULTI" INSTEAD OF SINGLE / SINGLERESTART?
---------------------------------------------
  SPC_REP_STD_SINGLERESTART  → every Ext0 edge plays the *same* memory buffer.
  SPC_REP_STD_MULTI          → on-card memory is split into equal *segments*;
                               each Ext0 edge plays the *next* segment, then
                               waits for another edge.

That lets you A/B several double-pulse delays (tau) without reloading the card:
  Ext0 #1 → segment 0 (tau[0])
  Ext0 #2 → segment 1 (tau[1])
  Ext0 #3 → segment 2 (tau[2])
  Ext0 #4 → segment 3 (tau[3])
  Ext0 #5 → wraps / continues according to card loops setting

HARDWARE HOOKUP
---------------
  SRS DG645 (or any TTL pulser)  TTL OUT ──coax──►  Spectrum Trg0 / Ext0
  Spectrum Ch0 SMA              ──►  RF to AOM / scope

  Do *not* wire the DG645 into Ch0. Ch0 is the analog RF output.
  Ext0/Trg0 is the trigger *input* that tells the card "play next segment now."

PHYSICS KNOB (tau)
------------------
  tau enters AmpFcn.double_pulse in ps_calculations.py. It sets the fringing
  in the RF mask so the *optical* field becomes two pulses separated by tau.
  Changing TAU_LIST below is the experiment your PI asked for.

LIBRARY USED HERE
-----------------
  `spcm` = Spectrum's official high-level Python package (Card, Clock, Trigger,
  Multi, …). This script does *not* use hardware/AWG/_spectrumAWG.py.
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
# from __future__ import annotations
#   Lets us write modern type hints; harmless on older Python if present.
from __future__ import annotations

# numpy: array math for waveforms and int16 conversion.
import numpy as np

# spcm: Spectrum Instrumentation Python API (wraps the low-level spcm DLL).
import spcm

# units: pint-based physical units (V, MHz, S, …) used by spcm setters so you
# do not pass raw ambiguous numbers into clock/trigger registers.
from spcm import units

# Pulse_Shaper_Calculations: lab math that turns masks (re, im, phase) into V(t).
from ps_calculations import Pulse_Shaper_Calculations


# ===========================================================================
# USER KNOBS — change these; leave the rest alone until you know why
# ===========================================================================

# SR_MSa — sample rate in mega-samples per second (MSa/s).
#   The M4i.6631-x8 tops out at 1250 MSa/s. Higher SR → finer time grid and
#   more samples for the same pulse duration T_us.
#   NOTE: Pulse_Shaper_Calculations uses the same numeric convention as main.py:
#   pass 1250 here (not 1.25e9). Internally t[i] = i / SR with SR in "mega" units,
#   matching f0 in MHz.
SR_MSa = 1250

# F0_MHz — RF carrier frequency in megahertz, programmed into phi_car = 2*pi*f0*t.
#   This is the tone that drives the AOM; 100 MHz is a typical lab value.
F0_MHz = 100

# T_us — duration of *one segment* in microseconds.
#   In MULTI mode, one Ext0 edge plays exactly one segment of this length.
#   Example: 10 us @ 1250 MSa/s ≈ 12_500 samples (then rounded up to a multiple
#   of 32 — see _align32).
T_us = 10

# NUM_SEGMENTS — how many equal pieces to split on-card memory into.
#   Must match len(TAU_LIST). Each segment holds one double-pulse V(t).
NUM_SEGMENTS = 4

# TAU_LIST — double-pulse delay parameter for each segment (PI experiment).
#   Passed as the `tau` argument of AmpFcn.double_pulse(parent, R, w0, tau, phi).
#   Larger |tau| → larger optical pulse separation (and different RF mask shape).
#   Try values like 0, 0.25, 0.5, 1.0 and compare scope / spectrometer traces.
TAU_LIST = [0.0, 0.25, 0.5, 1.0]

# Sanity check at import time: MULTI needs one tau per segment.
assert len(TAU_LIST) == NUM_SEGMENTS, "TAU_LIST length must equal NUM_SEGMENTS"

# EXT0_LEVEL — Ext0 analog comparator threshold.
#   Spectrum Ext0 is *not* a pure digital TTL pin; it is a window comparator.
#   DG645 high is often ~2.5 V. Setting level0 to 1 V means "fire on rising edge
#   through 1 V," which sits cleanly between logic low (~0 V) and high (~2.5 V).
#   `units.V` tags the number as volts for the spcm Trigger API.
EXT0_LEVEL = 1.0 * units.V


# ===========================================================================
# Helper functions
# ===========================================================================

def _align32(n: int) -> int:
    """
    Round *up* to the next multiple of 32 samples.

    WHY 32?
      Spectrum M4i DMA / MEMSIZE rules require sample counts that are multiples
      of 32. If you ask for 12_500 samples, the card may reject the setup or
      behave oddly. Ceil-to-32 avoids that.

    Example: 12500 → 12512  (12500 / 32 = 390.625 → need 391*32).
    """
    n = int(n)
    return n if n % 32 == 0 else n + (32 - n % 32)


def _vt_to_int16(vt: np.ndarray, max_value: int) -> np.ndarray:
    """
    Convert floating-point V(t) into the int16 codes the AWG DAC expects.

    Parameters
    ----------
    vt :
        Real waveform from Pulse_Shaper_Calculations, ideally in [-1, 1].
        +1 → nearly full-scale positive DAC code; -1 → full-scale negative.
    max_value :
        From card.max_sample_value() — typically 32768 for 16-bit AWG.
        We use (max_value - 1) so we never hit the extreme code the driver
        reserves / warns about.

    Returns
    -------
    numpy.ndarray of dtype int16, same length as vt.

    Steps
    -----
    1. clip to [-1, 1] so a math glitch cannot overflow the DAC.
    2. scale by (max_value - 1).
    3. cast to int16 (what DMA expects for 16-bit AO cards).
    """
    clipped = np.clip(np.asarray(vt, dtype=np.float64), -1.0, 1.0)
    return ((max_value - 1) * clipped).astype(np.int16)


def _build_double_pulse_vt(
    sr_msa: float,
    record_length: int,
    f0_mhz: float,
    tau: float,
) -> np.ndarray:
    """
    Build one RF segment V(t) for a chosen double-pulse delay tau.

    This is the physics path your PI sketched on the board:

        arg   = tau * (ω - ω0) + phi          (in code: tau*(wt - w0) + phi)
        re    = 0.5 * (1 + R * cos(arg))
        im    = 0.5 * R * sin(arg)
        φ_tot = 2 π f0 t                      (carrier)
        V(t)  = re*cos(φ_tot) - im*sin(φ_tot)

    Parameters
    ----------
    sr_msa :
        Sample rate in MSa/s (same convention as SR_MSa).
    record_length :
        Number of samples in this segment (must match MULTI segment size).
    f0_mhz :
        Carrier in MHz (same convention as F0_MHz).
    tau :
        Double-pulse delay parameter — the value you vary across segments.

    Returns
    -------
    Vt : 1-D float array of length record_length.
    """
    # Create the pulse-shaper object: builds time base t and optical axis wt.
    pulse = Pulse_Shaper_Calculations(sr_msa, record_length)

    # Carrier phase φ_car = 2 π f0 t  (RF tone inside the segment).
    pulse.set_carrier_freq_phi(f0_mhz)

    # Overall amplitude scale in [0, 1]. 1.0 = use full programmed mask height.
    pulse.set_amp_control(1.0)

    # AmpFcn.double_pulse(parent, R, w0, tau, phi)
    #   R   = relative strength of the delayed arm (1.0 = equal arms)
    #   w0  = center locking frequency on the wt axis (0 with default calib)
    #   tau = delay knob (THE experiment variable)
    #   phi = extra relative phase between arms (0 = none)
    pulse.AmpFcn.double_pulse(pulse, 1.0, 0.0, tau, 0.0)

    # generate_waveform combines re/im with φ_tot into Vt.
    # randomize_phi=False → same carrier phase every segment so differences you
    # see between segments are from tau, not from a random phase draw.
    return pulse.generate_waveform(randomize_phi=False)


# ===========================================================================
# main() — open card, fill segments, arm, wait for Ext0
# ===========================================================================

def main() -> None:
    """
    End-to-end MULTI replay:

      1. Size segments (32-aligned).
      2. Open the Spectrum AO card.
      3. Program MULTI mode, clock, Ext0 trigger, channel 0.
      4. Allocate a multi-segment DMA buffer.
      5. Fill each segment with double_pulse(Vt) for that tau.
      6. DMA to the card.
      7. START + ENABLETRIGGER and wait (Ctrl+C to stop).
    """

    # ----- Size the memory layout -----
    # segment_rl = samples in ONE segment ≈ SR_MSa * T_us, then align to 32.
    # np.ceil(.../32)*32 is an alternate way to align; _align32 does the same.
    segment_rl = _align32(int(np.ceil(SR_MSa * T_us / 32) * 32))

    # total_samples = entire on-card MEMSIZE = (#segments) × (samples/segment).
    # In MULTI: SPC_MEMSIZE = total_samples, SPC_SEGMENTSIZE = segment_rl.
    # The spcm Multi helper sets SEGMENTSIZE when you allocate_buffer(...).
    total_samples = segment_rl * NUM_SEGMENTS

    print(
        f"MULTI replay: {NUM_SEGMENTS} segments × {segment_rl} samples "
        f"(~{T_us} us @ {SR_MSa} MSa/s), taus={TAU_LIST}"
    )

    # ----- Open the card -----
    # `/dev/spcm0` is Spectrum's device path for the first card (Windows + Linux).
    # If that fails (busy, missing, wrong index), fall back to "first AO card"
    # discovery via card_type=SPCM_TYPE_AO (analog output / AWG).
    #
    # We use __enter__/__exit__ manually so both open paths share one cleanup.
    # (A normal `with spcm.Card(...) as card:` is equivalent for a single path.)
    try:
        card_ctx = spcm.Card("/dev/spcm0", verbose=True)
        card = card_ctx.__enter__()
    except Exception:
        card_ctx = spcm.Card(card_type=spcm.SPCM_TYPE_AO, verbose=True)
        card = card_ctx.__enter__()

    try:
        # ----- Card mode: Multiple Replay -----
        # SPC_REP_STD_MULTI = split memory into equal segments; one segment
        # per accepted trigger. See Spectrum M4i manual "Multiple Replay".
        card.card_mode(spcm.SPC_REP_STD_MULTI)

        # SPC_LOOPS (via card.loops):
        #   0  → keep accepting triggers indefinitely until STOP / Ctrl+C
        #   N>0 → stop after N segment plays total
        card.loops(0)

        # Full-scale DAC magnitude (e.g. 32768). Needed to scale float→int16.
        max_value = int(card.max_sample_value())

        # ----- Analog channel 0 -----
        # CHANNEL0: only enable Ch0 (RF out). Extra channels waste memory bandwidth.
        channels = spcm.Channels(card, card_enable=spcm.CHANNEL0)
        channels.enable(True)

        # highZ: output load assumption for amplitude programming.
        #   high-Z vs 50 Ω changes how the programmed "1 V" maps to the SMA.
        #   Lab AOM paths are often treated as high-Z in examples; match your cabling.
        channels.output_load(units.highZ)

        # amp(1 V): full-scale output range ±1 V into the programmed load model.
        channels.amp(1 * units.V)

        # ----- Sample clock -----
        clock = spcm.Clock(card)

        # sample_rate: how fast MEMSIZE samples are clocked out of the DAC.
        #   We pass SR_MSa * units.MHz so 1250 → 1250 MHz = 1.25 GSa/s.
        #   This MUST match the SR used in Pulse_Shaper_Calculations, or the
        #   intended T_us duration on the scope will be wrong.
        clock.sample_rate(SR_MSa * units.MHz)

        # clock_output(False): do not emit a clock on a multi-purpose clock SMA.
        clock.clock_output(False)

        # ----- External trigger (Ext0 / Trg0) -----
        trigger = spcm.Trigger(card)

        # or_mask: which sources can fire a trigger (OR combination).
        #   SPC_TMASK_EXT0 = the front-panel Ext0/Trg0 comparator input.
        #   (Software trigger would be SPC_TMASK_SOFTWARE — not used here.)
        trigger.or_mask(spcm.SPC_TMASK_EXT0)

        # ext0_mode POS: trigger on a rising edge through the level threshold.
        trigger.ext0_mode(spcm.SPC_TM_POS)

        # COUPLING_DC: Ext0 path is DC-coupled (correct for slow TTL from DG645).
        trigger.ext0_coupling(spcm.COUPLING_DC)

        # ext0_level0: comparator threshold in volts (see EXT0_LEVEL comment).
        trigger.ext0_level0(EXT0_LEVEL)

        print(
            f"Ext0 rising @ {EXT0_LEVEL} — wire DG645 TTL → Trg0. "
            "Each edge plays the next tau segment."
        )

        # ----- Multi-segment DMA buffer -----
        # spcm.Multi: helper that knows about SPC_SEGMENTSIZE + reshaping the
        # host buffer to [segment_index, sample_index, channel_index].
        multiple_replay = spcm.Multi(card)

        # This card family uses 2 bytes/sample (16-bit DAC). Bail if not.
        if multiple_replay.bytes_per_sample != 2:
            raise spcm.SpcmException(text="Non 16-bit DA not supported")

        # memory_size: total samples on the card (all segments concatenated).
        #   units.S = "samples" (pint), not seconds.
        multiple_replay.memory_size(total_samples * units.S)

        # allocate_buffer(segment_samples, num_segments):
        #   - sets SPC_SEGMENTSIZE to segment_rl
        #   - allocates PC RAM for DMA
        #   - reshapes .buffer to shape (NUM_SEGMENTS, segment_rl, n_channels)
        multiple_replay.allocate_buffer(segment_rl * units.S, NUM_SEGMENTS)

        # ----- Fill each segment with a different-tau V(t) -----
        for i, tau in enumerate(TAU_LIST):
            # Build float waveform for this tau (length = segment_rl).
            vt = _build_double_pulse_vt(SR_MSa, segment_rl, F0_MHz, tau)
            if vt.size != segment_rl:
                raise RuntimeError(
                    f"Vt length {vt.size} != segment_rl {segment_rl} (tau={tau})"
                )

            # buffer[i, :, 0]:
            #   i  = segment index (which Ext0 edge will play this)
            #   :  = all samples in that segment
            #   0  = channel 0
            multiple_replay.buffer[i, :, 0] = _vt_to_int16(vt, max_value)
            print(f"  segment[{i}] ← double_pulse tau={tau:g}")

        # ----- DMA host → card -----
        # M2CMD_DATA_STARTDMA: begin the transfer into on-card memory.
        # M2CMD_DATA_WAITDMA:  block until the DMA finishes so we do not arm
        #                      before the segments are valid on the card.
        multiple_replay.start_buffer_transfer(
            spcm.M2CMD_DATA_STARTDMA, spcm.M2CMD_DATA_WAITDMA
        )

        # ----- Arm and wait for triggers -----
        # M2CMD_CARD_ENABLETRIGGER: open the trigger gate (listen for Ext0).
        # M2CMD_CARD_WAITREADY:     block until the card reports "done".
        #
        # With loops=0, MULTI never finishes by itself, so WAITREADY sits here
        # until you Ctrl+C (KeyboardInterrupt) or kill the process.
        # That is intentional: the card stays armed for as many DG645 edges
        # as you want.
        print("Armed — waiting for Ext0 (Ctrl+C to stop).")
        try:
            card.start(spcm.M2CMD_CARD_ENABLETRIGGER, spcm.M2CMD_CARD_WAITREADY)
        except KeyboardInterrupt:
            print("\nCtrl+C — stopping card.")
        finally:
            # card.stop(): issue STOP so RF ceases and the driver is idle.
            try:
                card.stop()
            except Exception:
                pass
    finally:
        # Always close the driver handle (spcm_vClose under the hood), even if
        # setup failed mid-way. Leaving the handle open blocks SBench / others.
        card_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------
# `python multi_pulse_test.py` sets __name__ to "__main__", so main() runs.
# If another file imports this module, main() does *not* auto-run.
if __name__ == "__main__":
    main()