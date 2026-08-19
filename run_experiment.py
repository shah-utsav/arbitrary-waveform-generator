#!/usr/bin/env python
"""
run_experiment.py
=================

Command-line entry point for optical pulse shaping experiments.

On each experiment run (unless --no-prompt), asks interactively for:
  1. Trigger mode — default external (DG645 → Trg0); software/internal on request
  2. Peak AWG voltage in millivolts (mV)
  3. Sampling rate in MSa/s (mega-samples per second)

Examples
--------
  python run_experiment.py --awg spectrum --spec nio_qmini
  python run_experiment.py --awg spectrum --spec nio_qmini --no-prompt
  python run_experiment.py --dry-run
  python run_experiment.py --check-awg
  python run_experiment.py --check-spec --acquire-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path when invoked as script
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiment_config import load_experiment_config
from experiment_runner import run_experiment


def _prompt_choice(prompt: str, options: dict[str, str], default: str) -> str:
    """
    Ask until the user picks a valid option key or presses Enter for default.

    options maps accepted input strings -> canonical value.
    """
    keys = "/".join(sorted(set(options.keys()), key=lambda k: (len(k), k)))
    while True:
        raw = input(f"{prompt} [{keys}] (default: {default}): ").strip().lower()
        if not raw:
            return default
        if raw in options:
            return options[raw]
        print(f"  Invalid choice '{raw}'. Enter one of: {keys}")


def _prompt_float(prompt: str, default: float, unit: str, min_val: float, max_val: float) -> float:
    while True:
        raw = input(f"{prompt} (default: {default} {unit}): ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            print(f"  Enter a number in {unit}.")
            continue
        if not (min_val <= value <= max_val):
            print(f"  Value must be between {min_val} and {max_val} {unit}.")
            continue
        return value


def _prompt_int(prompt: str, default: int, unit: str, min_val: int, max_val: int) -> int:
    while True:
        raw = input(f"{prompt} (default: {default} {unit}): ").strip()
        if not raw:
            return default
        try:
            value = int(float(raw))  # allow "2000.0"
        except ValueError:
            print(f"  Enter an integer in {unit}.")
            continue
        if not (min_val <= value <= max_val):
            print(f"  Value must be between {min_val} and {max_val} {unit}.")
            continue
        return value


def prompt_awg_settings(config_path: str | Path) -> dict:
    """
    Interactive AWG settings. Defaults come from the YAML config.

    Returns dict with trigger_mode, voltage_max_mV, sampling_rate_MSa_s.
    """
    cfg = load_experiment_config(config_path)
    awg = cfg.awg

    print("\n" + "=" * 60)
    print("AWG run settings (press Enter to keep the YAML default)")
    print("=" * 60)

    # 1) Trigger — lab default is external; ask if they want software/internal instead
    print(
        "\n1) Trigger source (default: external)\n"
        "   external         = DG645 TTL -> Spectrum Trg0/Ext0 (lab default)\n"
        "   software/internal = AWG starts when Python arms the card (no DG645)"
    )
    trigger = _prompt_choice(
        "   Choose trigger",
        {
            "external": "external",
            "ext": "external",
            "2": "external",
            "software": "software",
            "internal": "software",
            "1": "software",
        },
        default="external",
    )

    # 2) Voltage — card API uses millivolts; ±2000 mV is typical M4i limit
    print(
        "\n2) Peak AWG output voltage\n"
        "   Enter the peak amplitude in millivolts (mV).\n"
        "   Example: 2000 means +/-2.0 V into 50 Ohm (card limit is usually +/-2000 mV)."
    )
    voltage_mV = _prompt_int(
        "   Peak voltage",
        default=awg.voltage_max_mV,
        unit="mV",
        min_val=1,
        max_val=2000,
    )

    # 3) Sampling rate — config / pyspcm use MSa/s
    print(
        "\n3) Sampling rate\n"
        "   Enter the rate in MSa/s (mega-samples per second).\n"
        "   Example: 1250 means 1.25 GSa/s. Must be supported by your M4i card."
    )
    sampling = _prompt_float(
        "   Sampling rate",
        default=awg.sampling_rate_MSa_s,
        unit="MSa/s",
        min_val=1.0,
        max_val=1250.0,
    )

    print("\nUsing:")
    print(f"  trigger_mode        = {trigger}")
    print(f"  voltage_max_mV      = {voltage_mV} mV  (+/-{voltage_mV / 1000:.3f} V)")
    print(f"  sampling_rate_MSa_s = {sampling} MSa/s")
    print("=" * 60 + "\n")

    return {
        "trigger_mode": trigger,
        "voltage_max_mV": voltage_mV,
        "sampling_rate_MSa_s": sampling,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run optical pulse shaping experiment from YAML config"
    )
    parser.add_argument(
        "--config",
        default=str(_REPO_ROOT / "config" / "default_experiment.yaml"),
        help="Path to experiment YAML",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build waveforms and sequences only; do not open hardware or save data",
    )
    parser.add_argument(
        "--awg",
        choices=["dummy", "spectrum"],
        default=None,
        help="Override AWG backend from config file",
    )
    parser.add_argument(
        "--check-awg",
        action="store_true",
        help="Verify Spectrum driver DLL and scan for connected AWG cards, then exit",
    )
    parser.add_argument(
        "--spec",
        choices=["dummy", "nio_qmini"],
        default=None,
        help="Override spectrometer backend from config file",
    )
    parser.add_argument(
        "--check-spec",
        action="store_true",
        help="Verify Qmini (NioLink/PyUSB) and optional Spectrum AWG, then exit",
    )
    parser.add_argument(
        "--acquire-test",
        action="store_true",
        help="With --check-spec: open Qmini and acquire one test spectrum",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip interactive prompts; use YAML (and CLI) values as-is",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="One spectrum cycle only, then keep Ext0 RF until Ctrl+C (skip continuous collect)",
    )
    args = parser.parse_args()

    if args.check_awg:
        from hardware.AWG.spectrum_driver_check import main as check_main

        raise SystemExit(check_main())

    if args.check_spec:
        from hardware.spectrometer.nio_driver_check import main as check_spec_main

        check_argv = ["--acquire-test"] if args.acquire_test else []
        raise SystemExit(check_spec_main(check_argv))

    overrides: dict = {}
    if not args.no_prompt:
        overrides = prompt_awg_settings(args.config)

    run_experiment(
        config_path=args.config,
        dry_run=args.dry_run,
        awg_backend_override=args.awg,
        spec_backend_override=args.spec,
        trigger_mode_override=overrides.get("trigger_mode"),
        voltage_max_mV_override=overrides.get("voltage_max_mV"),
        sampling_rate_MSa_s_override=overrides.get("sampling_rate_MSa_s"),
        wait_for_exit=not args.no_wait and not args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
