"""
hardware/spectrometer/_nio_qmini.py
===================================

Broadcom / RGB Photonics Qmini via pyrgbdriverkit (NioLink USB protocol).

Uses PyUSB + pure Python driver — no pythonnet, no 32-bit RGBDriverKit.dll.
Close WAVES / Waves.exe before opening the device (exclusive USB access).
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from hardware.spectrometer._base import SpectrometerBase
from hardware.spectrometer._nio_paths import bootstrap_nio_vendor_path
from hardware.spectrometer._usb_backend import ensure_pyusb_backend

bootstrap_nio_vendor_path()


class NioQminiSpectrometer(SpectrometerBase):
    """
    Qseries spectrometer through vendored pyrgbdriverkit (v0.3.7).

    Parameters
    ----------
    integration_time_ms : exposure per frame (seconds internally)
    device_index        : index into Qseries.search_devices() list
    serial_number       : if set, search only this device
    verbose             : print connection status
    """

    def __init__(
        self,
        integration_time_ms: float = 10.0,
        device_index: int = 0,
        serial_number: str | None = None,
        verbose: bool = True,
    ):
        self.integration_time_ms = integration_time_ms
        self.device_index = device_index
        self.serial_number = serial_number
        self.verbose = verbose

        self._spec: Any = None
        self._is_open = False
        self.wavelengths_nm: np.ndarray | None = None
        self.device_name: str | None = None

    def open(self) -> None:
        if self._is_open:
            return

        ensure_pyusb_backend()

        from rgbdriverkit.calibratedspectrometer import SpectrometerProcessing
        from rgbdriverkit.qseriesdriver import Qseries

        devices = Qseries.search_devices(self.serial_number)
        if not devices:
            raise RuntimeError(
                "No Qseries spectrometer found on USB. "
                "Close WAVES / Waves.exe if running. "
                "Run: python run_experiment.py --check-spec"
            )

        if self.device_index < 0 or self.device_index >= len(devices):
            raise IndexError(
                f"device_index={self.device_index} out of range; "
                f"found {len(devices)} device(s)"
            )

        self._spec = Qseries(devices[self.device_index])
        self._spec.open()
        self._spec.processing_steps = SpectrometerProcessing.AdjustOffset

        wl = self._spec.get_wavelengths()
        self.wavelengths_nm = np.asarray(wl, dtype=np.float64)
        self.device_name = (
            f"{self._spec.model_name} (s/n: {self._spec.serial_number})"
            if self._spec.serial_number
            else str(self._spec.model_name)
        )
        self._is_open = True

        if self.verbose:
            print(
                f"[NioQmini] Open '{self.device_name}' "
                f"({self.wavelengths_nm.size} px, "
                f"{float(self.wavelengths_nm.min()):.1f}-"
                f"{float(self.wavelengths_nm.max()):.1f} nm, "
                f"FW {self._spec.software_version}, "
                f"{self.integration_time_ms} ms integration)"
            )

    def close(self) -> None:
        if self._spec is not None:
            try:
                self._spec.close()
            except Exception:
                pass
        self._spec = None
        self._is_open = False
        if self.verbose:
            print("[NioQmini] Closed")

    def acquire_frame(self) -> np.ndarray:
        if not self._is_open or self._spec is None:
            raise RuntimeError("[NioQmini] Not open — call open() first")

        self._spec.exposure_time = self.integration_time_ms / 1000.0
        self._spec.start_exposure(1)

        deadline = time.monotonic() + max(self.integration_time_ms / 1000.0 * 10, 5.0)
        while not self._spec.available_spectra:
            if time.monotonic() > deadline:
                raise TimeoutError("[NioQmini] Timed out waiting for spectrum data")
            time.sleep(0.005)

        data = self._spec.get_spectrum_data()
        return np.asarray(data.Spectrum, dtype=np.float64)
