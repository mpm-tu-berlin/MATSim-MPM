# -*- coding: utf-8 -*-
"""
v³/v²-Aero-Jensen-Frage (offene Forschungsfrage; fuer Sec 2/5 des Papers).

Aero-Energie ist konvex in der Geschwindigkeit (E_aero = fa * Integral v² ds).
Wer mit einer GEMITTELTEN Geschwindigkeit rechnet, unterschaetzt sie (Jensen).
Diese Auswertung quantifiziert den Fehler auf den 504 Sweep-Laeufen in
leserfreundlichen Einheiten (kWh/100 km, % der Aero-Energie, % des Verbrauchs):

  E_exakt     = fa * Sum_i (v0_i² + v1_i²)/2 * L_i   (Modell: Jensen-korrekte
                Integration ueber den Link bei konstanter Beschleunigung)
  E_linkmean  = fa * Sum_i vAvg_i² * L_i             (Link-Mittelgeschwindigkeit)
  E_routemean = fa * v̄² * L_tot,  v̄ = L_tot/T_tot   (eine konstante Geschwindig-
                keit fuer die ganze Route — die "naive" Annahme)

Gap_within  = E_exakt − E_linkmean   (Geschwindigkeitsaenderung INNERHALB der Links)
Gap_between = E_linkmean − E_routemean (Schwankung ZWISCHEN Links: Anfahrt,
              Power-Cap-Bergauf; waechst mit feinem Gitter/steilem Terrain)

Links werden wie in der Sweep-Auswertung forward-gefiltert. fa = 0,5·1,225·cdXA
aus dem Kalibrierset je Beladung.

Ausgabe: aero_jensen.csv (je Sektion/Beladung/Stufe) + Konsolen-Zusammenfassung.
"""

from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent
RHO = 1.225


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    import argparse
    parser = argparse.ArgumentParser(description="v3/v2-Aero-Jensen-Auswertung.")
    parser.add_argument("--results-dir", type=str, required=True)
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = _SCRIPT_DIR / results_dir

    rsea = _import_module("rsea", "run_section_energy_analysis.py")

    rows = []
    for section in ["flat"] + list(rsea.SECTIONS):
        for max_len in rsea.LINK_LENGTHS:
            if section == "flat":
                network_path = results_dir / "flat_networks" / f"flat_{max_len}m.xml.gz"
            else:
                network_path = results_dir / f"section_{section}_{max_len}m.xml.gz"
            if not network_path.exists():
                continue
            for loading in ("empty", "loaded"):
                run_dir = results_dir / "sim_results" / f"{section}_{max_len}m_{loading}"
                debug_csv = run_dir / "resistance_debug.csv"
                if not debug_csv.exists():
                    continue
                fwd = rsea.get_forward_links_per_vehicle(network_path, debug_csv)
                ddf = pd.read_csv(debug_csv)
                ddf["linkId"] = ddf["linkId"].astype(str)
                vehicle_id, fwd_ids = next(iter(fwd.items()))
                vdf = (ddf[ddf.vehicleId == vehicle_id].set_index("linkId")
                       .reindex(fwd_ids).dropna(how="all"))

                fa = 0.5 * RHO * rsea.CALIBRATION_PER_LOADING[loading]["cdXA"]
                L = vdf["length_m"].values
                v0 = vdf["vEntry_kmh"].values / 3.6
                v1 = vdf["vExit_kmh"].values / 3.6
                t = vdf["tPhysical_s"].values
                vavg = 0.5 * (v0 + v1)

                e_exact = fa * float(np.sum(0.5 * (v0 * v0 + v1 * v1) * L))      # [J]
                e_linkmean = fa * float(np.sum(vavg * vavg * L))
                L_tot = float(np.sum(L))
                T_tot = float(np.sum(t))
                v_route = L_tot / T_tot
                e_routemean = fa * v_route * v_route * L_tot

                e_total_sim = float(vdf["energy_Wh"].sum()) * 3600.0             # [J]

                rows.append({
                    "section": section, "loading": loading, "max_link_length": max_len,
                    "L_km": L_tot / 1000.0,
                    "v_route_kmh": v_route * 3.6,
                    "E_total_kWh": e_total_sim / 3.6e6,
                    "E_aero_exact_kWh": e_exact / 3.6e6,
                    "E_aero_linkmean_kWh": e_linkmean / 3.6e6,
                    "E_aero_routemean_kWh": e_routemean / 3.6e6,
                    "gap_within_kWh": (e_exact - e_linkmean) / 3.6e6,
                    "gap_between_kWh": (e_linkmean - e_routemean) / 3.6e6,
                })
        print(f"{section}: fertig")

    df = pd.DataFrame(rows)
    df["gap_total_kWh"] = df.gap_within_kWh + df.gap_between_kWh
    df["gap_total_pct_of_aero"] = 100.0 * df.gap_total_kWh / df.E_aero_exact_kWh
    df["gap_total_pct_of_total"] = 100.0 * df.gap_total_kWh / df.E_total_kWh
    df.to_csv(results_dir / "aero_jensen.csv", index=False)

    print("\n=== v3/v2-Aero-Jensen: Gap = exakte Integration - Mittelwert-Annahme ===")
    print("(gap_within: innerhalb der Links; gap_between: konstante Routen-")
    print(" geschwindigkeit statt Profil — die 'naive' Annahme)\n")
    for loading in ("empty", "loaded"):
        sub = df[(df.loading == loading) & (df.section != "flat")]
        print(f"--- {loading} (240 Laeufe) ---")
        print(f"  Gap gesamt:  Median {sub.gap_total_kWh.median():.2f} kWh/100 km "
              f"(P95 {sub.gap_total_kWh.quantile(.95):.2f}, max {sub.gap_total_kWh.max():.2f})")
        print(f"               = {sub.gap_total_pct_of_aero.median():.2f} % der Aero-Energie, "
              f"{sub.gap_total_pct_of_total.median():.3f} % des Verbrauchs (Median)")
        print(f"  davon within {sub.gap_within_kWh.median():.3f} / between "
              f"{sub.gap_between_kWh.median():.3f} kWh (Median)")
        worst = sub.loc[sub.gap_total_kWh.idxmax()]
        print(f"  Worst Case: {worst.section}/{int(worst.max_link_length)} m — "
              f"{worst.gap_total_kWh:.2f} kWh = {worst.gap_total_pct_of_total:.2f} % des Verbrauchs "
              f"(v_route {worst.v_route_kmh:.1f} km/h)")
        fl = df[(df.loading == loading) & (df.section == "flat")]
        print(f"  Flat-Kontrolle: Gap {fl.gap_total_kWh.median():.3f} kWh (nur Anfahrt)\n")

    # Aufloesungs-/Terrainabhaengigkeit (loaded)
    sub = df[(df.loading == "loaded") & (df.section != "flat")]
    piv = sub.pivot_table(index="max_link_length", values=["gap_total_kWh"], aggfunc=["median", "max"])
    print("=== loaded: Gap nach Aufloesungsstufe (kWh/100 km) ===")
    print(piv.round(2).to_string())
    print(f"\nCSV: {results_dir / 'aero_jensen.csv'}")


if __name__ == "__main__":
    main()
