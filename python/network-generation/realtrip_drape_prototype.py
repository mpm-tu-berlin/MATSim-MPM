# -*- coding: utf-8 -*-
"""
Prototyp: Bayes-Drapierung eines Routen-Korridors statt Sampling-plus-Glaettung.

Zweck: auf EINEM Korridor zeigen, ob eine MAP-Schaetzung mit Hoehenfehler- und
Steigungs-Prior (plus Entkopplung von Bruecken/Tunneln) die Scheinsteigungen
entfernt, ohne echtes Terrain zu loeschen. Vergleich gegen das gemessene
Hoehenprofil als Ground Truth.

Das ist NICHT BayesianDrape (Cooper 2024), sondern dessen Kern fuer den
1-D-Korridorfall: bei gaussschen Priors ist die MAP-Loesung ein lineares
Bandsystem. Fuer die Produktion bleibt das Originalwerkzeug die Referenz
(zusaetzlich Pitch-Prior, einstellbare Slope-Continuity, 2-D-Behandlung).

Modell (z = Knotenhoehen, L = Linklaengen):
    J(z) = sum_i w_i (z_i - z_dem_i)^2 / sigma_z^2
         + sum_l ((z_{l+1} - z_l) / L_l)^2 / sigma_g^2
w_i = 0 fuer entkoppelte Knoten (Tunnel/Bruecke: kein DGM-Beleg).

Aufruf:
  python realtrip_drape_prototype.py --trip 24t --km 40 46
  python realtrip_drape_prototype.py --trip 24t --km 40 46 --sigma-g 0.03

VERTRAULICH: nutzt reale Messdaten nur als aggregierte Vergleichskurve.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from pyproj import Transformer

from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent
_DATA = _SCRIPT_DIR / "data"
DEFAULT_DTM = _DATA / "DTM Germany 20m v3b by Sonny.tif"
NET_CRS = "EPSG:4839"


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sample_dtm(dtm_path, xs, ys, net_crs=NET_CRS):
    """Bilineare DGM-Hoehe an Netzkoordinaten [m]."""
    with rasterio.open(dtm_path) as ds:
        tf = Transformer.from_crs(net_crs, ds.crs, always_xy=True)
        u, v = tf.transform(xs, ys)
        vals = np.array([s[0] for s in ds.sample(np.column_stack([u, v]), 1)],
                        dtype=float)
        if ds.nodata is not None:
            vals[vals == ds.nodata] = np.nan
    return vals


def map_drape(s, z_dem, sigma_z, sigma_g, coupled):
    """MAP-Hoehen auf einer Kette. s = Bogenlaenge [m], coupled = bool-Maske."""
    n = len(s)
    L = np.diff(s)
    L = np.maximum(L, 1.0)

    # Datenterm: nur fuer gekoppelte Knoten mit gueltigem DGM
    w = np.where(coupled & np.isfinite(z_dem), 1.0 / sigma_z ** 2, 0.0)
    A = sp.diags(w).tolil()
    b = w * np.nan_to_num(z_dem)

    # Steigungsterm: ((z_{i+1}-z_i)/L_i)^2 / sigma_g^2
    rows, cols, vals = [], [], []
    for i in range(n - 1):
        c = 1.0 / (sigma_g ** 2 * L[i] ** 2)
        for (r, cc, v) in ((i, i, c), (i, i + 1, -c), (i + 1, i, -c), (i + 1, i + 1, c)):
            rows.append(r); cols.append(cc); vals.append(v)
    A = (A.tocsr() + sp.csr_matrix((vals, (rows, cols)), shape=(n, n)))

    # Entkoppelte Ketten ohne jeden Datenbezug wuerden singulaer -> winzige Regularisierung
    A = A + sp.identity(n, format="csr") * 1e-9
    return spla.spsolve(A.tocsc(), b)


def climb(s, z, baseline=250.0):
    grid = np.arange(s[0], s[-1], baseline)
    dz = np.diff(np.interp(grid, s, z))
    return float(np.sum(dz[dz > 0]))


def slope_mae(s, z, s_ref, z_ref, baseline=250.0):
    grid = np.arange(s[0], s[-1], baseline)
    gn = np.diff(np.interp(grid, s, z)) / baseline
    gm = np.diff(np.interp(grid, s_ref, z_ref)) / baseline
    return 100.0 * float(np.mean(np.abs(gn - gm)))


def main():
    ap = argparse.ArgumentParser(description="Bayes-Drapierung: Korridor-Prototyp.")
    ap.add_argument("--trip", default="24t")
    ap.add_argument("--km", type=float, nargs=2, default=[40.0, 46.0],
                    help="Korridorfenster entlang der Route [km]")
    ap.add_argument("--sigma-z", type=float, default=2.0, help="DGM-Hoehenfehler [m]")
    ap.add_argument("--sigma-g", type=float, default=0.0464,
                    help="Steigungs-Prior (Default 2,66 Grad wie BayesianDrape)")
    ap.add_argument("--struct-grade", type=float, default=6.0,
                    help="Proxy-Schwelle zur Strukturerkennung [%%] (Produktion: OSM-Tags)")
    ap.add_argument("--decouple-km", type=float, nargs=2, default=None,
                    help="Bauwerk explizit vorgeben [km von bis] — simuliert das "
                         "OSM-Tag, statt die Struktur aus der Steigung zu raten")
    ap.add_argument("--dtm", type=str, default=str(DEFAULT_DTM))
    ap.add_argument("--outdir", type=str, default=str(_DATA / "realtrip_elevation"))
    args = ap.parse_args()

    rme = _import_module("rme", "realtrip_measured_eval.py")
    rep = _import_module("rep", "realtrip_elevation_profile.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    version = kpa._next_version(outdir, base=f"drape_{args.trip}")

    net_path, s_meas, z_meas, _mass = rep.resolve_route(args.trip)
    prof = rme.load_chain_profile(net_path)
    s_all, z_all, order = prof["s"], prof["z"], prof["order"]
    nodes_xy = {}
    root = prof["root"]
    for nd in root.find("nodes").findall("node"):
        nodes_xy[nd.get("id")] = (float(nd.get("x")), float(nd.get("y")))

    direction, offset, scale, corr = rme.align_profiles(s_all, z_all, s_meas, z_meas)
    if direction < 0:
        s_al = (s_meas[-1] - s_meas[::-1]) * scale + offset
        z_al = z_meas[::-1]
    else:
        s_al = s_meas * scale + offset
        z_al = z_meas

    lo, hi = args.km[0] * 1000.0, args.km[1] * 1000.0
    sel = (s_all >= lo) & (s_all <= hi)
    idx = np.where(sel)[0]
    if len(idx) < 10:
        raise SystemExit("Fenster zu klein oder ausserhalb der Route.")
    s = s_all[idx]
    z_net = z_all[idx]
    xs = np.array([nodes_xy[order[i]][0] for i in idx])
    ys = np.array([nodes_xy[order[i]][1] for i in idx])

    z_dem = sample_dtm(args.dtm, xs, ys)
    grade_net = 100.0 * np.diff(z_net) / np.maximum(np.diff(s), 1.0)
    struct_link = np.abs(grade_net) > args.struct_grade
    # Knoten entkoppeln, wenn ALLE anliegenden Links als Struktur gelten
    coupled = np.ones(len(s), dtype=bool)
    if args.decouple_km:
        a, b = args.decouple_km[0] * 1000.0, args.decouple_km[1] * 1000.0
        coupled[(s > a) & (s < b)] = False
        proxy_note = f"explizit vorgegeben ({args.decouple_km[0]}–{args.decouple_km[1]} km)"
    else:
        for i in range(len(s)):
            inc = [struct_link[j] for j in (i - 1, i) if 0 <= j < len(struct_link)]
            if inc and all(inc):
                coupled[i] = False
        proxy_note = f"Proxy |Steigung| > {args.struct_grade:g} %"

    z_map = map_drape(s, z_dem, args.sigma_z, args.sigma_g, np.ones(len(s), bool))
    z_map_dc = map_drape(s, z_dem, args.sigma_z, args.sigma_g, coupled)

    variants = [
        ("DGM roh (Punktwert)", z_dem),
        ("Netz aktuell (Spline)", z_net),
        ("MAP ohne Entkopplung", z_map),
        ("MAP + Entkopplung", z_map_dc),
    ]
    ref_climb = climb(s_al[(s_al >= s[0]) & (s_al <= s[-1])],
                      z_al[(s_al >= s[0]) & (s_al <= s[-1])])

    print(f"Route {args.trip}, Korridor {args.km[0]:.1f}-{args.km[1]:.1f} km, "
          f"{len(s)} Knoten, Netz {net_path.name}")
    print(f"entkoppelte Knoten: {int((~coupled).sum())} von {len(s)}  "
          f"({proxy_note})")
    print(f"sigma_z = {args.sigma_z} m, sigma_g = {args.sigma_g:.4f} "
          f"({np.degrees(np.arctan(args.sigma_g)):.2f} Grad)\n")
    print(f"{'Variante':24s} {'Anstieg[m]':>11s} {'vs Mess[m]':>11s} "
          f"{'Steig.-MAE[%]':>14s} {'max|Steig|[%]':>14s}")
    print(f"{'Messung':24s} {ref_climb:11.1f} {0.0:11.1f} {'-':>14s} "
          f"{100*np.max(np.abs(np.diff(z_al)/np.maximum(np.diff(s_al),1))):14.1f}")
    rows = []
    for name, z in variants:
        c = climb(s, z)
        mae = slope_mae(s, z, s_al, z_al)
        gmax = float(np.max(np.abs(100.0 * np.diff(z) / np.maximum(np.diff(s), 1.0))))
        print(f"{name:24s} {c:11.1f} {c - ref_climb:11.1f} {mae:14.3f} {gmax:14.1f}")
        rows.append((name, c, c - ref_climb, mae, gmax))

    fig, ax = plt.subplots(figsize=(11, 5.2))
    ax.plot(s_al / 1000.0, z_al, color="#d62728", lw=2.0, label="Messung (Fahrzeug)", zorder=5)
    styles = [("0.55", 0.9, ":"), ("#1f77b4", 1.3, "-"),
              ("#2ca02c", 1.3, "--"), ("#9467bd", 1.8, "-")]
    for (name, z), (c_, lw, ls) in zip(variants, styles):
        ax.plot(s / 1000.0, z, color=c_, lw=lw, ls=ls, label=name)
    if (~coupled).any():
        ax.plot(s[~coupled] / 1000.0, z_net[~coupled], "v", ms=6, color="black",
                label="entkoppelt (Struktur)")
    ax.set_xlim(args.km[0], args.km[1])
    ax.set_xlabel("Bogenlänge entlang der Route (km)")
    ax.set_ylabel("Höhe [m ü. NN]")
    ax.set_title(f"Bayes-Drapierung Prototyp — Route {args.trip}, "
                 f"km {args.km[0]:.0f}–{args.km[1]:.0f}")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    base = outdir / f"drape_{args.trip}_km{int(args.km[0])}-{int(args.km[1])}_V{version}"
    fig.savefig(base.with_suffix(".png"), dpi=200, bbox_inches="tight")
    fig.savefig(base.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)
    print(f"\nPlot: {base.with_suffix('.png')}")


if __name__ == "__main__":
    main()
