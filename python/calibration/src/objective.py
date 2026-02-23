import optuna

from src.config import PARAM_BOUNDS
from src.matsim_runner import run_all_scenarios
from src.error_computation import compute_combined_error, format_trial_report


def objective(trial: optuna.Trial) -> float:
    """Optuna-Zielfunktion: Schreibt Kalibrierungsparameter, fuehrt MATSim
    fuer beide Szenarien (Long Haul + Regional Delivery) aus und berechnet
    den kombinierten RMSE gegenueber Referenzdaten."""

    # Kalibrierungsparameter samplen
    params = {}
    for name, (low, high) in PARAM_BOUNDS.items():
        params[name] = trial.suggest_float(name, low, high)

    # MATSim fuer alle Szenarien ausfuehren
    run_id_prefix = f"trial_{trial.number}"
    scenario_outputs = run_all_scenarios(params, run_id_prefix=run_id_prefix)

    # Detaillierten Bericht ausgeben (landet via Tee auch im Log)
    report = format_trial_report(scenario_outputs, trial.number, params)
    print(report, flush=True)

    # Kombinierten Fehler ueber alle Szenarien berechnen
    error = compute_combined_error(scenario_outputs)

    return error
