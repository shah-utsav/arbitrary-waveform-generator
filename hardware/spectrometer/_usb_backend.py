"""
hardware/spectrometer/_usb_backend.py
=====================================

PyUSB on Windows does not ship libusb-1.0.dll. The ``libusb-package`` pip
wheel bundles the DLL; this module registers it before pyrgbdriverkit calls
usb.core.find().
"""

from __future__ import annotations

from typing import Any


def ensure_pyusb_backend() -> tuple[Any, str | None]:
    """
    Return a working libusb1 backend and patch usb.core.find to use it by default.

    Raises RuntimeError with install instructions if no backend is available.
    """
    import usb.backend.libusb1 as libusb1
    import usb.core

    backend = libusb1.get_backend()
    lib_path: str | None = None

    if backend is None:
        try:
            import libusb_package

            backend = libusb1.get_backend(find_library=libusb_package.find_library)
            lib_path = libusb_package.get_library_path()
        except ImportError:
            pass

    if backend is None:
        raise RuntimeError(
            "PyUSB has no libusb backend (NoBackendError).\n"
            "  python -m pip install libusb-package\n"
            "  python -c \"import libusb_package; print(libusb_package.get_library_path())\""
        )

    if not getattr(usb.core, "_ops_patched", False):
        _original_find = usb.core.find

        def find_with_backend(*args: Any, **kwargs: Any):
            if kwargs.get("backend") is None:
                kwargs["backend"] = backend
            return _original_find(*args, **kwargs)

        usb.core.find = find_with_backend  # type: ignore[assignment]
        usb.core._ops_patched = True  # type: ignore[attr-defined]

    return backend, lib_path


def pyusb_status() -> tuple[bool, str]:
    """Return (ok, message) for hardware check CLI."""
    try:
        backend, lib_path = ensure_pyusb_backend()
        name = getattr(backend, "name", "libusb1")
        if lib_path:
            return True, f"pyusb OK (backend: {name}, dll: {lib_path})"
        return True, f"pyusb OK (backend: {name})"
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"pyusb backend error: {exc}"
