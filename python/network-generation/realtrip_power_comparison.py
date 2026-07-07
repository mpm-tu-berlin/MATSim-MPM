# -*- coding: utf-8 -*-
"""
WP4: Wegaufgeloester Leistungsvergleich — Sim-Batterieleistung vs. gemessene
batterieseitige FAHR-Leistung (Batterie - HVAC - Sonstige Aux) je Fahrt.

Zweck: LOKALISIEREN, wo Verbrauchsabweichungen entstehen (Steigung vs. Ebene
vs. Gefaelle) — insbesondere fuer die bekannte 43t-Ueberschaetzung.

VERTRAULICHKEIT: Die Messdaten und ALLE wegaufgeloesten Ableitungen (Kurven,
Bin-CSVs, Plots) verbleiben im gitignorten Netz-Ordner; Konsole/Doku erhalten
NUR Aggregate (RMSE, Bias, Korrelation, Klassen-Statistiken). Die Rohdaten
werden nicht angezeigt (User-Regel 2026-07-07).

Methode je (Fahrt, C-Set) auf der kanonischen 250-m-Stufe:
  1. Sim: resistance_debug.csv -> pBattery_W je Link, forward-gefiltert, als
     Stufenfunktion ueber der Netz-Bogenlaenge.
  2. Messung: BatteryPower/HVAC/OtherAux ueber Mileage-Bogenlaenge; Ausrichtung
     (Richtung/Offset/Skala) aus realtrip_profile_validation.csv wiederverwendet.
     Fahr-Leistung = -(BatteryPower) - HVAC - OtherAux (Entlade-Konvention wird
     per Vorzeichen des Energieintegrals verifiziert).
  3. Gemeinsames 100-m-Bin-Raster; Metriken gesamt + stratifiziert nach
     Netz-Steigungsklasse (bergauf >1 %, eben +-1 %, bergab <-1 %).
  4. Kumulative Energie-Drift (Sim - Messung) ueber der Strecke.

Ausgaben (gitignorter net_dir):
  - power_comparison_metrics.csv          NUR Aggregate
  - power_bins_<trip>_<cset>.csv          Bin-Daten (VERTRAULICH, bleibt lokal)
  - power_profile_<trip>_<cset>_V<N>.pdf  Kurvenplots (VERTRAULICH, bleibt lokal)
"""

import argparse
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

_SCRIPT_DIR = Path(__file__).parent
BIN_M = 100.0
GRADE_CLASSES = [("uphill", 0.01, np.inf), ("flat", -0.01, 0.01),
                 ("downhill", -np.inf, -0.01)]
CANONICAL_L = 250
TRIPS = {"19t": "19t", "24t": "25t", "43t": "43t"}
POWER_SUFFIX = {"19t": "", "24t": ".1", "43t": ".2"}


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_chain(path):
    """Kette: geordnete Links mit (id, s0, s1, grade)."""
    with gzip.open(path, "rb") as f:
        root = ET.parse(f).getroot()
    nodes = {n.get("id"): (float(n.get("z")) if n.get("z") else np.nan)
             for n in root.find("nodes").findall("node")}
    links = {}
    adj = {}
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        links[(u, v)] = l
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    ends = [n for n, nb in adj.items() if len(nb) == 1]
    order = [ends[0]]
    prev = None
    while order[-1] != ends[1]:
        nxt = [n for n in adj[order[-1]] if n != prev]
        prev = order[-1]
        order.append(nxt[0])
    # BEIDE gerichteten Link-IDs je Kettensegment registrieren (die Sim kann
    # die Route in Gegenrichtung fahren und nutzt dann die Reverse-Links);
    # Steigung jeweils in FAHRTRICHTUNG des Links (fuer die Stratifizierung).
    arc_by_link = {}
    s = 0.0
    for a, b in zip(order[:-1], order[1:]):
        l_f = links.get((a, b))
        l_r = links.get((b, a))
        ref = l_f if l_f is not None else l_r
        length = float(ref.get("length"))
        grade_ab = (nodes[b] - nodes[a]) / length
        if l_f is not None:
            arc_by_link[l_f.get("id")] = {"s0": s, "s1": s + length, "grade": grade_ab}
        if l_r is not None:
            arc_by_link[l_r.get("id")] = {"s0": s, "s1": s + length, "grade": -grade_ab}
        s += length
    return arc_by_link, s


def main():
    parser = argparse.ArgumentParser(description="Wegaufgeloester Leistungsvergleich (WP4).")
    parser.add_argument("--networks-dir", type=str, required=True)
    parser.add_argument("--measurements", type=str,
                        default=str(_SCRIPT_DIR.parent / "calibration" / "data"
                                    / "Geschwindigkeitsprofile.xlsx"))
    args = parser.parse_args()
    net_dir = Path(args.networks_dir)

    rsea = _import_module("rsea", "run_section_energy_analysis.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")
    meas = pd.read_excel(args.measurements)
    align_df = pd.read_csv(net_dir / "realtrip_profile_validation.csv")
    version = kpa._next_version(net_dir, base="power_profile_19t_lh_low")

    metric_rows = []
    for trip, col in TRIPS.items():
        al = align_df[(align_df.trip == trip)
                      & (align_df.max_link_length == CANONICAL_L)]
        if al.empty:
            print(f"  WARNING: keine Ausrichtung fuer {trip} @{CANONICAL_L} m — skip")
            continue
        al = al.iloc[0]

        m = meas[[f"Mileage {col}", f"Altitude {col}", f"Velocity {col}"]].copy()
        sfx = POWER_SUFFIX[trip]
        for c in ("BatteryPower", "HVACpower", "PowerOtherAuxiliary"):
            m[c] = meas[f"{c}{sfx}"]
        m = m.dropna()
        s_meas = (m[f"Mileage {col}"].values - m[f"Mileage {col}"].values[0]) * 1000.0
        v_meas_ms = m[f"Velocity {col}"].values / 3.6
        # Entlade-Konvention verifizieren (Netz-Integral < 0 -> Entladung negativ)
        batt = m["BatteryPower"].values
        sign = -1.0 if np.nansum(batt) < 0 else 1.0
        p_drive_meas = sign * batt - m["HVACpower"].values \
            - m["PowerOtherAuxiliary"].values  # [kW], Fahranteil positiv

        if al.direction == -1:
            s_m = s_meas[-1] - s_meas[::-1]
            p_m = p_drive_meas[::-1]
            v_m = v_meas_ms[::-1]
        else:
            s_m, p_m, v_m = s_meas, p_drive_meas, v_meas_ms
        s_m = s_m * al.scale + al.offset_m

        # Netz-Kette (realspeed-Netz = Sim-Netz) + Sim-Leistung je Link
        net_path = net_dir / f"section_{trip}_{CANONICAL_L}m_realspeed.xml.gz"
        arc_by_link, total_len = load_chain(net_path)

        for cset in ("lh_low", "lh_high"):
            run_dir = net_dir / "validation_sims" / f"{trip}_{CANONICAL_L}m_{cset}"
            debug_csv = run_dir / "resistance_debug.csv"
            if not debug_csv.exists():
                print(f"  WARNING: {debug_csv} fehlt — erst Sims laufen lassen")
                continue
            fwd = rsea.get_forward_links_per_vehicle(net_path, debug_csv)
            ddf = pd.read_csv(debug_csv)
            ddf["linkId"] = ddf["linkId"].astype(str)
            vid, fwd_ids = next(iter(fwd.items()))
            vdf = (ddf[ddf.vehicleId == vid].set_index("linkId")
                   .reindex(fwd_ids).dropna(how="all"))

            # Bins ueber Netz-Bogenlaenge
            bins = np.arange(0.0, total_len + BIN_M, BIN_M)
            centers = 0.5 * (bins[:-1] + bins[1:])
            # Sim: Leistung je Bin aus Link-Stufenfunktion
            p_sim = np.full(len(centers), np.nan)
            g_bin = np.full(len(centers), np.nan)
            for lid, r in vdf.iterrows():
                c = arc_by_link.get(str(lid))
                if c is None:
                    continue
                i0 = int(c["s0"] // BIN_M)
                i1 = min(int(np.ceil(c["s1"] / BIN_M)), len(centers))
                p_sim[i0:i1] = float(r["pBattery_W"]) / 1000.0  # [kW]
                g_bin[i0:i1] = c["grade"]
            # Messung: Mittel je Bin (Leistung + Geschwindigkeit fuer Zeitbasis)
            idx = np.clip((s_m // BIN_M).astype(int), 0, len(centers) - 1)
            p_meas_bin = np.full(len(centers), np.nan)
            v_meas_bin = np.full(len(centers), np.nan)
            for i in np.unique(idx):
                sel = idx == i
                if sel.sum():
                    p_meas_bin[i] = float(np.nanmean(p_m[sel]))
                    v_meas_bin[i] = float(np.nanmean(v_m[sel]))

            valid = np.isfinite(p_sim) & np.isfinite(p_meas_bin) & np.isfinite(g_bin)
            d = p_sim[valid] - p_meas_bin[valid]
            row = {"trip": trip, "cset": cset, "n_bins": int(valid.sum()),
                   "bias_kW": float(np.mean(d)),
                   "rmse_kW": float(np.sqrt(np.mean(d ** 2))),
                   "corr": float(np.corrcoef(p_sim[valid], p_meas_bin[valid])[0, 1]),
                   "mean_p_meas_kW": float(np.mean(p_meas_bin[valid]))}
            for cname, lo, hi in GRADE_CLASSES:
                sel = valid & (g_bin > lo) & (g_bin <= hi)
                if sel.sum() < 5:
                    continue
                dd = p_sim[sel] - p_meas_bin[sel]
                row[f"bias_{cname}_kW"] = float(np.mean(dd))
                row[f"rmse_{cname}_kW"] = float(np.sqrt(np.mean(dd ** 2)))
                row[f"n_{cname}"] = int(sel.sum())
                row[f"share_len_{cname}_pct"] = 100.0 * sel.sum() / valid.sum()
            metric_rows.append(row)

            # Bin-CSV (VERTRAULICH, bleibt im gitignorten Ordner)
            pd.DataFrame({"s_m": centers, "p_sim_kW": p_sim,
                          "p_meas_kW": p_meas_bin, "grade": g_bin}) \
                .to_csv(net_dir / f"power_bins_{trip}_{cset}.csv", index=False)

            # Plots (VERTRAULICH): Leistung + kumulative Energie ueber Strecke
            fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
            axes[0].plot(centers / 1000.0, p_meas_bin, color="#555555", lw=0.8,
                         label="measured (battery - aux)")
            axes[0].plot(centers / 1000.0, p_sim, color="#C0392B", lw=0.8,
                         label=f"simulated ({cset})")
            axes[0].set_ylabel("Battery drive power [kW]")
            axes[0].legend(loc="best", fontsize=9)
            axes[0].grid(True, alpha=0.25)
            # kWh-Drift: gemeinsame Zeitbasis = Messgeschwindigkeit je Bin
            # (Realspeed-Netz faehrt dieselben Geschwindigkeiten)
            dt_h = np.where(np.isfinite(v_meas_bin) & (v_meas_bin > 0.5),
                            BIN_M / np.maximum(v_meas_bin, 0.5) / 3600.0, np.nan)
            e_sim = np.nancumsum(np.where(np.isfinite(p_sim * dt_h), p_sim * dt_h, 0))
            e_meas = np.nancumsum(np.where(np.isfinite(p_meas_bin * dt_h),
                                           p_meas_bin * dt_h, 0))
            axes[1].plot(centers / 1000.0, e_sim - e_meas, color="#21618C", lw=1.2)
            axes[1].set_ylabel("Cumulative energy drift\nsim - measured [kWh]")
            axes[1].set_xlabel("Distance [km]")
            axes[1].grid(True, alpha=0.25)
            fig.tight_layout()
            kpa._savefig(fig, net_dir, f"power_profile_{trip}_{cset}", version)
            plt.close(fig)
        print(f"{trip}: fertig")

    mdf = pd.DataFrame(metric_rows)
    mdf.to_csv(net_dir / "power_comparison_metrics.csv", index=False)
    print("\n=== Leistungsvergleich (NUR Aggregate) — Bias/RMSE [kW], 100-m-Bins ===")
    base_cols = ["trip", "cset", "n_bins", "bias_kW", "rmse_kW", "corr", "mean_p_meas_kW"]
    print(mdf[base_cols].round(2).to_string(index=False))
    print("\n--- stratifiziert nach Steigungsklasse (bias | rmse | Laengenanteil %) ---")
    for cname, _, _ in GRADE_CLASSES:
        cols = ["trip", "cset"] + [c for c in
                (f"bias_{cname}_kW", f"rmse_{cname}_kW", f"share_len_{cname}_pct")
                if c in mdf.columns]
        if len(cols) > 2:
            print(f"\n{cname}:")
            print(mdf[cols].round(2).to_string(index=False))
    print(f"\nAggregate: {net_dir / 'power_comparison_metrics.csv'}")
    print("Bin-CSVs + Plots: VERTRAULICH, nur im gitignorten Ordner.")


if __name__ == "__main__":
    main()
