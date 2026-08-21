# pulse_shaper

Simple Spectrum **M4i.6631-X8** control for optical pulse shaping (LCLS-II / SLAC).
Same wrapping style as `Optical-Pulse-Shaping` (ctypes + `pyspcm`), with one
`main.py` front panel and a Dummy backend for offline work.

`ps_calculations.py` is the PI’s math — leave it alone.

## Quick start

```text
cd pulse_shaper
pip install -r requirements.txt
python main.py
```

| Knob | Meaning |
|------|---------|
| `USE_DUMMY = True` | No card — plots and call-order dry-run |
| `USE_DUMMY = False` | Real M4i (needs Spectrum driver / `spcm_win64.dll`) |

Edit knobs and pulse masks at the top of `main.py` (same idea as
`Optical-Pulse-Shaping/main.py`: calibration, phase, amplitude, plots).

## Layout

```text
pulse_shaper/
  main.py                         # front panel + run path
  ps_calculations.py              # PI pulse math (do not rewrite)
  requirements.txt
  hardware/AWG/
    _spectrumAWG.py               # real card
    _dummyAWG.py                  # same API, no hardware
    _awg_helpers.py               # shared align / envelope / period pad
    spectrum_AWG_drivers/         # pyspcm + regs (from Optical-Pulse-Shaping)
```

Call order (both backends):

```text
open_card → setup_card → allocate_buffer → load_* → write_waveform_to_card → output_waveform
```

There is **one** `setup_card(...)` for internal/external and single/multi
(no separate `setup_multi`).

## Trigger modes

Sample clock is always the card’s **INTPLL** (built-in). “Trigger” here means
*what starts each shot*, not the sample clock source.

| `TRIGGER_MODE` | `INTERNAL_SHOT_TRIGGER` | What happens | Scope tip |
|----------------|-------------------------|--------------|-----------|
| `"external"` | (ignored) | Ext0 starts each play (`SINGLERESTART`) | Trigger scope from same Ext0 source (DG645 / photodiode) |
| `"internal"` | `"ext0"` (**desk default**) | INTPLL clock + Ext0 shot start | Keep scope on DG645 — **no walk** |
| `"internal"` | `"free_run"` | Free-run `[tip?][RF\|idle]` on sample clock | **Unplug** DG645 from scope; Normal on Ch0 tip |

`PLAYBACK = "multi"` loads several segments (e.g. different `tau`s). Segment
advance needs Ext0 edges, so multi always uses the external shot path.

## Why free-run “walked” on the scope

External looked locked because the DG645 fired **both** AWG Ext0 and (usually)
the scope. Free-run ignores Ext0. If the scope still triggers from the DG645,
the two clocks beat and the pulse walks L→R. Software tips cannot fix that
while the scope stays on the DG645.

Desk fix: `INTERNAL_SHOT_TRIGGER = "ext0"` with DG645 → AWG Ext0.

## Hardware notes

- Ch0 only (50 Ω). MEMSIZE / DMA length must be multiples of **32**.
- Ext0 / Trg0: rising edge, threshold `EXT0_LEVEL_MV` (default 1500 mV), high-Z.
- Do **not** use `SPC_REP_STD_CONTINUOUS` on this bench — it left Ch0 quiet;
  free-run uses `SPC_REP_STD_SINGLE` + `SPC_LOOPS = 0`.
- Arm once and leave it running (`run_until_interrupt`). Do not re-arm from a
  Python loop (adds host jitter / looks like walk).

## Experimenting (PI)

In `main.py`, uncomment / swap the same style of lines as Optical-Pulse-Shaping:

- `pulse.calibration([...])`
- phase masks: `PhiFcn.constant`, `PhiFcn.taylor_series`, …
- amplitude masks: `AmpFcn.constant`, `multi_gaussian`, `double_pulse`, …
- plots: `"time"` or `"freq"`

Optional card flags (set on the `awg` object before load if needed):

- `awg.envelope_burst` — soft RF edges (~200 ns)
- `awg.scope_lock_pulse` — Ch0 tip for free-run scope lock
- `awg.enable_x0_sync` — optional X0 period / trigger marker

## Requirements

- Python 3 with `numpy`, `matplotlib`
- Real card: Spectrum Instrumentation driver for the M4i
- Close **SBench 6** before running (it can hold the driver)
