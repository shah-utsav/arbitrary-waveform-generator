"""
Software stand-in for Spectrum_AWG.

Every public method has the same name and roughly the same arguments as
Spectrum_AWG. Nothing talks to a PCIe card. Use this to:

  - learn the call order (open → setup → allocate → load → write → output)
  - debug pulse-shaping math and plots without the M4i on the desk
  - try a change here, then copy the idea into _spectrumAWG.py

Set USE_DUMMY = True at the top of main.py.
"""

import time

import numpy as np


def _align32(n):
    """Same 32-sample rounding the real M4i requires — dummy follows the rule too."""
    n = int(n)
    if n % 32 == 0:
        return n
    return n + (32 - n % 32)


class Dummy_AWG:
    def __init__(self, record_length, sampling_rate_MSa_s, voltage_max_mV, verbose=True):
        """Same constructor as Spectrum_AWG — see that class for the meaning of each number."""
        if not (32 <= record_length <= 125e6):
            print(
                f"[Dummy_AWG] Record length {record_length} is outside 32 … 125e6 samples."
            )
            raise SystemExit(1)
        self.RECORD_LENGTH = int(record_length)

        if not (0 < sampling_rate_MSa_s <= 1250):
            print(
                f"[Dummy_AWG] Sampling rate {sampling_rate_MSa_s} MSa/s is outside 0 … 1250."
            )
            raise SystemExit(1)
        self.SR = int(sampling_rate_MSa_s * 1e6)

        if not (80 <= voltage_max_mV <= 2000):
            print(
                f"[Dummy_AWG] Output range {voltage_max_mV} mV is outside 80 … 2000."
            )
            raise SystemExit(1)
        self.OUTPUT_RANGE_MV = int(voltage_max_mV)

        self.verbose = verbose
        self.buffer = None
        self.is_started = False
        self.is_card_open = False
        self.lMaxADC = 32767  # pretend 16-bit DAC (Spectrum reports 32768, we scale to 32767)
        self.lBytesPerSample = 2

        self.trigger_mode = "internal"
        self.playback = "single"
        self.loops = 0
        self.num_segments = 1
        self.segment_length = int(record_length)
        self._card_is_running = False

    def open_card(self, device_path="/dev/spcm0"):
        """Always succeeds — there is no hardware to miss."""
        self.is_card_open = True
        if self.verbose:
            print(
                f"[Dummy_AWG] Simulated card open (no hardware). "
                f"device_path={device_path} was ignored."
            )
        return True

    def close_card(self):
        self.stop_output()
        self.is_card_open = False
        self.buffer = None
        if self.verbose:
            print("[Dummy_AWG] Simulated card closed.")

    def reconfigure_for_sequence(self, total_samples):
        total_samples = _align32(total_samples)
        if total_samples < 32:
            print("[Dummy_AWG] total_samples must be at least 32.")
            raise SystemExit(1)
        self.RECORD_LENGTH = total_samples
        if self.verbose:
            print(f"[Dummy_AWG] RECORD_LENGTH is now {self.RECORD_LENGTH}.")

    def setup_card(
        self,
        trigger_mode="internal",
        playback="single",
        loops=0,
        num_segments=1,
        segment_samples=None,
        ext0_level_mV=1500,
    ):
        """Mirror Spectrum_AWG.setup_card — we only store the settings and print them."""
        if not self.is_card_open:
            print("[Dummy_AWG] Card not open. Call open_card() first.")
            raise SystemExit(1)

        if trigger_mode in ("software", "internal"):
            trigger_mode = "internal"
        if trigger_mode not in ("internal", "external"):
            print(f"[Dummy_AWG] Unknown trigger_mode '{trigger_mode}'.")
            raise SystemExit(1)
        if playback not in ("single", "multi"):
            print(f"[Dummy_AWG] Unknown playback '{playback}'.")
            raise SystemExit(1)

        self.trigger_mode = trigger_mode
        self.playback = playback
        self.loops = int(loops)

        if playback == "multi":
            if segment_samples is None:
                segment_samples = self.RECORD_LENGTH // max(int(num_segments), 1)
            self.segment_length = _align32(segment_samples)
            self.num_segments = int(num_segments)
            self.RECORD_LENGTH = self.segment_length * self.num_segments
            if trigger_mode == "internal":
                self.loops = 0
        else:
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
            if trigger_mode == "internal":
                self.loops = 0

        if self.verbose:
            mem_us = 1e6 * float(self.RECORD_LENGTH) / float(self.SR)
            print(
                f"[Dummy_AWG] Configured (simulated): SR={self.SR * 1e-6} MSa/s  "
                f"MEMSIZE={self.RECORD_LENGTH} (~{mem_us:.3f} us)  "
                f"±{self.OUTPUT_RANGE_MV / 1000} V  "
                f"trigger={trigger_mode}  playback={playback}  "
                f"loops={self.loops}  Ext0 level would be {ext0_level_mV} mV"
            )
            if playback == "multi":
                print(
                    f"[Dummy_AWG]   segments={self.num_segments} × "
                    f"{self.segment_length} samples"
                )

    def allocate_buffer(self):
        self.buffer = np.zeros(self.RECORD_LENGTH, dtype=np.int16)
        if self.verbose:
            print(f"[Dummy_AWG] Allocated dummy buffer of {self.RECORD_LENGTH} int16 samples.")

    def build_internal_period(self, burst, period_s):
        """Same RF + zeros padding as Spectrum_AWG.build_internal_period."""
        burst = np.asarray(burst, dtype=np.float64).ravel()
        burst_n = _align32(max(burst.size, 32))
        total = _align32(max(burst_n + 32, int(round(float(period_s) * self.SR))))
        period = np.zeros(total, dtype=np.float64)
        n = min(burst.size, burst_n)
        period[:n] = np.clip(burst[:n], -1.0, 1.0)
        if self.verbose:
            print(
                f"[Dummy_AWG] Internal period: {n} RF samples + {total - n} zeros "
                f"= {total} samples ({1e6 * total / self.SR:.6f} us)"
            )
        return period

    def load_waveform_in_buffer(self, voltage_array):
        voltage_array = np.asarray(voltage_array)
        if voltage_array.size != self.RECORD_LENGTH:
            print(
                f"[Dummy_AWG] Waveform length {voltage_array.size} does not match "
                f"buffer {self.RECORD_LENGTH}."
            )
            raise SystemExit(1)

        max_dac = self.lMaxADC
        if not np.issubdtype(voltage_array.dtype, np.integer):
            if np.any(voltage_array < -1) or np.any(voltage_array > 1):
                print("[Dummy_AWG] Warning: waveform clipped to [-1, 1] before scaling.")
            clipped = np.clip(voltage_array, -1, 1)
            self.buffer = (max_dac * clipped).astype(np.int16)
        else:
            self.buffer = np.clip(voltage_array, -max_dac, max_dac).astype(np.int16)
        if self.verbose:
            print("[Dummy_AWG] Loaded waveform into dummy buffer.")

    def load_waveforms_in_buffer(self, voltage_arrays):
        arrays = [np.asarray(a, dtype=np.float64).ravel() for a in voltage_arrays]
        if not arrays:
            print("[Dummy_AWG] load_waveforms_in_buffer needs at least one waveform.")
            raise SystemExit(1)
        raw_len = arrays[0].size
        for i, a in enumerate(arrays):
            if a.size != raw_len:
                print(
                    f"[Dummy_AWG] Segment {i} length {a.size} != segment 0 length {raw_len}."
                )
                raise SystemExit(1)

        seg_len = _align32(raw_len)
        expected = seg_len * len(arrays)
        if expected != self.RECORD_LENGTH:
            print(
                f"[Dummy_AWG] Concatenated length {expected} != buffer {self.RECORD_LENGTH}."
            )
            raise SystemExit(1)

        max_dac = self.lMaxADC
        self.buffer = np.zeros(self.RECORD_LENGTH, dtype=np.int16)
        for i, a in enumerate(arrays):
            padded = np.zeros(seg_len, dtype=np.float64)
            padded[: a.size] = np.clip(a, -1.0, 1.0)
            self.buffer[i * seg_len : (i + 1) * seg_len] = (max_dac * padded).astype(np.int16)
        if self.verbose:
            print(
                f"[Dummy_AWG] Loaded {len(arrays)} segments × {seg_len} samples into dummy buffer."
            )

    def write_waveform_to_card(self):
        if self.verbose:
            print("[Dummy_AWG] DMA transfer simulated (nothing left the PC).")

    def output_waveform(self, wait_ready=True):
        if self.buffer is None:
            print("[Dummy_AWG] No waveform loaded.")
            raise SystemExit(1)
        self.is_started = True
        self._card_is_running = True
        if self.verbose:
            how = "blocking" if wait_ready else "non-blocking"
            print(
                f"[Dummy_AWG] Output started (simulated, {how}). "
                "No volts appear on a BNC — this is a dry run."
            )

    def retrigger(self):
        if self.is_started:
            if self.verbose:
                print("[Dummy_AWG] Retrigger (simulated).")
        else:
            print("[Dummy_AWG] Cannot retrigger: output not started.")

    def run_until_interrupt(self):
        mem_us = 1e6 * float(self.RECORD_LENGTH) / float(self.SR)
        print(
            f"[Dummy_AWG] Simulated run (MEMSIZE ~{mem_us:.3f} us, "
            f"trigger={self.trigger_mode}, playback={self.playback}). "
            "Ctrl+C to stop. No hardware output."
        )
        try:
            if not self._card_is_running:
                self.output_waveform(wait_ready=False)
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n[Dummy_AWG] Ctrl+C — stopping.")
        finally:
            self.stop_output()

    def stop_output(self):
        self.is_started = False
        self._card_is_running = False
        if self.verbose:
            print("[Dummy_AWG] Output stopped (simulated).")
