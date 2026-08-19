"""
ARCHIVED LEGACY CODE (pythonnet / 32-bit WAVES / RGBDriverKit.dll)
Active replacement: hardware/spectrometer/_nio_qmini.py
See README section "Architecture history".
The code below is preserved for reference and is NOT executed.
---

'''
hardware/spectrometer/broadcom_driver_check.py
==============================================

Verify Broadcom Qmini spectrometer SDK and list connected devices.

Run from repo root:
    python hardware/spectrometer/broadcom_driver_check.py
    python run_experiment.py --check-spec

Prerequisites
-------------
1. Install Broadcom WAVES (includes USB driver + RgbDriverKit.dll)
2. pip install pythonnet
3. Copy RgbDriverKit.dll (from Broadcom SDK) to hardware/spectrometer/broadcom_sdk/
4. Copy FTD2XX_NET.dll (from FTDI, NOT Broadcom) into the same folder — see:
   https://ftdichip.com/software-examples/code-examples/csharp-examples/
'''

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def check_pythonnet() -> tuple[bool, str]:
    try:
        import clr  # noqa: F401

        return True, "pythonnet (clr) import OK"
    except ImportError as exc:
        return False, f"pythonnet not installed: {exc}\n  pip install pythonnet"


def check_dll(user_dir: str | Path | None = None) -> tuple[bool, str, Path | None]:
    from hardware.spectrometer._broadcom_sdk import sdk_status

    status = sdk_status(user_dir)
    dll_dir = status["sdk_dir"]
    if dll_dir is None:
        return False, "RGBDriverKit.dll not found — install WAVES or set spectrometer.dll_dir", None

    lines = [f"RGBDriverKit.dll: {status['rgb_driver_kit']}"]
    if status.get("assembly_name"):
        lines.append(f"Assembly name:    {status['assembly_name']}")
    if status.get("rgb_driver_kit_bitness"):
        lines.append(f"DLL bitness:      {status['rgb_driver_kit_bitness']}")
    lines.append(f"Python bitness:   {status['python_bitness']}-bit")
    if status.get("bitness_warning"):
        lines.append(f"WARNING:          {status['bitness_warning']}")
    if status["ftd2xx_net"]:
        lines.append(f"FTD2XX_NET.dll:   {status['ftd2xx_net']}")
    else:
        lines.append(
            "FTD2XX_NET.dll:   not found (normal for newer Broadcom SDK — get from FTDI)"
        )
    return True, "\n          ".join(lines), Path(dll_dir)


def list_devices(user_dir: str | Path | None = None) -> tuple[list[dict], list[str]]:
    from hardware.spectrometer._broadcom_sdk import (
        dotnet_array_to_numpy,
        ensure_rgb_driver_kit,
        find_working_spectrometers,
    )

    _, _, dll_dir = ensure_rgb_driver_kit(user_dir)
    opened, discovery_log = find_working_spectrometers(user_dir)
    found: list[dict] = []
    for idx, dev in enumerate(opened):
        serial = str(getattr(dev, "SerialNumber", "") or f"device_{idx}")
        try:
            wl = dotnet_array_to_numpy(dev.GetWavelengths())
            found.append(
                {
                    "index": idx,
                    "serial": serial,
                    "n_pixels": int(wl.size),
                    "wl_min_nm": float(wl.min()),
                    "wl_max_nm": float(wl.max()),
                    "sdk_dir": str(dll_dir),
                }
            )
        except Exception as exc:
            found.append(
                {
                    "index": idx,
                    "serial": serial,
                    "error": str(exc),
                    "sdk_dir": str(dll_dir),
                }
            )
        finally:
            try:
                dev.Close()
            except Exception:
                pass
    return found, discovery_log


def main(user_dir: str | Path | None = None) -> int:
    print("=" * 60)
    print("Broadcom Qmini spectrometer driver check")
    print("=" * 60)

    ok, msg = check_pythonnet()
    print(f"\n[pythonnet] {'OK' if ok else 'FAIL'}")
    print(f"            {msg}")
    if not ok:
        return 1

    ok, msg, dll_dir = check_dll(user_dir)
    print(f"\n[SDK DLL] {'OK' if ok else 'FAIL'}")
    print(f"          {msg}")
    if not ok:
        print("\nInstall Broadcom WAVES from the product page, or copy SDK DLLs to:")
        print("  hardware/spectrometer/broadcom_sdk/")
        return 2

    print("\n[Load test] Trying to import RgbDriverKit...")
    try:
        from hardware.spectrometer._broadcom_sdk import ensure_rgb_driver_kit

        _, _, loaded_dir = ensure_rgb_driver_kit(user_dir)
        print(f"          OK — loaded from {loaded_dir}")
    except RuntimeError as exc:
        print(f"          FAIL (bitness):\n          {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"          FAIL:\n          {exc}")
        return 2
    except Exception as exc:
        print(f"          FAIL: {exc}")
        print("\nIf the error mentions FTD2XX or FTDI, download FTD2XX_NET.dll from:")
        print("  https://ftdichip.com/software-examples/code-examples/csharp-examples/")
        print("Copy it next to RgbDriverKit.dll in hardware/spectrometer/broadcom_sdk/")
        return 2

    print("\n[USB / OS] System checks...")
    try:
        from hardware.spectrometer._broadcom_sdk import windows_usb_diagnostics

        for line in windows_usb_diagnostics():
            print(f"          {line}")
    except Exception as exc:
        print(f"          skipped — {exc}")

    print("\n[SDK classes] Types exposing SearchDevices()...")
    try:
        from hardware.spectrometer._broadcom_sdk import (
            _search_device_type_names,
            find_rgb_driver_kit_path,
        )

        rgb_path = find_rgb_driver_kit_path(user_dir)
        class_names = _search_device_type_names(rgb_path) if rgb_path else []
        if class_names:
            for name in class_names:
                print(f"          {name}")
        else:
            print("          (none found — unexpected)")
    except Exception as exc:
        print(f"          skipped — {exc}")

    print("\n[WAVES] Checking if the WAVES app is holding the device...")
    try:
        from hardware.spectrometer._broadcom_sdk import waves_process_running

        running = waves_process_running()
        if running is True:
            print("          Waves.exe IS RUNNING — close it fully (also check the system tray).")
            print("          The device can only be opened by ONE program at a time.")
        elif running is False:
            print("          Waves.exe not running — good.")
        else:
            print("          Could not determine (non-Windows or tasklist unavailable).")
    except Exception as exc:
        print(f"          skipped — {exc}")

    print("\n[Devices] Searching spectrometers via RgbDriverKit...")
    try:
        devices, discovery_log = list_devices(user_dir)
        for line in discovery_log:
            print(f"          {line}")
    except Exception as exc:
        print(f"          FAIL: {exc}")
        return 3

    if not devices:
        print("          No spectrometer found.")
        print("\nTroubleshooting (most common first):")
        print("  1. Open Broadcom WAVES — does it see the Qmini?")
        print("     If WAVES also fails -> USB/driver/cable issue, not Python.")
        print("  2. Device Manager -> 'Measurement and Control' -> should show 'Qmini'.")
        print("     If missing -> try another USB port, data-capable USB-C cable, or powered hub.")
        print("  3. Close WAVES completely before running Python (it locks the device).")
        print("  4. Point spectrometer.dll_dir at the WAVES folder, e.g.:")
        print('     dll_dir: "C:/Program Files (x86)/Broadcom/Waves"')
        print("     Use RGBDriverKit.dll from there (not an old copy in broadcom_sdk/).")
        print("  5. If check shows 32-bit DLL + 64-bit Python -> install 32-bit Python (see bitness WARNING).")
        print("  6. 0 FTDI devices is normal for Qmini — it does not use the FTDI bus.")
        return 4

    for dev in devices:
        if "error" in dev:
            print(f"          [{dev['index']}] {dev['serial']}: ERROR — {dev['error']}")
        else:
            print(
                f"          [{dev['index']}] {dev['serial']}: "
                f"{dev['n_pixels']} px, "
                f"{dev['wl_min_nm']:.1f}-{dev['wl_max_nm']:.1f} nm"
            )

    print("\nSet in config/default_experiment.yaml:")
    print("  spectrometer:")
    print("    backend: broadcom_qmini")
    if dll_dir and dll_dir.name == "broadcom_sdk":
        print("    # dll_dir optional — using repo broadcom_sdk/")
    elif dll_dir:
        print(f"    dll_dir: \"{dll_dir}\"")
    print("\nRun (spectrometer only, no AWG):")
    print("  python run_experiment.py --awg dummy --spec broadcom_qmini")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
