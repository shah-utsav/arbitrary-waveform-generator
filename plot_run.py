#!/usr/bin/env python
"""
plot_run.py
===========

Quick viewer for a saved experiment run: plots the acquired spectra and the
played waveforms V(t) side by side, and (optionally) saves a PNG summary.

This is the fast, code-driven replacement for reading traces off instrument
screens by hand.

Usage
-----
  # Plot the most recent run under data/experiments/
  python plot_run.py

  # Plot a specific run folder
  python plot_run.py data/experiments/double_pulse_demo_20260701_172206

  # Save PNG without opening a window (headless / lab PC)
  python plot_run.py --save --no-show

Notes
-----
- matplotlib is required ONLY for this script (optional project dependency):
    pip install matplotlib
- Works even on older runs that saved spectra but no waveform files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_ROOT = _REPO_ROOT / "data" / "experiments"


def _latest_run(root: Path) -> Path | None:
    """Return the most recently modified run directory, or None."""
    if not root.is_dir():
        return None
    runs = [p for p in root.iterdir() if p.is_dir()]
    if not runs:
        return None
    return max(runs, key=lambda p: p.stat().st_mtime)


def _load_optional(path: Path) -> np.ndarray | None:
    return np.load(path) if path.is_file() else None


def _collect_blocks(run_dir: Path) -> list[dict]:
    """
    Gather (spectrum, waveform, metadata) for each block in a run.

    Pairs spectrum_*.npy with its sidecar .json (which names the waveform file).
    Falls back gracefully when waveform files are absent (older runs).
    """
    blocks: list[dict] = []
    for spec_npy in sorted(run_dir.glob("spectrum_*.npy")):
        meta_path = spec_npy.with_suffix(".json")
        meta: dict = {}
        if meta_path.is_file():
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)

        wf_id = meta.get("waveform_id", spec_npy.stem.replace("spectrum_", ""))
        waveform_file = meta.get("waveform_file", f"waveform_{wf_id}.npy")

        blocks.append(
            {
                "label": spec_npy.stem.replace("spectrum_", ""),
                "waveform_id": wf_id,
                "spectrum": np.load(spec_npy),
                "waveform": _load_optional(run_dir / waveform_file),
                "meta": meta,
            }
        )
    return blocks


def plot_run(run_dir: Path, save: bool, show: bool) -> int:
    try:
        import matplotlib

        if not show:
            matplotlib.use("Agg")  # headless backend
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting. Install it with:\n  pip install matplotlib")
        return 1

    blocks = _collect_blocks(run_dir)
    if not blocks:
        print(f"No spectrum_*.npy files found in {run_dir}")
        return 2

    wavelengths = _load_optional(run_dir / "wavelengths_nm.npy")
    time_axis_us = _load_optional(run_dir / "time_axis_us.npy")

    have_waveforms = any(b["waveform"] is not None for b in blocks)
    ncols = 2 if have_waveforms else 1
    fig, axes = plt.subplots(1, ncols, figsize=(7 * ncols, 5), squeeze=False)

    # --- Left: spectra ---
    ax_spec = axes[0][0]
    for b in blocks:
        spec = b["spectrum"]
        if wavelengths is not None and wavelengths.size == spec.size:
            ax_spec.plot(wavelengths, spec, label=b["label"])
            ax_spec.set_xlabel("Wavelength (nm)")
        else:
            ax_spec.plot(spec, label=b["label"])
            ax_spec.set_xlabel("Pixel index")
    ax_spec.set_ylabel("Intensity (a.u.)")
    ax_spec.set_title("Acquired spectra")
    ax_spec.legend()
    ax_spec.grid(True, alpha=0.3)

    # --- Right: waveforms V(t) ---
    if have_waveforms:
        ax_wf = axes[0][1]
        for b in blocks:
            vt = b["waveform"]
            if vt is None:
                continue
            if time_axis_us is not None and time_axis_us.size == vt.size:
                ax_wf.plot(time_axis_us, vt, label=b["waveform_id"])
                ax_wf.set_xlabel("Time (µs)")
            else:
                ax_wf.plot(vt, label=b["waveform_id"])
                ax_wf.set_xlabel("Sample index")
        ax_wf.set_ylabel("V(t) (normalized)")
        ax_wf.set_title("Played waveforms")
        ax_wf.legend()
        ax_wf.grid(True, alpha=0.3)

    fig.suptitle(run_dir.name)
    fig.tight_layout()

    if save:
        out = run_dir / "run_summary.png"
        fig.savefig(out, dpi=120)
        print(f"Saved plot -> {out}")

    if show:
        plt.show()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot a saved experiment run")
    parser.add_argument(
        "run_dir",
        nargs="?",
        default=None,
        help="Run folder to plot (default: most recent under data/experiments/)",
    )
    parser.add_argument("--save", action="store_true", help="Save a PNG in the run folder")
    parser.add_argument("--no-show", action="store_true", help="Do not open a window")
    args = parser.parse_args()

    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        latest = _latest_run(_DEFAULT_ROOT)
        if latest is None:
            print(f"No runs found under {_DEFAULT_ROOT}")
            return 2
        run_dir = latest
        print(f"Plotting most recent run: {run_dir}")

    if not run_dir.is_dir():
        print(f"Run folder not found: {run_dir}")
        return 2

    return plot_run(run_dir, save=args.save, show=not args.no_show)


if __name__ == "__main__":
    raise SystemExit(main())
