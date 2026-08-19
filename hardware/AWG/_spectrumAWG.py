"""
Spectrum M4i.6631-X8 analog-output (AWG) wrapper.

This is the same *style* of wrapper as Optical-Pulse-Shaping: ctypes + pyspcm,
one class, one method per hardware step. Dummy_AWG in this folder uses the
same method names so you can swap backends in main.py.

Card memory / replay modes we use (all with the card's internal sample clock):
  - internal + single  →  SPC_REP_STD_SINGLE
        One software trigger starts free-run. Memory is [RF burst | idle zeros]
        so the built-in clock sets the period. No delay generator needed.
  - external + single  →  SPC_REP_STD_SINGLERESTART
        Each rising edge on Ext0 (Trg0) plays the same MEMSIZE once.
        Office: SRS delay generator. Lab: photodiode.
  - external + multi   →  SPC_REP_STD_MULTI
        Memory is split into equal segments. Ext0 #1 → segment 0, #2 → segment 1, …
  - internal + multi   →  SPC_REP_STD_SINGLE
        All segments are concatenated into one period (optional idle after each)
        and free-run on the sample clock. No host retrigger loop.

M4i rule: MEMSIZE, SEGMENTSIZE, and DMA length must be multiples of 32 samples.
"""

import ctypes
import sys
import time

import numpy as np

# pyspcm loads Spectrum's DLL at import time (spcm_win64.dll on Windows).
# If that fails we do *not* dump a ctypes traceback — we print what to do.
try:
    from .spectrum_AWG_drivers.pyspcm import *
    from .spectrum_AWG_drivers.spcm_tools import *
except OSError as exc:
    print(
        "\n[Spectrum_AWG] Could not load the Spectrum driver library.\n"
        "  Windows looks for  spcm_win64.dll  (or spcm_win32.dll).\n"
        "  Linux   looks for  libspcm_linux.so.\n\n"
        "  Typical fixes:\n"
        "    1. Install the Spectrum Instrumentation driver for the M4i card.\n"
        "    2. Close SBench 6 if it is open (it can hold the driver).\n"
        "    3. If you only want to look at plots / debug math, set\n"
        "       USE_DUMMY = True  in main.py  (no card required).\n\n"
        f"  Underlying OS error: {exc}\n"
    )
    raise SystemExit(1)


def _align32(n):
    """Round sample count up to a multiple of 32 (M4i DMA / MEMSIZE rule)."""
    n = int(n)
    if n % 32 == 0:
        return n
    return n + (32 - n % 32)


class Spectrum_AWG:
    def __init__(self, record_length, sampling_rate_MSa_s, voltage_max_mV, verbose=True):
        """
        Store user numbers. Nothing is sent to the card yet.

        record_length        : samples in the *first* buffer you plan to load
                               (for multi, this is usually one segment; setup_card
                               will grow RECORD_LENGTH to N × segment).
        sampling_rate_MSa_s  : Mega-samples per second. M4i.6631-X8 max is 1250.
        voltage_max_mV       : analog range into 50 Ω, millivolts (80 … 2000).
        """
        # Practical host-RAM limit is well below the card's ~2e9 sample max.
        if not (32 <= record_length <= 125e6):
            print(
                f"[Spectrum_AWG] Record length {record_length} is outside "
                f"32 … 125e6 samples (the useful range on a typical PC)."
            )
            raise SystemExit(1)
        self.RECORD_LENGTH = int(record_length)

        if not (0 < sampling_rate_MSa_s <= 1250):
            print(
                f"[Spectrum_AWG] Sampling rate {sampling_rate_MSa_s} MSa/s is "
                f"outside 0 … 1250 (M4i.6631-X8 maximum)."
            )
            raise SystemExit(1)
        self.SR = int(sampling_rate_MSa_s * 1e6)  # convert MSa/s → Sa/s

        if not (80 <= voltage_max_mV <= 2000):
            print(
                f"[Spectrum_AWG] Output range {voltage_max_mV} mV is outside "
                f"±80 mV … ±2000 mV (50 Ω)."
            )
            raise SystemExit(1)
        self.OUTPUT_RANGE_MV = int(voltage_max_mV)

        self.verbose = verbose
        self.hCard = None              # driver handle, set by open_card()
        self.lMaxADC = None            # 16-bit full scale, typically 32768
        self.lBytesPerSample = None    # 2 for 16-bit
        self.pvBuffer = None           # page-aligned DMA buffer
        self.pnBuffer = None           # same buffer viewed as int16*

        # Remember how the card was last configured (dummy mirrors these).
        self.trigger_mode = "internal"   # "internal" or "external"
        self.playback = "single"         # "single" or "multi"
        self.loops = 0
        self.num_segments = 1
        self.segment_length = int(record_length)
        self._card_is_running = False

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_card(self, device_path="/dev/spcm0"):
        """
        Open the Spectrum driver handle and confirm this is an analog-output card.

        Spectrum uses Unix-looking paths even on Windows: /dev/spcm0, /dev/spcm1, …
        If /dev/spcm0 is busy or empty we try the next few indices.

        Returns True on success. On failure we print a plain-English diagnosis
        and return False — we do not raise a ctypes stack trace.
        """
        tried = []

        def _try_open(path):
            tried.append(path)
            handle = spcm_hOpen(create_string_buffer(path.encode("ascii")))
            return handle

        self.hCard = _try_open(device_path)

        if not self.hCard:
            # Another program (or another slot) may own spcm0.
            for idx in range(16):
                path = f"/dev/spcm{idx}"
                if path in tried:
                    continue
                handle = _try_open(path)
                if not handle:
                    continue
                fnc = int32(0)
                spcm_dwGetParam_i32(handle, SPC_FNCTYPE, byref(fnc))
                if fnc.value == SPCM_TYPE_AO:
                    self.hCard = handle
                    device_path = path
                    break
                # Digitizer / other card — close and keep looking.
                spcm_vClose(handle)

        if not self.hCard:
            print(
                "\n[Spectrum_AWG] No Spectrum AWG card was detected.\n\n"
                "  The program looked for a card at: "
                + ", ".join(tried[:5])
                + (" …" if len(tried) > 5 else "")
                + "\n\n"
                "  What is usually wrong:\n"
                "    1. The M4i.6631-X8 is not installed, or the PC is not the lab PC.\n"
                "    2. The Spectrum driver is installed but the card has no power / PCIe link.\n"
                "    3. SBench 6 (or another Python script) already opened the card — close it.\n"
                "    4. You only want to debug plots / math: set USE_DUMMY = True in main.py.\n"
            )
            return False

        # SPC_FNCTYPE must be analog output. Digitizers use a different value.
        lFncType = int32(0)
        spcm_dwGetParam_i32(self.hCard, SPC_FNCTYPE, byref(lFncType))
        if lFncType.value != SPCM_TYPE_AO:
            print(
                f"[Spectrum_AWG] {device_path} is not an analog-output (AWG) card.\n"
                "  This program is written for the M4i.6631-X8 AWG."
            )
            spcm_vClose(self.hCard)
            self.hCard = None
            return False

        # DAC full-scale integer (16-bit → 32768). We scale floats into this.
        self.lMaxADC = int32(0)
        spcm_dwGetParam_i32(self.hCard, SPC_MIINST_MAXADCVALUE, byref(self.lMaxADC))

        self.lBytesPerSample = int32(0)
        spcm_dwGetParam_i32(self.hCard, SPC_MIINST_BYTESPERSAMPLE, byref(self.lBytesPerSample))

        if self.verbose:
            print(
                f"[Spectrum_AWG] Card open at {device_path}  "
                f"(max ADC={self.lMaxADC.value}, bytes/sample={self.lBytesPerSample.value})"
            )
        return True

    def close_card(self):
        """Release the driver handle. Always call this (main.py uses try/finally)."""
        if self.hCard:
            self.stop_output()
            spcm_vClose(self.hCard)
            self.hCard = None
            self.pvBuffer = None
            self.pnBuffer = None
            if self.verbose:
                print("[Spectrum_AWG] Card closed.")

    # ------------------------------------------------------------------
    # Configure (one method for single, multi, internal, external)
    # ------------------------------------------------------------------

    def reconfigure_for_sequence(self, total_samples):
        """
        Change RECORD_LENGTH *before* allocate_buffer().

        Used when internal mode pads RF with idle zeros, or when multi
        concatenates N segments.
        """
        total_samples = _align32(total_samples)
        if total_samples < 32:
            print("[Spectrum_AWG] total_samples must be at least 32.")
            raise SystemExit(1)
        self.RECORD_LENGTH = total_samples

    def setup_card(
        self,
        trigger_mode="internal",
        playback="single",
        loops=0,
        num_segments=1,
        segment_samples=None,
        ext0_level_mV=1500,
    ):
        """
        Program clock, memory layout, trigger, and analog output.

        trigger_mode
            "internal"  (also accepts "software")
                Start from software. Timing comes from SPC_SAMPLERATE (INTPLL).
            "external"
                Start from Ext0 / Trg0 (SRS delay generator or photodiode).

        playback
            "single"  one waveform in memory (same burst every shot)
            "multi"   N equal segments (different burst every Ext0, or
                      concatenated on the sample clock if trigger is internal)

        loops
            0 = keep going until stop_output() / Ctrl+C
            N = stop after N plays (Spectrum's SPC_LOOPS)

        The sample clock is always the card's internal PLL (SPC_CM_INTPLL).
        We do not switch to an external reference clock here.
        """
        if trigger_mode in ("software", "internal"):
            trigger_mode = "internal"
        if trigger_mode not in ("internal", "external"):
            print(
                f"[Spectrum_AWG] Unknown trigger_mode '{trigger_mode}'. "
                "Use 'internal' or 'external'."
            )
            raise SystemExit(1)
        if playback not in ("single", "multi"):
            print(
                f"[Spectrum_AWG] Unknown playback '{playback}'. "
                "Use 'single' or 'multi'."
            )
            raise SystemExit(1)

        self.trigger_mode = trigger_mode
        self.playback = playback
        self.loops = int(loops)

        # ----- choose Spectrum CARDMODE -----
        if playback == "multi" and trigger_mode == "external":
            # One segment per Ext0 edge.
            card_mode = SPC_REP_STD_MULTI
            if segment_samples is None:
                segment_samples = self.RECORD_LENGTH // max(int(num_segments), 1)
            self.segment_length = _align32(segment_samples)
            self.num_segments = int(num_segments)
            self.RECORD_LENGTH = self.segment_length * self.num_segments
        elif playback == "multi" and trigger_mode == "internal":
            # No Ext0: play every segment back-to-back on the sample clock.
            card_mode = SPC_REP_STD_SINGLE
            if segment_samples is None:
                segment_samples = self.RECORD_LENGTH // max(int(num_segments), 1)
            self.segment_length = _align32(segment_samples)
            self.num_segments = int(num_segments)
            self.RECORD_LENGTH = _align32(self.segment_length * self.num_segments)
            self.loops = 0  # free-run the concatenated period
        elif trigger_mode == "external":
            # Same MEMSIZE on every Ext0.
            card_mode = SPC_REP_STD_SINGLERESTART
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
        else:
            # Internal single: free-run MEMSIZE after one software start.
            card_mode = SPC_REP_STD_SINGLE
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
            self.loops = 0

        # Replay mode + internal sample clock (the AWG's built-in clock).
        spcm_dwSetParam_i32(self.hCard, SPC_CARDMODE, card_mode)
        spcm_dwSetParam_i32(self.hCard, SPC_CLOCKMODE, SPC_CM_INTPLL)
        spcm_dwSetParam_i64(self.hCard, SPC_SAMPLERATE, int64(self.SR))

        # Channel 0 only (matches Optical-Pulse-Shaping).
        spcm_dwSetParam_i64(self.hCard, SPC_CHENABLE, 0x1)

        # SEGMENTSIZE must equal MEMSIZE in non-MULTI modes.
        if card_mode == SPC_REP_STD_MULTI:
            spcm_dwSetParam_i64(self.hCard, SPC_SEGMENTSIZE, int64(self.segment_length))
        else:
            spcm_dwSetParam_i64(self.hCard, SPC_SEGMENTSIZE, int64(self.RECORD_LENGTH))
        spcm_dwSetParam_i64(self.hCard, SPC_MEMSIZE, int64(self.RECORD_LENGTH))
        spcm_dwSetParam_i64(self.hCard, SPC_LOOPS, int64(self.loops))

        # Trigger: Ext0 rising ~1.5 V (TTL), or software (internal).
        spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ANDMASK, 0)
        if trigger_mode == "external":
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ORMASK, SPC_TMASK_EXT0)
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_EXT0_MODE, SPC_TM_POS)
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_EXT0_LEVEL0, int32(int(ext0_level_mV)))
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_TERM, 0)  # high-Z, not 50 Ω
        else:
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ORMASK, SPC_TMASK_SOFTWARE)

        # Analog range and enable Ch0.
        spcm_dwSetParam_i32(self.hCard, SPC_AMP0, int32(self.OUTPUT_RANGE_MV))
        spcm_dwSetParam_i64(self.hCard, SPC_ENABLEOUT0, int32(1))

        if self.verbose:
            mem_us = 1e6 * float(self.RECORD_LENGTH) / float(self.SR)
            names = {
                SPC_REP_STD_SINGLE: "SINGLE",
                SPC_REP_STD_SINGLERESTART: "SINGLERESTART",
                SPC_REP_STD_MULTI: "MULTI",
            }
            print(
                f"[Spectrum_AWG] {names.get(card_mode, card_mode)}  "
                f"clock=INTPLL  SR={self.SR * 1e-6} MSa/s  "
                f"MEMSIZE={self.RECORD_LENGTH} (~{mem_us:.3f} us)  "
                f"trigger={trigger_mode}  playback={playback}  "
                f"SPC_LOOPS={self.loops}"
            )
            if playback == "multi":
                print(
                    f"[Spectrum_AWG]   segments={self.num_segments} × "
                    f"{self.segment_length} samples"
                )

    # ------------------------------------------------------------------
    # Buffers and waveforms
    # ------------------------------------------------------------------

    def allocate_buffer(self):
        """Page-aligned host buffer for DMA (Spectrum requires 4096-byte align)."""
        nbytes = self.RECORD_LENGTH * self.lBytesPerSample.value
        self.pvBuffer = pvAllocMemPageAligned(nbytes)
        self.pnBuffer = cast(self.pvBuffer, ptr16)
        if self.verbose:
            print(
                f"[Spectrum_AWG] Allocated buffer of {self.RECORD_LENGTH} int16 samples."
            )

    def build_internal_period(self, burst, period_s):
        """
        Build one sample-clock period: RF samples + trailing zeros.

        Why zeros? If you free-run *only* the RF burst, the card plays it
        back-to-back and you see a continuous wave, not pulses. Padding idle
        to period_s makes the built-in clock the pulse repetition clock.

        burst     : float array in [-1, 1], length ≈ SR * T
        period_s  : desired period in seconds (e.g. 0.001 for 1 kHz)
        """
        burst = np.asarray(burst, dtype=np.float64).ravel()
        burst_n = _align32(max(burst.size, 32))
        total = _align32(max(burst_n + 32, int(round(float(period_s) * self.SR))))
        period = np.zeros(total, dtype=np.float64)
        n = min(burst.size, burst_n)
        period[:n] = np.clip(burst[:n], -1.0, 1.0)
        if self.verbose:
            print(
                f"[Spectrum_AWG] Internal period: {n} RF samples + {total - n} zeros "
                f"= {total} samples ({1e6 * total / self.SR:.6f} us at {self.SR * 1e-6} MSa/s)"
            )
        return period

    def load_waveform_in_buffer(self, voltage_array):
        """
        Copy one float waveform ([-1, 1]) into the DMA buffer as int16 DAC codes.

        Length must equal RECORD_LENGTH. For internal single mode, pass the
        output of build_internal_period(), not the bare RF burst.
        """
        voltage_array = np.asarray(voltage_array)
        if voltage_array.size != self.RECORD_LENGTH:
            print(
                f"[Spectrum_AWG] Waveform length {voltage_array.size} does not match "
                f"buffer {self.RECORD_LENGTH}. For internal mode, pad with "
                "build_internal_period() first."
            )
            raise SystemExit(1)

        max_dac = self.lMaxADC.value - 1  # 32767; avoids wrapping at +32768
        if not np.issubdtype(voltage_array.dtype, np.integer):
            below = np.any(voltage_array < -1)
            above = np.any(voltage_array > 1)
            if below or above:
                print(
                    "[Spectrum_AWG] Warning: waveform clipped to [-1, 1] before DAC scaling."
                )
            clipped = np.clip(voltage_array, -1, 1)
            codes = (max_dac * clipped).astype(np.int16)
        else:
            below = np.any(voltage_array < -max_dac)
            above = np.any(voltage_array > max_dac)
            if below or above:
                print("[Spectrum_AWG] Warning: integer waveform clipped to DAC range.")
            codes = np.clip(voltage_array, -max_dac, max_dac).astype(np.int16)

        ctypes.memmove(self.pnBuffer, codes.ctypes.data, codes.nbytes)
        if self.verbose:
            print("[Spectrum_AWG] Loaded waveform into buffer.")

    def load_waveforms_in_buffer(self, voltage_arrays):
        """
        Concatenate several float waveforms into the DMA buffer.

        Layout: [seg0 | seg1 | … | segN-1]  — this is what MULTI expects,
        and it is also what internal-multi free-run expects.
        Every array must be the same length (we pad to a multiple of 32).
        """
        arrays = [np.asarray(a, dtype=np.float64).ravel() for a in voltage_arrays]
        if not arrays:
            print("[Spectrum_AWG] load_waveforms_in_buffer needs at least one waveform.")
            raise SystemExit(1)
        raw_len = arrays[0].size
        for i, a in enumerate(arrays):
            if a.size != raw_len:
                print(
                    f"[Spectrum_AWG] Segment {i} length {a.size} != segment 0 length {raw_len}."
                )
                raise SystemExit(1)

        seg_len = _align32(raw_len)
        expected = seg_len * len(arrays)
        if expected != self.RECORD_LENGTH:
            print(
                f"[Spectrum_AWG] Concatenated length {expected} != buffer {self.RECORD_LENGTH}. "
                "Call setup_card(..., playback='multi', num_segments=N, segment_samples=...) "
                "and allocate_buffer() first."
            )
            raise SystemExit(1)

        max_dac = self.lMaxADC.value - 1
        base = ctypes.cast(self.pnBuffer, ctypes.c_void_p).value
        for i, a in enumerate(arrays):
            padded = np.zeros(seg_len, dtype=np.float64)
            padded[: a.size] = np.clip(a, -1.0, 1.0)
            codes = (max_dac * padded).astype(np.int16)
            dest = ctypes.cast(
                ctypes.c_void_p(base + i * seg_len * self.lBytesPerSample.value),
                ptr16,
            )
            ctypes.memmove(dest, codes.ctypes.data, codes.nbytes)
        if self.verbose:
            print(
                f"[Spectrum_AWG] Loaded {len(arrays)} segments × {seg_len} samples into buffer."
            )

    def write_waveform_to_card(self):
        """DMA the host buffer onto the card (blocks until the copy finishes)."""
        nbytes = self.RECORD_LENGTH * self.lBytesPerSample.value
        spcm_dwDefTransfer_i64(
            self.hCard,
            SPCM_BUF_DATA,
            SPCM_DIR_PCTOCARD,
            int32(0),
            self.pvBuffer,
            uint64(0),
            uint64(nbytes),
        )
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_DATA_STARTDMA | M2CMD_DATA_WAITDMA)
        if self.verbose:
            print("[Spectrum_AWG] Waveform written to card (DMA done).")

    # ------------------------------------------------------------------
    # Run / stop
    # ------------------------------------------------------------------

    def output_waveform(self, wait_ready=True):
        """
        Arm the card.

        internal : START + ENABLETRIGGER + FORCETRIGGER
                   (one software start; then the sample clock owns the timing)
        external : START + ENABLETRIGGER
                   (card waits for Ext0; do not FORCETRIGGER)

        wait_ready: if True, block until the card reports READY. Internal
        free-run (SINGLE + LOOPS=0) never goes READY — we skip WAITREADY there.
        """
        if self.trigger_mode == "internal":
            cmd = (
                M2CMD_CARD_START
                | M2CMD_CARD_ENABLETRIGGER
                | M2CMD_CARD_FORCETRIGGER
            )
        else:
            cmd = M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER

        if wait_ready and not (self.trigger_mode == "internal" and int(self.loops) == 0):
            cmd |= M2CMD_CARD_WAITREADY

        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, cmd)
        self._card_is_running = True
        if self.verbose:
            print("[Spectrum_AWG] Output started. Card is armed / running.")

    def retrigger(self):
        """
        Extra software trigger pulse. Not used for internal free-run (that
        would fight the sample clock). Kept so Dummy_AWG and this class match.
        """
        if self.verbose:
            print("[Spectrum_AWG] Software retrigger (ENABLETRIGGER).")
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_ENABLETRIGGER)

    def run_until_interrupt(self):
        """
        Keep the card running until Ctrl+C.

        We arm once and then just sleep. Re-arming from Python on every loop
        adds host jitter and can look like a 'walking' pulse on a scope.
        """
        mem_us = 1e6 * float(self.RECORD_LENGTH) / float(self.SR)
        if self.trigger_mode == "internal":
            print(
                f"[Spectrum_AWG] Internal clock running (period ~{mem_us:.3f} us). "
                "Ctrl+C to stop."
            )
        else:
            extra = ""
            if self.playback == "multi":
                extra = f"  ({self.num_segments} segments, one per Ext0)"
            print(
                f"[Spectrum_AWG] Waiting for Ext0 / Trg0 (~{mem_us:.3f} us per play)."
                f"{extra}  Ctrl+C to stop."
            )

        try:
            if not self._card_is_running:
                self.output_waveform(wait_ready=False)
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n[Spectrum_AWG] Ctrl+C — stopping.")
        finally:
            self.stop_output()

    def stop_output(self):
        """Halt replay. The handle stays open until close_card()."""
        if self.hCard is None:
            self._card_is_running = False
            return
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_STOP)
        self._card_is_running = False
        if self.verbose:
            print("[Spectrum_AWG] Output stopped.")
