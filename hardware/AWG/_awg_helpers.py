"""
Shared helpers for Dummy_AWG and Spectrum_AWG.

Keep math / padding here so the two backends stay in lockstep.
"""

import numpy as np


def align32(n):
    """Round sample count up to a multiple of 32 (M4i MEMSIZE / DMA rule)."""
    n = int(n)
    if n % 32 == 0:
        return n
    return n + (32 - n % 32)


def apply_burst_envelope(burst, sr_hz, edge_s=200e-9):
    """Raised-cosine on/off at the ends of an RF burst (cleaner scope edge)."""
    x = np.asarray(burst, dtype=np.float64).copy()
    n = x.size
    if n < 64:
        return x
    edge = int(round(float(edge_s) * float(sr_hz)))
    edge = max(32, min(edge, n // 10))
    ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(edge) / float(edge)))
    x[:edge] *= ramp
    x[-edge:] *= ramp[::-1]
    return x


def build_internal_period(burst, sr_hz, period_s, envelope=False, scope_tip=False):
    """
    One free-run period on the sample clock: [optional tip][RF][idle zeros].

    Returns (period_array, tip_sample_count).
    """
    burst = np.asarray(burst, dtype=np.float64).ravel()
    if envelope:
        burst = apply_burst_envelope(burst, sr_hz)

    tip_n = gap_n = 0
    if scope_tip:
        tip_n = align32(max(32, int(round(2e-6 * float(sr_hz)))))
        gap_n = align32(max(32, int(round(0.5e-6 * float(sr_hz)))))

    burst_n = align32(max(burst.size, 32))
    head = tip_n + gap_n
    total = align32(
        max(head + burst_n + 32, int(round(float(period_s) * float(sr_hz))))
    )

    period = np.zeros(total, dtype=np.float64)
    if tip_n:
        period[:tip_n] = 1.0
    n = min(burst.size, burst_n)
    period[head : head + n] = np.clip(burst[:n], -1.0, 1.0)
    return period, tip_n
