"""
hardware/AWG/spectrum_driver_check.py
=====================================

Verify Spectrum AWG driver installation and detect connected cards.

Run from repo root:
    python hardware/AWG/spectrum_driver_check.py

WHAT THIS CHECKS
----------------
1. spcm_win64.dll (or win32) is present — user-space driver library
2. Python can load pyspcm bindings
3. Spectrum cards respond at /dev/spcm0 .. /dev/spcm15 (Spectrum device naming on Windows)

NOTE: Kernel driver must be installed separately via Spectrum's Windows installer.
      If DLL is missing, download from:
      https://spectrum-instrumentation.com/products/drivers_examples/win_driver.php
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as script from repo root or this folder
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def check_dll() -> tuple[bool, str]:
    """Return (ok, message) for Spectrum spcm DLL availability."""
    try:
        from hardware.AWG.spectrum_AWG_drivers import pyspcm

        path = getattr(pyspcm, "SPCM_DLL_PATH", "spcm_win64.dll")
        if os.path.isfile(path):
            ver = "unknown version"
            try:
                from ctypes import windll, create_string_buffer, byref, c_uint32

                buf = create_string_buffer(256)
                size = c_uint32(256)
                # Not all builds expose version export; path existence is enough
                ver = Path(path).name
            except Exception:
                pass
            return True, f"Loaded Spectrum DLL: {path}"
        return False, f"DLL path recorded but file missing: {path}"
    except OSError as exc:
        return False, (
            f"Could not load Spectrum DLL: {exc}\n"
            "Install the Spectrum Windows driver from:\n"
            "  https://spectrum-instrumentation.com/products/drivers_examples/win_driver.php"
        )
    except Exception as exc:
        return False, f"Unexpected error loading pyspcm: {exc}"


def list_cards(max_cards: int = 16) -> list[dict]:
    """
    Try opening /dev/spcm0 .. /dev/spcm{N-1}.

    Spectrum uses Unix-style paths even on Windows. First responsive index is
    usually spcm0 when one card is installed.
    """
    from ctypes import byref, create_string_buffer

    from hardware.AWG.spectrum_AWG_drivers.pyspcm import (
        SPCM_TYPE_AO,
        SPC_FNCTYPE,
        SPC_MIINST_MAXADCVALUE,
        int32,
        spcm_dwGetParam_i32,
        spcm_hOpen,
        spcm_vClose,
    )

    found: list[dict] = []
    for idx in range(max_cards):
        dev = f"/dev/spcm{idx}"
        handle = spcm_hOpen(create_string_buffer(dev.encode("ascii")))
        if not handle:
            continue

        fnc_type = int32(0)
        max_adc = int32(0)
        spcm_dwGetParam_i32(handle, SPC_FNCTYPE, byref(fnc_type))
        spcm_dwGetParam_i32(handle, SPC_MIINST_MAXADCVALUE, byref(max_adc))

        is_ao = fnc_type.value == SPCM_TYPE_AO
        found.append(
            {
                "device": dev,
                "function_type": fnc_type.value,
                "is_analog_output": is_ao,
                "max_adc": max_adc.value,
            }
        )
        spcm_vClose(handle)

    return found


def main() -> int:
    print("=" * 60)
    print("Spectrum AWG driver check")
    print("=" * 60)

    ok, msg = check_dll()
    print(f"\n[DLL] {'OK' if ok else 'FAIL'}")
    print(f"      {msg}")

    if not ok:
        print("\nInstall the Spectrum Windows driver, reboot if prompted, then re-run this script.")
        return 1

    print("\n[Cards] Scanning /dev/spcm0 .. /dev/spcm15 ...")
    cards = list_cards()
    if not cards:
        print("      No cards found.")
        print("\nPossible causes:")
        print("  - AWG card not installed in PCIe slot / not powered")
        print("  - Spectrum kernel driver not bound to the device (Device Manager)")
        print("  - Another program (SBench 6) has the card open - close it first")
        print("\nDriver DLL is OK — software should work once the card is connected.")
        return 2

    ao_cards = [c for c in cards if c["is_analog_output"]]
    print(f"      Found {len(cards)} device(s), {len(ao_cards)} analog output (AWG):")
    for c in cards:
        role = "AWG (AO)" if c["is_analog_output"] else f"type={c['function_type']}"
        print(f"        {c['device']}: {role}, max_adc={c['max_adc']}")

    if not ao_cards:
        print("\nWARN: Cards detected but none are analog output (AO).")
        print("      This project expects an AWG such as M4i.6631-x8.")
        return 3

    print(f"\nUse device '{ao_cards[0]['device']}' in config (awg.device) or leave default.")
    print("Run experiment: python run_experiment.py --awg spectrum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())