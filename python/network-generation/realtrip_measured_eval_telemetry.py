# -*- coding: utf-8 -*-
"""telemetry-Erweiterung der WP4-Validierungskette (TTE Sec. VI, 2026-08):
25-m-Telemetrie-Profile (HoLa-Export, trip_<label>.csv) gegen die neuen
Routen-Netze — Hoehen-/Steigungs-Validierung + Realspeed-Mapping + Energie-
Ground-Truth. Methodik identisch zu realtrip_measured_eval.py (Funktionen
werden von dort importiert), nur die Eingabe ist der CSV-Export statt des
Mess-Excels der ersten drei Trips.

Ausgaben (alle im gitignorten Netz-Ordner, stdout nur Aggregate):
  - realtrip_profile_validation.csv   (Format wie Bestand, inkl. direction)
  - realtrip_measured_energy.csv      (Format wie Bestand -> validation_sims)
  - section_<label>_<L>m_realspeed.xml.gz
  - profile_<label>_<L>m_V<N>.pdf/png

Aufruf:
  ../../.venv/Scripts/python realtrip_measured_eval_telemetry.py \
      --networks-dir data/realtrip_networks_telemetry_20260806
"""
import argparse
import copy
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

_SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = _SCRIPT_DIR / "data" / "realtrip_telemetry_profiles"


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_chain_profile(path):
    """Robustes Ketten-Profil: wie rme.load_chain_profile, aber als
    Eulerpfad (Hierholzer). Fallback-geroutete Netze koennen einen Knoten
    zweimal besuchen (Selbstberuehrung) — der naive Kettenlauf oszilliert
    dort endlos. Der Eulerpfad nutzt jede ungerichtete Kante genau einmal
    und laeuft Mehrfachbesuche korrekt ab."""
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()
    nodes = {n.get("id"): (float(n.get("x")), float(n.get("y")),
                           float(n.get("z")) if n.get("z") else np.nan)
             for n in root.find("nodes").findall("node")}
    pair_link = {}
    adj = {}
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        pair = frozenset((u, v))
        if pair not in pair_link:
            pair_link[pair] = l
            adj.setdefault(u, []).append(v)
            adj.setdefault(v, []).append(u)
    # Endpunkte: Grad-1-Knoten mit maximalem Euklid-Abstand (doppelt
    # befahrene Links wurden beim Export dedupliziert -> kurze Sackgassen-
    # Stiche mit eigenen Grad-1-Spitzen sind moeglich; die echten Routen-
    # Enden liegen raeumlich am weitesten auseinander)
    deg1 = [n for n, nb in adj.items() if len(nb) == 1]
    if len(deg1) < 2:
        deg1 = sorted(adj, key=lambda n: len(adj[n]))[:4]
    best_pair, best_d = None, -1.0
    for i in range(len(deg1)):
        for j in range(i + 1, len(deg1)):
            a, b = deg1[i], deg1[j]
            d = ((nodes[a][0] - nodes[b][0]) ** 2
                 + (nodes[a][1] - nodes[b][1]) ** 2)
            if d > best_d:
                best_pair, best_d = (a, b), d
    start, end = best_pair
    # Kette = kuerzester Pfad Start->Ende (das faehrt auch die Sim);
    # Stichkanten bleiben ausserhalb der s-Achse
    import heapq
    dist = {start: 0.0}
    parent = {start: None}
    heap = [(0.0, start)]
    while heap:
        d, v = heapq.heappop(heap)
        if v == end:
            break
        if d > dist.get(v, float("inf")):
            continue
        for w in adj[v]:
            nd = d + float(pair_link[frozenset((v, w))].get("length"))
            if nd < dist.get(w, float("inf")):
                dist[w] = nd
                parent[w] = v
                heapq.heappush(heap, (nd, w))
    if end not in parent:
        raise ValueError(f"{path}: kein Pfad zwischen den Endpunkten")
    order = []
    n = end
    while n is not None:
        order.append(n)
        n = parent[n]
    order.reverse()
    n_off = len(pair_link) - (len(order) - 1)
    if n_off:
        print(f"    Hinweis: {n_off} Kanten ausserhalb der Hauptkette "
              f"(deduplizierte Stichfahrten) — nicht in der s-Achse")
    s = [0.0]
    chain_links = []
    for a, b in zip(order[:-1], order[1:]):
        l = pair_link[frozenset((a, b))]
        length = float(l.get("length"))
        chain_links.append({"id": l.get("id"), "s0": s[-1], "s1": s[-1] + length,
                            "elem_from": a, "elem_to": b})
        s.append(s[-1] + length)
    z = [nodes[n][2] for n in order]
    return {"s": np.array(s), "z": np.array(z), "chain": chain_links,
            "tree": tree, "root": root, "order": order,
            "node_xy": {n: (nodes[n][0], nodes[n][1])
                        for n in (order[0], order[-1])}}


def main():
    parser = argparse.ArgumentParser(description="telemetry-Trips vs. Routen-Netze.")
    parser.add_argument("--networks-dir", type=str, required=True)
    parser.add_argument("--link-lengths", type=str, default="250")
    parser.add_argument("--trips", type=str, default=None,
                        help="Nur diese Labels (kommagetrennt); CSVs werden "
                             "gemerged statt ueberschrieben")
    args = parser.parse_args()

    net_dir = Path(args.networks_dir)
    link_lengths = [int(x) for x in args.link_lengths.split(",")]
    rme = _import_module("rme", "realtrip_measured_eval.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")

    # Energie-Ground-Truth aus dem Export-Meta (1-Hz-direkt) ins Format der
    # Bestands-Kette bringen (validation_sims liest *_kWh_per_km-Spalten)
    meta = pd.read_csv(PROFILES_DIR / "trips_meta.csv").set_index("label")
    LABELS = list(meta.index)
    if args.trips:
        LABELS = [l for l in LABELS if l in args.trips.split(",")]
    version = kpa._next_version(net_dir, base=f"profile_{LABELS[0]}_250m")
    energy_rows = []
    for label in meta.index:  # immer alle (auch bei --trips-Filter)
        m = meta.loc[label]
        energy_rows.append({
            "trip": label, "dist_km": m["dist_odo_km"],
            "BatteryPower_kWh_per_km": m["E_bat_kWh"] / m["dist_odo_km"],
            "HVACpower_kWh_per_km": m["E_hvac_kWh"] / m["dist_odo_km"],
            "PowerOtherAuxiliary_kWh_per_km": m["E_aux_kWh"] / m["dist_odo_km"],
            "MotorPower_kWh_per_km": m["E_motor_kWh"] / m["dist_odo_km"],
        })
    pd.DataFrame(energy_rows).to_csv(net_dir / "realtrip_measured_energy.csv",
                                     index=False)

    rows = []
    for label in LABELS:
        prof_csv = pd.read_csv(PROFILES_DIR / f"trip_{label}.csv")
        ok = np.isfinite(prof_csv["alt_m"])
        s_meas = prof_csv.loc[ok, "s_m"].to_numpy()
        z_meas = prof_csv.loc[ok, "alt_m"].to_numpy()
        v_all_s = prof_csv["s_m"].to_numpy()
        v_all = prof_csv["v_ms"].to_numpy()

        alignment = None
        for L in link_lengths:
            net_path = net_dir / f"section_{label}_{L}m.xml.gz"
            if not net_path.exists():
                print(f"  WARNING: {net_path.name} fehlt — skip")
                continue
            prof = load_chain_profile(net_path)
            if alignment is None:
                alignment = rme.align_profiles(prof["s"], prof["z"], s_meas, z_meas)
            direction, offset, scale, corr = alignment

            if direction == -1:
                s_m = s_meas[-1] - s_meas[::-1]
                z_m = z_meas[::-1]
                s_v = v_all_s[-1] - v_all_s[::-1]
                v_m = v_all[::-1]
            else:
                s_m, z_m = s_meas, z_meas
                s_v, v_m = v_all_s, v_all
            s_m = s_m * scale + offset
            s_v = s_v * scale + offset

            grid = np.arange(0, prof["s"][-1], rme.GRID_M)
            zn = np.interp(grid, prof["s"], prof["z"])
            zm = np.interp(grid, s_m, z_m)
            dz = zn - zm
            bias = float(np.nanmedian(dz))
            row = {
                "trip": label, "max_link_length": L,
                "direction": direction, "offset_m": offset, "scale": scale,
                "corr": corr,
                "net_length_km": prof["s"][-1] / 1000.0,
                "meas_length_km": (s_meas[-1] - s_meas[0]) / 1000.0,
                "elev_bias_m": bias,
                "elev_mae_raw_m": float(np.nanmean(np.abs(dz))),
                "elev_mae_debiased_m": float(np.nanmean(np.abs(dz - bias))),
                "elev_rmse_debiased_m": float(np.sqrt(np.nanmean((dz - bias) ** 2))),
            }
            for b in rme.SLOPE_BASELINES_M:
                row[f"slope_mae_{b}m_pct"] = rme.slope_mae(
                    prof["s"], prof["z"], s_m, z_m, b)
            rows.append(row)

            # Realspeed: freespeed je Link = Mittel der Messgeschwindigkeit
            new_root = copy.deepcopy(prof["root"])
            link_elems = {l.get("id"): l
                          for l in new_root.find("links").findall("link")}
            span = {}
            for cl in prof["chain"]:
                mask = (s_v >= cl["s0"]) & (s_v < cl["s1"])
                if mask.sum() >= 1:
                    v_link = float(np.nanmean(v_m[mask]))
                else:
                    v_link = float(np.interp(0.5 * (cl["s0"] + cl["s1"]), s_v, v_m))
                span[cl["id"]] = max(v_link, rme.MIN_FREESPEED_MS)
            for lid, elem in link_elems.items():
                if lid in span:
                    elem.set("freespeed", f"{span[lid]:.3f}")
            out_path = net_dir / f"section_{label}_{L}m_realspeed.xml.gz"
            ET.indent(new_root, space="  ")
            with gzip.open(out_path, "wt", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write('<!DOCTYPE network SYSTEM '
                        '"http://www.matsim.org/files/dtd/network_v2.dtd">\n')
                f.write(ET.tostring(new_root, encoding="unicode"))
                f.write('\n')

            fig, ax = plt.subplots(figsize=(11, 4.2))
            ax.plot(grid / 1000.0, zm, color="#555555", lw=1.0, label="measured")
            ax.plot(grid / 1000.0, zn, color="#C0392B", lw=1.0,
                    label=f"network {L} m")
            ax.set_xlabel("Distance [km]", fontsize=11)
            ax.set_ylabel("Elevation [m]", fontsize=11)
            ax.grid(True, alpha=0.25)
            ax.legend(loc="best", fontsize=9)
            fig.tight_layout()
            kpa._savefig(fig, net_dir, f"profile_{label}_{L}m", version)
            plt.close(fig)

        print(f"{label}: fertig")

    df = pd.DataFrame(rows)
    out_csv = net_dir / "realtrip_profile_validation.csv"
    if args.trips and out_csv.exists():
        old = pd.read_csv(out_csv)
        df = pd.concat([old[~old["trip"].isin(LABELS)], df], ignore_index=True)
    df.to_csv(out_csv, index=False)
    print("\n=== Hoehen-/Steigungs-Validierung (Netz vs. Telemetrie) ===")
    cols = ["trip", "max_link_length", "direction", "offset_m", "scale", "corr",
            "elev_bias_m", "elev_mae_debiased_m", "elev_rmse_debiased_m"] + \
           [f"slope_mae_{b}m_pct" for b in rme.SLOPE_BASELINES_M]
    print(df[cols].round(3).to_string(index=False))
    print(f"\nRealspeed-Netze + CSVs + Profile in: {net_dir}")


if __name__ == "__main__":
    main()
