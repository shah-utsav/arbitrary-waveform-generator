# Optical Pulse Shaping — Python Control Stack

Python experiment framework for **optical pulse shaping** with a **Spectrum M4i.66xx AWG** and a **Broadcom Qmini** miniature spectrometer. It computes frequency-domain masks, builds RF waveforms `V(t)`, plays scheduled pulse trains on hardware, acquires averaged spectra, and **saves spectrum + waveform data to disk in milliseconds**.

This project replaces the old LabVIEW nested-loop workflow — and slow manual saves on bench instruments — with a **YAML-driven, vectorized pipeline** (PI target architecture in *Optical Pulse Shaping.pdf*, page 4).

**Status (Aug 2026):** Full **AWG + Qmini** on **64-bit Python** validated on the SLAC lab PC. External Ext0 (**SINGLERESTART**), sample-clock **internal** periods, and Ext0 **MULTI** (different waveform per trigger) are all supported in `hardware/AWG/_spectrumAWG.py`.

- **Full stack (AWG + spec):** `data/experiments/double_pulse_demo_20260706_224225`
- **Continuous collect (external trigger, until Ctrl+C):** `data/experiments/double_pulse_demo_20260717_221325`
- Spectrometer-only (64-bit NioLink): `data/experiments/double_pulse_demo_20260706_214505`
- Legacy 32-bit WAVES stack: `data/experiments/double_pulse_demo_20260702_224342`

---

## Architecture history (why we changed stacks)

### Phase 1 — Original implementation (Jul 2025)

The first working stack drove the Broadcom Qmini through **Broadcom WAVES** and the **`.NET` SDK** (`RGBDriverKit.dll`) via **pythonnet**:

```
Python 32-bit  →  pythonnet  →  RGBDriverKit.dll (x86)  →  Qseries USB
```

This was validated end-to-end with the Spectrum AWG (`double_pulse_demo_20260702_224342`).

**What worked**

- Official vendor Windows stack (same DLL as the WAVES GUI)
- Reliable device discovery via `Qseries.SearchDevices()` on **32-bit Python**
- No USB driver rebinding on locked-down lab PCs

**Shortcomings that motivated the transition**

| Problem | Impact |
|---------|--------|
| **`RGBDriverKit.dll` is 32-bit only** | 64-bit Python loads the DLL but `SearchDevices()` returns **0 devices** |
| **Forced 32-bit Python for AWG + Qmini together** | Even though Spectrum ships `spcm_win64.dll`, the spectrometer locked the whole project to x86 |
| **`pythonnet` + .NET interop** | Extra dependency, harder debugging, reflection edge cases |
| **`matplotlib` on 32-bit Python 3.13** | Often **no pip wheel** — plotting had to happen on a separate 64-bit machine |
| **Cross-platform goal** | .NET/WAVES path is Windows-only; mentor target includes macOS spectrometer + future web backend |
| **FTD2XX_NET.dll** | Sometimes required from FTDI; not bundled in newer Broadcom SDK zips |

Legacy code for this stack is **preserved, not deleted**, in:

- `hardware/spectrometer/_broadcom_sdk.py` (archived inside `"""…"""`)
- `hardware/spectrometer/_broadcom_qmini.py` (archived)
- `hardware/spectrometer/broadcom_driver_check.py` (archived)

### Phase 2 — Ideation (Jul 2026)

Requirements from the PI / long-term roadmap:

1. **64-bit Python** on the lab PC — one environment for AWG, spectrometer, **and matplotlib**
2. **Cross-platform spectrometer** (Windows + macOS) for alignment / remote lab server
3. **Web frontend later** — backend on the lab machine, browser anywhere on the network
4. Keep Spectrum AWG on Windows (no macOS driver exists today)

A colleague’s [`laser_spectrometer`](https://github.com/MattBainLCLS/laser_spectrometer) repo showed the key insight: use RGB Photonics’ **NioLink Python driver** (`pyrgbdriverkit`) — **PyUSB + libusb**, pure Python USB protocol — instead of the WAVES `.NET` DLL. That removes the 32-bit lock-in.

### Phase 3 — Current implementation (Jul–Aug 2026)

```
Python 64-bit  →  PyUSB + libusb-package  →  pyrgbdriverkit (vendored)  →  Qmini USB
Python 64-bit  →  pyspcm  →  spcm_win64.dll  →  Spectrum AWG
```

| Component | Active module | Backend name |
|-----------|---------------|--------------|
| Spectrometer | `hardware/spectrometer/_nio_qmini.py` | `nio_qmini` |
| USB/libusb helper | `hardware/spectrometer/_usb_backend.py` | (internal) |
| Vendored driver | `hardware/spectrometer/vendor/pyrgbdriverkit-0.3.7/` | NioLink 0.3.7 |
| AWG | `hardware/AWG/_spectrumAWG.py` | `spectrum` |
| Health check | `hardware/spectrometer/nio_driver_check.py` | `--check-spec` |

**Validated on SLAC lab PC:** `double_pulse_demo_20260706_214505` — 64-bit Python, `nio_qmini`, 2018 px, 454–1112 nm.

**Still required:** Close **WAVES / `Waves.exe`** before Python — exclusive USB access, same as before.

---

## Table of contents

1. [Why use this code (the speed problem)](#why-use-this-code-the-speed-problem)
2. [Quick start — run it now](#quick-start--run-it-now)
3. [Step-by-step lab procedure (PI demo)](#step-by-step-lab-procedure-pi-demo)
4. [AWG trigger modes (SINGLERESTART / MULTI / internal)](#awg-trigger-modes-singlerestart--multi--internal)
5. [What this project does](#what-this-project-does)
6. [Physical signal chain](#physical-signal-chain)
7. [Software architecture](#software-architecture)
8. [Repository layout](#repository-layout)
9. [Software compatibility (bitness)](#software-compatibility-bitness)
10. [Setup (one-time)](#setup-one-time)
11. [Running experiments — all commands](#running-experiments--all-commands)
12. [Saved data and plotting](#saved-data-and-plotting)
13. [Validated real runs](#validated-real-runs-what-success-looks-like)
14. [Hardware requirements](#hardware-requirements)
15. [What we have achieved](#what-we-have-achieved)
16. [Configuration reference](#configuration-reference)
17. [Roadmap](#roadmap)
18. [Troubleshooting](#troubleshooting)
19. [Architecture history](#architecture-history-why-we-changed-stacks)
20. [References](#references)

---

## Why use this code (the speed problem)

**Old bench workflow:** play a waveform → read spectrum on spectrometer → navigate scope menus to save `V(t)` → repeat. This can take **~120 seconds per point** because of manual UI navigation on legacy equipment.

**This code:**

```
one command  →  AWG plays V(t)  +  Qmini averages spectrum  +  both saved to disk
                 (milliseconds of file I/O, not minutes of button-pushing)
```

What gets saved automatically every run:

| Data | Source | File |
|------|--------|------|
| Computed RF waveform `V(t)` | Software (mask math) | `waveform_<id>.npy` |
| Exact buffer sent to AWG | Software (what was played) | `played_c0000_block*_<id>.npy` (older: `played_block*_<id>.npy`) |
| Averaged optical spectrum | Qmini spectrometer | `spectrum_cNNNN_block*_<id>.npy` (older: `spectrum_block*_<id>.npy`) |
| Wavelength axis | Qmini calibration | `wavelengths_nm.npy` |
| Time axis for `V(t)` | AWG sample rate | `time_axis_us.npy` |
| Summary figure | `plot_run.py` | `run_summary.png` |

The oscilloscope and delay generator **do not need to be connected to the laptop**. They stay on their own bench setup. This software replaces the *data capture* step that used to require reading traces off the scope by hand.

**What `V(t)` means here:** the computed/played RF waveform from the mask — **not** a measured photodiode trace from the scope. That is intentional and matches the PI workflow target (pre-compute waveforms, save with spectra).

---

## Quick start — run it now

On the **lab PC** with AWG + Qmini connected. Use **64-bit Python** (see [Setup](#setup-one-time)).

```cmd
cd C:\path\to\Optical-Pulse-Shaping
.venv64\Scripts\activate.bat

REM Close WAVES and SBench 6 first!

python run_experiment.py --check-awg
python run_experiment.py --check-spec

python run_experiment.py --awg spectrum --spec nio_qmini

python plot_run.py --save
explorer data\experiments
```

**AWG-only demos** (no Qmini):

```cmd
python main.py          REM Ext0 SINGLERESTART — same waveform every Trg0
python multi_pulse.py   REM Ext0 MULTI — different tau each Trg0
```

---

## Step-by-step lab procedure (PI demo)

### Hardware connections (SLAC laptop)

| Device | How to connect | Software name |
|--------|----------------|---------------|
| **Spectrum M4i AWG** | Card in Sonnet Echo III → **Thunderbolt 3** to laptop | `/dev/spcm0` |
| **Broadcom Qmini** | **USB-C** data cable to laptop | `nio_qmini` backend |
| **SRS DG645 delay generator** | TTL out → coax → Spectrum **Ext0 / Trg0** (not to the laptop) | `awg.trigger_mode: external` |
| Oscilloscope | Standalone on bench (optional) | not controlled by this software |

Power Echo III before connecting TB. Use a **data-capable** USB-C cable for the Qmini (~200 mA).

**AWG trigger choice** (YAML prompt / `config/default_experiment.yaml`):

| `awg.trigger_mode` | Behavior |
|--------------------|----------|
| `external` (**default**) | Wait for DG645 TTL on Ext0/Trg0, then play MEMSIZE once per edge (`SINGLERESTART`, `SPC_LOOPS=0`) |
| `software` / `internal` | No DG645. Period = `[RF \| zeros]` on the AWG sample clock; one `FORCETRIGGER` starts free-run |

The DG645 stays a bench instrument — Python does not program it.

**Overlap with spectrometer:** AWG arms first (`wait_ready=False`). After `spectrometer.settle_delay_ms` (default **10**), the Qmini averages while Ext0-locked bursts continue.

**YAML ↔ demos:**

| YAML | Demo knob | Meaning today |
|------|-----------|---------------|
| `segment_duration_us` | `main.py` `T` | One library-segment length (µs) |
| `schedule.repeats` | — | **Tiles** that segment into MEMSIZE (longer RF per Trg0) |
| `acquire_loops` | `main.py` `LOOPS` | Explicit `SPC_LOOPS` override; **0** = one MEMSIZE per Ext0 forever |
| — | `multi_pulse.py` `TAU_LIST` | Different double-pulse delay per MULTI segment |

**Continuous collection:** By default `run_experiment.py` **repeats the full schedule** (arm → acquire → save) until **Ctrl+C**. Then it keeps Ext0-locked RF on (`run_until_interrupt`, like `main.py`) until a **second Ctrl+C**, and leaves devices **dormant** (not closed). Use `--no-wait` for a single spectrum cycle before that RF hold.

### One-time software setup (SLAC laptop, no admin)

If you cannot install 64-bit Python with the normal installer (admin password required):

1. Download **Windows embeddable package (64-bit)** from [python.org](https://www.python.org/downloads/)
2. Extract to e.g. `C:\Users\<you>\Programs\Python314-64\`
3. Enable pip in the embed package (edit `python314._pth`, add `import site`, create `Lib\site-packages`)
4. Create the project venv:

```cmd
cd "C:\D Drive\Optical-Pulse-Shaping"
C:\Users\<you>\Programs\Python314-64\python.exe -m pip install virtualenv
C:\Users\<you>\Programs\Python314-64\python.exe -m virtualenv .venv64
.venv64\Scripts\activate.bat
python -m pip install -r requirements.txt
```

Use **`activate.bat` in cmd** if PowerShell blocks `Activate.ps1`. Point VS Code / Cursor at `.venv64\Scripts\python.exe`.

### Before every run

| Step | Action |
|------|--------|
| 1 | Echo III powered, TB cable to laptop, AWG at `/dev/spcm0` |
| 2 | Qmini on USB (data-capable USB-C cable) |
| 3 | **Close WAVES** (Task Manager → no `Waves.exe`) |
| 4 | **Close SBench 6** (locks AWG card) |
| 5 | Confirm **64-bit Python**: `python -c "import struct; print(struct.calcsize('P')*8)"` → `64` |
| 6 | Activate `.venv64`: `.venv64\Scripts\activate.bat` |

### Commands (in order) — full AWG + spectrometer

```cmd
cd "C:\D Drive\Optical-Pulse-Shaping"
.venv64\Scripts\activate.bat

python -c "import struct; print(struct.calcsize('P')*8, 'bit')"
REM Expect: 64 bit

python run_experiment.py --check-awg
REM Expect: /dev/spcm0 AWG (AO), spcm_win64.dll

python run_experiment.py --check-spec --acquire-test
REM Expect: PyUSB OK, Found 1 device, Acquire test Open OK

python run_experiment.py --dry-run
REM Optional: validate YAML + waveform math only

python run_experiment.py --awg spectrum --spec nio_qmini
REM Expect: spectrum cycles until Ctrl+C, then Ext0 RF hold until second Ctrl+C
REM Optional one-shot: add --no-wait

python plot_run.py --save --no-show
explorer data\experiments
```

### Expected output files (each run folder)

| File | Content |
|------|---------|
| `run_metadata.json` | Full experiment record (64-bit, backends, schedule; includes `continuous`, `cycles_completed`) |
| `wavelengths_nm.npy` | Qmini calibration axis |
| `waveform_wf*.npy` | Computed V(t) per mask |
| `played_c0000_block*_wf*.npy` | Exact buffer sent to AWG (saved on cycle 0) |
| `spectrum_cNNNN_block*_wf*.npy` | Averaged spectra per cycle |
| `time_axis_us.npy` | Time base for V(t) |
| `run_summary.png` | Plot from `plot_run.py --save` |

---

## AWG trigger modes (SINGLERESTART / MULTI / internal)

All programming lives in `hardware/AWG/_spectrumAWG.py`. **Do not** swap `SPC_REP_STD_SINGLERESTART` → `SPC_REP_STD_MULTI` inside `setup_card` — MULTI needs `SPC_SEGMENTSIZE`, `MEMSIZE = N×SEGMENTSIZE`, and N waveforms via `play_multi_voltage_arrays`.

| Mode | Card mode | When to use | Entry point |
|------|-----------|-------------|-------------|
| **External, same waveform every Ext0** | `SPC_REP_STD_SINGLERESTART` + `SPC_LOOPS=0` | DG645 → Trg0; one MEMSIZE per edge forever | `main.py`, `run_experiment.py` (`setup_card` / `play_voltage_array`) |
| **External, different waveform every Ext0** | `SPC_REP_STD_MULTI` | Tau scan / PI figure: edge 1→seg0, edge 2→seg1, … | `multi_pulse.py` → `play_multi_voltage_arrays([...])` |
| **Internal / software (no DG645)** | `SPC_REP_STD_SINGLE` + `SPC_LOOPS=0` | Period = `[RF \| zeros]` on INTPLL sample clock; one `FORCETRIGGER` | `main.py` with `TRIGGER_MODE = "software"` |

### `main.py` knobs

| Knob | Default | Role |
|------|---------|------|
| `T` | `10` | µs RF burst |
| `SR` | `1250` | MSa/s |
| `F0` | `100` | MHz carrier |
| `TRIGGER_MODE` | `"external"` | `"external"` or `"software"` / `"internal"` |
| `INTERNAL_PERIOD_MS` | `1.0` | Full period for internal (burst + idle zeros) |
| `LOOPS` | `0` | External `SPC_LOOPS` (0 = once per Trg0 forever) |

Uses `AmpFcn.double_pulse(..., tau=0.5)`. Arm once; `run_until_interrupt()` — **no Python re-trigger loop** (that path looked like CW / scope walk).

### `multi_pulse.py` knobs

| Knob | Default | Role |
|------|---------|------|
| `T_us` | `10` | Segment duration (µs) per Ext0 |
| `NUM_SEGMENTS` | `4` | Number of MULTI segments |
| `TAU_LIST` | `[0.0, 0.25, 0.5, 1.0]` | Double-pulse delay per segment |

Requires DG645 → Ext0/Trg0. Scope: four different bursts for four edges (vs `main.py` four identical bursts).

### Scope tip

Edge-triggering the scope on the **100 MHz RF carrier** walks L→R. Prefer **DG645** or AWG multi-purpose **X0** (`TRIGOUT` external / `CONTOUTMARK` internal). The driver enables X0 sync in `setup_card` / `setup_multi`.

---

## What this project does

### Goal

1. **Define pulse shapes in software** using phase φ(ω) and amplitude M(ω) masks (e.g. double pulse with delay τ).
2. **Pre-compute all RF waveforms** before hardware I/O — not inside slow nested loops.
3. **Play structured schedules** on the AWG, e.g. **10× waveform A, then 10× waveform B**.
4. **Acquire and average** spectrometer data per block.
5. **Save spectrum + waveform data together** — fast, reproducible, no manual instrument menus.

### Old workflow (LabVIEW / manual bench)

```
for each delay τ:
    compute M(ω), V(t)
    for i = 1 to 30:
        output → measure → stop
    navigate scope/spectrometer menus to save data   # ~120 s overhead
```

### New workflow (this repo)

```
load YAML config
build waveform library V(t) for all masks        # once, vectorized
expand schedule (10× wf1, 10× wf2, …)
for each schedule block:
    AWG play buffer → spectrometer average → save spectrum + V(t)
plot or analyze offline with plot_run.py
```

---

## Physical signal chain

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────┐     ┌────────┐
│ Python masks    │     │ Spectrum AWG │     │ AOM + grating       │     │ Laser  │
│ φ(ω), M(ω)→V(t) │ ──► │ RF output    │ ──► │ pulse shaper        │ ──► │ shaped │
└─────────────────┘     └──────────────┘     └─────────────────────┘     └────────┘
                                                                              │
                                                                              ▼
                                                          ┌───────────────────────────────┐
                                                          │ Broadcom Qmini spectrometer    │
                                                          │ (FROG validates duration/sep.) │
                                                          └───────────────────────────────┘
```

**Time scales:**

| Domain | Units in code | Example |
|--------|---------------|---------|
| RF / acoustic (AWG) | µs, MSa/s | `segment_duration_us: 10`, `SR: 1250 MSa/s` |
| Optical frequency | ω from calibration polynomial | `calibration.coefficients` |
| Optical time (lab) | fs | Set by mask; validated with FROG |

**Delay τ** in a double-pulse mask is **accurate by construction** in the mask; FROG validates pulse duration and separation in the lab.

---

## Software architecture

```
config/default_experiment.yaml
        │
        ▼
experiment_config.py          ← load & validate YAML
        │
        ▼
waveform_builder.py           ← masks → V(t) via ps_calculations.py
        │
        ▼
pulse_sequence.py             ← expand schedule, concatenate segments
        │
        ▼
experiment_runner.py          ← AWG + spectrometer + save spectrum + V(t)
        │
        ├── hardware/AWG/_spectrumAWG.py             (Spectrum M4i)
        ├── hardware/AWG/_dummyAWG.py
        ├── hardware/spectrometer/_nio_qmini.py        (Qmini via NioLink / PyUSB)
        ├── hardware/spectrometer/_usb_backend.py      (libusb for PyUSB on Windows)
        ├── hardware/spectrometer/vendor/pyrgbdriverkit-0.3.7/
        └── hardware/spectrometer/_dummy_spectrometer.py
```

Legacy (archived, not imported): `_broadcom_qmini.py`, `_broadcom_sdk.py`, `broadcom_driver_check.py`

### Entry points

| Command / script | Purpose |
|------------------|---------|
| `python run_experiment.py` | Full experiment from YAML (+ Qmini) |
| `python run_experiment.py --dry-run` | Config + buffer validation only |
| `python run_experiment.py --check-awg` | Verify Spectrum DLL + card |
| `python run_experiment.py --check-spec` | Verify Qmini PyUSB + NioLink |
| `python run_experiment.py --awg spectrum --spec nio_qmini` | Full real run |
| `python plot_run.py` | Plot latest saved run |
| `python main.py` | AWG-only Ext0 **SINGLERESTART** (or internal) until Ctrl+C |
| `python multi_pulse.py` | AWG-only Ext0 **MULTI** (different tau per edge) |

### Backends

| Kind | Options | YAML key | CLI override |
|------|---------|----------|--------------|
| AWG | `dummy`, `spectrum` | `awg.backend` | `--awg` |
| Spectrometer | `dummy`, `nio_qmini` | `spectrometer.backend` | `--spec` |

CLI flags override YAML. Default config has `spectrometer.backend: nio_qmini`; use `--awg spectrum` for real AWG even if YAML says `dummy`.

### Playback modes (`run_experiment`)

| Mode | Behavior |
|------|----------|
| `per_block` | **Recommended.** One library segment (optionally tiled by `schedule.repeats` into MEMSIZE); `SPC_LOOPS=0` unless `acquire_loops > 0`; one tagged spectrum per waveform id |
| `concatenated` | Entire schedule in one long buffer; one spectrum for whole run |

### Spectrum driver APIs (`Spectrum_AWG`)

| Method | Role |
|--------|------|
| `setup_card(trigger_mode, loops)` | SINGLERESTART (external) or SINGLE (software) |
| `setup_multi(segment_samples, num_segments, …)` | Program MULTI registers |
| `play_voltage_array(...)` | Single-buffer path (experiment / `main.py`) |
| `play_multi_voltage_arrays([...])` | Fill N segments + DMA + arm (`multi_pulse.py`) |
| `build_internal_period(...)` | RF + idle zeros for sample-clock period |
| `run_until_interrupt()` | Arm once; sleep until Ctrl+C (no host re-arm) |

---

## Repository layout

```
Optical-Pulse-Shaping/
├── README.md
├── requirements.txt
├── run_experiment.py             # main CLI (YAML + Qmini)
├── plot_run.py
├── main.py                       # Ext0 SINGLERESTART / internal demo
├── multi_pulse.py                # Ext0 MULTI tau-scan demo
├── experiment_config.py
├── experiment_runner.py
├── waveform_builder.py
├── pulse_sequence.py
├── ps_calculations.py            # mask math (double_pulse, etc.)
├── config/
│   └── default_experiment.yaml
├── hardware/
│   ├── AWG/
│   │   ├── _spectrumAWG.py       # active Spectrum driver
│   │   ├── _dummyAWG.py
│   │   ├── _chaseAWG.py          # legacy Chase share (not in runner)
│   │   ├── spectrum_driver_check.py
│   │   └── spectrum_AWG_drivers/ # pyspcm
│   └── spectrometer/
│       ├── _nio_qmini.py           # active Qmini backend (64-bit)
│       ├── _usb_backend.py
│       ├── nio_driver_check.py
│       ├── vendor/pyrgbdriverkit-0.3.7/
│       ├── _broadcom_*.py          # ARCHIVED (legacy pythonnet)
│       └── …
├── data/experiments/
└── Manuals & Documents/
```

---

## Software compatibility (bitness)

**Use 64-bit Python 3.11+ on the lab PC** (recommended and validated Jul 2026).

| Component | Package / DLL | 64-bit Python | 32-bit Python (legacy) |
|-----------|---------------|:---:|:---:|
| Broadcom Qmini | `pyrgbdriverkit` + PyUSB + `libusb-package` | ✅ **active** | not needed |
| Spectrum AWG | `spcm_win64.dll` / `spcm_win32.dll` | ✅ | ✅ |
| numpy, PyYAML, matplotlib | pip | ✅ | ✅ |
| Legacy WAVES path | `RGBDriverKit.dll` + pythonnet | ⚠️ 0 devices | ✅ (archived code) |

```cmd
python -c "import struct, platform; print(platform.python_version(), struct.calcsize('P')*8, 'bit')"
REM Want: 64 bit
```

### Qmini notes (NioLink / current stack)

| Topic | Detail |
|-------|--------|
| Driver | Vendored **`pyrgbdriverkit` 0.3.7** under `hardware/spectrometer/vendor/` |
| USB access | **`pip install libusb-package`** — PyUSB needs bundled `libusb-1.0.dll` on Windows |
| Device class | `Qseries.search_devices()` (vendor USB `0x276E`) |
| WAVES lock | Close **WAVES / `Waves.exe`** before Python |
| macOS | Same NioLink path for spectrometer-only; no Spectrum AWG on Mac |

<details>
<summary>Legacy Qmini notes (WAVES / pythonnet — archived Jul 2026)</summary>

| Topic | Detail |
|-------|--------|
| DLL | WAVES installs **`RGBDriverKit.dll`** under `C:\Program Files (x86)\Broadcom\Waves\` |
| Bitness | **32-bit Python required** |
| Archived code | `_broadcom_sdk.py`, `_broadcom_qmini.py` |

</details>

---

## Setup (one-time)

### 1. Python (lab PC — 64-bit)

```cmd
cd C:\path\to\Optical-Pulse-Shaping
python -m virtualenv .venv64
.venv64\Scripts\activate.bat
python -m pip install -r requirements.txt
python -c "import struct; print(struct.calcsize('P')*8, 'bit')"
```

### 2. Spectrum AWG

1. Install [Spectrum Windows driver](https://spectrum-instrumentation.com/products/drivers_examples/win_driver.php).
2. Connect **Sonnet Echo III** via Thunderbolt 3.
3. Verify: `python run_experiment.py --check-awg`

### 3. Broadcom Qmini (NioLink)

1. Install **Broadcom WAVES** for the USB driver (close Waves.exe during Python runs).
2. `pip install pyusb libusb-package` (in `requirements.txt`).
3. Verify: `python run_experiment.py --check-spec`

### 4. Config

Edit `config/default_experiment.yaml` — see [Configuration reference](#configuration-reference). Defaults: double-pulse wf1 (τ=1), wf2 (τ=2), `repeats: 5` each (MEMSIZE tiling), Qmini backend enabled.

---

## Running experiments — all commands

### Interactive AWG prompts

Unless `--no-prompt`, each experiment run asks for trigger (`external` / `software`), peak voltage (mV), and sampling rate (MSa/s). Checks skip prompts.

```cmd
python run_experiment.py --awg spectrum --spec nio_qmini
python run_experiment.py --awg spectrum --spec nio_qmini --no-prompt
python run_experiment.py --awg spectrum --spec nio_qmini --no-wait
```

Default: **collect spectra until Ctrl+C**, then **Ext0-locked RF hold** until a **second Ctrl+C**. `--no-wait` = one spectrum cycle, then the same RF hold.

### Pre-flight / dry / partial / full

```cmd
python run_experiment.py --check-awg
python run_experiment.py --check-spec
python run_experiment.py --dry-run
python run_experiment.py --awg dummy --spec dummy
python run_experiment.py --awg spectrum --spec nio_qmini
```

### AWG-only demos

```cmd
python main.py            REM edit TRIGGER_MODE / LOOPS / T at top of file
python multi_pulse.py     REM edit TAU_LIST for MULTI tau scan
```

### What happens during a real `run_experiment` run

| Step | Action |
|------|--------|
| [1/4] | Build waveform library `V(t)` from YAML masks |
| [2/4] | Open Spectrum AWG; configure SR, trigger, buffer |
| [3/4] | Open Qmini via `nio_qmini`; cache `wavelengths_nm.npy` |
| [4/4] | Spectrum cycle loop: for each block, MEMSIZE = segment×`repeats`, `SPC_LOOPS=0` (unless `acquire_loops>0`) → settle → average → save |
| RF hold | Re-arm last waveform (`run_until_interrupt`) until **second Ctrl+C** |
| Exit | Stop RF; leave AWG + Qmini **dormant** (not closed) |

---

## Saved data and plotting

### Run folder layout

```
data/experiments/<name>_<YYYYMMDD_HHMMSS>/
├── run_metadata.json
├── time_axis_us.npy
├── wavelengths_nm.npy
├── waveform_wf1.npy
├── waveform_wf2.npy
├── played_c0000_block0_wf1.npy
├── spectrum_c0000_block0_wf1.npy
├── …
└── run_summary.png
```

Older one-shot runs used names without the `cNNNN_` prefix. Both layouts are readable by `plot_run.py`.

### Load / plot

```python
import numpy as np
run = "data/experiments/double_pulse_demo_20260717_221325"
spec = np.load(f"{run}/spectrum_c0000_block0_wf1.npy")
vt   = np.load(f"{run}/waveform_wf1.npy")
wl   = np.load(f"{run}/wavelengths_nm.npy")
```

```cmd
python plot_run.py
python plot_run.py data/experiments/double_pulse_demo_...
python plot_run.py --save --no-show
```

---

## Validated real runs (what success looks like)

### Full stack (64-bit AWG + Qmini) — **`double_pulse_demo_20260706_224225`**

| Field | Value |
|-------|-------|
| Python | **64-bit** (`.venv64`) |
| AWG | **`spectrum`** (`/dev/spcm0`) |
| Spectrometer | **`nio_qmini`** |
| Schedule | 10× wf1 (τ=1), 10× wf2 (τ=2) |
| Frames averaged | 50 per block |
| Wavelength axis | 2018 px, **454–1112 nm** |
| Waveform segment | 12512 samples, 10 µs @ 1250 MSa/s |

### Continuous collect + external trigger — **`double_pulse_demo_20260717_221325`**

| Field | Value |
|-------|-------|
| AWG | **`spectrum`**, `trigger_mode: external` |
| Spectrometer | **`nio_qmini`**, `settle_delay_ms: 10` |
| Mode | Continuous spectrum cycles until Ctrl+C |
| Cycles completed | **3** |

Current runner adds a post-collect **RF hold** until a second Ctrl+C; that hold was not part of this Jul 2026 folder.

### Spectrometer-only — **`double_pulse_demo_20260706_214505`**

64-bit, `nio_qmini`, dummy AWG — 2018 px, 454–1112 nm.

### Legacy stack (32-bit WAVES) — **`double_pulse_demo_20260702_224342`**

`broadcom_qmini` via pythonnet — archived path.

### Interpreting spectra

| Observation | Meaning |
|-------------|---------|
| Real nm axis, non-Gaussian shape | ✅ Qmini working |
| Flat baseline, edge noise | Usually **no laser / misaligned optics** |
| Distinct V(t) on plot right panel | ✅ Masks differ (τ) — AWG path working |

---

## Hardware requirements

| Component | Connection | Notes |
|-----------|------------|-------|
| Spectrum M4i.66xx AWG | PCIe in Sonnet Echo III | `/dev/spcm0` |
| Sonnet Echo III | Thunderbolt 3 to lab PC | TB port + cable required |
| Broadcom Qmini | USB Type-C | NioLink / PyUSB; close WAVES first |
| SRS DG645 | TTL → Spectrum Ext0 | `trigger_mode: external` or MULTI demos |
| Lab PC | TB + USB, **64-bit Python** | `--awg spectrum --spec nio_qmini` |

Oscilloscope and DG645 are **not** programmed by this software.

---

## What we have achieved

| Milestone | Status |
|-----------|--------|
| YAML-driven experiment config | ✅ |
| Vectorized waveform library | ✅ |
| Spectrum AWG on lab PC | ✅ |
| Qmini via NioLink / PyUSB (64-bit) | ✅ Jul 2026 |
| Full AWG + Qmini on 64-bit Python | ✅ `double_pulse_demo_20260706_224225` |
| Ext0 SINGLERESTART (`SPC_LOOPS=0`, once per edge forever) | ✅ `main.py` / runner |
| Internal sample-clock periods (no DG645) | ✅ `main.py` `TRIGGER_MODE=software` |
| Ext0 MULTI (different segment per edge) | ✅ `multi_pulse.py` / `play_multi_voltage_arrays` |
| Continuous spectrum collect + RF hold | ✅ |
| Lab calibration from `.txt` files | Not started |
| MULTI in YAML / `run_experiment` | Not yet |
| FROG integration | Out of scope v1 |

---

## Configuration reference

File: `config/default_experiment.yaml`

### AWG

```yaml
awg:
  sampling_rate_MSa_s: 1250
  segment_duration_us: 10         # main.py T — one library segment (µs)
  voltage_max_mV: 2000
  backend: dummy                  # use --awg spectrum for real card
  device: /dev/spcm0
  trigger_mode: external          # or software / internal
  acquire_loops: 0                # 0 = SPC_LOOPS 0 (once/Ext0 forever); >0 = stop after N
  playback_mode: per_block
  inter_segment_gap_us: 0

spectrometer:
  settle_delay_ms: 10
```

**`schedule.repeats` vs `SPC_LOOPS` (important)**

| Knob | What it does **now** |
|------|----------------------|
| `schedule.repeats` | Tiles the library segment into **MEMSIZE** (e.g. 5×10 µs = 50 µs RF per Trg0) |
| `acquire_loops: 0` | Driver `SPC_LOOPS=0` — one MEMSIZE per Ext0, forever (Table 57 / correct pulsed mode) |
| `acquire_loops: N>0` | Explicit `SPC_LOOPS=N` — card stops after N total plays |

Do **not** treat `repeats` as `SPC_LOOPS`. Mapping repeats→loops was the old wrong model.

**Wire:** DG645 TTL → coax → Spectrum Ext0/Trg0. Example DG645 period **1 ms** is fine for default 10 µs (or tiled) segments.

### Waveforms / schedule

```yaml
waveforms:
  - id: wf1
    carrier_freq_MHz: 100
    amplitude_mask: { type: double_pulse, R: 1.0, w0: 0.0, tau: 1.0, phi: 0.0 }
  - id: wf2
    carrier_freq_MHz: 100
    amplitude_mask: { type: double_pulse, R: 1.0, w0: 0.0, tau: 2.0, phi: 0.0 }

schedule:
  - { waveform_id: wf1, repeats: 5 }   # MEMSIZE = 5 × segment
  - { waveform_id: wf2, repeats: 5 }
```

**Mask types** — Phase: `constant`, `taylor_series`. Amplitude: `constant`, `gaussian`, `delayed_pulse`, `double_pulse`, `multi_gaussian`.

---

## Roadmap

1. Lab calibration — load `t→ω` from `.txt` files
2. Mask parameter sweeps — automate τ, chirp across runs
3. Expose MULTI / tau lists in YAML + `run_experiment`
4. Equalizers / corrections in `ps_calculations.py`
5. Per-block timing printout for PI speed comparison
6. Clean up stale runner print strings that still say “repeats → SPC_LOOPS”

---

## Troubleshooting

### AWG

| Symptom | Fix |
|---------|-----|
| DLL FAIL | Reinstall Spectrum driver; match Python bitness to DLL |
| DLL OK, no cards | Echo III TB connected; close SBench 6 |
| Run hangs with `trigger_mode: external` | Wire DG645 **TTL → Trg0**; Ext0 rising @ 1.5 V is set by the driver |
| Continuous RF / ignores Ext0 | Usually wrong mode: software free-run of pure RF, or Python WAITREADY re-arm loop. Use Ext0 SINGLERESTART + `SPC_LOOPS=0`, arm once |
| Scope walks L→R | Trigger scope on **DG645/X0**, not on 100 MHz Ch0 carrier |
| Swapped `SINGLERESTART`→`MULTI` in `setup_card` and it broke | Expected — use `python multi_pulse.py` / `play_multi_voltage_arrays` instead |
| Want only one spectrum cycle | Pass `--no-wait` |
| RF still on after first Ctrl+C | Expected — second Ctrl+C ends RF hold |
| `acquire_loops: 0` | Correct pulsed forever mode — **not** free-run CW when card mode is SINGLERESTART |

### Qmini (NioLink)

| Symptom | Fix |
|---------|-----|
| `NoBackendError` | `pip install libusb-package` |
| No devices found | Close WAVES + kill `Waves.exe` |
| Weak / flat spectra | Optics/laser — increase `integration_time_ms`, align bench |

### Data / plotting

| Symptom | Fix |
|---------|-----|
| Smooth Gaussian spectra | Using `--spec dummy`; use `nio_qmini` |
| Flat real spectra | Optics/laser alignment |

---

## References

- Spectrum driver: [spectrum-instrumentation.com](https://spectrum-instrumentation.com/products/drivers_examples/win_driver.php)
- Broadcom Qmini / NioLink: vendored `pyrgbdriverkit` (RGB Photonics)
- Legacy WAVES / RGBDriverKit: archived in repo (see Architecture history)
- Sonnet Echo III: [sonnettech.com](https://www.sonnettech.com/)
- PI architecture: *Optical Pulse Shaping.pdf* (page 4)
- Spectrum manuals: `Manuals & Documents/`

---

## License / attribution

Lab research code. Spectrum © Spectrum Instrumentation GmbH; pyrgbdriverkit © RGB Photonics GmbH.
