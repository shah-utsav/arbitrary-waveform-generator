"""
ARCHIVED LEGACY CODE (pythonnet / 32-bit WAVES / RGBDriverKit.dll)
Active replacement: hardware/spectrometer/_nio_qmini.py
See README section "Architecture history".
The code below is preserved for reference and is NOT executed.
---

'''
hardware/spectrometer/_broadcom_qmini.py
========================================

Broadcom Qmini (AFBR-S20M2xx) spectrometer via RgbDriverKit .NET SDK.

ACQUISITION FLOW (matches SDK / WAVES manual)
-----------------------------------------------
1. SearchDevices() -> list of connected spectrometers
2. Open()
3. Set ExposureTime (seconds)
4. StartExposure()
5. Poll Status until not TakingSpectrum
6. GetData() -> intensity array; GetWavelengths() -> nm axis

Works over USB on any Windows PC with WAVES drivers installed — no Thunderbolt
required. Pair with --awg dummy on your Inspiron for spectrometer-only tests, or
use real AWG on the SLAC laptop.
'''

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from hardware.spectrometer._base import SpectrometerBase
from hardware.spectrometer._broadcom_sdk import (
    dotnet_array_to_numpy,
    ensure_rgb_driver_kit,
    find_working_spectrometers,
)


class BroadcomQminiSpectrometer(SpectrometerBase):
    '''
    Real Broadcom / RGB Photonics miniature spectrometer (Qmini series).

    Parameters
    ----------
    integration_time_ms : exposure per frame (SDK ExposureTime property, converted to s)
    device_index        : which device from SearchDevices() to open (0 = first)
    dll_dir             : folder with RgbDriverKit.dll; auto-detected if None
    verbose             : print connection status
    '''

    def __init__(
        self,
        integration_time_ms: float = 10.0,
        device_index: int = 0,
        dll_dir: str | Path | None = None,
        verbose: bool = True,
    ):
        self.integration_time_ms = integration_time_ms
        self.device_index = device_index
        self.dll_dir = Path(dll_dir) if dll_dir else None
        self.verbose = verbose

        self._device = None
        self._SpectrometerStatus = None
        self._dll_dir: Path | None = None
        self._is_open = False

        # Populated after open(); save alongside .npy spectra in the runner.
        self.wavelengths_nm: np.ndarray | None = None
        self.device_name: str | None = None

    def open(self) -> None:
        if self._is_open:
            return

        _, status_type, dll_dir = ensure_rgb_driver_kit(self.dll_dir)
        self._SpectrometerStatus = status_type
        self._dll_dir = dll_dir

        # find_working_spectrometers tries every class/overload and Opens each,
        # returning only interfaces that actually respond (skips the Qmini's
        # serial-port fallback that times out). Devices come back already open.
        devices, log = find_working_spectrometers(self.dll_dir)
        if not devices:
            detail = "\n  ".join(log)
            raise RuntimeError(
                "No Broadcom spectrometer could be opened. Run "
                "`python run_experiment.py --check-spec` for details.\n"
                f"  {detail}"
            )

        if self.device_index < 0 or self.device_index >= len(devices):
            raise IndexError(
                f"device_index={self.device_index} out of range; "
                f"opened {len(devices)} device(s)"
            )

        # Close the devices we are not going to use.
        for i, dev in enumerate(devices):
            if i != self.device_index:
                try:
                    dev.Close()
                except Exception:
                    pass

        self._device = devices[self.device_index]

        # Cache wavelength axis once — pixel→nm calibration is fixed per device.
        wl = self._device.GetWavelengths()
        self.wavelengths_nm = dotnet_array_to_numpy(wl)

        # Friendly label when the SDK exposes a serial / model string.
        self.device_name = str(getattr(self._device, "SerialNumber", "") or "") or None
        if not self.device_name:
            self.device_name = f"Broadcom#{self.device_index}"

        self._is_open = True
        if self.verbose:
            n_pix = self.wavelengths_nm.size
            wl_min = float(self.wavelengths_nm.min())
            wl_max = float(self.wavelengths_nm.max())
            print(
                f"[BroadcomQmini] Open '{self.device_name}' "
                f"({n_pix} px, {wl_min:.1f}-{wl_max:.1f} nm, "
                f"{self.integration_time_ms} ms integration, SDK: {dll_dir})"
            )

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.Close()
            except Exception:
                pass
        self._device = None
        self._is_open = False
        if self.verbose:
            print("[BroadcomQmini] Closed")

    def acquire_frame(self) -> np.ndarray:
        if not self._is_open or self._device is None:
            raise RuntimeError("[BroadcomQmini] Not open — call open() first")

        # SDK ExposureTime is in seconds (WAVES manual).
        self._device.ExposureTime = self.integration_time_ms / 1000.0
        self._device.StartExposure()

        if self._SpectrometerStatus is not None:
            taking = self._SpectrometerStatus.TakingSpectrum
            while self._device.Status == taking:
                time.sleep(0.001)
        else:
            time.sleep(max(self.integration_time_ms / 1000.0, 0.01))

        data = self._device.GetData()
        return dotnet_array_to_numpy(data)

"""
