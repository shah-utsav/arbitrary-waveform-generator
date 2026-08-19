"""
hardware/spectrometer/nio_driver_check.py
=========================================

Verify NioLink / PyUSB spectrometer stack and optional Spectrum AWG.

Run from repo root:
    python hardware/spectrometer/nio_driver_check.py
    python run_experiment.py --check-spec
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hardware.spectrometer._nio_paths import VENDOR_RGB_ROOT, bootstrap_nio_vendor_path, python_bitness
from hardware.spectrometer._usb_backend import ensure_pyusb_backend, pyusb_status

bootstrap_nio_vendor_path()


def check_python_bitness() -> tuple[bool, str]:
    bits = python_bitness()
    ver = sys.version.split()[0]
    if bits == 64:
        return True, f"Python {ver} ({bits}-bit) — recommended"
    return True, f"Python {ver} ({bits}-bit) — 64-bit recommended for AWG + matplotlib"


def check_pyrgbdriverkit() -> tuple[bool, str]:
    if not VENDOR_RGB_ROOT.is_dir():
        return False, f"Vendored driver missing: {VENDOR_RGB_ROOT}"
    try:
        import rgbdriverkit
        from rgbdriverkit.qseriesdriver import Qseries  # noqa: F401

        return True, f"pyrgbdriverkit {rgbdriverkit.__version__} (Qseries import OK)"
    except Exception as exc:
        return False, f"Cannot import vendored pyrgbdriverkit: {exc}"


def check_spectrometer_devices() -> tuple[bool, str, list]:
    ensure_pyusb_backend()
    from rgbdriverkit.qseriesdriver import Qseries

    devices = Qseries.search_devices()
    if not devices:
        return False, (
            "No Qseries USB devices found (vendor 0x276E).\n"
            "  - Close WAVES / Waves.exe\n"
            "  - Confirm USB cable and device power\n"
            "  - python -m pip install libusb-package"
        ), []

    lines = [f"Found {len(devices)} device(s):"]
    summaries = []
    for idx, dev in enumerate(devices):
        label = f"{dev.manufacturer} {dev.product} s/n={dev.serial_number}"
        lines.append(f"  [{idx}] {label}")
        summaries.append({"index": idx, "label": label})
    return True, "\n".join(lines), summaries


def probe_open_spectrometer(device_index: int = 0) -> tuple[bool, str]:
    ensure_pyusb_backend()
    from rgbdriverkit.calibratedspectrometer import SpectrometerProcessing
    from rgbdriverkit.qseriesdriver import Qseries

    devices = Qseries.search_devices()
    if not devices:
        return False, "No device to open"
    if device_index >= len(devices):
        return False, f"device_index {device_index} out of range ({len(devices)} found)"

    spec = Qseries(devices[device_index])
    try:
        spec.open()
        spec.processing_steps = SpectrometerProcessing.AdjustOffset
        wl = spec.get_wavelengths()
        spec.exposure_time = 0.05
        spec.start_exposure(1)
        deadline = time.monotonic() + 10.0
        while not spec.available_spectra:
            if time.monotonic() > deadline:
                return False, "Timed out waiting for test spectrum"
            time.sleep(0.01)
        data = spec.get_spectrum_data()
        return True, (
            f"Open OK: {spec.model_name} s/n={spec.serial_number}\n"
            f"  FW {spec.software_version}, {len(wl)} px, "
            f"load={data.LoadLevel:.2f}, max={max(data.Spectrum):.1f}"
        )
    except Exception as exc:
        return False, f"Open/acquire failed: {exc}"
    finally:
        try:
            spec.close()
        except Exception:
            pass


def check_awg() -> tuple[bool, str]:
    try:
        from hardware.AWG.spectrum_driver_check import check_dll, list_cards

        ok, msg = check_dll()
        if not ok:
            return False, msg
        cards = list_cards()
        if not cards:
            return False, f"{msg}\nNo Spectrum AWG cards found at /dev/spcm*"
        ao_cards = [c for c in cards if c.get("is_analog_output")]
        lines = [msg, f"Found {len(cards)} card(s), {len(ao_cards)} analog-output:"]
        for card in ao_cards or cards:
            lines.append(
                f"  {card['device']}  max_adc={card['max_adc']}  ao={card.get('is_analog_output')}"
            )
        return True, "\n".join(lines)
    except Exception as exc:
        return False, f"AWG check failed: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectrometer check",
        description="Qmini (NioLink) + optional Spectrum AWG check",
    )
    parser.add_argument("--spec-only", action="store_true")
    parser.add_argument("--awg-only", action="store_true")
    parser.add_argument("--acquire-test", action="store_true")
    parser.add_argument("--device-index", type=int, default=0)

    if argv is None:
        # When called via `run_experiment.py --check-spec`, strip that flag.
        argv = [a for a in sys.argv[1:] if a != "--check-spec"]

    args = parser.parse_args(argv)

    run_spec = not args.awg_only
    run_awg = not args.spec_only

    print("=" * 60)
    print("Qmini spectrometer driver check (NioLink / 64-bit)")
    print("=" * 60)

    failures = 0

    ok, msg = check_python_bitness()
    print(f"\n[Python] {'OK' if ok else 'WARN'}")
    print(f"         {msg}")

    if run_spec:
        ok, msg = pyusb_status()
        print(f"\n[PyUSB] {'OK' if ok else 'FAIL'}")
        print(f"        {msg}")
        if not ok:
            failures += 1
        else:
            ok, msg = check_pyrgbdriverkit()
            print(f"\n[pyrgbdriverkit] {'OK' if ok else 'FAIL'}")
            print(f"               {msg}")
            if not ok:
                failures += 1
            else:
                ok, msg, _ = check_spectrometer_devices()
                print(f"\n[USB scan] {'OK' if ok else 'FAIL'}")
                print(f"           {msg}")
                if not ok:
                    failures += 1
                elif args.acquire_test:
                    ok, msg = probe_open_spectrometer(args.device_index)
                    print(f"\n[Acquire test] {'OK' if ok else 'FAIL'}")
                    print(f"               {msg}")
                    if not ok:
                        failures += 1

    if run_awg:
        ok, msg = check_awg()
        print(f"\n[Spectrum AWG] {'OK' if ok else 'FAIL'}")
        print(f"              {msg}")
        if not ok:
            failures += 1

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED ({failures} check group(s))")
        print("\nSet in config/default_experiment.yaml:")
        print("  spectrometer:")
        print("    backend: nio_qmini")
        print("\nRun:")
        print("  python run_experiment.py --awg spectrum --spec nio_qmini")
        return 1

    print("All requested checks passed.")
    print("\nRun:")
    print("  python run_experiment.py --awg spectrum --spec nio_qmini")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
