# -*- coding: utf-8 -*-
"""Validierungs-Sims fuer die 6 telemetry-Trips (TTE Sec. VI, Erweiterung
2026-08) — Methodik identisch zu realtrip_validation_sims.py (WP4):
Realspeed-Netze, beide B2v2-C-Sets, optional Counterfactual-Overrides;
Target = FAHR-Energie (|Batterie| - HVAC - Aux) aus dem Telemetrie-Export.

Massen aus trips_meta.csv (Median VehicleWeight): Tare 19 t + Payload-Split
wie bei den Bestands-Trips.

Aufruf (Basis + Counterfactual wie WP4):
  ../../.venv/Scripts/python realtrip_validation_sims_telemetry.py \
      --networks-dir data/realtrip_networks_telemetry_20260806
  ... zusaetzlich --eta-t 0.96 --recup-eff 0.90 --max-recup-frac 1.0
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = _SCRIPT_DIR / "data" / "realtrip_telemetry_profiles"
TARE_KG = 19000
LINK_LENGTHS = [100, 250, 400]


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def trip_vehicles():
    meta = pd.read_csv(PROFILES_DIR / "trips_meta.csv").set_index("label")
    vehicles = {}
    for label in meta.index:
        total = int(round(meta.loc[label, "mass_t"] * 1000))
        mass = min(total, TARE_KG)
        vehicles[label] = {
            "id": f"truck_{label}", "mass": mass, "payload": total - mass,
            "cdXA": 5.79, "rollingC": 0.0048,  # unbenutzt (CalibrationParams)
            "maxMotorPower": 600000, "maxSpeed": 27.778,
        }
    return vehicles


def main():
    parser = argparse.ArgumentParser(description="telemetry-Validierungs-Sims.")
    parser.add_argument("--networks-dir", type=str, required=True)
    parser.add_argument("--jar", type=str, default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--link-lengths", type=str, default=None)
    parser.add_argument("--eta-t", type=str, default=None)
    parser.add_argument("--recup-eff", type=float, default=None)
    parser.add_argument("--max-recup-frac", type=float, default=None)
    parser.add_argument("--trips", type=str, default=None,
                        help="Nur diese Labels (kommagetrennt)")
    parser.add_argument("--out-name", type=str,
                        default="realtrip_validation_results.csv")
    args = parser.parse_args()
    net_dir = Path(args.networks_dir)
    link_lengths = ([int(x) for x in args.link_lengths.split(",")]
                    if args.link_lengths else LINK_LENGTHS)

    rsea = _import_module("rsea", "run_section_energy_analysis.py")
    jar_path = Path(args.jar) if args.jar else Path(rsea.DEFAULT_JAR).resolve()
    if not jar_path.exists():
        raise SystemExit(f"JAR fehlt: {jar_path}")

    csets = {"lh_low": rsea.CALIBRATION_PER_LOADING["empty"],
             "lh_high": rsea.CALIBRATION_PER_LOADING["loaded"]}
    if args.eta_t:
        base = csets
        csets = {}
        for eta in (float(x) for x in args.eta_t.split(",")):
            for cname, calib in base.items():
                cf = dict(calib)
                cf["tractionEfficiency"] = eta
                suffix = f"_eta{int(round(eta * 100)):02d}"
                if args.recup_eff is not None:
                    cf["recupEfficiency"] = args.recup_eff
                    suffix += f"_rec{int(round(args.recup_eff * 100)):02d}"
                if args.max_recup_frac is not None:
                    cf["maxRecupPowerFraction"] = args.max_recup_frac
                csets[f"{cname}{suffix}"] = cf

    edf = pd.read_csv(net_dir / "realtrip_measured_energy.csv").set_index("trip")
    targets = {t: (abs(edf.loc[t, "BatteryPower_kWh_per_km"])
                   - edf.loc[t, "HVACpower_kWh_per_km"]
                   - edf.loc[t, "PowerOtherAuxiliary_kWh_per_km"])
               for t in edf.index}

    sim_dir = net_dir / "validation_sims"
    sim_dir.mkdir(exist_ok=True)

    # robuster Loader (Eulerpfad) aus der Telemetrie-Eval — Fallback-geroutete
    # Netze koennen Knoten doppelt besuchen
    rme = _import_module("rmet", "realtrip_measured_eval_telemetry.py")
    align = pd.read_csv(net_dir / "realtrip_profile_validation.csv") \
        .drop_duplicates("trip").set_index("trip")

    vehicles = trip_vehicles()
    if args.trips:
        wanted = args.trips.split(",")
        vehicles = {k: v for k, v in vehicles.items() if k in wanted}
    tasks = []
    for trip, vp in vehicles.items():
        direction = int(align.loc[trip, "direction"]) if trip in align.index else +1
        for L in link_lengths:
            network = net_dir / f"section_{trip}_{L}m_realspeed.xml.gz"
            if not network.exists():
                print(f"  WARNING: {network.name} fehlt — skip")
                continue
            prof = rme.load_chain_profile(network)
            start_node = prof["order"][0] if direction == +1 else prof["order"][-1]
            end_node = prof["order"][-1] if direction == +1 else prof["order"][0]
            sx, sy = prof["node_xy"][start_node]
            ex, ey = prof["node_xy"][end_node]
            for cname, calib in csets.items():
                run_dir = sim_dir / f"{trip}_{L}m_{cname}"
                tasks.append((trip, L, cname, str(network), str(run_dir),
                              [vp], str(jar_path), calib,
                              f"{sx},{sy}", f"{ex},{ey}", rsea.QSIM_TIMESTEP))

    print(f"{len(tasks)} Validierungs-Sims ({args.workers} Worker)\n")
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(rsea._run_one_simulation, t): t for t in tasks}
        for fut in as_completed(futures):
            trip, L, cname, results, log = fut.result()
            for msg in log:
                print(msg)
            if not results:
                continue
            for vid, r in results.items():
                target = targets.get(trip)
                rows.append({
                    "trip": trip, "max_link_length": L, "cset": cname,
                    "sim_kWh_per_km": r["kWh_per_km"],
                    "sim_total_kWh": r["total_energy_Wh"] / 1000.0,
                    "driven_km": r["total_length_m"] / 1000.0,
                    "target_drive_kWh_per_km": target,
                    "diff_pct": (100.0 * (r["kWh_per_km"] / target - 1.0)
                                 if target else np.nan),
                })

    df = pd.DataFrame(rows).sort_values(["trip", "cset", "max_link_length"])
    out_csv = net_dir / args.out_name
    df.to_csv(out_csv, index=False)

    print("\n=== telemetry-Validierung: Sim (Fahr-Energie) vs. Telemetrie ===")
    print(df.round(3).to_string(index=False))
    print("\nTargets [kWh/km]: " + ", ".join(f"{t}: {v:.3f}"
                                             for t, v in targets.items()))
    print(f"\nCSV: {out_csv}")


if __name__ == "__main__":
    main()
