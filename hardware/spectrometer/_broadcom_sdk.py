"""
ARCHIVED LEGACY CODE (pythonnet / 32-bit WAVES / RGBDriverKit.dll)
Active replacement: hardware/spectrometer/_nio_qmini.py
See README section "Architecture history".
The code below is preserved for reference and is NOT executed.
---

'''
hardware/spectrometer/_broadcom_sdk.py
======================================

Shared helpers for Broadcom / RGB Photonics spectrometers (Qmini, Qwave, Qred).

WAVES installs the SDK as RGBDriverKit.dll (capital RGB) under e.g.:
  C:\\Program Files (x86)\\Broadcom\\Waves\\

Use THAT file (same version as the USB driver) — not an older standalone SDK copy.
Qmini uses the Broadcom USB driver; 0 FTDI devices on the bus is normal.

Optional: FTD2XX_NET.dll from FTDI if an older SDK build requires it:
  https://ftdichip.com/software-examples/code-examples/csharp-examples/
'''

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from typing import Any

# Repo-local fallback — only used if WAVES install is not found.
_DEFAULT_DLL_DIR = Path(__file__).resolve().parent / "broadcom_sdk"

# WAVES install roots (authoritative SDK + driver). Checked BEFORE broadcom_sdk/.
_WAVES_ROOT_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Broadcom\Waves"),
    Path(r"C:\Program Files\Broadcom\Waves"),
    Path(r"C:\Program Files (x86)\RGB Photonics\Waves"),
    Path(r"C:\Program Files\RGB Photonics\Waves"),
]

_DRIVER_KIT_GLOBS = ("RGBDriverKit.dll", "RgbDriverKit.dll", "*DriverKit.dll")
_FTD2XX_NET_NAMES = ("FTD2XX_NET.dll", "FTDI.FTD2XX_NET.dll")

# Cached after first successful load: (RgbSpectrometer type, SpectrometerStatus enum, sdk_dir)
_LOADED: tuple[Any, Any, Path] | None = None


def candidate_search_roots(user_dir: str | Path | None = None) -> list[Path]:
    '''
    Folders to search for RGBDriverKit.dll.

    WAVES install paths come first so we do not accidentally load an older copy
    from hardware/spectrometer/broadcom_sdk/.
    '''
    roots: list[Path] = []
    if user_dir:
        roots.append(Path(user_dir))
    roots.extend(_WAVES_ROOT_CANDIDATES)
    roots.append(_DEFAULT_DLL_DIR)
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        key = root.resolve() if root.exists() else root
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _find_driver_kit_in_root(root: Path) -> Path | None:
    '''Find RGBDriverKit.dll / RgbDriverKit.dll under one root.'''
    if not root.exists():
        return None
    if root.is_file() and root.name.lower().endswith("driverkit.dll"):
        return root
    for pattern in _DRIVER_KIT_GLOBS:
        try:
            for path in root.rglob(pattern):
                if path.is_file() and path.name.lower().endswith("driverkit.dll"):
                    return path
        except OSError:
            continue
    return None


def find_rgb_driver_kit_path(user_dir: str | Path | None = None) -> Path | None:
    '''Locate RGBDriverKit.dll — prefers WAVES install over repo broadcom_sdk/.'''
    for root in candidate_search_roots(user_dir):
        found = _find_driver_kit_in_root(root)
        if found is not None:
            return found
    return None


def find_ftd2xx_net_path(user_dir: str | Path | None = None) -> Path | None:
    '''Locate FTDI .NET wrapper (optional for newer Qmini + WAVES builds).'''
    for root in candidate_search_roots(user_dir):
        if not root.exists():
            continue
        for name in _FTD2XX_NET_NAMES:
            try:
                for path in root.rglob(name):
                    if path.is_file():
                        return path
            except OSError:
                continue
    return None


def find_rgb_driver_kit_dir(user_dir: str | Path | None = None) -> Path | None:
    rgb = find_rgb_driver_kit_path(user_dir)
    return rgb.parent if rgb is not None else None


def pe_machine_type(dll_path: Path) -> int | None:
    '''Return PE machine field (0x14C=x86, 0x8664=x64) or None.'''
    try:
        with dll_path.open("rb") as fh:
            fh.seek(0x3C)
            pe_offset = struct.unpack("<I", fh.read(4))[0]
            fh.seek(pe_offset + 4)
            return struct.unpack("<H", fh.read(2))[0]
    except OSError:
        return None


def dll_bitness_label(dll_path: Path) -> str:
    machine = pe_machine_type(dll_path)
    if machine == 0x14C:
        return "32-bit (x86)"
    if machine == 0x8664:
        return "64-bit (x64)"
    if machine == 0x0200:
        return "64-bit (IA64)"
    return f"unknown (PE machine=0x{machine:X})" if machine else "unknown"


def python_bitness() -> int:
    return struct.calcsize("P") * 8


def bitness_mismatch_warning(dll_path: Path) -> str | None:
    '''
    WAVES is typically 32-bit (Program Files x86). 64-bit Python + 32-bit DLL
    can import the .NET assembly but SearchDevices() often returns 0 devices.
    '''
    machine = pe_machine_type(dll_path)
    py_bits = python_bitness()
    dll_is_32 = machine == 0x14C
    dll_is_64 = machine == 0x8664
    if dll_is_32 and py_bits == 64:
        return (
            f"BITNESS MISMATCH: {dll_path.name} is 32-bit but Python is 64-bit.\n"
            "  WAVES / RGBDriverKit is usually x86. Use 32-bit Python, e.g.:\n"
            "    py -3.12-32 -m venv venv32\n"
            "    venv32\\Scripts\\activate\n"
            "    pip install pythonnet numpy PyYAML\n"
            "  Or install 64-bit WAVES if Broadcom offers it for your model."
        )
    if dll_is_64 and py_bits == 32:
        return (
            f"BITNESS MISMATCH: {dll_path.name} is 64-bit but Python is 32-bit.\n"
            "  Use 64-bit Python with this DLL."
        )
    return None


def _prepend_path(directory: str) -> None:
    if directory and directory not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def _ftdi_download_hint() -> str:
    return (
        "FTD2XX_NET.dll is optional for many Qmini builds.\n"
        "If required, download the FTDI .NET Wrapper from:\n"
        "  https://ftdichip.com/software-examples/code-examples/csharp-examples/\n"
        f"  and copy FTD2XX_NET.dll next to RGBDriverKit.dll in:\n"
        f"  {_DEFAULT_DLL_DIR}"
    )


def ensure_rgb_driver_kit(
    user_dir: str | Path | None = None,
) -> tuple[Any, Any | None, Path]:
    '''
    Load RGBDriverKit via pythonnet.

    Returns (RgbSpectrometer_class, SpectrometerStatus_enum_or_None, sdk_dir).
    '''
    global _LOADED
    if _LOADED is not None and (user_dir is None or Path(user_dir) == _LOADED[2]):
        return _LOADED

    try:
        import clr  # pythonnet
    except ImportError as exc:
        raise ImportError(
            "Broadcom spectrometer backend requires pythonnet. Install with:\n"
            "  pip install pythonnet"
        ) from exc

    rgb_path = find_rgb_driver_kit_path(user_dir)
    if rgb_path is None:
        searched = "\n  ".join(str(p) for p in candidate_search_roots(user_dir))
        raise FileNotFoundError(
            "RGBDriverKit.dll not found. Install Broadcom WAVES or set spectrometer.dll_dir to:\n"
            "  C:/Program Files (x86)/Broadcom/Waves\n"
            "Searched:\n"
            f"  {searched}"
        )

    mismatch = bitness_mismatch_warning(rgb_path)
    if mismatch:
        raise RuntimeError(mismatch)

    sdk_dir = rgb_path.parent
    ftdi_path = find_ftd2xx_net_path(user_dir)

    _prepend_path(str(sdk_dir.resolve()))
    if ftdi_path is not None:
        _prepend_path(str(ftdi_path.parent.resolve()))

    if str(sdk_dir.resolve()) not in sys.path:
        sys.path.append(str(sdk_dir.resolve()))

    # Optional FTDI wrapper — reference it first so RgbDriverKit can resolve it.
    if ftdi_path is not None:
        try:
            clr.AddReference(str(ftdi_path.resolve()))
        except Exception:
            try:
                clr.AddReference("FTD2XX_NET")
            except Exception:
                pass

    # Register the assembly with pythonnet's import system. Referencing by the
    # simple assembly name ("RgbDriverKit") is what exposes the namespace so we
    # get REAL callable classes (static methods work), unlike Assembly.GetTypes()
    # reflection which only yields RuntimeType objects.
    reference_errors: list[str] = []
    for ref in (str(rgb_path.resolve()), rgb_path.stem, "RgbDriverKit", "RGBDriverKit"):
        try:
            clr.AddReference(ref)
            break
        except Exception as exc:  # try next form
            reference_errors.append(f"{ref}: {exc}")

    try:
        from RgbDriverKit import RgbSpectrometer  # noqa: WPS433 — .NET namespace
    except Exception as exc:
        details = "\n  ".join(reference_errors) or "(none)"
        raise ImportError(
            f"Loaded {rgb_path} but could not import RgbDriverKit.RgbSpectrometer.\n"
            f"AddReference attempts:\n  {details}\n"
            f"Underlying error: {exc}"
        ) from exc

    try:
        from RgbDriverKit import SpectrometerStatus  # noqa: WPS433
    except Exception:
        SpectrometerStatus = None  # polling falls back to a fixed sleep

    _LOADED = (RgbSpectrometer, SpectrometerStatus, sdk_dir)
    return _LOADED


def sdk_status(user_dir: str | Path | None = None) -> dict[str, Any]:
    rgb = find_rgb_driver_kit_path(user_dir)
    ftdi = find_ftd2xx_net_path(user_dir)
    status: dict[str, Any] = {
        "rgb_driver_kit": str(rgb) if rgb else None,
        "rgb_driver_kit_bitness": dll_bitness_label(rgb) if rgb else None,
        "python_bitness": python_bitness(),
        "ftd2xx_net": str(ftdi) if ftdi else None,
        "sdk_dir": str(rgb.parent) if rgb else None,
        "bitness_warning": bitness_mismatch_warning(rgb) if rgb else None,
    }
    if rgb is not None:
        try:
            import clr  # noqa: F401
            from System.Reflection import Assembly

            asm = Assembly.LoadFrom(str(rgb.resolve()))
            status["assembly_name"] = asm.GetName().Name
        except Exception as exc:
            status["assembly_name"] = f"(could not read: {exc})"
    return status


def dotnet_array_to_numpy(net_array: Any):
    import numpy as np

    return np.asarray(list(net_array), dtype=np.float64)


def _normalize_device_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        return [raw]
    try:
        return list(raw)
    except TypeError:
        return [raw]


def _search_device_type_names(rgb_path: Path) -> list[str]:
    '''
    Full names of every public type exposing a STATIC SearchDevices method
    (including overloaded ones — GetMethods enumerates all, unlike GetMethod).
    '''
    from System.Reflection import Assembly

    asm = Assembly.LoadFrom(str(rgb_path.resolve()))
    names: list[str] = []
    for net_type in asm.GetTypes():
        if not (net_type.IsPublic or net_type.IsNestedPublic):
            continue
        try:
            methods = net_type.GetMethods()
        except Exception:
            continue
        if any(m.Name == "SearchDevices" and m.IsStatic for m in methods):
            names.append(net_type.FullName)
    return names


def _resolve_pythonnet_class(full_name: str) -> Any:
    '''Turn a .NET type full name into a real pythonnet class via namespace walk.'''
    parts = full_name.split(".")
    obj: Any = __import__(parts[0])
    for part in parts[1:]:
        obj = getattr(obj, part)
    return obj


def _looks_like_spectrometer(dev: Any) -> bool:
    '''Distinguish spectrometers from lasers/other RgbDriverKit devices.'''
    return any(hasattr(dev, attr) for attr in ("GetWavelengths", "GetSpectrum", "PixelCount"))


def _spectrometer_class_candidates(
    user_dir: str | Path | None,
    include_simulated: bool = False,
) -> list[tuple[str, Any]]:
    '''
    Ordered real pythonnet classes whose SearchDevices() may return a spectrometer.

    Q-series (Qmini/Qred, native USB) is tried before RgbSpectrometer (which can
    fall back to a slow serial/UART interface that times out for the Qmini).
    Lasers and (by default) simulated devices are excluded.
    '''
    import RgbDriverKit as rdk  # noqa: WPS433 — pythonnet namespace module

    out: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def _push(name: str) -> None:
        if name in seen:
            return
        cls = getattr(rdk, name, None)
        if cls is not None and hasattr(cls, "SearchDevices"):
            out.append((name, cls))
            seen.add(name)

    for name in ("Qseries", "QSeriesSpectrometer", "RgbSpectrometer", "Spectrometer", "ExtendedSpectrometer"):
        _push(name)

    rgb_path = find_rgb_driver_kit_path(user_dir)
    if rgb_path is not None:
        for full_name in _search_device_type_names(rgb_path):
            simple = full_name.rsplit(".", 1)[-1]
            if simple in seen:
                continue
            if "Laser" in simple:
                continue
            if "Simulated" in simple and not include_simulated:
                continue
            try:
                out.append((simple, _resolve_pythonnet_class(full_name)))
                seen.add(simple)
            except Exception:
                continue

    if include_simulated:
        _push("SimulatedSpectrometer")

    return out


def _search_all(cls: Any) -> list[tuple[str, Any]]:
    '''Call SearchDevices() and its bool overloads; return (overload_desc, device) list.'''
    results: list[tuple[str, Any]] = []
    for desc, args in (("()", ()), ("(False)", (False,)), ("(True)", (True,))):
        try:
            raw = cls.SearchDevices(*args)
        except Exception:
            continue
        for dev in _normalize_device_list(raw):
            if _looks_like_spectrometer(dev):
                results.append((desc, dev))
    return results


def discover_spectrometer_devices(
    user_dir: str | Path | None = None,
    include_simulated: bool = False,
) -> tuple[list[Any], list[str]]:
    '''Lightweight listing (no Open) — used for diagnostics.'''
    ensure_rgb_driver_kit(user_dir)
    log: list[str] = []
    devices: list[Any] = []
    seen_serials: set[str] = set()

    for name, cls in _spectrometer_class_candidates(user_dir, include_simulated):
        hits = _search_all(cls)
        if hits:
            log.append(f"{name}: {len(hits)} spectrometer(s) via {', '.join(d for d, _ in hits)}")
        for _desc, dev in hits:
            serial = str(getattr(dev, "SerialNumber", "") or "")
            if serial and serial in seen_serials:
                continue
            if serial:
                seen_serials.add(serial)
            devices.append(dev)

    if not devices:
        if waves_process_running() is True:
            log.append("WAVES.EXE IS RUNNING — it locks the device. Close WAVES and retry.")
        log.append("No spectrometer returned by any class.")
    return devices, log


def find_working_spectrometers(
    user_dir: str | Path | None = None,
    include_simulated: bool = False,
) -> tuple[list[Any], list[str]]:
    '''
    Enumerate candidate classes/overloads and actually Open() each device.

    Returns (opened_devices, log). Only devices that Open successfully are
    returned, and they are left OPEN and ready for GetWavelengths/GetData.
    This automatically skips interfaces that time out (e.g. serial for Qmini).
    '''
    ensure_rgb_driver_kit(user_dir)
    log: list[str] = []
    opened: list[Any] = []
    seen_serials: set[str] = set()
    tried_ids: set[int] = set()

    for name, cls in _spectrometer_class_candidates(user_dir, include_simulated):
        for desc, dev in _search_all(cls):
            if id(dev) in tried_ids:
                continue
            tried_ids.add(id(dev))
            label = f"{name}.SearchDevices{desc}"

            # Skip a unit we already opened (its serial is often readable before
            # Open) to avoid repeating a slow serial-interface timeout.
            pre_serial = str(getattr(dev, "SerialNumber", "") or "")
            if pre_serial and pre_serial in seen_serials:
                continue

            try:
                dev.Open()
            except Exception as exc:
                short = str(exc).splitlines()[0]
                log.append(f"{label}: found device but Open() failed — {short}")
                try:
                    dev.Close()
                except Exception:
                    pass
                continue

            serial = str(getattr(dev, "SerialNumber", "") or "")
            if serial and serial in seen_serials:
                try:
                    dev.Close()
                except Exception:
                    pass
                continue
            if serial:
                seen_serials.add(serial)
            opened.append(dev)
            log.append(f"{label}: OPENED '{serial or '(no serial)'}'")

    if not opened:
        if waves_process_running() is True:
            log.append("WAVES.EXE IS RUNNING — it locks the device. Close WAVES and retry.")
        log.append("No spectrometer could be opened by any class/overload.")
    return opened, log


def waves_process_running() -> bool | None:
    '''True/False if Waves.exe is running; None if it can't be determined.'''
    import platform
    import subprocess

    if platform.system() != "Windows":
        return None
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Waves.exe"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return "waves.exe" in (proc.stdout or "").lower()
    except Exception:
        return None


def windows_usb_diagnostics() -> list[str]:
    import platform
    import subprocess

    lines = [
        f"Python {platform.python_version()} ({python_bitness()}-bit)",
        "Note: Qmini uses Broadcom USB driver — 0 FTDI devices is normal, not an error.",
    ]

    if platform.system() != "Windows":
        lines.append("USB diagnostics: Windows-only (skipped)")
        return lines

    rgb = find_rgb_driver_kit_path()
    if rgb is not None:
        lines.append(f"RGBDriverKit: {rgb} ({dll_bitness_label(rgb)})")
        warn = bitness_mismatch_warning(rgb)
        if warn:
            lines.append(warn.replace("\n", "\n          "))

    ps = (
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.FriendlyName -match 'Qmini|Qred|Qwave|Broadcom|RGB Photonics|Spectrometer' } | "
        "Select-Object Status, Class, FriendlyName | Format-Table -HideTableHeaders | Out-String -Width 200"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if out:
            lines.append("Windows PnP (Qmini/Broadcom):")
            lines.extend(f"  {row}" for row in out.splitlines() if row.strip())
        else:
            lines.append("Windows PnP: no Qmini entry (unexpected if Device Manager shows Qmini)")
    except Exception as exc:
        lines.append(f"Windows PnP scan failed: {exc}")

    return lines

"""
