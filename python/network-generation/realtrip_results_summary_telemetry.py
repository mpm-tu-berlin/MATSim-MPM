# -*- coding: utf-8 -*-
"""Zusammenfassung der Telemetrie-Trip-Validierung (TTE Sec. VI, 2026-08):
liest die Sim-Ergebnis-CSVs (Basis + Counterfactual, inkl. Nachzuegler) aus
dem gitignorten Netz-Ordner und baut die Vergleichstabelle je Teilstueck —
Referenzaufloesung 250 m, Fallback auf die feinste vorhandene Stufe.

stdout nur Abweichungsprozente (keine absoluten Messwerte noetig ausser
den bereits aggregierten kWh/km-Targets, die im gitignorten Ordner bleiben).

Aufruf:
  ../../.venv/Scripts/python realtrip_results_summary_telemetry.py \
      --networks-dir data/realtrip_networks_telemetry_20260806
"""
import argparse
from pathlib import Path

import pandas as pd

_SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = _SCRIPT_DIR / "data" / "realtrip_telemetry_profiles"

RESULT_FILES = [
    "realtrip_validation_results_base.csv",
    "realtrip_validation_results_cf.csv",
    "realtrip_validation_results_h27_base.csv",
    "realtrip_validation_results_h27_cf.csv",
]


def pick_resolution(g):
    """250 m bevorzugt, sonst feinste vorhandene Stufe."""
    if (g["max_link_length"] == 250).any():
        return g[g["max_link_length"] == 250].iloc[0]
    return g.sort_values("max_link_length").iloc[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--networks-dir", type=str, required=True)
    args = parser.parse_args()
    net_dir = Path(args.networks_dir)

    frames = [pd.read_csv(net_dir / f) for f in RESULT_FILES
              if (net_dir / f).exists()]
    df = pd.concat(frames, ignore_index=True)
    meta = pd.read_csv(PROFILES_DIR / "trips_meta.csv").set_index("label")

    rows = []
    for (trip, cset), g in df.groupby(["trip", "cset"]):
        r = pick_resolution(g)
        rows.append({"trip": trip, "cset": cset,
                     "L_m": int(r["max_link_length"]),
                     "diff_pct": round(float(r["diff_pct"]), 1)})
    piv = (pd.DataFrame(rows)
           .pivot(index="trip", columns="cset", values="diff_pct"))
    res = pd.DataFrame(rows).groupby("trip")["L_m"].first()
    piv["L_m"] = res
    piv["dist_km"] = meta["dist_odo_km"]
    piv["mass_t"] = meta["mass_t"]
    piv = piv.reindex(["f22a", "f22c", "w24", "h19", "h27", "h30",
                       "h42a", "h42b"])
    cols = ["dist_km", "mass_t", "L_m", "lh_high", "lh_low",
            "lh_high_eta96_rec90", "lh_low_eta96_rec90"]
    piv = piv[[c for c in cols if c in piv.columns]]
    print("=== Abweichung Sim vs. Messung [%] je Teilstueck ===")
    print(piv.to_string())
    out = net_dir / "realtrip_results_summary.csv"
    piv.to_csv(out)
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()
