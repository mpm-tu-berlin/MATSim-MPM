# -*- coding: utf-8 -*-
"""A/B-Test der korridoruebergreifenden Bauwerksbehandlung.

Baut fuer eine bbox das Netz wie in der Produktion (short_edges) und rechnet die
Hoehen zweimal: global_structures=False (alter Stand) und True (Fix). Vergleicht
Knotenhoehen, mit Schwerpunkt auf den Knoten, die in einem Bauwerk liegen.

Aufruf:
  python structure_fix_ab_test.py --bbox 8.40,49.45,8.50,49.52
"""
import argparse
import sys
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).parent


def _imp(name, path):
    spec = spec_from_file_location(name, str(path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plot_location(nvt, dense, corpus, sx, sy, sb, sk, lon0, lat0,
                  out_png, radius_m=1500.0, max_trips=8):
    """Messung, altes und neues Netzprofil an einer Stelle uebereinanderlegen."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{nvt.TARGET_EPSG}", always_xy=True)
    trees, zs = {}, {}
    for lab in ("alt", "neu"):
        dn = dense[lab]
        rx, ry = tf.transform(dn[:, 0], dn[:, 1])
        trees[lab] = cKDTree(np.column_stack([np.asarray(rx), np.asarray(ry)]))
        zs[lab] = dn[:, 2].astype(float)
    stree = cKDTree(np.column_stack([sx, sy]))
    tx, ty = tf.transform(np.array([lon0]), np.array([lat0]))
    tx, ty = float(tx[0]), float(ty[0])

    # Gemeinsame RAEUMLICHE Achse: Projektion auf die Strassenrichtung an der
    # Zielkoordinate. Ohne das erscheinen Fahrten der Gegenrichtung gespiegelt,
    # weil jede Fahrt ihre eigene Bogenlaenge mitbringt.
    axis_u = None
    fig, ax = plt.subplots(figsize=(11, 4.6))
    drawn_net, n_plotted = False, 0
    for tid, g in corpus.groupby("trip_id", sort=False):
        g = g.sort_values("s_m")
        x, y = tf.transform(g.lon.to_numpy(float), g.lat.to_numpy(float))
        x = np.asarray(x); y = np.asarray(y)
        dist = np.hypot(x - tx, y - ty)
        if dist.min() > 120.0:
            continue
        k0 = int(np.argmin(dist))
        s = g.s_m.to_numpy(float)
        w = np.abs(s - s[k0]) <= radius_m
        if w.sum() < 30:
            continue
        s, x, y = s[w], x[w], y[w]
        alt = g.alt_m.to_numpy(float)[w]
        ok = np.isfinite(alt) & (alt > -50) & (alt < 3000)
        s, x, y, alt = s[ok], x[ok], y[ok], alt[ok]
        # Bauwerksflag wie in der Messkette
        bear = nvt.trace_bearing(x, y)
        ds, si = stree.query(np.column_stack([x, y]), k=4,
                             distance_upper_bound=nvt.STRUCT_M, workers=-1)
        ds = np.atleast_2d(ds); si = np.atleast_2d(si)
        on = np.zeros(len(s), bool)
        for c in range(ds.shape[1]):
            cand = np.isfinite(ds[:, c]) & (si[:, c] < len(sx)) & (~on)
            if not cand.any():
                continue
            j = si[cand, c]
            turn = np.abs((sb[j] - bear[cand] + 90.0) % 180.0 - 90.0)
            on[np.flatnonzero(cand)[turn <= nvt.STRUCT_BEARING_DEG]] = True
        zref = {}
        for lab in ("alt", "neu"):
            d, idx = trees[lab].query(np.column_stack([x, y]), workers=-1)
            v = np.where(np.isfinite(d) & (d <= nvt.MATCH_M),
                         zs[lab][np.clip(idx, 0, len(zs[lab]) - 1)], np.nan)
            zref[lab] = v
        # Drift-Basislinie aus bauwerksfreien Punkten (wie in der Messkette)
        dz = zref["alt"] - alt
        ser = pd.Series(np.where(on, np.nan, dz))
        base = ser.rolling(120, center=True, min_periods=5).median()
        base = base.interpolate(limit_direction="both").to_numpy()
        k1 = int(np.argmin(np.hypot(x - tx, y - ty)))
        if axis_u is None:
            a, b = max(0, k1 - 5), min(len(x) - 1, k1 + 5)
            v = np.array([x[b] - x[a], y[b] - y[a]], float)
            nv = float(np.hypot(*v))
            axis_u = v / nv if nv > 1e-6 else np.array([1.0, 0.0])
        xx = (x - tx) * axis_u[0] + (y - ty) * axis_u[1]
        if not drawn_net:
            ax.plot(xx, zref["alt"], lw=1.8, color="tab:red",
                    label="Netz alt (korridorlokal)")
            ax.plot(xx, zref["neu"], lw=1.8, color="tab:blue",
                    label="Netz neu (korridoruebergreifend)")
            for i in np.flatnonzero(on):
                ax.axvspan(xx[i] - 10, xx[i] + 10, color="0.5", alpha=0.10, lw=0)
            drawn_net = True
        ax.plot(xx, alt + base, lw=0.9, alpha=0.75, color="tab:green",
                label="Messung (driftkorrigiert)" if n_plotted == 0 else None)
        n_plotted += 1
        if n_plotted >= max_trips:
            break
    if not drawn_net:
        print("  keine Fahrt an dieser Stelle gefunden")
        return None
    ax.set_xlabel("Weg entlang der Fahrt, 0 = Zielkoordinate [m]")
    ax.set_ylabel("Höhe [m]")
    ax.set_title(f"{lon0:.4f}/{lat0:.4f} — {n_plotted} Durchfahrten, "
                 f"graue Bänder = als Bauwerk geflaggt", fontsize=9)
    ax.grid(alpha=0.3); ax.legend(fontsize=8)
    fig.tight_layout()
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="8.40,49.45,8.50,49.52")
    ap.add_argument("--max-link-length", type=float, default=250.0)
    ap.add_argument("--plot-at", default=None,
                    help="lon,lat einer Stelle: Messung + Netz alt/neu plotten")
    ap.add_argument("--groundtruth", action="store_true",
                    help="zusaetzlich dichte Profile alt/neu gegen die "
                         "Telemetrie in derselben bbox messen")
    args = ap.parse_args()
    bbox = tuple(float(v) for v in args.bbox.split(","))

    saa = _imp("saa", _SCRIPT_DIR / "structure_anchor_audit.py")
    s04 = saa._import_script04()
    print(f"bbox {bbox}", flush=True)
    e_simp, e_det, n_det = saa.load_region(bbox)
    edges_short, node_lonlat = saa.build_shortened_network(
        s04, e_simp, e_det, n_det, args.max_link_length)
    print(f"  {len(edges_short)} Kanten, {len(node_lonlat)} Knoten", flush=True)
    dtm = s04.load_dtm(str(saa.DTM_PATH))

    print("\n--- ALT (global_structures=False) ---", flush=True)
    z_old = s04.assign_heights_along_corridors(
        edges_short, node_lonlat, dtm, target_epsg=saa.TARGET_EPSG,
        sample_step_m=saa.SAMPLE_STEP_M, smooth_rms_m=saa.SMOOTH_RMS_M,
        global_structures=False)
    print("\n--- NEU (global_structures=True) ---", flush=True)
    z_new = s04.assign_heights_along_corridors(
        edges_short, node_lonlat, dtm, target_epsg=saa.TARGET_EPSG,
        sample_step_m=saa.SAMPLE_STEP_M, smooth_rms_m=saa.SMOOTH_RMS_M,
        global_structures=True)

    common = [n for n in z_old if n in z_new
              and np.isfinite(z_old[n]) and np.isfinite(z_new[n])]
    d = np.array([z_new[n] - z_old[n] for n in common], float)
    print(f"\n== Vergleich ueber {len(common)} Knoten ==")
    print(f"  unveraendert (<1 cm):     {int((np.abs(d) < 0.01).sum()):6d} "
          f"({100.0*(np.abs(d) < 0.01).mean():.1f} %)")
    print(f"  veraendert > 0,5 m:       {int((np.abs(d) > 0.5).sum()):6d}")
    print(f"  veraendert > 5 m:         {int((np.abs(d) > 5.0).sum()):6d}")
    ch = d[np.abs(d) > 0.01]
    if ch.size:
        print(f"  Aenderung: median {np.median(ch):+.2f} m, "
              f"p90 {np.percentile(np.abs(ch), 90):.2f} m, "
              f"max {np.max(np.abs(ch)):.2f} m")
        print(f"  Richtung: {int((ch > 0).sum())} angehoben / "
              f"{int((ch < 0).sum())} abgesenkt")
    print("\n  (Anhebung = Bauwerk wird nicht mehr auf die Talsohle gezogen;"
          "\n   Absenkung = Tunnel folgt nicht mehr dem Berg)")

    if not args.groundtruth:
        return

    # ---- Abnahme gegen die Telemetrie: dichte Profile alt/neu vergleichen ----
    import pandas as pd
    from pyproj import Transformer
    nvt = _imp("nvt", _SCRIPT_DIR / "network_elevation_vs_telemetry.py")

    print("\n== Ground-Truth-Vergleich in der bbox ==", flush=True)
    dense = {}
    for lab, gs in (("alt", False), ("neu", True)):
        _z, dn = s04.assign_heights_along_corridors(
            edges_short, node_lonlat, dtm, target_epsg=saa.TARGET_EPSG,
            sample_step_m=saa.SAMPLE_STEP_M, smooth_rms_m=saa.SMOOTH_RMS_M,
            collect_dense=True, global_structures=gs)
        dense[lab] = dn
        print(f"  {lab}: {len(dn):,} dichte Profilpunkte", flush=True)

    corpus = pd.read_parquet(nvt.CORPUS)
    lon0, lat0, lon1, lat1 = bbox
    m = (corpus["lon"].between(lon0, lon1)) & (corpus["lat"].between(lat0, lat1))
    corpus = corpus[m]
    print(f"  Telemetrie in der bbox: {len(corpus):,} Punkte, "
          f"{corpus['trip_id'].nunique()} Fahrten", flush=True)
    if corpus.empty:
        print("  keine Telemetrie in dieser bbox")
        return

    sx, sy, sb, sk = nvt.load_structure_segments()

    if args.plot_at:
        plon, plat = (float(v) for v in args.plot_at.split(","))
        out = (_SCRIPT_DIR / "data" / "structure_fix_ab" /
               f"loc_{plon:.4f}_{plat:.4f}.png")
        p = plot_location(nvt, dense, corpus, sx, sy, sb, sk, plon, plat, out)
        if p:
            print(f"  Plot: {p}", flush=True)

    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{nvt.TARGET_EPSG}", always_xy=True)
    res = {}
    for lab in ("alt", "neu"):
        dn = dense[lab]
        rx, ry = tf.transform(dn[:, 0], dn[:, 1])
        P, C, DF = nvt.evaluate(corpus, np.asarray(rx), np.asarray(ry),
                                dn[:, 2].astype(float), sx, sy, sb, sk)
        res[lab] = (P, C, DF)

    print("\n  Kennzahl                         alt        neu")
    Pa, Ca, Da = res["alt"]; Pn, Cn, Dn = res["neu"]
    def _f(v, unit=" m"):
        return f"{v:9.2f}{unit}"
    if Pa.empty or Pn.empty:
        print("  keine Bauwerksdurchfahrten in der bbox")
        return
    rows = [
        ("Bauwerksdurchfahrten", len(Pa), len(Pn), ""),
        ("max|dev| p50 [m]", Pa.dev_max_abs_m.median(), Pn.dev_max_abs_m.median(), "m"),
        ("max|dev| p90 [m]", Pa.dev_max_abs_m.quantile(0.9),
         Pn.dev_max_abs_m.quantile(0.9), "m"),
        ("max|dev| max [m]", Pa.dev_max_abs_m.max(), Pn.dev_max_abs_m.max(), "m"),
        ("Anteil zu tief [%]", 100.0 * (Pa.dev_mean_m < 0).mean(),
         100.0 * (Pn.dev_mean_m < 0).mean(), "%"),
        ("Anstiegsueberschuss [m]", Pa.excess_climb_m.sum(),
         Pn.excess_climb_m.sum(), "m"),
        ("Rauschboden bauwerksfrei p50 [m]", np.median(np.abs(Ca)),
         np.median(np.abs(Cn)), "m"),
        ("Rauschboden bauwerksfrei p90 [m]", np.percentile(np.abs(Ca), 90),
         np.percentile(np.abs(Cn), 90), "m"),
    ]
    for name, a, b, u in rows:
        print(f"  {name:32s} {a:9.2f} {b:9.2f}")

    # Defekte nach Bauwerksbezug (unabhaengig vom Fix)
    for lab, Dx in (("alt", Da), ("neu", Dn)):
        if Dx is None or Dx.empty:
            continue
        ohne = Dx[Dx.on_struct_share == 0.0]
        print(f"  Defekte {lab}: {len(Dx)} Abschnitte, "
              f"{Dx.len_m.sum()/1000:.1f} km, davon ohne Bauwerksbezug "
              f"{len(ohne)} ({100*ohne.len_m.sum()/max(Dx.len_m.sum(),1):.0f} % der Laenge)")

    # ---- paarweiser Vergleich: welche Durchfahrten werden schlechter? ----
    key = ["trip_id", "s0_m"]
    J = Pa[key + ["len_m", "kind", "lat", "lon", "dev_max_abs_m", "dev_mean_m",
                  "excess_climb_m"]].merge(
        Pn[key + ["dev_max_abs_m", "dev_mean_m", "excess_climb_m"]],
        on=key, suffixes=("_alt", "_neu"))
    if J.empty:
        print("\n  (paarweiser Vergleich nicht moeglich)")
        return
    J["delta"] = J.dev_max_abs_m_neu - J.dev_max_abs_m_alt
    better = J[J.delta < -0.5]
    worse = J[J.delta > 0.5]
    print(f"\n  paarweise ({len(J)} Durchfahrten): "
          f"{len(better)} besser, {len(worse)} schlechter, "
          f"{len(J) - len(better) - len(worse)} unveraendert (+-0,5 m)")
    print(f"  Summe Verbesserung {better.delta.sum():+.1f} m, "
          f"Summe Verschlechterung {worse.delta.sum():+.1f} m, "
          f"netto {J.delta.sum():+.1f} m")
    if len(worse):
        print("\n  die 10 groessten Verschlechterungen:")
        print(worse.sort_values("delta", ascending=False).head(10)[
            ["trip_id", "kind", "len_m", "dev_max_abs_m_alt", "dev_max_abs_m_neu",
             "delta", "excess_climb_m_alt", "excess_climb_m_neu", "lon", "lat"]]
            .round(2).to_string(index=False))
    # Gruppierung nach Ort, damit Wiederholungen nicht mehrfach zaehlen
    J["ort"] = (J.lon.round(3).astype(str) + "/" + J.lat.round(3).astype(str))
    g = J.groupby("ort").agg(n=("delta", "size"), delta=("delta", "median"),
                             len_m=("len_m", "median"))
    print(f"\n  nach Ort: {len(g)} Stellen, "
          f"{int((g.delta < -0.5).sum())} besser, {int((g.delta > 0.5).sum())} schlechter")


if __name__ == "__main__":
    main()
