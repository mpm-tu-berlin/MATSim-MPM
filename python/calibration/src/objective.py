import optuna

from src.config import PARAM_BOUNDS
from src.matsim_runner import run_all_scenarios
from src.error_computation import compute_combined_errors


def objective(trial: optuna.Trial) -> float:
    """Optuna-Zielfunktion: Schreibt Kalibrierungsparameter, fuehrt MATSim
    fuer beide Szenarien (Long Haul + Regional Delivery) aus und berechnet
    den kombinierten RMSE in % gegenueber Referenzdaten."""

    # Kalibrierungsparameter samplen
    params = {}
    for name, (low, high) in PARAM_BOUNDS.items():
        params[name] = trial.suggest_float(name, low, high)
    params["maxRecupPowerFraction"] = 1.0  # Fixwert, nicht optimiert

    # MATSim fuer alle Szenarien ausfuehren
    run_id_prefix = f"trial_{trial.number}"
    scenario_outputs = run_all_scenarios(params, run_id_prefix=run_id_prefix)

    # Kompakte Ausgabe pro Trial (landet via Tee auch im Log)
    rmse_pct, mae_pct = compute_combined_errors(scenario_outputs)
    params_str = "  ".join(
        f"{k}={v:.3f}" if k == "inertiaC" else
        f"{k}={v:.0f}" if k == "auxPowerW" else
        f"{k}={v:.2f}"
        for k, v in params.items()
    )
    print(f"Trial {trial.number:>4d}: RMSE={rmse_pct:.2f}%  MAE={mae_pct:.2f}%  | {params_str}", flush=True)

    return rmse_pct
