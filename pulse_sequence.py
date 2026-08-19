"""
pulse_sequence.py
=================

Expands experiment schedule into playable RF sample arrays.

TWO PLAYBACK STRATEGIES (PI / PDF page 4)
-----------------------------------------

1. CONCATENATED (playback_mode = "concatenated")
   - Expand schedule: wf1×10, wf2×10, ... into one list of segment indices.
   - Concatenate all V(t) segments (+ optional zero gaps) → single long buffer.
   - AWG plays once (or loops) — minimizes stop/start overhead.
   - Matches "New (target)" on page 4: V(t,τ) as N×M matrix flattened to 1×NM.

2. PER_BLOCK (playback_mode = "per_block")
   - For each schedule entry, concatenate repeats of ONE waveform only.
   - Play block → acquire/average spectrometer → next block.
   - Easier data tagging without per-shot spectrometer trigger (PI answer #5).
   - Slightly more AWG reloads than full concatenation but simpler analysis.

SEGMENT METADATA
----------------
SegmentInfo records where each shot lives in sample indices — useful for logging
and future trigger-synced acquisition even if we don't use it fully in MVP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from experiment_config import AwgConfig, ExperimentConfig, ScheduleEntry


@dataclass
class SegmentInfo:
    """
    Describes one contiguous RF segment inside a concatenated buffer.

    start_sample / end_sample : half-open interval [start, end) in DAC indices
    waveform_id               : which library waveform was played
    repeat_index              : 0-based index within that schedule entry's repeats
    schedule_index            : which schedule row produced this segment
    """

    start_sample: int
    end_sample: int
    waveform_id: str
    repeat_index: int
    schedule_index: int

    @property
    def length_samples(self) -> int:
        return self.end_sample - self.start_sample


@dataclass
class PulseSequence:
    """
    Result of building a playable sequence from config + waveform library.

    samples        : 1D normalized V(t), values in ~[-1, 1]
    segments       : metadata for each logical shot in order
    segment_length : RL (samples per one waveform, constant across library)
    gap_samples    : zeros inserted between segments (may be 0)
    """

    samples: np.ndarray
    segments: list[SegmentInfo] = field(default_factory=list)
    segment_length: int = 0
    gap_samples: int = 0

    @property
    def total_samples(self) -> int:
        return int(self.samples.size)

    @property
    def total_shots(self) -> int:
        return len(self.segments)


def _make_gap(gap_samples: int) -> np.ndarray:
    """
    Gap between segments is literal zeros in V(t).

    APPROACH: zero RF ≈ no drive / minimum diffraction during gap.
    Alternative (not used): repeat last sample — could cause AOM hold; zeros safer.
    """
    if gap_samples <= 0:
        return np.array([], dtype=np.float64)
    return np.zeros(gap_samples, dtype=np.float64)


def _concat_segments(
    segment_arrays: list[np.ndarray],
    gap_samples: int,
    waveform_ids: list[str],
    repeat_indices: list[int],
    schedule_indices: list[int],
) -> PulseSequence:
    """
    Low-level concatenation with metadata tracking.

    Walks segment_arrays in order, appends gap (except after last segment),
    records SegmentInfo for each segment's final position in the buffer.
    """
    if not (
        len(segment_arrays)
        == len(waveform_ids)
        == len(repeat_indices)
        == len(schedule_indices)
    ):
        raise ValueError("segment_arrays and metadata lists must have same length")

    gap = _make_gap(gap_samples)
    pieces: list[np.ndarray] = []
    segments: list[SegmentInfo] = []
    cursor = 0
    rl = segment_arrays[0].size

    for i, arr in enumerate(segment_arrays):
        if arr.size != rl:
            raise ValueError(
                f"All segments must have same length RL={rl}, got {arr.size} at index {i}"
            )

        start = cursor
        end = cursor + rl
        segments.append(
            SegmentInfo(
                start_sample=start,
                end_sample=end,
                waveform_id=waveform_ids[i],
                repeat_index=repeat_indices[i],
                schedule_index=schedule_indices[i],
            )
        )
        pieces.append(arr)
        cursor = end

        # Gap after each segment except the last — avoids trailing silence
        if gap_samples > 0 and i < len(segment_arrays) - 1:
            pieces.append(gap)
            cursor += gap_samples

    samples = np.concatenate(pieces) if pieces else np.array([], dtype=np.float64)

    return PulseSequence(
        samples=samples,
        segments=segments,
        segment_length=rl,
        gap_samples=gap_samples,
    )


def expand_schedule_to_shots(schedule: list["ScheduleEntry"]) -> list[tuple[str, int, int]]:
    """
    Flatten schedule into ordered list of (waveform_id, repeat_index, schedule_index).

    Example schedule: wf1×3, wf2×2 →
      [(wf1,0,0), (wf1,1,0), (wf1,2,0), (wf2,0,1), (wf2,1,1)]
    """
    shots: list[tuple[str, int, int]] = []
    for sched_idx, entry in enumerate(schedule):
        for rep in range(entry.repeats):
            shots.append((entry.waveform_id, rep, sched_idx))
    return shots


def build_full_concatenated_sequence(
    config: "ExperimentConfig",
    library: dict[str, np.ndarray],
) -> PulseSequence:
    """
    Build ONE buffer containing the entire expanded schedule (all repeats, all wf ids).

    Used when awg.playback_mode == "concatenated".
    """
    awg = config.awg
    gap_samples = awg.gap_samples()
    shots = expand_schedule_to_shots(config.schedule)

    arrays: list[np.ndarray] = []
    wf_ids: list[str] = []
    rep_idxs: list[int] = []
    sched_idxs: list[int] = []

    for wf_id, rep_idx, sched_idx in shots:
        if wf_id not in library:
            raise KeyError(f"Waveform '{wf_id}' missing from library")
        arrays.append(library[wf_id])
        wf_ids.append(wf_id)
        rep_idxs.append(rep_idx)
        sched_idxs.append(sched_idx)

    if not arrays:
        raise ValueError("Schedule produced zero shots — check schedule.repeats")

    return _concat_segments(arrays, gap_samples, wf_ids, rep_idxs, sched_idxs)


@dataclass
class ScheduleBlock:
    """
    One schedule entry expanded to a playable sub-sequence.

    Example: wf1×10 → one PulseSequence with 10 identical segments back-to-back.
    """

    waveform_id: str
    schedule_index: int
    repeats: int
    sequence: PulseSequence


def build_per_block_sequences(
    config: "ExperimentConfig",
    library: dict[str, np.ndarray],
) -> list[ScheduleBlock]:
    """
    Build one PulseSequence per schedule row (for per_block playback).

    PI pattern 10× wf1 then 10× wf2 → two ScheduleBlock objects, each with
    10 segments in its .sequence.samples buffer.
    """
    awg = config.awg
    gap_samples = awg.gap_samples()
    blocks: list[ScheduleBlock] = []

    for sched_idx, entry in enumerate(config.schedule):
        wf_id = entry.waveform_id
        if wf_id not in library:
            raise KeyError(f"Waveform '{wf_id}' missing from library")

        vt = library[wf_id]
        arrays = [vt] * entry.repeats
        wf_ids = [wf_id] * entry.repeats
        rep_idxs = list(range(entry.repeats))
        sched_idxs = [sched_idx] * entry.repeats

        seq = _concat_segments(arrays, gap_samples, wf_ids, rep_idxs, sched_idxs)
        blocks.append(
            ScheduleBlock(
                waveform_id=wf_id,
                schedule_index=sched_idx,
                repeats=entry.repeats,
                sequence=seq,
            )
        )

    return blocks


def summarize_sequence(seq: PulseSequence, label: str = "") -> str:
    """Human-readable summary for dry-run / logging."""
    lines = [
        f"=== Pulse sequence summary {label} ===",
        f"  Total samples   : {seq.total_samples}",
        f"  Segment length  : {seq.segment_length}",
        f"  Gap samples     : {seq.gap_samples}",
        f"  Total shots     : {seq.total_shots}",
        f"  Duration (approx): {seq.total_samples / 1e6:.4f} ms at 1000 MSa/s reference",
    ]
    if seq.segments:
        first = seq.segments[0]
        last = seq.segments[-1]
        lines.append(
            f"  First segment   : {first.waveform_id} rep={first.repeat_index} "
            f"[{first.start_sample}:{first.end_sample})"
        )
        lines.append(
            f"  Last segment    : {last.waveform_id} rep={last.repeat_index} "
            f"[{last.start_sample}:{last.end_sample})"
        )
    return "\n".join(lines)
