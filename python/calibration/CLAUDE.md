# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Calibration system for heavy-duty vehicle (HDV) simulation: uses **Optuna** to optimize vehicle parameters so that MATSim energy consumption matches VECTO reference data. Active vehicle group: `BET_G5`.

## Setup & Commands

```bash
# Create/activate virtual environment
python -m venv .venv
.venv/Scripts/activate        # Windows

# Install dependencies
pip install -r requirements.txt
```

## Einstiegspunkte

```bash
# Vollständige Optuna-Kalibrierung (alle Studien, alle Szenarien)
.venv/Scripts/python run_optimization.py

# Einzelner Testlauf mit Standardparametern (Verifikation)
.venv/Scripts/python run_test.py

# Sensitivitätsanalyse: Einfluss jedes Parameters
.venv/Scripts/python run_sensitivity.py
```

## Ordnerstruktur

```
python/calibration/
├── run_optimization.py     ← Haupteinstieg: Optuna-Kalibrierung
├── run_test.py             ← Einzeltestlauf zur Verifikation
├── run_sensitivity.py      ← Parametereinfluss-Analyse
├── requirements.txt
│
├── src/                    ← Kernlogik (importierbar)
│   ├── config.py           ← Alle Pfade, Konstanten, Parameterbereiche (Single Source of Truth)
│   ├── objective.py        ← Optuna-Zielfunktion
│   ├── matsim_runner.py    ← MATSim-Subprocess-Steuerung
│   ├── error_computation.py← RMSE/MAE vs. VECTO-Referenz
│   ├── convert_vdri_to_network.py ← VDRI→MATSim-Netzwerk-Konverter (modular)
│   ├── cdxa_mapping.py     ← EU CdxA-Luftwiderstandsklassen
│   └── vecto_wrapper.py    ← VECTO-Subprocess (TODO: nicht implementiert)
│
├── analysis/               ← Visualisierungs- und Analyseskripte (interaktiv)
│   ├── analyse_resistance.py      ← Energiebilanz + Speed-Profil vs. VECTO (Hauptanalyse)
│   ├── speed_comparison.py        ← Nur Geschwindigkeitsvergleich LH (schnell)
│   ├── plot_network_profiles.py   ← Höhenprofile VECTO vs. MATSim-Netz
│   └── analyze_network_grades.py  ← Steigungsverteilung + maxGradeAbs-Sensitivität
│
├── scripts/                ← Einmalige Utility-Skripte
│   ├── generate_vecto_networks.py ← Netzwerke erzeugen (--resolution 1|100|250|all)
│   ├── extract_vehicle_excel.py
│   └── gruppe5_pev_axle.py
│
├── data/                   ← Eingabedaten
│   ├── reference_consumption.csv  ← VECTO EEA 2023 Referenzwerte
│   ├── LongHaul.vdri              ← VECTO Long-Haul-Fahrzyklus
│   └── RegionalDelivery.vdri      ← VECTO Regional-Delivery-Fahrzyklus
│
├── notebooks/
│   └── explore_mission_profiles.ipynb
│
└── results/                ← Laufzeit-Output (nicht im Git)
    ├── runs/YYYYMMDD_HHMMSS/   ← Optuna-Laufverzeichnisse
    └── *.html                  ← Analyse-Plots
```

## Architecture

```
Optuna (objective.py)
  ├── sample 4 Parameter aus PARAM_BOUNDS (config.py)
  ├── run_all_scenarios(params) → matsim_runner.py
  │     ├── schreibt trial_{n}_params.properties
  │     └── startet LongHaul + RegionalDelivery parallel (ThreadPoolExecutor)
  └── compute_combined_errors(outputs) → error_computation.py
        └── RMSE in % vs. VECTO-Referenzwerte
```

**Pipeline pro Trial:** Optuna sampelt 4 Parameter → MATSim läuft für alle Szenarien → SoC-Differenz aus `individual_charge_time_profiles.txt` → RMSE vs. VECTO → Optuna minimiert.

## Key Modules

- **`src/config.py`** — Single Source of Truth: MATSIM_MPM_DIR (relativ), Fahrzeuggruppen,
  Szenariokonfigurationen, PARAM_BOUNDS, Studien-Definitionen.
- **`src/objective.py`** — Optuna-Zielfunktion (4 Parameter → RMSE %).
- **`src/matsim_runner.py`** — Startet MATSim als `java -jar`, parallele Szenarien via ThreadPoolExecutor.
- **`src/error_computation.py`** — Liest SoC-Profile, berechnet RMSE/MAE pro Fahrzeug und Szenario.
- **`src/convert_vdri_to_network.py`** — Konvertiert .vdri → MATSim-Netzwerk (modular, mit Segment-Aggregation).

## Netzwerkgenerierung

```bash
# Alle drei Auflösungen erzeugen (1m, 100m, 250m)
.venv/Scripts/python scripts/generate_vecto_networks.py

# Nur eine Auflösung:
.venv/Scripts/python scripts/generate_vecto_networks.py --resolution 250
```

QSim-Zeitdiskretisierung (0.04s Zeitschritte) erfordert Mindest-Segmentlänge:
- 100m bei 83 km/h → Zeitfehler < 1 %
- 250m bei 83 km/h → Zeitfehler < 0.2 %  ← Standard für Kalibrierung

## Analyse-Workflow

```bash
# 1. MATSim mit Debug-CSV laufen lassen (resistance_debug.csv im Arbeitsverzeichnis)
# 2. Dateien umbenennen: resistance_debug_lh.csv, resistance_debug_rd.csv
# 3. Analyse starten:
.venv/Scripts/python analysis/analyse_resistance.py resistance_debug_lh.csv resistance_debug_rd.csv
# → Öffnet PowerShell-Dateiauswahl, erzeugt results/speed_profile.html
```

## Kalibrierungsparameter (PARAM_BOUNDS)

| Parameter           | Bereich        | Bedeutung                            |
|---------------------|----------------|--------------------------------------|
| tractionEfficiency  | [0.80, 0.90]   | Batterie→Rad-Effizienz bei Traktion  |
| inertiaC            | [1.01, 1.05]   | Trägheitsbeiwert (rotierende Massen) |
| recupEfficiency     | [0.45, 0.85]   | Rekuperations-Wirkungsgrad           |
| auxPowerW           | [4000, 5000]   | Konstante Nebenverbrauchsleistung    |

## Conventions

- Code comments and docstrings are in **German**.
- Imports use `from src.config import ...` (not relative imports).
- Analysis scripts add `sys.path.insert(0, ...)` to find the `src` package.
- Windows-only: VECTO is a native Windows executable; MATSim runs cross-platform via Java.
- `MATSIM_MPM_DIR` in config.py is computed relative to this project's location — no hardcoded user paths.