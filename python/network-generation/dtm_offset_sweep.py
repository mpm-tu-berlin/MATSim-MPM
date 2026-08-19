# -*- coding: utf-8 -*-
"""Sucht den optimalen horizontalen Versatz der DTM-Abtastung.

Hintergrund: `sample_heights` (Skript 04, Zeile 91) rechnet
`cols, rows = (~ds.transform) * (xs, ys)` und interpoliert dann bilinear zwischen
floor(cols) und floor(cols)+1. Diese Pixelkoordinaten zaehlen ab der PIXELECKE,
das Zentrum von Pixel 0 liegt bei 0,5. Fuer eine Interpolation zwischen
Pixel-ZENTREN muesste vor dem floor ein -0,5 stehen; ohne das ist die Abtastung
um ein halbes Pixel (10 m bei 20 m Raster) verschoben. Zusaetzlich traegt das
Raster AREA_OR_POINT=Point, was die Konvention noch einmal um ein halbes Pixel
verschieben kann.

Statt zu argumentieren wird gemessen: die Telemetriehoehen sind die Referenz. Fuer
ein Gitter von Versaetzen (dx, dy) im DTM-CRS wird die Hoehe neu gezogen und der
Fehler gegen die Messung bestimmt (je Fahrt um den konstanten Antennenversatz
bereinigt). Liegt das Optimum bei (0, 0), ist die Abtastung richtig; liegt es bei
etwa (+/-10, +/-10) m, ist es der Halbpixel-Fehler.

Nur bauwerksfreie Punkte, damit Bruecken/Tunnel das Ergebnis nicht verzerren.

Aufruf:  python dtm_offset_sweep.py --bbox 7.85,49.10,8.10,49.30 --step 5 --range 20
"""
import argparse
import sys
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).parent
DTM_CRS = "EPSG:32632"


def _imp(name, path):
    spec = spec_from_file_location(name, str(path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="7.85,49.10,8.10,49.30")
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--range", type=float, default=20.0)
    ap.add_argument("--max-points", type=int, default=60000)
    args = ap.parse_args()
    bbox = tuple(float(v) for v in args.bbox.split(","))

    nvt = _imp("nvt", _SCRIPT_DIR / "network_elevation_vs_telemetry.py")
    saa = _imp("saa", _SCRIPT_DIR / "structure_anchor_audit.py")
    s04 = saa._import_script04()
    dtm = s04.load_dtm(str(saa.DTM_PATH))

    corpus = pd.read_parquet(nvt.CORPUS)
    m = (corpus.lon.between(bbox[0], bbox[2])) & (corpus.lat.between(bbox[1], bbox[3]))
    c = corpus[m].copy()
    if len(c) > args.max_points:
        c = c.sample(args.max_points, random_state=0).sort_values(["trip_id", "s_m"])
    print(f"bbox {bbox}: {len(c):,} Punkte, {c.trip_id.nunique()} Fahrten", flush=True)

    # bauwerksfreie Punkte auswaehlen (Bruecken/Tunnel wuerden das Optimum ziehen)
    sx, sy, sb, sk = nvt.load_structure_segments()
    tf49 = Transformer.from_crs("EPSG:4326", f"EPSG:{nvt.TARGET_EPSG}", always_xy=True)
    x49, y49 = tf49.transform(c.lon.to_numpy(), c.lat.to_numpy())
    stree = cKDTree(np.column_stack([sx, sy]))
    d_s, _ = stree.query(np.column_stack([np.asarray(x49), np.asarray(y49)]),
                         distance_upper_bound=40.0, workers=-1)
    free = ~np.isfinite(d_s)          # weiter als 40 m von jedem Bauwerk
    c = c[free]
    print(f"  bauwerksfrei (>40 m): {len(c):,} Punkte", flush=True)

    alt = c.alt_m.to_numpy(float)
    ok = np.isfinite(alt) & (alt > -50) & (alt < 3000)
    c, alt = c[ok], alt[ok]
    trip = c.trip_id.to_numpy()

    # in das DTM-CRS, dort wird verschoben
    tfd = Transformer.from_crs("EPSG:4326", DTM_CRS, always_xy=True)
    inv = Transformer.from_crs(DTM_CRS, "EPSG:4326", always_xy=True)
    xd, yd = tfd.transform(c.lon.to_numpy(), c.lat.to_numpy())
    xd = np.asarray(xd, float); yd = np.asarray(yd, float)

    offs = np.arange(-args.range, args.range + 1e-9, args.step)
    res = []
    for dy in offs:
        for dx in offs:
            lo, la = inv.transform(xd + dx, yd + dy)
            z = s04.sample_heights(dtm, np.asarray(lo), np.asarray(la))
            dz = z - alt
            good = np.isfinite(dz)
            if good.sum() < 100:
                continue
            # je Fahrt den konstanten Versatz (Antenne) abziehen
            df = pd.DataFrame({"trip": trip[good], "dz": dz[good]})
            df["dev"] = df.dz - df.groupby("trip").dz.transform("median")
            a = df.dev.to_numpy()
            res.append(dict(dx=dx, dy=dy, n=int(good.sum()),
                            mae=float(np.mean(np.abs(a))),
                            p50=float(np.median(np.abs(a))),
                            rms=float(np.sqrt(np.mean(a ** 2)))))
        print(f"  dy={dy:+.0f} m fertig", flush=True)

    R = pd.DataFrame(res)
    out = _SCRIPT_DIR / "data" / "dtm_offset_sweep.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    R.to_csv(out, index=False, encoding="utf-8")

    print("\n== MAE [m] als Funktion des Versatzes (Zeilen dy, Spalten dx) ==")
    piv = R.pivot(index="dy", columns="dx", values="mae").round(3)
    print(piv.to_string())
    best = R.loc[R.mae.idxmin()]
    base = R[(R.dx == 0) & (R.dy == 0)]
    print(f"\n  Optimum: dx {best.dx:+.0f} m, dy {best.dy:+.0f} m -> "
          f"MAE {best.mae:.3f} m (p50 {best.p50:.3f}, RMS {best.rms:.3f})")
    if len(base):
        b = base.iloc[0]
        print(f"  ohne Versatz (0,0):              MAE {b.mae:.3f} m "
              f"(p50 {b.p50:.3f}, RMS {b.rms:.3f})")
        print(f"  Verbesserung durch den Versatz:  "
              f"{100.0 * (1 - best.mae / b.mae):.1f} % MAE")
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()
