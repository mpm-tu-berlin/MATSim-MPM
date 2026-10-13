# -*- coding: utf-8 -*-
"""
WP4: Validierungs-Sims auf den Realspeed-Routen-Netzen (V2) gegen die
batterieseitige Mess-Ground-Truth.

Setup je Fahrt (19t/24t/43t) x C-Set x Aufloesung [100, 250, 400] m:
  - Netz: section_<t>_<L>m_realspeed.xml.gz (freespeed = gemessene
    Geschwindigkeit je Link -> Speed-Profil-Fehler eliminiert)
  - Fahrzeug: reale Massen (19t: 19+0, 24t: 19+5, 43t: 19+24 t), 600 kW,
    vMax aus alten Szenarien
  - Kalibrierung: BEIDE B2v2-C-Sets (lh_low + lh_high) fuer ALLE Fahrten
    (2x3-Matrix = Identifizierbarkeits-/Sensitivitaets-Story; User-Entscheidung
    2026-07-07: 24t mit beiden Sets als Sensitivitaet)
  - JAR: C2-aktiver Build; rolling-Boundary-Handling wie im Sweep

Zielgroesse: FAHR-Energie batterieseitig. Messung liefert
  target = |BatteryPower| - HVACpower - PowerOtherAuxiliary   [kWh/km]
(die Sim-Energie energy_Wh enthaelt keinen Aux -> sauberer Vergleich ohne
Aux-Annahme; frueher musste aux geschaetzt werden).

Alle Ein-/Ausgaben liegen im gitignorten Netz-Ordner (private Realfahrten);
ausgegeben werden nur Aggregate.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent

TRIP_VEHICLES = {
    "19t": {"id": "truck_19t", "mass": 19000, "payload": 0,
            "cdXA": 5.79, "rollingC": 0.0048,  # unbenutzt (CalibrationParams)
            "maxMotorPower": 600000, "maxSpeed": 25.0},
    # 24t-Fahrt: Beladungswechsel unterwegs (erste 2/5: 24 t, letzte 3/5: 25 t;
    # User 2026-07-07) -> streckengewichtetes Mittel 24,6 t. MATSim kann die
    # Masse nicht mid-trip aendern; bei Bedarf spaeter Routen-Split.
    "24t": {"id": "truck_24t", "mass": 19000, "payload": 5600,
            "cdXA": 5.79, "rollingC": 0.0048,
            "maxMotorPower": 600000, "maxSpeed": 27.778},
    "43t": {"id": "truck_43t", "mass": 19000, "payload": 24000,
            "cdXA": 5.79, "rollingC": 0.0048,
            "maxMotorPower": 600000, "maxSpeed": 27.778},
}
LINK_LENGTHS = [250]  # nur die Kalibrierskala (User 2026-08-18); 100/400 waren
                      # eine Robustheitsleiter, die in keiner Paper-Zahl vorkommt


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser(description="WP4-Validierungs-Sims (Realspeed-Netze).")
    parser.add_argument("--networks-dir", type=str, required=True)
    parser.add_argument("--jar", type=str, default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--link-lengths", type=str, default=None,
                        help="Kommaliste [m], Default: 100,250,400")
    parser.add_argument("--eta-t", type=str, default=None,
                        help="Kommaliste tractionEfficiency-Overrides; erzeugt je "
                             "C-Set Counterfactual-Varianten <cset>_etaNN")
    parser.add_argument("--recup-eff", type=float, default=None,
                        help="recupEfficiency-Override (zusaetzlich zu --eta-t)")
    parser.add_argument("--max-recup-frac", type=float, default=None,
                        help="maxRecupPowerFraction-Override")
    parser.add_argument("--extra-params", type=str, default=None,
                        help="Kommaliste key=value, wird in JEDES C-Set gemerged (z. B. rollingLoadExponent=0.9,rollingRefMassKg=35500 fuer die Lastabhaengigkeits-Sensitivitaet ohne Rekalibrierung)")
    parser.add_argument("--out-name", type=str, default="realtrip_validation_results.csv")
    args = parser.parse_args()
    net_dir = Path(args.networks_dir)
    link_lengths = ([int(x) for x in args.link_lengths.split(",")]
                    if args.link_lengths else LINK_LENGTHS)

    rsea = _import_module("rsea", "run_section_energy_analysis.py")
    jar_path = Path(args.jar) if args.jar else Path(rsea.DEFAULT_JAR).resolve()
    if not jar_path.exists():
        raise SystemExit(f"JAR fehlt: {jar_path}")

    # C-Sets: 'empty' = lh_low (#79), 'loaded' = lh_high (#261)
    csets = {"lh_low": rsea.CALIBRATION_PER_LOADING["empty"],
             "lh_high": rsea.CALIBRATION_PER_LOADING["loaded"]}
    # Counterfactual (2026-07-07): nur tractionEfficiency ersetzen, Rest der
    # Kalibrierung unveraendert -> isoliert die VECTO-Deklarations-Luecke
    # (deklariert 0,80/0,87 vs. real-world ~0,96, s. paper_findings Sec 14.5).
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

    if args.extra_params:
        extra = dict(kv.split("=") for kv in args.extra_params.split(","))
        extra = {k.strip(): float(v) for k, v in extra.items()}
        # Eigene Laufnamen: Extra-Laeufe duerfen die Basis-Laufordner nicht teilen
        csets = {f"{name}_x": {**calib, **extra} for name, calib in csets.items()}
        print(f"Extra-Parameter in allen C-Sets: {extra}")

    # Mess-Ground-Truth (Aggregate aus realtrip_measured_eval.py)
    energy_csv = net_dir / "realtrip_measured_energy.csv"
    targets = {}
    if energy_csv.exists():
        edf = pd.read_csv(energy_csv).set_index("trip")
        for t in edf.index:
            targets[t] = (abs(edf.loc[t, "BatteryPower_kWh_per_km"])
                          - edf.loc[t, "HVACpower_kWh_per_km"]
                          - edf.loc[t, "PowerOtherAuxiliary_kWh_per_km"])
    else:
        print(f"WARNING: {energy_csv} fehlt — Targets leer, nur Sim-Werte.")

    sim_dir = net_dir / "validation_sims"
    sim_dir.mkdir(exist_ok=True)

    # Reale Fahrtrichtung aus der Profil-Ausrichtung: dir=+1 -> Start am
    # Kettenanfang, dir=-1 -> Start am Kettenende. endpoints_from_reference
    # lieferte BELIEBIGE Reihenfolge -> Sim konnte rueckwaerts fahren
    # (Leistungs-corr -0,49 beim 19t; bei Netto-Hoehendifferenz nicht symmetrisch).
    rme = _import_module("rme", "realtrip_measured_eval.py")
    align = pd.read_csv(net_dir / "realtrip_profile_validation.csv") \
        .drop_duplicates("trip").set_index("trip")

    tasks = []
    for trip, vp in TRIP_VEHICLES.items():
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
            from_coord = f"{sx},{sy}"
            to_coord = f"{ex},{ey}"
            for cname, calib in csets.items():
                run_dir = sim_dir / f"{trip}_{L}m_{cname}"
                tasks.append((trip, L, cname, str(network), str(run_dir),
                              [vp], str(jar_path), calib,
                              from_coord, to_coord, rsea.QSIM_TIMESTEP))

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

    print("\n=== WP4-Validierung: Sim (Fahr-Energie) vs. Messung (Batterie - Aux) ===")
    print(df.round(3).to_string(index=False))
    if targets:
        print("\nTargets [kWh/km]: " + ", ".join(f"{t}: {v:.3f}" for t, v in targets.items()))
    print(f"\nCSV: {out_csv}")


if __name__ == "__main__":
    main()
