"""Path bootstrap for vendored pyrgbdriverkit (NioLink USB driver)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SPECTROMETER_ROOT = Path(__file__).resolve().parent
VENDOR_RGB_ROOT = _SPECTROMETER_ROOT / "vendor" / "pyrgbdriverkit-0.3.7"


def bootstrap_nio_vendor_path() -> None:
    """Ensure vendored pyrgbdriverkit is importable."""
    entry = str(VENDOR_RGB_ROOT)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def python_bitness() -> int:
    return struct.calcsize("P") * 8
