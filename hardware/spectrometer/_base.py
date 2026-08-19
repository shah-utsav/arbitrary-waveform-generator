"""
hardware/spectrometer/_base.py
==============================

Abstract interface for spectrometer acquisition.

WHY AN INTERFACE
----------------
PI said spectrometer reads are averaged over many triggers; hardware triggering
is future work. We start with DummySpectrometer for dry runs, then plug in
Avantes / Ocean Optics drivers without changing experiment_runner logic.

DESIGN
------
- acquire_frame()      : one spectrum (1D array)
- acquire_average(n)   : mean of n frames — matches PI workflow
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class SpectrometerBase(ABC):
    """Minimal spectrometer contract for experiment runner."""

    @abstractmethod
    def open(self) -> None:
        """Connect to device (or no-op for dummy)."""

    @abstractmethod
    def close(self) -> None:
        """Disconnect / release resources."""

    @abstractmethod
    def acquire_frame(self) -> np.ndarray:
        """Return one spectrum as 1D float array (wavelength or pixel order)."""

    def acquire_average(self, n_frames: int) -> np.ndarray:
        """
        Average n_frames spectra — PI's standard measurement mode.

        LOGIC: simple arithmetic mean reduces laser/AWG trigger jitter noise
        without needing per-shot timestamp alignment.
        """
        if n_frames < 1:
            raise ValueError("n_frames must be >= 1")

        stack = np.stack([self.acquire_frame() for _ in range(n_frames)], axis=0)
        return np.mean(stack, axis=0)
