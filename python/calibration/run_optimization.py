"""
Optuna-Kalibrierungslauf: optimiert Fahrzeugparameter separat fuer jede
Beladungsklasse (low / high).

Aufruf:
    .venv/Scripts/python run_optimization.py

Jeder Lauf legt einen neuen Ordner an:
    results/runs/YYYYMMDD_HHMMSS/
        optimization.log          <- gemeinsames Log beider Klassen
        low/
            optuna_study.db
            optuna_plots/
            matsim_runs/
        high/
            optuna_study.db
            optuna_plots/
            matsim_runs/
"""

import datetime
import sys

import optuna
import optuna.visualization as vis

import src.config as _cfg  # Modul-Referenz fuer spaeteres Monkey-Patching

# === 1. Basisverzeichnis fuer diesen Lauf ===
_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_BASE_RESULTS = _cfg.RESULTS_DIR        # Original-Pfad sichern
RUN_BASE = _BASE_RESULTS / "runs" / _timestamp
RUN_BASE.mkdir(parents=True, exist_ok=True)


# === 2. Tee: stdout gleichzeitig auf Terminal und Log-Datei ===
class _Tee:
    """Schreibt alle Ausgaben gleichzeitig in mehrere Streams."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


_log_path = RUN_BASE / "optimization.log"
_log_file = open(_log_path, "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_file)

# === 3. Imports nach Tee-Setup (damit erste Prints ins Log gehen) ===
from src.objective import objective          # noqa: E402
from src.error_computation import format_final_report  # noqa: E402
from src.config import (                     # noqa: E402
    PAYLOAD_CLASSES, N_TRIALS, N_JOBS, SCENARIOS,
    ACTIVE_VEHICLE_GROUP,
)

optuna.logging.set_verbosity(optuna.logging.WARNING)

print(f"Run-Verzeichnis:     {RUN_BASE}")
print(f"Log:                 {_log_path}")
print(f"Fahrzeuggruppe:      {ACTIVE_VEHICLE_GROUP}")
print(f"Beladungsklassen:    {PAYLOAD_CLASSES}")
print(f"Trials je Klasse:    {N_TRIALS}  (n_jobs={N_JOBS})")
print(f"RAM-Bedarf je Klasse:{N_JOBS} x 2 x {_cfg.MATSIM_MEMORY}")

# === 4. Schleife ueber Beladungsklassen ===
for payload_class in PAYLOAD_CLASSES:
    print(f"\n{'=' * 65}")
    print(f"  Beladungsklasse: {payload_class.upper()}")
    print(f"{'=' * 65}\n")

    # Klassen-spezifisches Unterverzeichnis
    class_dir = RUN_BASE / payload_class
    class_dir.mkdir(parents=True, exist_ok=True)

    # Monkey-Patch: Laufzeit-Konfiguration fuer diese Klasse setzen
    _cfg.RESULTS_DIR = class_dir
    _cfg.ACTIVE_PAYLOAD_CLASS = payload_class

    storage = f"sqlite:///{class_dir / 'optuna_study.db'}"
    study_name = f"matsim-vecto-{ACTIVE_VEHICLE_GROUP}-{payload_class}"

    print(f"Storage: {storage}")
    print(f"Starte {N_TRIALS} Trials ...\n")

    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        direction="minimize",
        load_if_exists=False,
    )
    study.optimize(objective, n_trials=N_TRIALS, n_jobs=N_JOBS)

    # --- Abschlussbericht ---
    best = study.best_trial
    best_outputs = {
        name: class_dir / "matsim_runs" / f"trial_{best.number}_{name}"
        for name in SCENARIOS
    }
    print()
    print(format_final_report(best_outputs, best.number, best.params))
    print(f"Bester RMSE ({payload_class}): {best.value:.2f}%")

    # --- Optuna-Visualisierungen ---
    print(f"\nErstelle Optuna-Grafiken ...")
    plots_dir = class_dir / "optuna_plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    _plots = [
        ("optimization_history", vis.plot_optimization_history),
        ("param_importances",    vis.plot_param_importances),
        ("parallel_coordinate",  vis.plot_parallel_coordinate),
        ("contour",              vis.plot_contour),
        ("slice",                vis.plot_slice),
        ("edf",                  vis.plot_edf),
        ("timeline",             vis.plot_timeline),
    ]
    for name, func in _plots:
        try:
            fig = func(study)
            fig.write_html(str(plots_dir / f"{name}.html"))
        except Exception as e:
            print(f"  Warnung: '{name}' konnte nicht erstellt werden: {e}")

    print(f"Grafiken gespeichert in: {plots_dir}")

# === 5. Abschluss ===
print(f"\n{'=' * 65}")
print(f"  Optimierung abgeschlossen. Ergebnisse: {RUN_BASE}")
print(f"{'=' * 65}")

_log_file.flush()
_log_file.close()
