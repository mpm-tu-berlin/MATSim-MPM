# -*- coding: utf-8 -*-
"""Steigungsvergleich HoLa-Truck-Hoehendaten vs. DTM (Fehlerbudget fuer Paper 2).

Design (User-Vorgaben 2026-07-06):
- NUR Steigung (offset-invariant -> GPS/Baro-Datumsfrage entfaellt), als Funktion
  der Basislaenge ds in {25, 50, 100, 250, 500} m.
- Gewertet werden nur Punkte AUF dem gefilterten Netz (motorway/trunk/primary,
  V2-Netz als On-Network-Filter) -> Testgelaende/Landstrassen fliegen raus.
- Zweistufige Aggregation gegen Korridor-Ungleichgewicht: (1) je Orts-Bin
  (50-m-Raster + Richtungsoktant) und Durchfahrt ein Wert; (2) Netz-Statistik
  ueber BINS (jeder Bin zaehlt einmal), nicht ueber Punkte.
- Wiederholbarkeit: Streuung der Truck-Steigung ueber Durchfahrten desselben Bins
  (>= 3 Durchfahrten) = Messunsicherheits-Schranke der Flottendaten.
- VERTRAULICH: Rohdaten werden nie ausgegeben; alle Outputs sind Aggregate.
  Datenablage + Output-Verzeichnis sind gitignored (data/*).

Referenz-Steigung: rohes DTM, bilinear an den Truck-Positionen gesampelt
(script04.sample_heights aus dem Netzgen-Worktree) -> misst DTM-Qualitaet direkt,
unabhaengig von Glaettung/Netz.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"

DATA_DIR = _NETGEN_DIR / "data" / "HoLa-Truck-Heights"
NETWORK = _NETGEN_DIR / "data" / "germany_network_250m_V2.xml.gz"
DTM_PATH = _SCRIPT_DIR / "data" / "DTM Germany 20m v3b by Sonny.tif"
OUT_DIR = _NETGEN_DIR / "data" / "truck_dtm_slope_eval"

BASELINES_M = [25, 50, 100, 250, 500]
GRID_M = 50.0            # Orts-Bin-Raster
MATCH_TOL_M = 35.0       # max. Abstand Punkt->Netz
MIN_SPEED_KMH = 10.0     # Stillstand/Rangieren raus
TRIP_GAP_S = 600         # Zeitluecke -> neue Durchfahrt/Fahrt
MIN_PASSES_REPEAT = 3    # Mindest-Durchfahrten fuer Wiederholbarkeit
NET_CRS = 25832          # metrisch fuer Distanzen/Raster (UTM32)


def _import_script04():
    from importlib.util import spec_from_file_location, module_from_spec
    p = _NETGEN_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    spec = spec_from_file_location("script04", str(p))
    mod = module_from_spec(spec)
    sys.modules["script04"] = mod
    spec.loader.exec_module(mod)
    return mod


def load_network_sample_tree():
    """cKDTree ueber ~50-m-Samples aller Netz-Links (On-Network-Filter)."""
    import gzip
    import xml.etree.ElementTree as ET
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    tf = Transformer.from_crs("EPSG:4839", f"EPSG:{NET_CRS}", always_xy=True)
    nodes, segs = {}, []
    with gzip.open(NETWORK, "rb") as f:
        for _, el in ET.iterparse(f):
            if el.tag == "node":
                nodes[el.get("id")] = (float(el.get("x")), float(el.get("y")))
                el.clear()
            elif el.tag == "link":
                u, v = nodes.get(el.get("from")), nodes.get(el.get("to"))
                if u and v:
                    segs.append((u, v))
                el.clear()
    pts = []
    for (x1, y1), (x2, y2) in segs:
        L = float(np.hypot(x2 - x1, y2 - y1))
        n = max(2, int(L // 50) + 1)
        t = np.linspace(0.0, 1.0, n)
        pts.append(np.column_stack([x1 + t * (x2 - x1), y1 + t * (y2 - y1)]))
    P = np.vstack(pts)
    mx, my = tf.transform(P[:, 0], P[:, 1])
    print(f"Netz-Filter: {len(segs)} Links, {len(P)} Sample-Punkte")
    return cKDTree(np.column_stack([mx, my]))


def iter_passes(df):
    """Zerlegt eine Wochen-Datei in Durchfahrten (Zeitluecken > TRIP_GAP_S)."""
    df = df.dropna(subset=["Latitude", "Longitude", "Altitude", "signal_ts"])
    df = df[df["Velocity"].fillna(0.0) >= MIN_SPEED_KMH]
    df = df.sort_values("signal_ts")
    if len(df) < 10:
        return
    gaps = df["signal_ts"].diff().dt.total_seconds().fillna(0.0)
    for _, part in df.groupby((gaps > TRIP_GAP_S).cumsum()):
        if len(part) >= 10:
            yield part


def process_pass(part, pass_id, tree, dtm, s04, tf, sink):
    """Eine Durchfahrt: matchen, DTM sampeln, Steigungen je Basislaenge binnen."""
    lon = part["Longitude"].to_numpy(float)
    lat = part["Latitude"].to_numpy(float)
    zt = part["Altitude"].to_numpy(float)
    mx, my = tf.transform(lon, lat)
    mx = np.asarray(mx, float); my = np.asarray(my, float)

    # ungueltige GPS-Fixe (0/0, ausserhalb Projektionsbereich -> inf) raus
    finite = np.isfinite(mx) & np.isfinite(my)
    if finite.sum() < 10:
        return 0
    mx, my, zt = mx[finite], my[finite], zt[finite]

    # On-Network-Filter
    dist, _ = tree.query(np.column_stack([mx, my]), k=1,
                         distance_upper_bound=MATCH_TOL_M)
    on = np.isfinite(dist)
    if on.sum() < 10:
        return 0
    # zusammenhaengende On-Network-Segmente getrennt behandeln (Luecken kappen)
    idx = np.flatnonzero(on)
    breaks = np.flatnonzero(np.diff(idx) > 5)
    segments = np.split(idx, breaks + 1)

    n_used = 0
    for seg in segments:
        if len(seg) < 10:
            continue
        sx, sy, sz = mx[seg], my[seg], zt[seg]
        step = np.hypot(np.diff(sx), np.diff(sy))
        # GPS-Spruenge kappen (Teleport/Tunnel-Reacquire)
        ok = np.concatenate([[True], step < 200.0])
        sx, sy, sz = sx[ok], sy[ok], sz[ok]
        if len(sx) < 10:
            continue
        s = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(sx), np.diff(sy)))])
        if s[-1] < min(BASELINES_M) * 2:
            continue
        lo, la = tf.transform(sx, sy, direction="INVERSE")
        zd = s04.sample_heights(str(DTM_PATH), np.asarray(lo), np.asarray(la))
        fin = np.isfinite(zd) & np.isfinite(sz)
        if fin.sum() < 10:
            continue
        s, sx, sy, sz, zd = s[fin], sx[fin], sy[fin], sz[fin], zd[fin]
        # streng monotone Bogenlaenge fuer np.interp
        mono = np.concatenate([[True], np.diff(s) > 0.5])
        s, sx, sy, sz, zd = s[mono], sx[mono], sy[mono], sz[mono], zd[mono]
        if len(s) < 10:
            continue

        for ds in BASELINES_M:
            if s[-1] < 2 * ds:
                continue
            grid = np.arange(0.0, s[-1], ds)
            if len(grid) < 3:
                continue
            gzt = np.interp(grid, s, sz)
            gzd = np.interp(grid, s, zd)
            gx = np.interp(grid, s, sx)
            gy = np.interp(grid, s, sy)
            g_t = np.diff(gzt) / ds
            g_d = np.diff(gzd) / ds
            midx = 0.5 * (gx[:-1] + gx[1:])
            midy = 0.5 * (gy[:-1] + gy[1:])
            head = np.degrees(np.arctan2(np.diff(gy), np.diff(gx))) % 360.0
            octant = (head // 45.0).astype(int)
            bx = np.round(midx / GRID_M).astype(np.int64)
            by = np.round(midy / GRID_M).astype(np.int64)
            for j in range(len(g_t)):
                sink[(int(bx[j]), int(by[j]), int(octant[j]), ds)].append(
                    (pass_id, float(g_t[j]), float(g_d[j])))
        n_used += len(s)
    return n_used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-files", type=int, default=None,
                    help="nur die ersten N Dateien (Smoke-Test)")
    args = ap.parse_args()

    from pyproj import Transformer
    s04 = _import_script04()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{NET_CRS}", always_xy=True)
    tree = load_network_sample_tree()
    dtm = s04.load_dtm(str(DTM_PATH))

    files = sorted(DATA_DIR.glob("*.parquet"))
    if args.limit_files:
        files = files[:args.limit_files]
    print(f"{len(files)} Dateien")

    # sink: (bin_x, bin_y, oktant, ds) -> [(pass_id, g_truck, g_dtm), ...]
    sink = defaultdict(list)
    n_pts = 0
    for fi, f in enumerate(files):
        df = pd.read_parquet(f, columns=["Latitude", "Longitude", "Altitude",
                                          "Velocity", "signal_ts"])
        for k, part in enumerate(iter_passes(df)):
            n_pts += process_pass(part, f"{f.stem}#{k}", tree, dtm, s04, tf, sink)
        print(f"  [{fi+1}/{len(files)}] {f.stem}: kumuliert {n_pts} Punkte, "
              f"{len(sink)} (Bin,ds)-Zellen", flush=True)

    # ---- Stufe 1: je Bin ueber Durchfahrten aggregieren ----
    rows = []
    for (bx, by, octant, ds), vals in sink.items():
        per_pass = defaultdict(list)
        for pid, gt, gd in vals:
            per_pass[pid].append((gt, gd))
        gts = np.array([np.mean([v[0] for v in lst]) for lst in per_pass.values()])
        gds = np.array([np.mean([v[1] for v in lst]) for lst in per_pass.values()])
        rows.append({
            "ds": ds, "n_passes": len(per_pass),
            "g_truck_med": float(np.median(gts)),
            "g_dtm_med": float(np.median(gds)),
            "g_truck_std": float(np.std(gts, ddof=1)) if len(gts) >= MIN_PASSES_REPEAT else np.nan,
            "vehicle_set": ",".join(sorted({p.split("_week_")[0] for p in per_pass})),
        })
    bins = pd.DataFrame(rows)
    bins.to_csv(OUT_DIR / "bins_aggregated.csv", index=False)

    # ---- Stufe 2: Netz-Statistik ueber Bins ----
    print("\n=== DTM-Steigungsfehler (Bin-gewichtet, Median-Truck vs. DTM) ===")
    print(f"{'ds [m]':>7} {'n_bins':>8} {'MAE [%]':>9} {'RMSE [%]':>9} "
          f"{'Bias [%]':>9} | {'Wdh. sig p50':>12} {'p90 [%]':>8} {'n_bins(>=3)':>11}")
    summary = []
    for ds in BASELINES_M:
        b = bins[bins.ds == ds]
        if b.empty:
            continue
        d = (b.g_truck_med - b.g_dtm_med).to_numpy()
        rep = b.g_truck_std.dropna().to_numpy()
        row = {
            "ds": ds, "n_bins": len(b),
            "mae_pct": float(np.mean(np.abs(d))) * 100,
            "rmse_pct": float(np.sqrt(np.mean(d ** 2))) * 100,
            "bias_pct": float(np.mean(d)) * 100,
            "repeat_sigma_p50_pct": float(np.median(rep)) * 100 if len(rep) else np.nan,
            "repeat_sigma_p90_pct": float(np.percentile(rep, 90)) * 100 if len(rep) else np.nan,
            "n_bins_repeat": int(len(rep)),
        }
        summary.append(row)
        print(f"{ds:>7} {row['n_bins']:>8} {row['mae_pct']:>9.3f} {row['rmse_pct']:>9.3f} "
              f"{row['bias_pct']:>+9.3f} | {row['repeat_sigma_p50_pct']:>12.3f} "
              f"{row['repeat_sigma_p90_pct']:>8.3f} {row['n_bins_repeat']:>11}")
    pd.DataFrame(summary).to_csv(OUT_DIR / "summary_by_baseline.csv", index=False)

    # Fahrzeug-Bias (Ausreisser-Sensorik): signierte Abweichung je Fahrzeug @50 m
    b50 = [(pid.split("_week_")[0], gt - gd)
           for (bx, by, o, ds), vals in sink.items() if ds == 50
           for pid, gt, gd in vals]
    if b50:
        vb = pd.DataFrame(b50, columns=["vehicle", "dev"]).groupby("vehicle")["dev"] \
               .agg(["count", "mean", "std"])
        vb.to_csv(OUT_DIR / "vehicle_bias_50m.csv")
        print("\nFahrzeug-Bias @50 m (mean der signierten Steigungsabweichung):")
        print((vb["mean"] * 100).round(3).to_string())

    print(f"\nOutputs (aggregiert, gitignored): {OUT_DIR}")


if __name__ == "__main__":
    main()
