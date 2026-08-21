"""
Spectrum M4i.6631-X8 wrapper (ctypes + pyspcm).

Same call order as Optical-Pulse-Shaping:
    open_card → setup_card → allocate_buffer → load_* → write → output

One setup_card() covers internal/external and single/multi.
Dummy_AWG mirrors this API with no hardware.

Modes (sample clock is always INTPLL):
  internal + single   → SPC_REP_STD_SINGLE + LOOPS=0
                        free-run [RF | idle]; pad with build_internal_period()
  external + single   → SPC_REP_STD_SINGLERESTART  (one Ext0 = one play)
  external + multi    → SPC_REP_STD_MULTI          (Ext0 advances segments)
  internal + multi    → SPC_REP_STD_SINGLE         (segments concatenated, free-run)

MEMSIZE / SEGMENTSIZE / DMA length must be multiples of 32.
"""

import ctypes
import time

import numpy as np

from ._awg_helpers import align32, apply_burst_envelope, build_internal_period

try:
    try:
        from .spectrum_AWG_drivers.pyspcm import *
        from .spectrum_AWG_drivers.spcm_tools import *
    except ImportError:
        from hardware.AWG.spectrum_AWG_drivers.pyspcm import *
        from hardware.AWG.spectrum_AWG_drivers.spcm_tools import *
except OSError as exc:
    print(
        "\n[Spectrum_AWG] Could not load the Spectrum driver library.\n"
        "  Windows: spcm_win64.dll   Linux: libspcm_linux.so\n"
        "  Install the Spectrum driver, close SBench 6, or set USE_DUMMY = True.\n"
        f"  OS error: {exc}\n"
    )
    raise SystemExit(1)


class Spectrum_AWG:
    def __init__(self, record_length, sampling_rate_MSa_s, voltage_max_mV, verbose=True):
        if not (32 <= record_length <= 125e6):
            print(f"[Spectrum_AWG] Record length {record_length} outside 32 … 125e6.")
            raise SystemExit(1)
        if not (0 < sampling_rate_MSa_s <= 1250):
            print(f"[Spectrum_AWG] Sampling rate {sampling_rate_MSa_s} outside 0 … 1250 MSa/s.")
            raise SystemExit(1)
        if not (80 <= voltage_max_mV <= 2000):
            print(f"[Spectrum_AWG] Output range {voltage_max_mV} mV outside 80 … 2000.")
            raise SystemExit(1)

        self.RECORD_LENGTH = int(record_length)
        self.SR = int(sampling_rate_MSa_s * 1e6)  # MSa/s → Sa/s
        self.OUTPUT_RANGE_MV = int(voltage_max_mV)
        self.verbose = verbose

        self.hCard = None
        self.lMaxADC = None
        self.lBytesPerSample = None
        self.pvBuffer = None
        self.pnBuffer = None

        self.trigger_mode = "internal"
        self.playback = "single"
        self.loops = 0
        self.num_segments = 1
        self.segment_length = int(record_length)
        self._card_is_running = False

        # Optional extras (set from main before setup / load)
        self.envelope_burst = False   # soft RF edges (~200 ns)
        self.scope_lock_pulse = False  # full-scale tip before RF (free_run scope lock)
        self._scope_lock_samples = 0
        self.enable_x0_sync = False   # optional X0 period / trigger marker

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_card(self, device_path="/dev/spcm0"):
        """Open an analog-output card. Tries /dev/spcm0, then spcm1…"""
        tried = []

        def _try(path):
            tried.append(path)
            return spcm_hOpen(create_string_buffer(path.encode("ascii")))

        self.hCard = _try(device_path)
        if not self.hCard:
            for idx in range(16):
                path = f"/dev/spcm{idx}"
                if path in tried:
                    continue
                handle = _try(path)
                if not handle:
                    continue
                fnc = int32(0)
                spcm_dwGetParam_i32(handle, SPC_FNCTYPE, byref(fnc))
                if fnc.value == SPCM_TYPE_AO:
                    self.hCard = handle
                    device_path = path
                    break
                spcm_vClose(handle)

        if not self.hCard:
            print(
                "\n[Spectrum_AWG] No Spectrum AWG card detected.\n"
                f"  Tried: {', '.join(tried[:5])}{' …' if len(tried) > 5 else ''}\n"
                "  Check PCIe power / driver / close SBench 6 — or set USE_DUMMY = True.\n"
            )
            return False

        lFncType = int32(0)
        spcm_dwGetParam_i32(self.hCard, SPC_FNCTYPE, byref(lFncType))
        if lFncType.value != SPCM_TYPE_AO:
            print(f"[Spectrum_AWG] {device_path} is not an analog-output card.")
            spcm_vClose(self.hCard)
            self.hCard = None
            return False

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
        if self.hCard:
            self.stop_output()
            spcm_vClose(self.hCard)
            self.hCard = None
            self.pvBuffer = None
            self.pnBuffer = None
            if self.verbose:
                print("[Spectrum_AWG] Card closed.")

    # ------------------------------------------------------------------
    # Configure
    # ------------------------------------------------------------------

    def reconfigure_for_sequence(self, total_samples):
        """Change RECORD_LENGTH before allocate_buffer() (padded period / multi)."""
        total_samples = align32(total_samples)
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
        Program clock, memory, trigger, and Ch0 output.

        trigger_mode: "internal" (software start) | "external" (Ext0 / Trg0)
        playback:     "single" | "multi"
        loops:        0 = until stop; N = stop after N plays
        """
        if trigger_mode in ("software", "internal"):
            trigger_mode = "internal"
        if trigger_mode not in ("internal", "external"):
            print(f"[Spectrum_AWG] Unknown trigger_mode '{trigger_mode}'.")
            raise SystemExit(1)
        if playback not in ("single", "multi"):
            print(f"[Spectrum_AWG] Unknown playback '{playback}'.")
            raise SystemExit(1)

        self.trigger_mode = trigger_mode
        self.playback = playback
        self.loops = int(loops)

        # --- memory layout / Spectrum CARDMODE ---
        if playback == "multi":
            if segment_samples is None:
                segment_samples = self.RECORD_LENGTH // max(int(num_segments), 1)
            self.segment_length = align32(segment_samples)
            self.num_segments = int(num_segments)
            self.RECORD_LENGTH = align32(self.segment_length * self.num_segments)
            if trigger_mode == "external":
                card_mode = SPC_REP_STD_MULTI
            else:
                # No Ext0: concatenate segments and free-run on the sample clock.
                card_mode = SPC_REP_STD_SINGLE
                self.loops = 0
        elif trigger_mode == "external":
            card_mode = SPC_REP_STD_SINGLERESTART
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
        else:
            # Do NOT use SPC_REP_STD_CONTINUOUS here — quiet Ch0 on this bench.
            card_mode = SPC_REP_STD_SINGLE
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
            self.loops = 0

        spcm_dwSetParam_i32(self.hCard, SPC_CARDMODE, card_mode)
        spcm_dwSetParam_i32(self.hCard, SPC_CLOCKMODE, SPC_CM_INTPLL)
        spcm_dwSetParam_i64(self.hCard, SPC_CHENABLE, CHANNEL0)
        spcm_dwSetParam_i64(self.hCard, SPC_SAMPLERATE, int64(self.SR))

        seg = self.segment_length if card_mode == SPC_REP_STD_MULTI else self.RECORD_LENGTH
        spcm_dwSetParam_i64(self.hCard, SPC_SEGMENTSIZE, int64(seg))
        spcm_dwSetParam_i64(self.hCard, SPC_MEMSIZE, int64(self.RECORD_LENGTH))
        spcm_dwSetParam_i64(self.hCard, SPC_LOOPS, int64(self.loops))

        # Trigger
        spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ANDMASK, 0)
        if trigger_mode == "external":
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ORMASK, SPC_TMASK_EXT0)
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_EXT0_MODE, SPC_TM_POS)
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_EXT0_LEVEL0, int32(int(ext0_level_mV)))
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_TERM, 0)  # high-Z
        else:
            spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ORMASK, SPC_TMASK_SOFTWARE)

        spcm_dwSetParam_i32(self.hCard, SPC_AMP0, int32(self.OUTPUT_RANGE_MV))
        spcm_dwSetParam_i64(self.hCard, SPC_ENABLEOUT0, int32(1))

        actual_sr = int64(0)
        spcm_dwGetParam_i64(self.hCard, SPC_SAMPLERATE, byref(actual_sr))
        if actual_sr.value > 0:
            self.SR = int(actual_sr.value)

        if self.enable_x0_sync:
            self._enable_x0_marker()

        if self.verbose:
            names = {
                SPC_REP_STD_SINGLE: "SINGLE",
                SPC_REP_STD_SINGLERESTART: "SINGLERESTART",
                SPC_REP_STD_MULTI: "MULTI",
            }
            mem_us = 1e6 * self.RECORD_LENGTH / self.SR
            print(
                f"[Spectrum_AWG] {names.get(card_mode, card_mode)}  INTPLL  "
                f"SR={self.SR * 1e-6} MSa/s  MEMSIZE={self.RECORD_LENGTH} "
                f"(~{mem_us:.3f} us)  trigger={trigger_mode}  playback={playback}  "
                f"LOOPS={self.loops}"
            )
            if playback == "multi":
                print(
                    f"[Spectrum_AWG]   segments={self.num_segments} × "
                    f"{self.segment_length} samples"
                )

    def _enable_x0_marker(self):
        """Optional X0 TTL: period mark (internal) or trigger-out (external)."""
        try:
            mode = (
                SPCM_XMODE_CONTOUTMARK
                if self.trigger_mode == "internal"
                else SPCM_XMODE_TRIGOUT
            )
            err = spcm_dwSetParam_i32(self.hCard, SPCM_LEGACY_X0_MODE, mode)
            if err != 0:
                err = spcm_dwSetParam_i32(self.hCard, SPCM_X0_MODE, mode)
            if self.verbose:
                print(f"[Spectrum_AWG] X0 marker {'ok' if err == 0 else f'err={err}'}.")
        except Exception as exc:
            if self.verbose:
                print(f"[Spectrum_AWG] X0 marker unavailable: {exc}")

    # ------------------------------------------------------------------
    # Buffers / waveforms
    # ------------------------------------------------------------------

    def allocate_buffer(self):
        nbytes = self.RECORD_LENGTH * self.lBytesPerSample.value
        self.pvBuffer = pvAllocMemPageAligned(nbytes)
        self.pnBuffer = cast(self.pvBuffer, ptr16)
        if self.verbose:
            print(f"[Spectrum_AWG] Allocated buffer of {self.RECORD_LENGTH} samples.")

    def build_internal_period(self, burst, period_s):
        """Pad RF with idle zeros so the sample clock sets the repetition rate."""
        period, tip_n = build_internal_period(
            burst,
            self.SR,
            period_s,
            envelope=self.envelope_burst,
            scope_tip=self.scope_lock_pulse,
        )
        self._scope_lock_samples = tip_n
        if self.verbose:
            hz = self.SR / float(period.size)
            print(
                f"[Spectrum_AWG] Internal period: {period.size} samples "
                f"({1e6 * period.size / self.SR:.6f} us, {hz:.9f} Hz)"
            )
        return period

    def load_waveform_in_buffer(self, voltage_array):
        """Copy one float waveform (−1…1) into the DMA buffer as int16."""
        voltage_array = np.asarray(voltage_array, dtype=np.float64).ravel()
        if voltage_array.size != self.RECORD_LENGTH:
            print(
                f"[Spectrum_AWG] Waveform length {voltage_array.size} != "
                f"buffer {self.RECORD_LENGTH}."
            )
            raise SystemExit(1)

        if self.envelope_burst and self.trigger_mode == "external":
            voltage_array = apply_burst_envelope(voltage_array, float(self.SR))

        max_dac = self.lMaxADC.value - 1
        codes = (max_dac * np.clip(voltage_array, -1.0, 1.0)).astype(np.int16)
        ctypes.memmove(self.pnBuffer, codes.ctypes.data, codes.nbytes)
        if self.verbose:
            print("[Spectrum_AWG] Loaded waveform into buffer.")

    def load_waveforms_in_buffer(self, voltage_arrays):
        """Concatenate equal-length segments into the DMA buffer (MULTI / multi)."""
        arrays = [np.asarray(a, dtype=np.float64).ravel() for a in voltage_arrays]
        if not arrays:
            print("[Spectrum_AWG] Need at least one waveform.")
            raise SystemExit(1)
        raw_len = arrays[0].size
        for i, a in enumerate(arrays):
            if a.size != raw_len:
                print(f"[Spectrum_AWG] Segment {i} length {a.size} != {raw_len}.")
                raise SystemExit(1)

        seg_len = align32(raw_len)
        if seg_len * len(arrays) != self.RECORD_LENGTH:
            print(
                f"[Spectrum_AWG] Concatenated length {seg_len * len(arrays)} != "
                f"buffer {self.RECORD_LENGTH}. Call setup_card(playback='multi', …) first."
            )
            raise SystemExit(1)

        max_dac = self.lMaxADC.value - 1
        base = ctypes.cast(self.pnBuffer, ctypes.c_void_p).value
        for i, a in enumerate(arrays):
            if self.envelope_burst:
                a = apply_burst_envelope(a, float(self.SR))
            padded = np.zeros(seg_len, dtype=np.float64)
            padded[: a.size] = np.clip(a, -1.0, 1.0)
            codes = (max_dac * padded).astype(np.int16)
            dest = ctypes.cast(
                ctypes.c_void_p(base + i * seg_len * self.lBytesPerSample.value),
                ptr16,
            )
            ctypes.memmove(dest, codes.ctypes.data, codes.nbytes)
        if self.verbose:
            print(f"[Spectrum_AWG] Loaded {len(arrays)} × {seg_len} samples.")

    def write_waveform_to_card(self):
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
          internal → START + ENABLETRIGGER + FORCETRIGGER (once)
          external → START + ENABLETRIGGER (wait for Ext0)
        """
        if self.trigger_mode == "internal":
            cmd = M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER | M2CMD_CARD_FORCETRIGGER
        else:
            cmd = M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER

        # Free-run SINGLE+LOOPS=0 never goes READY — skip WAITREADY there.
        if wait_ready and not (self.trigger_mode == "internal" and self.loops == 0):
            cmd |= M2CMD_CARD_WAITREADY

        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, cmd)
        self._card_is_running = True
        if self.verbose:
            print("[Spectrum_AWG] Output started. Card is armed / running.")

    def retrigger(self):
        """Extra software trigger. Not used for free-run (would fight the clock)."""
        if self.verbose:
            print("[Spectrum_AWG] Software retrigger.")
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_ENABLETRIGGER)

    def run_until_interrupt(self):
        """Arm once, then sleep until Ctrl+C. Do not re-arm in a Python loop."""
        mem_us = 1e6 * self.RECORD_LENGTH / self.SR
        if self.trigger_mode == "internal":
            print(
                f"[Spectrum_AWG] Internal free-run (~{mem_us:.3f} us/period). Ctrl+C to stop."
            )
        else:
            extra = f"  ({self.num_segments} segments)" if self.playback == "multi" else ""
            print(
                f"[Spectrum_AWG] Waiting for Ext0 (~{mem_us:.3f} us/play).{extra}  Ctrl+C to stop."
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
        if self.hCard is None:
            self._card_is_running = False
            return
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_STOP)
        self._card_is_running = False
        if self.verbose:
            print("[Spectrum_AWG] Output stopped.")
