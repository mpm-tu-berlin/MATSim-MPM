"""
Optuna-Kalibrierungslauf: optimiert Fahrzeugparameter separat fuer jede
Studie (lh_low, lh_high, rd_low, rd_high, all).

Aufruf:
    .venv/Scripts/python run_optimization.py                       # Default 250m, alle Studies
    .venv/Scripts/python run_optimization.py --resolution 1        # 1m-Baseline
    .venv/Scripts/python run_optimization.py --resolution 1 --study-name lh_low --n-trials 10
        # Smoke-Test: nur eine Study, wenige Trials, schnelle End-to-End-Verifikation

Jeder Lauf legt einen neuen Ordner an:
    results/runs/YYYYMMDD_HHMMSS_<N>m/
        optimization.log          <- gemeinsames Log aller Studien
        lh_low/
            optuna_study.db
            optuna_plots/
            matsim_runs/
        lh_high/
            ...
        rd_low/
            ...
        rd_high/
            ...
        all/
            optuna_study.db
            optuna_plots/
            matsim_runs/
"""

import argparse
import datetime
import shutil
import sys

import optuna
import optuna.visualization as vis

import src.config as _cfg  # Modul-Referenz fuer spaeteres Monkey-Patching

# === 0. CLI: Netzauflösung waehlen, dazu passend RAM/N_JOBS monkey-patchen ===
_parser = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
_parser.add_argument(
    "--resolution",
    type=int,
    default=_cfg.ACTIVE_RESOLUTION_M,
    help=(f"Netzauflösung in Metern fuer alle Studien dieses Laufs. "
          f"Default: {_cfg.ACTIVE_RESOLUTION_M}."),
)
_parser.add_argument(
    "--n-trials",
    type=int,
    default=None,
    help=f"Trials pro Study (uebersteuert N_TRIALS={_cfg.N_TRIALS}).",
)
_parser.add_argument(
    "--study-name",
    type=str,
    default=None,
    help=("Nur bestimmte Studien laufen, komma-separiert. Beispiele: 'lh_low' "
          "(Smoke-Test) oder 'lh_low,lh_high,rd_low,rd_high' (alle 4 Einzel-"
          "szenarien in EINEM Run-Ordner -> direkt vom Sweep nutzbar)."),
)
_parser.add_argument(
    "--fixed-params",
    type=str,
    default=None,
    help=("Kommaliste key=value fester Zusatzparameter (werden in jede Trial-"
          ".properties uebernommen, nicht optimiert), z. B. "
          "rollingLoadExponent=0.9,rollingRefMassKg=35500,airDensity=1.188"),
)
_args = _parser.parse_args()

if _args.fixed_params:
    _cfg.FIXED_PARAMS = {k.strip(): float(v) for k, v in
                         (kv.split("=") for kv in _args.fixed_params.split(","))}
    print(f"Feste Zusatzparameter: {_cfg.FIXED_PARAMS}")
_cfg.ACTIVE_RESOLUTION_M = _args.resolution
_cfg.MATSIM_MEMORY, _cfg.N_JOBS = _cfg.resource_profile_for(_args.resolution)
if _args.n_trials is not None:
    _cfg.N_TRIALS = _args.n_trials
if _args.study_name is not None:
    _requested = [n.strip() for n in _args.study_name.split(",") if n.strip()]
    _by_name = {s["name"]: s for s in _cfg.STUDIES}
    _unknown = [n for n in _requested if n not in _by_name]
    if _unknown:
        raise SystemExit(
            f"Unbekannte Study/Studien {_unknown}. "
            f"Verfuegbar: {list(_by_name)}"
        )
    _cfg.STUDIES = [_by_name[n] for n in _requested]  # Reihenfolge wie angefragt

# === 1. Basisverzeichnis fuer diesen Lauf ===
_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_BASE_RESULTS = _cfg.RESULTS_DIR        # Original-Pfad sichern
RUN_BASE = _BASE_RESULTS / "runs" / f"{_timestamp}_{_args.resolution}m"
RUN_BASE.mkdir(parents=True, exist_ok=True)


# === 2. Tee: stdout gleichzeitig auf Terminal und Log-Datei ===
class _Tee:
    """Schreibt alle Ausgaben gleichzeitig in mehrere Streams."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except ValueError:
                pass  # Stream wurde bereits geschlossen (Interpreter-Shutdown)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except ValueError:
                pass


_log_path = RUN_BASE / "optimization.log"
# buffering=1 -> zeilengepuffert: jede Zeile landet sofort auf der Platte,
# auch wenn der Lauf spaeter haengt/abgebrochen wird (vorher: leeres Log).
_log_file = open(_log_path, "w", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _log_file)

# === 3. Imports nach Tee-Setup (damit erste Prints ins Log gehen) ===
from src.objective import objective          # noqa: E402
from src.error_computation import format_final_report  # noqa: E402
from src.config import (                     # noqa: E402
    STUDIES, N_TRIALS,
    ACTIVE_VEHICLE_GROUP, VEHICLE_GROUPS,
)

optuna.logging.set_verbosity(optuna.logging.WARNING)

# Alle Szenarien der aktiven Fahrzeuggruppe sichern
_all_scenarios = VEHICLE_GROUPS[ACTIVE_VEHICLE_GROUP]

print(f"Run-Verzeichnis:     {RUN_BASE}")
print(f"Log:                 {_log_path}")
print(f"Fahrzeuggruppe:      {ACTIVE_VEHICLE_GROUP}")
print(f"Netzauflösung:       {_cfg.ACTIVE_RESOLUTION_M} m")
print(f"Studien:             {[s['name'] for s in STUDIES]}")
print(f"Trials je Studie:    {N_TRIALS}  (n_jobs={_cfg.N_JOBS})")
print(f"RAM-Bedarf je Studie:{_cfg.N_JOBS} x Szenarien x {_cfg.MATSIM_MEMORY}")

# === 4. Schleife ueber Studien ===
for study in STUDIES:
    study_name_str = study["name"]
    print(f"\n{'=' * 65}")
    print(f"  Studie: {study_name_str.upper()}")
    print(f"  Szenarien: {study['scenarios']}  Payload: {study['payload_class']}")
    print(f"{'=' * 65}\n")

    # Studien-spezifisches Unterverzeichnis
    class_dir = RUN_BASE / study_name_str
    class_dir.mkdir(parents=True, exist_ok=True)

    # Monkey-Patch: nur die aktiven Szenarien und Payload-Klasse setzen
    _cfg.RESULTS_DIR = class_dir
    _cfg.ACTIVE_PAYLOAD_CLASS = study["payload_class"]
    _cfg.SCENARIOS = {name: _all_scenarios[name] for name in study["scenarios"]}

    # Pro Trial laufen len(SCENARIOS) MATSim-JVMs parallel; bei hoher Heap-Groesse
    # (sub-50m) deckeln wir die Optuna-Parallelitaet so, dass total_jvms = n_jobs *
    # n_scenarios <= ursprueglich gewuenschtes N_JOBS bleibt. Verhindert OOM
    # speziell in der "all"-Study bei 1m (sonst 4 x 8 GB = 32 GB).
    _n_scenarios = len(_cfg.SCENARIOS)
    n_jobs_study = min(_cfg.MAX_PARALLEL_TRIALS_PER_STUDY,
                       max(1, _cfg.N_JOBS // _n_scenarios))

    storage = f"sqlite:///{class_dir / 'optuna_study.db'}"
    optuna_study_name = f"matsim-vecto-{ACTIVE_VEHICLE_GROUP}-{study_name_str}"

    print(f"Storage: {storage}")
    print(f"Starte {N_TRIALS} Trials (n_jobs={n_jobs_study} fuer {_n_scenarios} Szenarien/Trial) ...\n")

    study_obj = optuna.create_study(
        study_name=optuna_study_name,
        storage=storage,
        direction="minimize",
        load_if_exists=False,
    )

    _study_t0 = datetime.datetime.now()

    def _progress(study, _trial, _name=study_name_str, _t0=_study_t0):
        """Fortschrittsuebersicht nach jedem fertigen Trial (laeuft im Main-Thread)."""
        done = sum(1 for t in study.trials
                   if t.state == optuna.trial.TrialState.COMPLETE)
        try:
            best = study.best_value
        except ValueError:
            best = float("nan")
        elapsed = (datetime.datetime.now() - _t0).total_seconds()
        rate = done / elapsed * 60 if elapsed > 0 else 0.0
        eta_min = (N_TRIALS - done) / rate if rate > 0 else float("nan")
        print(f"  >>> [{_name}] {done}/{N_TRIALS} fertig | bester RMSE={best:.2f}% | "
              f"{rate:.1f} Trials/min | ETA ~{eta_min:.0f} min", flush=True)

    # catch: ein einzelner fehlgeschlagener Trial (z.B. MATSim-Crash bei
    # ungluecklichen Parametern, transienter IO-Fehler) wird als FAILED markiert
    # und uebersprungen, statt den ganzen mehrstuendigen Lauf abzubrechen.
    study_obj.optimize(objective, n_trials=N_TRIALS, n_jobs=n_jobs_study,
                       callbacks=[_progress], catch=(Exception,))

    # --- Abschlussbericht ---
    best = study_obj.best_trial
    best_outputs = {
        name: class_dir / "matsim_runs" / f"trial_{best.number}_{name}"
        for name in _cfg.SCENARIOS
    }
    print()
    print(format_final_report(best_outputs, best.number, best.params))
    print(f"Bester RMSE ({study_name_str}): {best.value:.2f}%")

    # --- Speicheroptimierung: nur die MATSim-Outputs des besten Trials behalten ---
    # Pro Study fallen sonst N_TRIALS x ~40 MB (resistance_debug.csv etc.) an.
    # Der Abschlussbericht oben hat best_outputs bereits gelesen, daher koennen wir
    # jetzt alle uebrigen Trial-Verzeichnisse und Parameterdateien loeschen.
    runs_dir = class_dir / "matsim_runs"
    keep = {f"trial_{best.number}_{name}" for name in _cfg.SCENARIOS}
    keep.add(f"trial_{best.number}_params.properties")
    removed = 0
    if runs_dir.is_dir():
        for entry in runs_dir.iterdir():
            if entry.name in keep or not entry.name.startswith("trial_"):
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
    print(f"Speicheroptimierung: {removed} Trial-Outputs geloescht, "
          f"nur bestes Trial #{best.number} behalten.")

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
            fig = func(study_obj)
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
