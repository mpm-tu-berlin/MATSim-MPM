"""
Diskretisierungs-Sweep: faehrt die 4 Einzelszenarien (lh_low, lh_high, rd_low,
rd_high) mit ihrem jeweils EIGENEN Einzel-Kalibrierungsparametersatz ueber alle
Netzauflösungen und protokolliert den Verbrauch je Fahrzeug gegen VECTO.

Der joint/'all'-Fall wird bewusst NICHT betrachtet (Entscheidung 2026-05-22):
pro Auflösung nur die 4 Einzelszenarien -> 8 Auflösungen x 4 Szenarien = 32 Runs.

Die Parameter stammen aus dem besten Trial der jeweiligen Studie eines
abgeschlossenen 1m-Kalibrierungslaufs (results/runs/<...>_1m/<study>/).

Aufruf:
    ../../.venv/Scripts/python run_convergence_sweep.py
    ../../.venv/Scripts/python run_convergence_sweep.py --resolutions 250 500   # Smoke-Test
    ../../.venv/Scripts/python run_convergence_sweep.py --calib-run 20260520_154835_1m

Ausgabe:
    results/convergence/<timestamp>/
        params/<scenario>.properties     <- verwendeter Parametersatz je Szenario
        matsim_runs/<scenario>_<N>m/     <- MATSim-Output (inkl. resistance_debug.csv)
        consumption.csv                  <- Verbrauch je (Auflösung, Szenario, Fahrzeug)
"""

import argparse
import csv
import datetime
import sys

import src.config as _cfg
from src.matsim_runner import run_matsim, write_calibration_params
from src.error_computation import parse_events_consumption, load_reference

DEFAULT_RESOLUTIONS = [1, 5, 10, 25, 50, 100, 200, 250, 300, 400, 500, 750]

# Szenario -> (Mission, Payload-Klasse). Die Studie heisst genauso wie das Szenario.
SCENARIOS = {
    "lh_low":  ("LongHaul",         "low"),
    "lh_high": ("LongHaul",         "high"),
    "rd_low":  ("RegionalDelivery", "low"),
    "rd_high": ("RegionalDelivery", "high"),
}


def load_best_params(calib_run, study: str) -> dict:
    """Liest den (einzig verbliebenen) Best-Trial-Parametersatz einer Studie."""
    mr = calib_run / study / "matsim_runs"
    files = sorted(mr.glob("trial_*_params.properties"))
    if not files:
        raise SystemExit(f"Keine Parameterdatei in {mr} gefunden.")
    params = {}
    for line in files[0].read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            params[k.strip()] = float(v.strip())
    return params


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calib-run", default=None,
                    help="Name des Kalibrierungslaufs unter results/runs (Default: neuester *_1m).")
    ap.add_argument("--resolutions", nargs="+", type=int, default=DEFAULT_RESOLUTIONS,
                    help=f"Auflösungen in m. Default: {DEFAULT_RESOLUTIONS}.")
    args = ap.parse_args()

    orig_results = _cfg.RESULTS_DIR
    runs_dir = orig_results / "runs"
    if args.calib_run:
        calib_run = runs_dir / args.calib_run
    else:
        candidates = sorted(runs_dir.glob("*_1m"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise SystemExit("Kein *_1m-Kalibrierungslauf unter results/runs gefunden.")
        calib_run = candidates[-1]

    print(f"Kalibrierungsquelle: {calib_run.name}")
    print(f"Auflösungen:         {args.resolutions}")
    print(f"Szenarien:           {list(SCENARIOS)} (joint/all bewusst ausgeschlossen)\n")

    params_by_scen = {s: load_best_params(calib_run, s) for s in SCENARIOS}

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = orig_results / "convergence" / ts
    pdir = base / "params"
    pdir.mkdir(parents=True, exist_ok=True)
    _cfg.RESULTS_DIR = base  # run_matsim schreibt nach base/matsim_runs/<run_id>

    pfiles = {}
    for scen, params in params_by_scen.items():
        pf = pdir / f"{scen}.properties"
        write_calibration_params(params, pf)
        pfiles[scen] = pf

    rows = []
    n_total = len(args.resolutions) * len(SCENARIOS)
    n_done = 0
    for res in args.resolutions:
        _cfg.ACTIVE_RESOLUTION_M = res
        _cfg.MATSIM_MEMORY, _cfg.N_JOBS = _cfg.resource_profile_for(res)
        for scen, (mission, payload) in SCENARIOS.items():
            n_done += 1
            run_id = f"{scen}_{res}m"
            config = _cfg.SCENARIOS[mission]["config"]
            route_km = _cfg.SCENARIOS[mission]["route_km"]
            try:
                out = run_matsim(run_id, config, pfiles[scen], scenario_name=mission)
                cons = parse_events_consumption(out)
            except Exception as e:
                print(f"[{n_done}/{n_total}] {run_id}: FEHLER - {e}", flush=True)
                continue

            ref = load_reference(mission, "BET_G5")
            n_veh = 0
            for vid, rd in ref.items():
                if not vid.endswith(f"_{payload}") or vid not in cons:
                    continue
                kwh = cons[vid]
                ekm = kwh / route_km
                vecto = rd["ee_kwh_per_km"]
                rows.append([res, scen, mission, payload, vid,
                             round(kwh, 4), route_km, round(ekm, 5), vecto,
                             round((ekm - vecto) / vecto * 100, 3)])
                n_veh += 1
            print(f"[{n_done}/{n_total}] {run_id}: ok ({n_veh} Fahrzeuge)", flush=True)

    csv_path = base / "consumption.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["resolution_m", "scenario", "mission", "payload", "vehicle_id",
                    "consumption_kwh", "route_km", "ee_kwh_per_km",
                    "vecto_kwh_per_km", "diff_pct"])
        w.writerows(rows)

    print(f"\nFertig. {len(rows)} Zeilen -> {csv_path}")


if __name__ == "__main__":
    main()
