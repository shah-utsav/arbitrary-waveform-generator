"""
experiment_runner.py
====================

Orchestrates a full optical pulse shaping experiment from YAML config.

EXECUTION FLOW (high level)
---------------------------
1. Load & validate config                    (experiment_config.py)
2. Pre-compute waveform library V(t)         (waveform_builder.py)  — page 4 "New target"
3. Build pulse sequence(s)                   (pulse_sequence.py)
4. Open AWG + spectrometer
5. Play sequence(s) + acquire averaged spectra
6. Save results to output_dir

PLAYBACK MODES (see config awg.playback_mode)
---------------------------------------------
per_block     : For each schedule row (e.g. 10× wf1), play block → average → save.
                Best match for PI answer #5 (spectrometer averaging, no per-shot trigger).

concatenated  : One long buffer with full pattern wf1×10 + wf2×10 + ...
                Fewer AWG reloads; one averaged spectrum for whole run unless extended later.

DRY RUN
-------
If dry_run=True, steps 1–3 run and summaries print; no hardware, no files unless save_dry_run_meta.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from experiment_config import ExperimentConfig, load_experiment_config
from pulse_sequence import (
    PulseSequence,
    ScheduleBlock,
    build_full_concatenated_sequence,
    build_per_block_sequences,
    summarize_sequence,
)
from waveform_builder import build_waveform_library


@dataclass
class BlockResult:
    """One averaged spectrum + metadata for a schedule block or full run."""

    waveform_id: str
    schedule_index: int
    repeats: int
    averaged_spectrum: np.ndarray
    frames_averaged: int
    sequence: PulseSequence


def _create_awg(config: ExperimentConfig):
    """
    Factory: return Dummy_AWG or Spectrum_AWG from config.

    Initial RECORD_LENGTH uses one segment RL — may grow via reconfigure_for_sequence.
    """
    awg_cfg = config.awg
    rl = awg_cfg.record_length_samples()
    sr = awg_cfg.sampling_rate_MSa_s
    vmv = awg_cfg.voltage_max_mV

    if awg_cfg.backend == "dummy":
        from hardware.AWG._dummyAWG import Dummy_AWG

        return Dummy_AWG(rl, sr, vmv)

    if awg_cfg.backend == "spectrum":
        from hardware.AWG._spectrumAWG import Spectrum_AWG

        awg = Spectrum_AWG(rl, sr, vmv)
        # Optional device override from config (e.g. /dev/spcm1 if two cards)
        device = getattr(awg_cfg, "device", "/dev/spcm0")
        awg.device_path = device
        return awg

    raise ValueError(f"Unknown awg.backend: {awg_cfg.backend}")


def _create_spectrometer(config: ExperimentConfig):
    """Factory for spectrometer backend."""
    spec_cfg = config.spectrometer
    if spec_cfg.backend == "dummy":
        from hardware.spectrometer._dummy_spectrometer import DummySpectrometer

        return DummySpectrometer(integration_time_ms=spec_cfg.integration_time_ms)

    if spec_cfg.backend == "nio_qmini":
        from hardware.spectrometer._nio_qmini import NioQminiSpectrometer

        return NioQminiSpectrometer(
            integration_time_ms=spec_cfg.integration_time_ms,
            device_index=spec_cfg.device_index,
            serial_number=spec_cfg.serial_number,
        )

    raise ValueError(f"Unknown spectrometer.backend: {spec_cfg.backend}")


def _ensure_output_dir(config: ExperimentConfig) -> Path:
    """Create timestamped run directory under config.output_dir."""
    root = Path(config.output_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = root / f"{config.name}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _save_run_metadata(config: ExperimentConfig, run_dir: Path, extra: dict[str, Any]) -> None:
    """Write config snapshot + extra keys as JSON for reproducibility."""
    payload = {
        "python_bitness": __import__("struct").calcsize("P") * 8,
        "experiment_name": config.name,
        "spectrometer_backend": config.spectrometer.backend,
        "awg": asdict(config.awg),
        "calibration": asdict(config.calibration),
        "waveforms": [asdict(w) for w in config.waveforms],
        "schedule": [asdict(s) for s in config.schedule],
        "spectrometer": asdict(config.spectrometer),
        **extra,
    }
    meta_path = run_dir / "run_metadata.json"
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def _save_waveform_library(
    run_dir: Path, library: dict[str, np.ndarray], config: ExperimentConfig
) -> None:
    """
    Save every computed waveform V(t) once, plus a shared time axis.

    This is the fast software replacement for reading V(t) off an oscilloscope by
    hand: np.save writes each array in well under a millisecond. Files:
      waveform_<id>.npy   normalized V(t) for one segment (one period of the mask)
      time_axis_us.npy    time base in microseconds (same length as a segment)
    """
    sr = config.awg.sampling_rate_MSa_s  # samples per microsecond
    for wf_id, vt in library.items():
        np.save(run_dir / f"waveform_{wf_id}.npy", vt)
    # Time axis matches one segment length (all waveforms share segment length).
    if library:
        n = next(iter(library.values())).size
        t_us = np.arange(n, dtype=np.float64) / sr
        np.save(run_dir / "time_axis_us.npy", t_us)


def _save_block_result(
    run_dir: Path,
    result: BlockResult,
    tag: str,
    *,
    cycle: int | None = None,
    save_played: bool = True,
    played: np.ndarray | None = None,
) -> None:
    """Save averaged spectrum + optional played buffer + sidecar JSON metadata."""
    base = run_dir / f"spectrum_{tag}_{result.waveform_id}"
    np.save(base.with_suffix(".npy"), result.averaged_spectrum)
    played_name = f"played_{tag}_{result.waveform_id}.npy"
    if save_played:
        played_arr = result.sequence.samples if played is None else played
        np.save(run_dir / played_name, played_arr)
    meta = {
        "waveform_id": result.waveform_id,
        "schedule_index": result.schedule_index,
        "repeats": result.repeats,
        "frames_averaged": result.frames_averaged,
        "total_samples": result.sequence.total_samples,
        "total_shots": result.sequence.total_shots,
        "segment_length": result.sequence.segment_length,
        "cycle": cycle,
        "spectrum_file": base.with_suffix(".npy").name,
        "waveform_file": f"waveform_{result.waveform_id}.npy",
        "played_buffer_file": played_name if save_played else None,
    }
    with base.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _effective_replay_loops(config: ExperimentConfig, schedule_repeats: int | None = None) -> int:
    """
    SPC_LOOPS for SINGLERESTART (Spectrum M4i Table 57 / Matthias Wilma).

    Always 0 unless awg.acquire_loops is an explicit positive override:
      0 → one MEMSIZE per trigger, indefinitely (correct pulsed mode)
      N → one MEMSIZE per trigger until N total plays, then card stops

    schedule.repeats is NOT written to SPC_LOOPS (that was the old wrong mapping).
    Longer RF per trigger = tile the segment into MEMSIZE (see _memsize_for_trigger).
    """
    del schedule_repeats  # kept in signature for call-site compatibility
    if config.awg.acquire_loops > 0:
        return int(config.awg.acquire_loops)
    return 0


def _memsize_for_trigger(segment: np.ndarray, schedule_repeats: int) -> np.ndarray:
    """
    With SINGLERESTART + SPC_LOOPS=0, one trigger plays MEMSIZE once.

    schedule.repeats used to be (wrongly) mapped to SPC_LOOPS. Preserve the
    intended longer burst by tiling the library segment into MEMSIZE instead.
    """
    segment = np.asarray(segment, dtype=np.float64)
    n = max(1, int(schedule_repeats))
    if n == 1:
        return segment
    return np.tile(segment, n)


def _play_then_acquire(
    awg,
    spectrometer,
    samples: np.ndarray,
    config: ExperimentConfig,
    *,
    loops: int,
    stop_after: bool = True,
) -> np.ndarray:
    """
    Arm AWG, wait settle_delay_ms, then average spectra.

    Both trigger modes use SINGLERESTART + SPC_LOOPS (typically 0).
    external: Ext0 edges; software: FORCETRIGGER inside output_waveform / hold.
    """
    settle_ms = float(config.spectrometer.settle_delay_ms)
    n_frames = config.spectrometer.frames_to_average
    loops = int(loops)
    trigger = config.awg.trigger_mode

    print(
        f"[acquire] Arming AWG: MEMSIZE={samples.size} samples, "
        f"SPC_LOOPS={loops}, trigger={trigger}"
    )

    awg.play_voltage_array(
        samples,
        loops=loops,
        auto_start=True,
        wait_ready=False,
    )

    try:
        if settle_ms > 0:
            print(f"[acquire] Settling {settle_ms:g} ms after AWG arm...")
            time.sleep(settle_ms / 1000.0)

        print(f"[acquire] Averaging {n_frames} spectrometer frames...")
        avg = spectrometer.acquire_average(n_frames)
    finally:
        if stop_after:
            awg.stop_output()

    return avg


def run_per_block_mode(
    config: ExperimentConfig,
    library: dict[str, np.ndarray],
    awg,
    spectrometer,
    run_dir: Path | None,
    dry_run: bool,
    *,
    cycle: int = 0,
) -> list[BlockResult]:
    """
    One schedule pass — same AWG model as main.py, plus Qmini averaging.

      MEMSIZE = library[wf_id]          (one segment, e.g. 10 µs)
      SPC_LOOPS = schedule.repeats      (or awg.acquire_loops if > 0)
      Arm non-blocking → settle → average → stop (reload next wf)

    Caller may repeat until Ctrl+C for continuous spectrum collection.
    """
    blocks = build_per_block_sequences(config, library)
    results: list[BlockResult] = []

    for block in blocks:
        seq = block.sequence
        print(summarize_sequence(seq, label=f"c{cycle:04d} block {block.waveform_id}"))

        if dry_run:
            continue

        if hasattr(spectrometer, "active_waveform_id"):
            spectrometer.active_waveform_id = block.waveform_id

        segment = library[block.waveform_id]
        played = _memsize_for_trigger(segment, block.repeats)
        loops = _effective_replay_loops(config, block.repeats)
        print(
            f"[acquire] Block {block.waveform_id}: MEMSIZE={played.size} samples "
            f"(segment×{block.repeats}), SPC_LOOPS={loops}"
        )

        n_frames = config.spectrometer.frames_to_average
        # Stop after every block so the next waveform can be DMA'd (main.py has one wf).
        avg = _play_then_acquire(
            awg,
            spectrometer,
            played,
            config,
            loops=loops,
            stop_after=True,
        )

        result = BlockResult(
            waveform_id=block.waveform_id,
            schedule_index=block.schedule_index,
            repeats=block.repeats,
            averaged_spectrum=avg,
            frames_averaged=n_frames,
            sequence=seq,
        )
        results.append(result)

        if run_dir is not None:
            tag = f"c{cycle:04d}_block{block.schedule_index}"
            _save_block_result(
                run_dir,
                result,
                tag=tag,
                cycle=cycle,
                save_played=(cycle == 0),
                played=played,
            )

    return results


def run_concatenated_mode(
    config: ExperimentConfig,
    library: dict[str, np.ndarray],
    awg,
    spectrometer,
    run_dir: Path | None,
    dry_run: bool,
    *,
    cycle: int = 0,
) -> list[BlockResult]:
    """
    Full concatenated buffer as MEMSIZE; each Trg0 plays it replay_loops times.

    Prefer per_block to match main.py (one segment × loops per edge).
    """
    seq = build_full_concatenated_sequence(config, library)
    print(summarize_sequence(seq, label=f"c{cycle:04d} full concatenated"))

    if dry_run:
        return []

    loops = _effective_replay_loops(config, schedule_repeats=None)
    avg = _play_then_acquire(
        awg,
        spectrometer,
        seq.samples,
        config,
        loops=loops,
        stop_after=True,
    )

    wf_ids = [e.waveform_id for e in config.schedule]
    composite_id = "+".join(dict.fromkeys(wf_ids))

    result = BlockResult(
        waveform_id=composite_id,
        schedule_index=-1,
        repeats=seq.total_shots,
        averaged_spectrum=avg,
        frames_averaged=config.spectrometer.frames_to_average,
        sequence=seq,
    )

    if run_dir is not None:
        _save_block_result(
            run_dir,
            result,
            tag=f"c{cycle:04d}_full_concatenated",
            cycle=cycle,
            save_played=(cycle == 0),
        )

    return [result]


def _run_acquisition_loop(
    config: ExperimentConfig,
    library: dict[str, np.ndarray],
    awg,
    spectrometer,
    run_dir: Path | None,
    *,
    continuous: bool,
) -> tuple[list[BlockResult], int]:
    """
    Run schedule once, or repeatedly until Ctrl+C when continuous=True.

    Each cycle saves new spectra under cNNNN_* tags. Returns last cycle's
    results and the number of completed cycles.
    """
    if continuous:
        print("\n" + "=" * 60)
        print("Ext0-locked bursts (same model as main.py) + Qmini averaging.")
        print("Repeating schedule until Ctrl+C; then RF stops, devices stay open.")
        print(
            f"  segment_duration_us={config.awg.segment_duration_us}  "
            f"acquire_loops={config.awg.acquire_loops} "
            f"(0 -> use schedule.repeats as SPC_LOOPS/Trg0)"
        )
        print("=" * 60 + "\n")

    cycle = 0
    results: list[BlockResult] = []
    summaries: list[dict[str, Any]] = []

    try:
        while True:
            print(f"--- Acquisition cycle {cycle} ---")
            if config.awg.playback_mode == "per_block":
                results = run_per_block_mode(
                    config, library, awg, spectrometer, run_dir, dry_run=False, cycle=cycle
                )
            elif config.awg.playback_mode == "concatenated":
                results = run_concatenated_mode(
                    config, library, awg, spectrometer, run_dir, dry_run=False, cycle=cycle
                )
            else:
                raise ValueError(f"Unknown playback_mode: {config.awg.playback_mode}")

            for r in results:
                summaries.append(
                    {
                        "cycle": cycle,
                        "waveform_id": r.waveform_id,
                        "schedule_index": r.schedule_index,
                        "frames_averaged": r.frames_averaged,
                    }
                )

            if run_dir is not None:
                _save_run_metadata(
                    config,
                    run_dir,
                    {
                        "continuous": continuous,
                        "cycles_completed": cycle + 1,
                        "results_summary": summaries,
                    },
                )
                print(f"  Saved cycle {cycle} -> {run_dir}")

            cycle += 1
            if not continuous:
                break
    except KeyboardInterrupt:
        print(
            f"\nCtrl+C — stopped after {cycle} completed cycle(s) "
            f"(partial cycle discarded if interrupted mid-acquire)."
        )

    return results, cycle


def _keep_rf_until_interrupt(awg, config: ExperimentConfig, library: dict[str, np.ndarray]) -> None:
    """
    After scheduled acquires, keep RF armed like main.py until Ctrl+C.

    external: Ext0-locked finite bursts (same loops policy as acquire).
    software: one FORCETRIGGER finite burst (SPC_LOOPS >= 1, never 0).
    """
    if awg is None or not hasattr(awg, "run_until_interrupt"):
        return

    last_id = config.schedule[-1].waveform_id if config.schedule else next(iter(library))
    segment = library[last_id]
    repeats = config.schedule[-1].repeats if config.schedule else 1
    trigger = config.awg.trigger_mode
    played = _memsize_for_trigger(segment, repeats)
    loops = _effective_replay_loops(config, repeats)

    print("\n" + "=" * 60)
    print(
        f"Scheduled acquires done — SINGLERESTART hold "
        f"({last_id}, MEMSIZE={played.size}, SPC_LOOPS={loops}, trigger={trigger}) "
        "until Ctrl+C."
    )
    print("=" * 60)
    awg.play_voltage_array(
        played,
        loops=loops,
        auto_start=True,
        wait_ready=False,
    )
    awg.run_until_interrupt()


def _leave_hardware_dormant(awg, spectrometer) -> None:
    """
    Stop AWG RF output but do not close the card or spectrometer.

    Next `run_experiment` opens devices again; process exit also releases handles.
    """
    if awg is not None:
        try:
            awg.stop_output()
        except Exception:
            pass
    # Intentionally do not call spectrometer.close() or awg.close_card()
    print("Hardware dormant (AWG stopped, devices not closed).")


def run_experiment(
    config_path: str | Path,
    dry_run: bool = False,
    awg_backend_override: str | None = None,
    spec_backend_override: str | None = None,
    trigger_mode_override: str | None = None,
    voltage_max_mV_override: int | None = None,
    sampling_rate_MSa_s_override: float | None = None,
    wait_for_exit: bool = True,
) -> dict[str, Any]:
    """
    Main entry: load config, build sequences, optionally run hardware loop.

    Parameters
    ----------
    config_path : path to YAML experiment file
    dry_run     : if True, compute only — no AWG/spectrometer I/O
    awg_backend_override : force 'dummy' or 'spectrum' regardless of YAML
    spec_backend_override : force 'dummy' or 'nio_qmini' regardless of YAML
    trigger_mode_override : force 'software' or 'external'
    voltage_max_mV_override : peak AWG output in millivolts
    sampling_rate_MSa_s_override : AWG sample rate in MSa/s
    wait_for_exit : if True (default), repeat play+acquire+save until Ctrl+C.
                    Then (and also after --no-wait one cycle) keep Ext0-locked RF
                    on the scope like main.py until another Ctrl+C, then dormant.

    Returns
    -------
    dict with library keys, mode, block results, run_dir (if saved)
    """
    config = load_experiment_config(config_path)

    if awg_backend_override:
        config.awg.backend = awg_backend_override  # type: ignore[assignment]

    if spec_backend_override:
        config.spectrometer.backend = spec_backend_override  # type: ignore[assignment]

    if trigger_mode_override:
        config.awg.trigger_mode = trigger_mode_override  # type: ignore[assignment]

    if voltage_max_mV_override is not None:
        config.awg.voltage_max_mV = int(voltage_max_mV_override)

    if sampling_rate_MSa_s_override is not None:
        config.awg.sampling_rate_MSa_s = float(sampling_rate_MSa_s_override)

    print(f"\n{'=' * 60}")
    print(f"Experiment: {config.name}")
    print(f"AWG backend: {config.awg.backend}, mode: {config.awg.playback_mode}")
    print(f"Spectrometer: {config.spectrometer.backend}")
    print(f"Trigger: {config.awg.trigger_mode}")
    print(f"Voltage: +/-{config.awg.voltage_max_mV} mV")
    print(f"Sampling rate: {config.awg.sampling_rate_MSa_s} MSa/s")
    print(f"Segment duration: {config.awg.segment_duration_us} us (main.py T)")
    print(
        f"Replay loops/Trg0: acquire_loops={config.awg.acquire_loops} "
        f"(0 -> schedule.repeats; main.py LOOPS)"
    )
    if config.awg.playback_mode == "per_block":
        for entry in config.schedule:
            eff = _effective_replay_loops(config, entry.repeats)
            print(f"  schedule {entry.waveform_id} x {entry.repeats} -> SPC_LOOPS={eff}")
    print(f"Spec settle delay: {config.spectrometer.settle_delay_ms} ms")
    print(f"Continuous spectrum collect until Ctrl+C: {wait_for_exit and not dry_run}")
    print(f"{'=' * 60}\n")

    print("[1/4] Building waveform library...")
    library = build_waveform_library(config.waveforms, config.awg, config.calibration)
    for wf_id, vt in library.items():
        print(f"  {wf_id}: {vt.size} samples, min={vt.min():.3f}, max={vt.max():.3f}")

    run_dir: Path | None = None
    if not dry_run:
        run_dir = _ensure_output_dir(config)
        _save_waveform_library(run_dir, library, config)
        print(f"  Saved waveform V(t) + time axis to {run_dir}")

    awg = None
    spectrometer = None
    results: list[BlockResult] = []
    cycles_completed = 0

    try:
        if dry_run:
            print("\n[DRY RUN] Skipping hardware — building sequences only...")
            if config.awg.playback_mode == "per_block":
                blocks = build_per_block_sequences(config, library)
                for b in blocks:
                    print(summarize_sequence(b.sequence, label=b.waveform_id))
            else:
                seq = build_full_concatenated_sequence(config, library)
                print(summarize_sequence(seq))
        else:
            print("\n[2/4] Opening AWG...")
            awg = _create_awg(config)
            awg.open_card()
            awg.setup_card(
                trigger_mode=config.awg.trigger_mode,
                loops=1,
            )
            awg.allocate_buffer()

            print("[3/4] Opening spectrometer...")
            spectrometer = _create_spectrometer(config)
            spectrometer.open()
            if run_dir is not None and getattr(spectrometer, "wavelengths_nm", None) is not None:
                np.save(run_dir / "wavelengths_nm.npy", spectrometer.wavelengths_nm)

            print("[4/4] Running playback + acquisition...")
            results, cycles_completed = _run_acquisition_loop(
                config,
                library,
                awg,
                spectrometer,
                run_dir,
                continuous=wait_for_exit,
            )
            # After spectra (one cycle or Ctrl+C), keep Ext0-locked RF like main.py
            # until a second Ctrl+C, then leave devices dormant (not closed).
            _keep_rf_until_interrupt(awg, config, library)
            _leave_hardware_dormant(awg, spectrometer)
            awg = None
            spectrometer = None

    finally:
        if awg is not None:
            try:
                awg.stop_output()
            except Exception:
                pass
            print("Hardware left dormant after interrupt/error (devices not closed).")

    return {
        "config": config,
        "library": library,
        "results": results,
        "cycles_completed": cycles_completed,
        "run_dir": str(run_dir) if run_dir else None,
        "dry_run": dry_run,
    }
