"""Sensitivitaetsanalyse: Einfluss jedes Parameters auf den Energieverbrauch.

Fuehrt fuer jeden der 4 Kalibrierungsparameter jeweils die Unter- und Obergrenze
auf beiden Szenarien (LongHaul + RegionalDelivery) durch und vergleicht
mit dem Default-Lauf.
"""

import sys
import json
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import PARAM_BOUNDS, SCENARIOS
from src.matsim_runner import run_matsim, write_calibration_params
from src.error_computation import parse_charge_profiles, load_reference

# Default-Werte (aus CalibrationParams.java)
DEFAULTS = {
    "drivetrainEfficiency": 0.935,
    "inertiaC": 1.05,
    "recupEfficiency": 0.6,
    "maxRecupPowerW": 150_000,
}


def run_scenario(params: dict, scenario_name: str, run_label: str) -> dict[str, float]:
    """Fuehrt ein Szenario aus und gibt Verbrauch in kWh/km zurueck."""
    scenario = SCENARIOS[scenario_name]
    run_id = f"sens_{run_label}_{scenario_name}"
    output_dir = run_matsim(params, run_id=run_id,
                            config_path=scenario["config"])
    consumption_kwh = parse_charge_profiles(output_dir)
    route_km = scenario["route_km"]
    return {vid: kwh / route_km for vid, kwh in consumption_kwh.items()}


def main():
    results = {}

    # 1) Baseline-Lauf mit Default-Parametern
    print("=" * 60)
    print("BASELINE (Default-Parameter)")
    print(f"  {DEFAULTS}")
    print("=" * 60)

    results["default"] = {}
    for scenario_name in SCENARIOS:
        print(f"  Lauf: default / {scenario_name} ...")
        ee = run_scenario(DEFAULTS, scenario_name, "default")
        results["default"][scenario_name] = ee
        for vid, val in sorted(ee.items()):
            print(f"    {vid}: {val:.4f} kWh/km")

    # 2) Fuer jeden Parameter: low und high
    for param_name, (low, high) in PARAM_BOUNDS.items():
        for bound_label, bound_value in [("low", low), ("high", high)]:
            label = f"{param_name}_{bound_label}"
            params = deepcopy(DEFAULTS)
            params[param_name] = bound_value

            print()
            print("=" * 60)
            print(f"{label}: {param_name} = {bound_value}")
            print(f"  Alle Parameter: {params}")
            print("=" * 60)

            results[label] = {}
            for scenario_name in SCENARIOS:
                print(f"  Lauf: {label} / {scenario_name} ...")
                try:
                    ee = run_scenario(params, scenario_name, label)
                    results[label][scenario_name] = ee
                    for vid, val in sorted(ee.items()):
                        print(f"    {vid}: {val:.4f} kWh/km")
                except Exception as e:
                    print(f"    FEHLER: {e}")
                    results[label][scenario_name] = {"ERROR": str(e)}

    # 3) Zusammenfassung
    print()
    print("=" * 80)
    print("ZUSAMMENFASSUNG: Abweichung vom Default [%]")
    print("=" * 80)

    ref = load_reference()

    for param_name, (low, high) in PARAM_BOUNDS.items():
        print(f"\n--- {param_name} ---")
        print(f"  Bounds: [{low}, {high}], Default: {DEFAULTS[param_name]}")

        for bound_label in ["low", "high"]:
            label = f"{param_name}_{bound_label}"
            if label not in results:
                continue
            print(f"\n  {bound_label} ({PARAM_BOUNDS[param_name][0 if bound_label == 'low' else 1]}):")

            total_delta_pct = []
            for scenario_name in SCENARIOS:
                if scenario_name not in results[label]:
                    continue
                ee_var = results[label][scenario_name]
                ee_def = results["default"][scenario_name]
                if "ERROR" in ee_var:
                    print(f"    {scenario_name}: FEHLER")
                    continue

                print(f"    {scenario_name}:")
                for vid in sorted(ee_def.keys()):
                    if vid in ee_var:
                        delta = ee_var[vid] - ee_def[vid]
                        pct = 100 * delta / ee_def[vid] if ee_def[vid] != 0 else 0
                        total_delta_pct.append(abs(pct))
                        print(f"      {vid}: {ee_var[vid]:.4f} vs {ee_def[vid]:.4f} "
                              f"({delta:+.4f}, {pct:+.1f}%)")

            if total_delta_pct:
                avg_impact = sum(total_delta_pct) / len(total_delta_pct)
                print(f"    => Mittlere Abweichung: {avg_impact:.1f}%")

    # Referenz-Vergleich
    print()
    print("=" * 80)
    print("REFERENZ-VERGLEICH (Default vs. VECTO-Referenz)")
    print("=" * 80)
    for scenario_name in SCENARIOS:
        ref_data = load_reference(scenario_name)
        ee_def = results["default"][scenario_name]
        route_km = SCENARIOS[scenario_name]["route_km"]
        print(f"\n  {scenario_name} ({route_km} km):")
        for vid in sorted(ee_def.keys()):
            if vid in ref_data:
                ratio = ee_def[vid] / ref_data[vid]["ee_kwh_per_km"]
                print(f"    {vid}: MATSim={ee_def[vid]:.4f}, "
                      f"Ref={ref_data[vid]['ee_kwh_per_km']:.4f}, "
                      f"Ratio={ratio:.3f}")

    # JSON-Export
    out_file = Path(__file__).parent / "results" / "sensitivity_analysis.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nErgebnisse gespeichert: {out_file}")


if __name__ == "__main__":
    main()
