# -*- coding: utf-8 -*-
"""
WP4: Realfahrt-Messprofile (Hoehe + Geschwindigkeit, wegstrecken-indiziert)
gegen die NEUEN V2-Routen-Netze — Validierung + Realspeed-Mapping.

Eingabe: Excel mit Spalten "Mileage <t>" [km], "Altitude <t>" [m],
"Velocity <t>" [km/h] je Fahrt (25-m-Raster); VERTRAULICH/gitignored —
dieses Skript liest sie nur maschinell und gibt ausschliesslich Aggregate aus.

Je Fahrt und Netz-Aufloesung:
  1. Netz-Kettenprofil extrahieren (geordneter Pfad, Bogenlaenge, z, freespeed).
  2. Ausrichtung Messung<->Netz ueber Kreuzkorrelation der detrendeten
     Hoehenprofile (Offset-Suche +-3 km, beide Fahrtrichtungen).
  3. Hoehen-Validierung: Elevations-MAE/RMSE (roh + offsetbereinigt) und
     Steigungs-MAE auf Basislaengen [100, 250, 500] m — gleiche Methodik wie
     die HoLa-Netzstudie, aber routen-exakt.
  4. Realspeed-Mapping: freespeed je Link = mittlere Messgeschwindigkeit ueber
     die Link-Bogenspanne -> section_<t>_<L>m_realspeed.xml.gz.

Ausgaben (alle im gitignorten Netz-Ordner):
  - realtrip_profile_validation.csv   Aggregat-Kennzahlen je Fahrt/Aufloesung
  - profile_<t>_<L>m_V<N>.pdf/png     Hoehenprofil Netz vs. Messung (Aggregat-Plot)
  - section_<t>_<L>m_realspeed.xml.gz Realspeed-Variante
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
GRID_M = 25.0
OFFSET_SEARCH_M = 3000.0
SLOPE_BASELINES_M = [100, 250, 500]
MIN_FREESPEED_MS = 2.0  # Stopps: freespeed 0 ist in MATSim unzulaessig

TRIPS = {"19t": "19t", "24t": "25t", "43t": "43t"}  # Label -> Excel-Spaltensuffix


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_chain_profile(path):
    """Geordnetes Kettenprofil eines Routen-Netzes.

    Rueckgabe: dict mit s_nodes [m], z_nodes [m], link-Liste in Pfadordnung
    (id, s0, s1, length) und dem geparsten ElementTree (fuer Realspeed-Export).
    """
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()
    nodes = {n.get("id"): (float(n.get("x")), float(n.get("y")),
                           float(n.get("z")) if n.get("z") else np.nan)
             for n in root.find("nodes").findall("node")}
    links = {}
    adj = {}
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        links[(u, v)] = l
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    ends = [n for n, nb in adj.items() if len(nb) == 1]
    if len(ends) != 2:
        raise ValueError(f"{path}: {len(ends)} Endpunkte statt 2")
    # Kette ablaufen
    order = [ends[0]]
    prev = None
    while order[-1] != ends[1]:
        nxt = [n for n in adj[order[-1]] if n != prev]
        prev = order[-1]
        order.append(nxt[0])
    s = [0.0]
    chain_links = []
    for a, b in zip(order[:-1], order[1:]):
        l = links.get((a, b)) or links.get((b, a))
        length = float(l.get("length"))
        chain_links.append({"id": l.get("id"), "s0": s[-1], "s1": s[-1] + length,
                            "elem_from": a, "elem_to": b})
        s.append(s[-1] + length)
    z = [nodes[n][2] for n in order]
    return {"s": np.array(s), "z": np.array(z), "chain": chain_links,
            "tree": tree, "root": root, "order": order,
            "node_xy": {n: (nodes[n][0], nodes[n][1]) for n in (order[0], order[-1])}}


SCALE_SEARCH = np.linspace(0.985, 1.015, 31)  # Odometer-/Projektions-Skalendrift


def align_profiles(s_net, z_net, s_meas, z_meas):
    """Kreuzkorrelations-Ausrichtung mit Offset UND Skalenfaktor, beide
    Richtungen. Netz-Laengen stammen aus projizierten Geometrien (UTM-Massstab,
    Kurvenschnitt), der Odometer misst echte Fahrstrecke (+ eigene Kalibrierung)
    -> ohne Streckung schmiert die Ausrichtung am Routenende (User-Hinweis
    2026-07-07). Rueckgabe (direction, offset_m, scale, corr):
    Messung bei Bogenlaenge s entspricht Netz bei s*scale + offset."""
    # Korrelation auf dem STEIGUNGSPROFIL (Ableitung), nicht auf der Hoehe:
    # ein fast-flaches Hoehenprofil sieht rueckwaerts aehnlich aus (19t:
    # z-corr 0,99 fuer die FALSCHE Richtung -> Leistungsvergleich corr -0,49),
    # aber die Steigungen sind vorzeichenverkehrt — die Ableitung
    # disambiguiert die Fahrtrichtung.
    grid = np.arange(0, max(s_net[-1], s_meas[-1]) * 1.02 + GRID_M, GRID_M)
    gn = np.diff(np.interp(grid, s_net, z_net))
    n_off = int(OFFSET_SEARCH_M / GRID_M)
    best = None
    for direction in (+1, -1):
        zm_s = s_meas if direction == +1 else (s_meas[-1] - s_meas[::-1])
        zm_z = z_meas if direction == +1 else z_meas[::-1]
        for scale in SCALE_SEARCH:
            gm = np.diff(np.interp(grid, zm_s * scale, zm_z))
            for k in range(-n_off, n_off + 1):
                if k >= 0:
                    a, b = gn[k:], gm[:len(gn) - k if k else None]
                else:
                    a, b = gn[:k], gm[-k:]
                m = min(len(a), len(b))
                a, b = a[:m], b[:m]
                valid = np.isfinite(a) & np.isfinite(b)
                if valid.sum() < 100:
                    continue
                aa, bb = a[valid] - a[valid].mean(), b[valid] - b[valid].mean()
                denom = aa.std() * bb.std()
                corr = float((aa * bb).mean() / denom) if denom > 0 else -1
                if best is None or corr > best[3]:
                    best = (direction, k * GRID_M, float(scale), corr)
    return best


def slope_mae(s_net, z_net, s_m_aligned, z_m, baseline):
    """Steigungs-MAE [%] auf Basislaenge; Messung bereits in Netz-Bogenlaenge."""
    grid = np.arange(0, s_net[-1], baseline)
    zn = np.interp(grid, s_net, z_net)
    zm = np.interp(grid, s_m_aligned, z_m)
    gn = np.diff(zn) / baseline
    gm = np.diff(zm) / baseline
    valid = np.isfinite(gn) & np.isfinite(gm)
    return 100.0 * float(np.mean(np.abs(gn[valid] - gm[valid])))


def main():
    parser = argparse.ArgumentParser(description="Realfahrt-Messprofile vs. V2-Routen-Netze.")
    parser.add_argument("--networks-dir", type=str, required=True,
                        help="Ordner mit section_<t>_<L>m.xml.gz (gitignored)")
    parser.add_argument("--measurements", type=str,
                        default=str(_SCRIPT_DIR.parent / "calibration" / "data"
                                    / "Geschwindigkeitsprofile.xlsx"))
    parser.add_argument("--link-lengths", type=str, default="250")
    args = parser.parse_args()

    net_dir = Path(args.networks_dir)
    link_lengths = [int(x) for x in args.link_lengths.split(",")]
    meas = pd.read_excel(args.measurements)
    kpa = _import_module("kpa", "knee_point_analysis.py")
    version = kpa._next_version(net_dir, base="profile_19t_250m")

    # Leistungsspalten je Fahrt (pandas dedupliziert: 19t ohne Suffix, 25t=.1, 43t=.2)
    POWER_SUFFIX = {"19t": "", "24t": ".1", "43t": ".2"}
    POWER_COLS = ("BatteryPower", "HVACpower", "MotorPower", "PowerOtherAuxiliary")

    rows = []
    energy_rows = []
    for label, col in TRIPS.items():
        m = meas[[f"Mileage {col}", f"Altitude {col}", f"Velocity {col}"]].dropna()
        s_meas = (m[f"Mileage {col}"].values - m[f"Mileage {col}"].values[0]) * 1000.0
        z_meas = m[f"Altitude {col}"].values
        v_meas = m[f"Velocity {col}"].values / 3.6  # [m/s]

        # --- Batterieseitige Energie-Ground-Truth (nur Aggregate ausgeben) ---
        sfx = POWER_SUFFIX[label]
        pcols = [f"{c}{sfx}" for c in POWER_COLS if f"{c}{sfx}" in meas.columns]
        if pcols:
            pm = meas[[f"Mileage {col}", f"Velocity {col}"] + pcols].dropna()
            sp = (pm[f"Mileage {col}"].values - pm[f"Mileage {col}"].values[0]) * 1000.0
            vp = pm[f"Velocity {col}"].values / 3.6
            ds = np.diff(sp)
            v_mid = np.maximum(0.5 * (vp[1:] + vp[:-1]), 0.5)  # [m/s]
            dt = ds / v_mid                                     # [s]
            dist_km = (sp[-1] - sp[0]) / 1000.0
            erow = {"trip": label, "dist_km": dist_km}
            for c in POWER_COLS:
                cn = f"{c}{sfx}"
                if cn not in pm.columns:
                    continue
                p = pm[cn].values  # Einheit unbekannt: kW oder W — per Aggregat pruefen
                p_mid = 0.5 * (p[1:] + p[:-1])
                med_abs = float(np.nanmedian(np.abs(p[np.abs(p) > 0])))
                unit_kw = med_abs < 2000.0  # kW-Skala, sonst W
                e_kwh = float(np.nansum(p_mid * dt)) / (3600.0 if unit_kw else 3.6e6)
                erow[f"{c}_kWh"] = e_kwh
                erow[f"{c}_kWh_per_km"] = e_kwh / dist_km
                erow[f"{c}_unit"] = "kW" if unit_kw else "W"
                erow[f"{c}_median_abs"] = med_abs
            energy_rows.append(erow)

        # Ausrichtung EINMAL je Fahrt auf dem feinsten Netz bestimmen und fuer
        # alle Stufen wiederverwenden (gleiche Hoehenbasis; verhindert lokale
        # Optima auf groben Profilen — 24t sprang sonst auf Offset 1,7 km)
        alignment = None
        for L in link_lengths:
            net_path = net_dir / f"section_{label}_{L}m.xml.gz"
            if not net_path.exists():
                print(f"  WARNING: {net_path.name} fehlt — skip")
                continue
            prof = load_chain_profile(net_path)
            if alignment is None:
                alignment = align_profiles(prof["s"], prof["z"], s_meas, z_meas)
            direction, offset, scale, corr = alignment

            # Messung in Netz-Bogenlaenge (Richtung + Skala + Offset)
            if direction == -1:
                s_m = s_meas[-1] - s_meas[::-1]
                z_m = z_meas[::-1]
                v_m = v_meas[::-1]
            else:
                s_m, z_m, v_m = s_meas, z_meas, v_meas
            s_m = s_m * scale + offset

            grid = np.arange(0, prof["s"][-1], GRID_M)
            zn = np.interp(grid, prof["s"], prof["z"])
            zm = np.interp(grid, s_m, z_m)
            dz = zn - zm
            bias = float(np.nanmedian(dz))
            row = {
                "trip": label, "max_link_length": L,
                "direction": direction, "offset_m": offset, "scale": scale, "corr": corr,
                "net_length_km": prof["s"][-1] / 1000.0,
                "meas_length_km": (s_meas[-1] - s_meas[0]) / 1000.0,
                "elev_bias_m": bias,
                "elev_mae_raw_m": float(np.nanmean(np.abs(dz))),
                "elev_mae_debiased_m": float(np.nanmean(np.abs(dz - bias))),
                "elev_rmse_debiased_m": float(np.sqrt(np.nanmean((dz - bias) ** 2))),
            }
            for b in SLOPE_BASELINES_M:
                row[f"slope_mae_{b}m_pct"] = slope_mae(prof["s"], prof["z"], s_m, z_m, b)
            rows.append(row)

            # --- Realspeed-Mapping: freespeed je Link = Mittel der Messung ---
            new_root = copy.deepcopy(prof["root"])
            link_elems = {l.get("id"): l for l in new_root.find("links").findall("link")}
            span = {}
            for cl in prof["chain"]:
                mask = (s_m >= cl["s0"]) & (s_m < cl["s1"])
                if mask.sum() >= 1:
                    v_link = float(np.mean(v_m[mask]))
                else:
                    v_link = float(np.interp(0.5 * (cl["s0"] + cl["s1"]), s_m, v_m))
                span[cl["id"]] = max(v_link, MIN_FREESPEED_MS)
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

            # --- Profilplot (Aggregat: Hoehenlinien, keine Rohdaten-Tabellen) ---
            fig, ax = plt.subplots(figsize=(11, 4.2))
            ax.plot(grid / 1000.0, zm, color="#555555", lw=1.0, label="measured")
            ax.plot(grid / 1000.0, zn, color="#C0392B", lw=1.0, label=f"network {L} m")
            ax.set_xlabel("Distance (km)", fontsize=11)
            ax.set_ylabel("Elevation (m)", fontsize=11)
            ax.grid(True, alpha=0.25)
            ax.legend(loc="best", fontsize=9)
            fig.tight_layout()
            kpa._savefig(fig, net_dir, f"profile_{label}_{L}m", version)
            plt.close(fig)

        print(f"{label}: fertig")

    df = pd.DataFrame(rows)
    df.to_csv(net_dir / "realtrip_profile_validation.csv", index=False)
    print("\n=== Hoehen-/Steigungs-Validierung (Netz vs. Messung) ===")
    cols = ["trip", "max_link_length", "direction", "offset_m", "scale", "corr",
            "elev_bias_m", "elev_mae_debiased_m", "elev_rmse_debiased_m"] + \
           [f"slope_mae_{b}m_pct" for b in SLOPE_BASELINES_M]
    print(df[cols].round(3).to_string(index=False))

    if energy_rows:
        edf = pd.DataFrame(energy_rows)
        edf.to_csv(net_dir / "realtrip_measured_energy.csv", index=False)
        print("\n=== Gemessene Energie-Aggregate (batterieseitige Ground Truth) ===")
        print(edf.round(3).to_string(index=False))

    print(f"\nRealspeed-Netze + CSVs + Profile in: {net_dir}")


if __name__ == "__main__":
    main()
