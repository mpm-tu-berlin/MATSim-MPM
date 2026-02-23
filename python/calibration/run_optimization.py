"""
Optuna-Kalibrierungslauf: optimiert 4 Fahrzeugparameter ueber N_TRIALS Trials.

Aufruf:
    .venv/Scripts/python run_optimization.py

Jeder Lauf legt einen neuen Ordner unter results/runs/YYYYMMDD_HHMMSS/ an.
Darin liegen: optuna_study.db, optimization.log, matsim_runs/, optuna_plots/.
"""

import datetime
import sys

import optuna
import optuna.visualization as vis

# === 1. Timestamped Run-Verzeichnis anlegen ===
import src.config as _cfg  # Modul-Referenz fuer spaeteres Monkey-Patching

_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_DIR = _cfg.RESULTS_DIR / "runs" / _timestamp
RUN_DIR.mkdir(parents=True, exist_ok=True)

# Monkey-Patch: alle Laufzeit-Zugriffe in matsim_runner etc. landen im RUN_DIR
_cfg.RESULTS_DIR = RUN_DIR
_cfg.CALIBRATION_PARAMS_FILE = RUN_DIR / "calibration_params.properties"

# Laufzeit-lokale Werte (werden nach dem Patch benoetigt)
STUDY_NAME = _cfg.STUDY_NAME
N_TRIALS = _cfg.N_TRIALS
STORAGE = f"sqlite:///{RUN_DIR / 'optuna_study.db'}"


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


_log_path = RUN_DIR / "optimization.log"
_log_file = open(_log_path, "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_file)

print(f"Run-Verzeichnis: {RUN_DIR}")
print(f"Log:             {_log_path}")
print(f"Storage:         {STORAGE}")

# === 3. Optuna-Imports (nach dem Patch, damit config-Werte korrekt sind) ===
from src.objective import objective  # noqa: E402  (Import nach Monkey-Patch)

optuna.logging.set_verbosity(optuna.logging.WARNING)  # Optuna-Spam unterdruecken

# === 4. Study starten (immer neu, da jeder Run einen eigenen Ordner hat) ===
study = optuna.create_study(
    study_name=STUDY_NAME,
    storage=STORAGE,
    direction="minimize",
    load_if_exists=False,
)

print(f"\nStarte neue Optimierung: {N_TRIALS} Trials\n")
study.optimize(objective, n_trials=N_TRIALS)

# === 5. Abschlussbericht ===
best = study.best_trial
print()
print("=== Optimierung abgeschlossen ===")
print(f"Bester Trial: #{best.number}")
print("Parameter:")
for name, value in best.params.items():
    print(f"  {name} = {value:.6f}")
print(f"Bester RMSE: {best.value:.6f} kWh/km")

# === 6. Optuna-Visualisierungen ===
print()
print("Erstelle Optuna-Grafiken ...")

PLOTS_DIR = RUN_DIR / "optuna_plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

_plots = [
    ("optimization_history",  vis.plot_optimization_history),
    ("param_importances",     vis.plot_param_importances),
    ("parallel_coordinate",   vis.plot_parallel_coordinate),
    ("contour",               vis.plot_contour),
    ("slice",                 vis.plot_slice),
    ("edf",                   vis.plot_edf),
    ("timeline",              vis.plot_timeline),
]

gespeichert = []
for name, func in _plots:
    try:
        fig = func(study)
        pfad = PLOTS_DIR / f"{name}.html"
        fig.write_html(str(pfad))
        gespeichert.append(pfad)
    except Exception as e:
        print(f"  Warnung: '{name}' konnte nicht erstellt werden: {e}")

print(f"\nGrafiken gespeichert in: {PLOTS_DIR}")
for pfad in gespeichert:
    print(f"  {pfad}")

# Log-Datei schliessen
_log_file.flush()
_log_file.close()
