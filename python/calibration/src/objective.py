import time
from datetime import datetime

import optuna

from src.config import PARAM_BOUNDS
from src.matsim_runner import run_all_scenarios
from src.error_computation import compute_combined_errors


def objective(trial: optuna.Trial) -> float:
    """Optuna-Zielfunktion: Schreibt Kalibrierungsparameter, fuehrt MATSim
    fuer beide Szenarien (Long Haul + Regional Delivery) aus und berechnet
    den kombinierten RMSE in % gegenueber Referenzdaten."""

    # Kalibrierungsparameter samplen (alle 5 in PARAM_BOUNDS, inkl. maxRecupPowerFraction)
    params = {}
    for name, (low, high) in PARAM_BOUNDS.items():
        params[name] = trial.suggest_float(name, low, high)

    # Start-Heartbeat: bei parallelen Trials sieht man so live, dass gerade
    # gerechnet wird (sonst erscheint erst nach ~2 min die Ergebniszeile).
    t0 = time.time()
    print(f"[{datetime.now():%H:%M:%S}] Trial {trial.number:>4d} gestartet ...", flush=True)

    # MATSim fuer alle Szenarien ausfuehren
    run_id_prefix = f"trial_{trial.number}"
    scenario_outputs = run_all_scenarios(params, run_id_prefix=run_id_prefix)

    # Kompakte Ausgabe pro Trial (landet via Tee auch im Log)
    rmse_pct, mae_pct = compute_combined_errors(scenario_outputs)
    params_str = "  ".join(
        f"{k}={v:.3f}" if k == "inertiaC" else
        f"{k}={v:.0f}" if k == "auxPowerW" else
        f"{k}={v:.5f}" if k == "rollingC" else
        f"{k}={v:.2f}"
        for k, v in params.items()
    )
    dt = time.time() - t0
    print(f"[{datetime.now():%H:%M:%S}] Trial {trial.number:>4d}: RMSE={rmse_pct:.2f}%  "
          f"MAE={mae_pct:.2f}%  ({dt:.0f}s)  | {params_str}", flush=True)

    return rmse_pct
