# -*- coding: utf-8 -*-
"""Bauwerks-Höhenfehler gegen die GEMESSENEN Höhen der Realfahrten.

Beantwortet die Vorfrage zum Brücken-Fix: weicht das Netz an getaggten
Brücken/Tunneln nachweisbar von der Messung ab, wie groß ist der Fehler, und
sitzt er dort, wo die Ankerlogik ihn vorhersagt (an den Bauwerksenden)?

Vorgehen je Route:
  1. Routen-Netz (Produktionshöhen) als Kettenprofil (s_net, z_net) laden,
     Knotenkoordinaten aus dem XML.
  2. Messprofil (s_meas, alt_m) einlesen und per Kreuzkorrelation auf die
     Netz-Bogenlänge ausrichten (dieselbe align_profiles wie in der
     WP4-Validierungskette).
  3. Bauwerksausdehnungen aus den DETAIL-Segmenten des Germany-GPKG holen
     (bridge/tunnel in OSM-Granularität) und geometrisch auf die Route
     projizieren -> Intervalle in Routen-Bogenlänge.
  4. Differenz dz = z_netz - z_mess auf 10-m-Raster, GPS-Drift per rollierendem
     Median (3 km) entfernen, wobei die Basislinie NUR aus Nicht-Bauwerks-
     Abschnitten geschätzt und über die Bauwerke interpoliert wird.
  5. Kennzahlen je Bauwerk (Fehler an den Ankerpunkten, im Deck, Anstiegs-
     überschuss) plus Kontrollverteilung auf bauwerksfreier Strecke = Rauschboden.

DATENSCHUTZ: Eingaben sind private Realfahrtdaten, Ausgaben landen ausschließlich
im gitignorten data/-Ordner. stdout nur Aggregate.

Aufruf:
  python structure_groundtruth_check.py --all
  python structure_groundtruth_check.py --trip 43t --plot-top 8
"""
import argparse
import sys
from datetime import datetime
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio
from pyproj import Transformer
from scipy.spatial import cKDTree

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).parent
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
DETAILED_GPKG = _NETGEN_DIR / "data" / "germany_detailed_sorted_DF.gpkg"
OUT_ROOT = _SCRIPT_DIR / "data" / "structure_groundtruth"

TARGET_EPSG = 4839
GRID_M = 10.0             # Auswerteraster entlang der Route
MATCH_TOL_M = 25.0        # max. Abstand Detailsegment <-> Route
MERGE_GAP_M = 25.0        # Lücke, bis zu der zwei Intervalle verschmelzen
BASELINE_WIN_M = 3000.0   # Fenster des rollierenden Medians (GPS-Drift)
APPROACH_M = 150.0        # Zone vor/nach dem Bauwerk (Widerlager/Damm)
PLOT_CONTEXT_M = 5000.0


def _import(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ Routendaten

def route_profile(rep, rme, label, resolution=None):
    """(s_net, z_net, X, Y, s_meas_aligned, z_meas, netzname) einer Route."""
    net_path, s_meas, z_meas, mass = rep.resolve_route(label, resolution)
    prof = rme.load_chain_profile(net_path)
    s_net, z_net, order, root = prof["s"], prof["z"], prof["order"], prof["root"]
    nxy = {n.get("id"): (float(n.get("x")), float(n.get("y")))
           for n in root.find("nodes").findall("node")}
    X = np.array([nxy[n][0] for n in order], float)
    Y = np.array([nxy[n][1] for n in order], float)

    direction, offset, scale, corr = rme.align_profiles(s_net, z_net, s_meas, z_meas)
    if direction < 0:
        s_al = (s_meas[-1] - s_meas[::-1]) * scale + offset
        z_al = np.asarray(z_meas, float)[::-1]
    else:
        s_al = np.asarray(s_meas, float) * scale + offset
        z_al = np.asarray(z_meas, float)
    return dict(label=label, net=net_path.name, mass_t=mass / 1000.0,
                s_net=s_net, z_net=z_net, X=X, Y=Y,
                s_meas=s_al, z_meas=z_al, align_corr=float(corr),
                align_dir=int(direction), align_scale=float(scale))


def structure_intervals(rt, s04):
    """Bauwerks-Intervalle in Routen-Bogenlänge aus den Detail-Segmenten."""
    s_net, X, Y = rt["s_net"], rt["X"], rt["Y"]
    grid = np.arange(0.0, float(s_net[-1]) + GRID_M, GRID_M)
    gx = np.interp(grid, s_net, X)
    gy = np.interp(grid, s_net, Y)
    tree = cKDTree(np.column_stack([gx, gy]))

    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{TARGET_EPSG}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{TARGET_EPSG}", "EPSG:4326", always_xy=True)
    lo, la = inv.transform(np.array([X.min(), X.max()]), np.array([Y.min(), Y.max()]))
    pad = 0.02
    bbox = (float(min(lo)) - pad, float(min(la)) - pad,
            float(max(lo)) + pad, float(max(la)) + pad)
    det = pyogrio.read_dataframe(DETAILED_GPKG, layer="edges", bbox=bbox)
    if det.empty:
        return [], grid

    ivals = []
    for _, row in det.iterrows():
        if not s04._is_structure(row):
            continue
        g = row.geometry
        if g is None or g.is_empty:
            continue
        c = np.asarray(list(g.coords), float)
        px, py = tf.transform(c[:, 0], c[:, 1])
        d, idx = tree.query(np.column_stack([px, py]))
        if np.median(d) > MATCH_TOL_M:
            continue
        ss = grid[idx]
        s0, s1 = float(ss.min()), float(ss.max())
        seg_len = float(np.hypot(np.diff(px), np.diff(py)).sum())
        if s1 - s0 > max(3.0 * seg_len, 200.0):   # Route läuft mehrfach vorbei
            continue
        # Parallelitaet: ein Bauwerk, auf dem die Route FAEHRT, deckt entlang der
        # Route etwa seine eigene Laenge ab. Eine QUERENDE Ueberfuehrung liegt zwar
        # nahe an der Route, projiziert aber nur auf einen kurzen Abschnitt.
        if seg_len > 25.0 and (s1 - s0) < 0.4 * seg_len:
            continue
        kind = "tunnel" if _is_tunnel(row) else "bridge"
        ivals.append([s0, s1, kind, float(np.median(d))])

    ivals.sort(key=lambda r: r[0])
    merged = []
    for iv in ivals:
        if merged and iv[0] - merged[-1][1] <= MERGE_GAP_M:
            merged[-1][1] = max(merged[-1][1], iv[1])
            if iv[2] not in merged[-1][2].split("+"):
                merged[-1][2] = "+".join(sorted(set(merged[-1][2].split("+") + [iv[2]])))
            merged[-1][3] = min(merged[-1][3], iv[3])
        else:
            merged.append(list(iv))
    return merged, grid


def _is_tunnel(row):
    if "tunnel" not in row.index:
        return False
    v = row["tunnel"]
    vals = v if isinstance(v, (list, tuple)) else [v]
    return any(str(x).strip().lower() in ("yes", "true", "1", "tunnel") for x in vals)


# ----------------------------------------------------------------- Auswertung

def deviation_series(rt, ivals, grid):
    """dz = Netz - Messung auf dem Raster, GPS-Drift entfernt.

    Basislinie = rollierender Median von dz, geschätzt NUR auf bauwerksfreien
    Stützstellen (inkl. Zufahrtszone ausgeschlossen) und über die Bauwerke
    hinweg interpoliert.
    """
    z_net_g = np.interp(grid, rt["s_net"], rt["z_net"])
    z_meas_g = np.interp(grid, rt["s_meas"], rt["z_meas"],
                         left=np.nan, right=np.nan)
    dz = z_net_g - z_meas_g

    struct_mask = np.zeros(len(grid), bool)
    for (s0, s1, _k, _d) in ivals:
        struct_mask |= (grid >= s0 - APPROACH_M) & (grid <= s1 + APPROACH_M)

    clean = pd.Series(np.where(struct_mask, np.nan, dz), index=grid)
    win = max(3, int(BASELINE_WIN_M / GRID_M))
    base = clean.rolling(win, center=True, min_periods=max(5, win // 6)).median()
    base = base.interpolate(limit_direction="both").to_numpy()
    dev = dz - base
    return dz, base, dev, struct_mask, z_net_g, z_meas_g


def excess_climb(s, z_a, z_b):
    """Anstiegsüberschuss von Profil a gegenüber b auf gemeinsamem Raster [m]."""
    da = np.diff(z_a); db = np.diff(z_b)
    return float(np.nansum(np.maximum(da, 0.0)) - np.nansum(np.maximum(db, 0.0)))


def analyze_route(rt, ivals, grid, dev, z_net_g, z_meas_g, struct_mask):
    rows = []
    for k, (s0, s1, kind, dmatch) in enumerate(ivals):
        m = (grid >= s0) & (grid <= s1)
        if m.sum() < 2 or not np.isfinite(dev[m]).any():
            continue
        i0 = int(np.argmin(np.abs(grid - s0)))
        i1 = int(np.argmin(np.abs(grid - s1)))
        seg = dev[m]
        # driftkorrigierte Messung: dev = z_netz - (z_mess + basislinie)
        znet_seg = z_net_g[m]; zmeas_seg = znet_seg - seg
        rows.append(dict(
            route=rt["label"], struct_id=f"{rt['label']}_{k:03d}", kind=kind,
            s0_m=s0, s1_m=s1, len_m=s1 - s0, match_dist_m=dmatch,
            dev_at_s0_m=float(dev[i0]), dev_at_s1_m=float(dev[i1]),
            dev_mean_m=float(np.nanmean(seg)),
            dev_max_abs_m=float(np.nanmax(np.abs(seg))),
            grade_net_pct=100.0 * (z_net_g[i1] - z_net_g[i0]) / max(s1 - s0, 1.0),
            grade_meas_pct=100.0 * (z_meas_g[i1] - z_meas_g[i0]) / max(s1 - s0, 1.0),
            excess_climb_m=excess_climb(grid[m], znet_seg, zmeas_seg),
        ))
    df = pd.DataFrame(rows)

    ctrl = dev[~struct_mask]
    ctrl = ctrl[np.isfinite(ctrl)]
    summary = dict(
        route=rt["label"], netz=rt["net"], masse_t=rt["mass_t"],
        len_km=float(rt["s_net"][-1]) / 1000.0, align_corr=rt["align_corr"],
        n_structs=len(df), struct_km=float(df["len_m"].sum()) / 1000.0 if len(df) else 0.0,
        struct_anteil_pct=(100.0 * float(df["len_m"].sum()) / float(rt["s_net"][-1])
                           if len(df) else 0.0),
        ctrl_dev_p50=float(np.median(np.abs(ctrl))) if ctrl.size else np.nan,
        ctrl_dev_p90=float(np.percentile(np.abs(ctrl), 90)) if ctrl.size else np.nan,
        ctrl_dev_max=float(np.max(np.abs(ctrl))) if ctrl.size else np.nan,
    )
    if len(df):
        summary.update(
            struct_dev_p50=float(df["dev_max_abs_m"].median()),
            struct_dev_p90=float(df["dev_max_abs_m"].quantile(0.9)),
            struct_dev_max=float(df["dev_max_abs_m"].max()),
            anchor_dev_p50=float(pd.concat([df["dev_at_s0_m"].abs(),
                                            df["dev_at_s1_m"].abs()]).median()),
            anchor_dev_p90=float(pd.concat([df["dev_at_s0_m"].abs(),
                                            df["dev_at_s1_m"].abs()]).quantile(0.9)),
            excess_climb_sum_m=float(df["excess_climb_m"].sum()),
        )
    return df, summary


# ---------------------------------------------------------------------- Plot

def plot_struct(rt, row, grid, z_net_g, z_meas_g, base, ivals, out_dir,
                context_m=PLOT_CONTEXT_M):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s0, s1 = float(row["s0_m"]), float(row["s1_m"])
    w0, w1 = s0 - context_m, s1 + context_m
    m = (grid >= w0) & (grid <= w1)
    if m.sum() < 5:
        return None
    x = (grid[m] - s0) / 1000.0
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.plot(x, z_net_g[m], lw=1.6, color="tab:blue", label="Netz (Produktionshöhen)")
    ax.plot(x, z_meas_g[m] + base[m], lw=1.2, color="tab:green",
            label="Messung (driftkorrigiert)")
    for (a, b, kind, _d) in ivals:
        if b < w0 or a > w1:
            continue
        ax.axvspan((a - s0) / 1000.0, (b - s0) / 1000.0,
                   color=("tab:purple" if kind == "tunnel" else "tab:red"),
                   alpha=0.12, lw=0)
    ax.axvline(0.0, color="0.4", ls=":", lw=0.8)
    ax.axvline((s1 - s0) / 1000.0, color="0.4", ls=":", lw=0.8)
    ax.set_xlabel("Bogenlänge relativ zum Bauwerksbeginn [km]")
    ax.set_ylabel("Höhe [m]")
    ax.set_title(f"{row['struct_id']}  {row['kind']}  L={row['len_m']:.0f} m  "
                 f"Fehler Anker {row['dev_at_s0_m']:+.2f}/{row['dev_at_s1_m']:+.2f} m, "
                 f"Deck max |{row['dev_max_abs_m']:.2f}| m, "
                 f"Neigung Netz {row['grade_net_pct']:+.2f} % vs. Messung "
                 f"{row['grade_meas_pct']:+.2f} %", fontsize=9)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{row['struct_id']}.png"
    fig.savefig(p, dpi=150)
    fig.savefig(out_dir / f"{row['struct_id']}.svg")
    plt.close(fig)
    return p


# ---------------------------------------------------------------------- Main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--resolution", type=int, default=None)
    ap.add_argument("--plot-top", type=int, default=0)
    ap.add_argument("--context-km", type=float, default=PLOT_CONTEXT_M / 1000.0)
    args = ap.parse_args()

    rep = _import("rep", "realtrip_elevation_profile.py")
    rme = _import("rme", "realtrip_measured_eval.py")
    s04_path = _NETGEN_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    spec = spec_from_file_location("script04", str(s04_path))
    s04 = module_from_spec(spec); spec.loader.exec_module(s04)

    labels = ([*rep.CLASSIC_SCEN] + rep.telemetry_labels()) if args.all \
        else [args.trip or "43t"]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Ausgabe: {out_dir}", flush=True)

    all_structs, all_summ = [], []
    for label in labels:
        try:
            rt = route_profile(rep, rme, label, args.resolution)
        except Exception as e:
            print(f"  {label}: uebersprungen ({type(e).__name__}: {e})", flush=True)
            continue
        ivals, grid = structure_intervals(rt, s04)
        dz, base, dev, smask, z_net_g, z_meas_g = deviation_series(rt, ivals, grid)
        df, summ = analyze_route(rt, ivals, grid, dev, z_net_g, z_meas_g, smask)
        all_structs.append(df); all_summ.append(summ)
        print(f"  {label:5s} {summ['len_km']:6.1f} km  {summ['n_structs']:3d} Bauwerke "
              f"({summ.get('struct_anteil_pct', 0):4.1f} % der Länge)  "
              f"Kontrolle |dev| p50/p90 {summ['ctrl_dev_p50']:.2f}/{summ['ctrl_dev_p90']:.2f} m  "
              f"Bauwerk p50/p90/max "
              f"{summ.get('struct_dev_p50', float('nan')):.2f}/"
              f"{summ.get('struct_dev_p90', float('nan')):.2f}/"
              f"{summ.get('struct_dev_max', float('nan')):.2f} m", flush=True)

        if args.plot_top > 0 and len(df):
            top = df.reindex(df["dev_max_abs_m"].abs().sort_values(ascending=False).index)
            for _, r in top.head(args.plot_top).iterrows():
                plot_struct(rt, r, grid, z_net_g, z_meas_g, base, ivals,
                            out_dir / "plots", context_m=args.context_km * 1000.0)

    if all_structs:
        S = pd.concat(all_structs, ignore_index=True)
        S.to_csv(out_dir / "structs.csv", index=False, encoding="utf-8")
        U = pd.DataFrame(all_summ)
        U.to_csv(out_dir / "routes.csv", index=False, encoding="utf-8")
        anc = pd.concat([S["dev_at_s0_m"].abs(), S["dev_at_s1_m"].abs()])
        txt = [f"Bauwerke gesamt: {len(S)} ({S['len_m'].sum()/1000:.1f} km) auf "
               f"{len(U)} Routen",
               f"Kontrolle (bauwerksfrei) |dev|: p50 {U['ctrl_dev_p50'].median():.2f} m, "
               f"p90 {U['ctrl_dev_p90'].median():.2f} m  = Rauschboden",
               f"Bauwerk max|dev| je Bauwerk: p50 {S['dev_max_abs_m'].median():.2f}, "
               f"p90 {S['dev_max_abs_m'].quantile(0.9):.2f}, max {S['dev_max_abs_m'].max():.2f} m",
               f"Fehler AN DEN ANKERPUNKTEN |dev|: p50 {anc.median():.2f}, "
               f"p90 {anc.quantile(0.9):.2f}, max {anc.max():.2f} m",
               f"Anstiegsüberschuss über alle Bauwerke: {S['excess_climb_m'].sum():+.1f} m",
               "", "je Bauwerkstyp (max|dev| p50/p90):"]
        for kind, g in S.groupby("kind"):
            txt.append(f"  {kind:14s} n={len(g):4d}  "
                       f"{g['dev_max_abs_m'].median():.2f}/{g['dev_max_abs_m'].quantile(0.9):.2f} m")
        txt.append("")
        txt.append("Netz-Neigung vs. Messung auf dem Bauwerk (Median |Differenz|): "
                   f"{(S['grade_net_pct']-S['grade_meas_pct']).abs().median():.2f} %-Punkte")
        out = "\n".join(txt)
        (out_dir / "summary.txt").write_text(out, encoding="utf-8")
        print("\n" + out, flush=True)
    print(f"\nfertig: {out_dir}", flush=True)


if __name__ == "__main__":
    main()
