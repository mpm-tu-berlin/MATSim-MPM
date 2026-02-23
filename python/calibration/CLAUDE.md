# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Calibration system for heavy-duty vehicle (HDV) simulation: uses **Optuna** to optimize VECTO vehicle parameters so that a coupled **VECTO → MATSim** simulation pipeline matches reference data. The project is in early development — the optimization scaffold is in place but VECTO execution, MATSim integration, and error computation are not yet implemented.

## Setup & Commands

```bash
# Create/activate virtual environment
python -m venv .venv
.venv/Scripts/activate        # Windows

# Install dependencies
pip install -r requirements.txt
```

There are no tests, linter config, or build steps yet. The main entry point would be:
```python
import optuna
from src.objective import objective
from src.config import STUDY_NAME, N_TRIALS, STORAGE

study = optuna.create_study(storage=STORAGE, study_name=STUDY_NAME)
study.optimize(objective, n_trials=N_TRIALS)
```

## Architecture

```
Optuna (objective.py)
  ├── sample parameters from PARAM_BOUNDS (config.py)
  ├── run_vecto(params)         → vecto_wrapper.py  [NOT IMPLEMENTED]
  ├── run_matsim(vecto_result)  → matsim_runner.py   [subprocess → Java JAR]
  └── compute_error(matsim_out) → vecto_wrapper.py  [NOT IMPLEMENTED]
```

**Pipeline per trial:** Optuna samples 5 vehicle parameters → VECTO simulates emissions → MATSim runs traffic simulation → error metric (RMSE) compared against reference data → Optuna minimizes.

## Key Modules

- **`src/config.py`** — All paths, constants, and parameter bounds. Single source of truth for MATSIM_JAR, VECTO_EXECUTABLE paths, Optuna settings, and the 5 calibration parameter ranges.
- **`src/objective.py`** — Optuna objective function orchestrating the full pipeline.
- **`src/vecto_wrapper.py`** — VECTO subprocess wrapper and error computation (both TODO).
- **`src/matsim_runner.py`** — Spawns MATSim as `java -jar` subprocess; output goes to `results/matsim_runs/{run_id}/`.

## External Dependencies

- **MATSim** — Java-based traffic simulator, invoked via JAR at `data/matsim.jar`
- **VECTO** — Windows executable at `data/vecto/VECTO.exe` for vehicle emission simulation
- **Optuna** stores trials in SQLite at `results/optuna_study.db`

## Data

- `data/hdv_2023_missionprofile/hdv_2023_missionprofile.csv` — HDV mission profiles (~875 MB), semicolon-separated, 85+ columns including vehicle specs, fuel consumption, and CO2 emissions.

## Conventions

- Code comments and docstrings are in **German**.
- Imports use `from src.config import ...` (not relative imports).
- Windows-only: VECTO is a native Windows executable; MATSim runs cross-platform via Java.
