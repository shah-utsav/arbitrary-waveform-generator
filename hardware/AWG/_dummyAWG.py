"""
Software stand-in for Spectrum_AWG (same method names, no hardware).

Use USE_DUMMY = True in main.py to plot / learn the call order without the M4i.
"""

import time

import numpy as np

from ._awg_helpers import align32, apply_burst_envelope, build_internal_period


class Dummy_AWG:
    def __init__(self, record_length, sampling_rate_MSa_s, voltage_max_mV, verbose=True):
        if not (32 <= record_length <= 125e6):
            print(f"[Dummy_AWG] Record length {record_length} outside 32 … 125e6.")
            raise SystemExit(1)
        if not (0 < sampling_rate_MSa_s <= 1250):
            print(f"[Dummy_AWG] Sampling rate {sampling_rate_MSa_s} outside 0 … 1250.")
            raise SystemExit(1)
        if not (80 <= voltage_max_mV <= 2000):
            print(f"[Dummy_AWG] Output range {voltage_max_mV} mV outside 80 … 2000.")
            raise SystemExit(1)

        self.RECORD_LENGTH = int(record_length)
        self.SR = int(sampling_rate_MSa_s * 1e6)
        self.OUTPUT_RANGE_MV = int(voltage_max_mV)
        self.verbose = verbose

        self.buffer = None
        self.is_started = False
        self.is_card_open = False
        self.lMaxADC = 32767
        self.lBytesPerSample = 2

        self.trigger_mode = "internal"
        self.playback = "single"
        self.loops = 0
        self.num_segments = 1
        self.segment_length = int(record_length)
        self._card_is_running = False

        self.envelope_burst = False
        self.scope_lock_pulse = False
        self._scope_lock_samples = 0
        self.enable_x0_sync = False

    def open_card(self, device_path="/dev/spcm0"):
        self.is_card_open = True
        if self.verbose:
            print(f"[Dummy_AWG] Simulated open (ignored path={device_path}).")
        return True

    def close_card(self):
        self.stop_output()
        self.is_card_open = False
        self.buffer = None
        if self.verbose:
            print("[Dummy_AWG] Simulated card closed.")

    def reconfigure_for_sequence(self, total_samples):
        total_samples = align32(total_samples)
        if total_samples < 32:
            print("[Dummy_AWG] total_samples must be at least 32.")
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
        if not self.is_card_open:
            print("[Dummy_AWG] Call open_card() first.")
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
        _ = ext0_level_mV  # accepted for API parity with Spectrum_AWG

        if playback == "multi":
            if segment_samples is None:
                segment_samples = self.RECORD_LENGTH // max(int(num_segments), 1)
            self.segment_length = align32(segment_samples)
            self.num_segments = int(num_segments)
            self.RECORD_LENGTH = align32(self.segment_length * self.num_segments)
            if trigger_mode == "internal":
                self.loops = 0
        else:
            self.num_segments = 1
            self.segment_length = int(self.RECORD_LENGTH)
            if trigger_mode == "internal":
                self.loops = 0

        if self.verbose:
            mem_us = 1e6 * self.RECORD_LENGTH / self.SR
            print(
                f"[Dummy_AWG] Configured: SR={self.SR * 1e-6} MSa/s  "
                f"MEMSIZE={self.RECORD_LENGTH} (~{mem_us:.3f} us)  "
                f"trigger={trigger_mode}  playback={playback}  loops={self.loops}"
            )
            if playback == "multi":
                print(
                    f"[Dummy_AWG]   segments={self.num_segments} × "
                    f"{self.segment_length} samples"
                )

    def allocate_buffer(self):
        self.buffer = np.zeros(self.RECORD_LENGTH, dtype=np.int16)
        if self.verbose:
            print(f"[Dummy_AWG] Allocated dummy buffer of {self.RECORD_LENGTH} samples.")

    def build_internal_period(self, burst, period_s):
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
                f"[Dummy_AWG] Internal period: {period.size} samples "
                f"({1e6 * period.size / self.SR:.6f} us, {hz:.9f} Hz)"
            )
        return period

    def load_waveform_in_buffer(self, voltage_array):
        voltage_array = np.asarray(voltage_array, dtype=np.float64).ravel()
        if voltage_array.size != self.RECORD_LENGTH:
            print(
                f"[Dummy_AWG] Waveform length {voltage_array.size} != "
                f"buffer {self.RECORD_LENGTH}."
            )
            raise SystemExit(1)

        if self.envelope_burst and self.trigger_mode == "external":
            voltage_array = apply_burst_envelope(voltage_array, float(self.SR))

        self.buffer = (self.lMaxADC * np.clip(voltage_array, -1, 1)).astype(np.int16)
        if self.verbose:
            print("[Dummy_AWG] Loaded waveform into dummy buffer.")

    def load_waveforms_in_buffer(self, voltage_arrays):
        arrays = [np.asarray(a, dtype=np.float64).ravel() for a in voltage_arrays]
        if not arrays:
            print("[Dummy_AWG] Need at least one waveform.")
            raise SystemExit(1)
        raw_len = arrays[0].size
        for i, a in enumerate(arrays):
            if a.size != raw_len:
                print(f"[Dummy_AWG] Segment {i} length {a.size} != {raw_len}.")
                raise SystemExit(1)

        seg_len = align32(raw_len)
        if seg_len * len(arrays) != self.RECORD_LENGTH:
            print(
                f"[Dummy_AWG] Concatenated length {seg_len * len(arrays)} != "
                f"buffer {self.RECORD_LENGTH}."
            )
            raise SystemExit(1)

        self.buffer = np.zeros(self.RECORD_LENGTH, dtype=np.int16)
        for i, a in enumerate(arrays):
            if self.envelope_burst:
                a = apply_burst_envelope(a, float(self.SR))
            padded = np.zeros(seg_len, dtype=np.float64)
            padded[: a.size] = np.clip(a, -1.0, 1.0)
            self.buffer[i * seg_len : (i + 1) * seg_len] = (
                self.lMaxADC * padded
            ).astype(np.int16)
        if self.verbose:
            print(f"[Dummy_AWG] Loaded {len(arrays)} × {seg_len} samples.")

    def write_waveform_to_card(self):
        if self.verbose:
            print("[Dummy_AWG] DMA simulated (nothing left the PC).")

    def output_waveform(self, wait_ready=True):
        if self.buffer is None:
            print("[Dummy_AWG] No waveform loaded.")
            raise SystemExit(1)
        self.is_started = True
        self._card_is_running = True
        if self.verbose:
            how = "blocking" if wait_ready else "non-blocking"
            print(f"[Dummy_AWG] Output started (simulated, {how}).")

    def retrigger(self):
        if self.is_started and self.verbose:
            print("[Dummy_AWG] Retrigger (simulated).")
        elif not self.is_started:
            print("[Dummy_AWG] Cannot retrigger: output not started.")

    def run_until_interrupt(self):
        mem_us = 1e6 * self.RECORD_LENGTH / self.SR
        print(
            f"[Dummy_AWG] Simulated run (~{mem_us:.3f} us, "
            f"trigger={self.trigger_mode}, playback={self.playback}). Ctrl+C to stop."
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
