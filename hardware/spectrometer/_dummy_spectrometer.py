"""
hardware/spectrometer/_dummy_spectrometer.py
============================================

Simulated spectrometer for dry runs without lab hardware.

BEHAVIOR
--------
Returns synthetic spectra shaped like a Gaussian bump. When the runner passes
an optional waveform_id label, the peak center shifts slightly — so you can
verify in logs that per_block averaging associates data with the right waveform.
"""

from __future__ import annotations

import time

import numpy as np

from hardware.spectrometer._base import SpectrometerBase


class DummySpectrometer(SpectrometerBase):
    """Fake spectrometer — no VISA, no Avantes DLL."""

    def __init__(
        self,
        n_pixels: int = 2048,
        integration_time_ms: float = 10.0,
        verbose: bool = True,
    ):
        self.n_pixels = n_pixels
        self.integration_time_ms = integration_time_ms
        self.verbose = verbose
        self._is_open = False
        # Runner can set this before acquire to simulate different spectra per wf
        self.active_waveform_id: str | None = None

    def open(self) -> None:
        self._is_open = True
        if self.verbose:
            print(
                f"[DummySpectrometer] Open ({self.n_pixels} pixels, "
                f"{self.integration_time_ms} ms integration)"
            )

    def close(self) -> None:
        self._is_open = False
        if self.verbose:
            print("[DummySpectrometer] Closed")

    def acquire_frame(self) -> np.ndarray:
        if not self._is_open:
            raise RuntimeError("[DummySpectrometer] Not open — call open() first")

        # Simulate readout delay so timing feels realistic in dry runs
        time.sleep(self.integration_time_ms / 1000.0)

        x = np.arange(self.n_pixels, dtype=np.float64)

        # Base peak position; shift by hash of waveform id for distinguishable blocks
        center = self.n_pixels * 0.5
        if self.active_waveform_id:
            # Simple deterministic offset — not physical, only for testing association
            center += (hash(self.active_waveform_id) % 200) - 100

        width = self.n_pixels * 0.08
        spectrum = np.exp(-0.5 * ((x - center) / width) ** 2)

        # Tiny noise mimics trigger jitter averaging down over many frames
        spectrum += np.random.normal(0, 0.002, size=spectrum.shape)
        return spectrum
