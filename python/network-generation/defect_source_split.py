# -*- coding: utf-8 -*-
"""Woher kommt der Höhenfehler: aus dem DTM oder aus der Pipeline?

Der Ground-Truth-Lauf zeigt, dass nur rund ein Fünftel der Defektlänge
überwiegend an Bauwerken sitzt und 36,5 % gar keinen Bauwerkskontakt haben. Diese
Auswertung trennt die verbleibende Masse nach URSACHE, mit dem rohen DTM als
Schiedsrichter:

  fuer jeden Telemetriepunkt
    dz_prof = z_Pipelineprofil - Alt_gemessen      (dense_V2)
    dz_raw  = z_DTM_roh        - Alt_gemessen      (bilinear am selben Punkt)
  beide mit derselben Drift-Basislinie entzerrt.

  |dev_prof| gross, |dev_raw| klein  -> PIPELINE (Glaettung, Korridor, Niveau-
                                        sprung, Bauwerkslogik)
  |dev_prof| gross, |dev_raw| gross  -> DTM (Bare Earth gegen Fahrbahn: Damm,
                                        Einschnitt, Bewuchs, 20-m-Raster)

Zusaetzlich wird jeder Defekt danach klassifiziert, ob er Bauwerkskontakt hat.

DATENSCHUTZ: Ein- und Ausgaben nur in gitignorten data/-Ordnern, stdout nur
Aggregate.

Aufruf:  python defect_source_split.py [--trips 300]
"""
import argparse
import sys
from datetime import datetime
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
OUT_ROOT = _SCRIPT_DIR / "data" / "defect_source_split"


def _imp(name, path):
    spec = spec_from_file_location(name, str(path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", type=int, default=300)
    args = ap.parse_args()

    nvt = _imp("nvt", _SCRIPT_DIR / "network_elevation_vs_telemetry.py")
    saa = _imp("saa", _SCRIPT_DIR / "structure_anchor_audit.py")
    s04 = saa._import_script04()
    dtm = s04.load_dtm(str(saa.DTM_PATH))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    rx, ry, rz = nvt.load_reference("dense_V2")
    sx, sy, sb, sk = nvt.load_structure_segments()
    rtree = cKDTree(np.column_stack([rx, ry]), balanced_tree=False,
                    compact_nodes=False)
    stree = cKDTree(np.column_stack([sx, sy]))
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{nvt.TARGET_EPSG}",
                              always_xy=True)

    corpus = pd.read_parquet(nvt.CORPUS)
    keep = corpus["trip_id"].drop_duplicates().head(args.trips)
    corpus = corpus[corpus["trip_id"].isin(keep)]
    print(f"{corpus['trip_id'].nunique()} Fahrten, {len(corpus):,} Punkte",
          flush=True)

    rows = []
    for gi, (tid, g) in enumerate(corpus.groupby("trip_id", sort=False), 1):
        g = g.sort_values("s_m")
        alt = g["alt_m"].to_numpy(float)
        ok = np.isfinite(alt) & (alt > nvt.ALT_MIN) & (alt < nvt.ALT_MAX)
        if ok.sum() < 50:
            continue
        s = g["s_m"].to_numpy(float)[ok]
        lon = g["lon"].to_numpy(float)[ok]
        lat = g["lat"].to_numpy(float)[ok]
        alt = alt[ok]
        x, y = tf.transform(lon, lat)
        x = np.asarray(x); y = np.asarray(y)

        d, idx = rtree.query(np.column_stack([x, y]), workers=-1)
        m = np.isfinite(d) & (d <= nvt.MATCH_M)
        if m.sum() < 50:
            continue
        s, x, y, alt, lon, lat = s[m], x[m], y[m], alt[m], lon[m], lat[m]
        z_prof = rz[idx[m]]
        z_raw = s04.sample_heights(dtm, lon, lat)      # rohes DTM am selben Punkt

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

        near = on.copy()
        if on.any():
            si_s = s[on]
            pos = np.searchsorted(si_s, s)
            left = np.where(pos > 0, s - si_s[np.clip(pos - 1, 0, len(si_s) - 1)],
                            np.inf)
            right = np.where(pos < len(si_s),
                             si_s[np.clip(pos, 0, len(si_s) - 1)] - s, np.inf)
            near |= (np.minimum(left, right) <= nvt.APPROACH_M)
        if float((~near).mean()) < nvt.MIN_BASELINE_SHARE:
            continue

        def _dev(z):
            dz = z - alt
            ser = pd.Series(np.where(near, np.nan, dz))
            base = ser.rolling(nvt.BASELINE_WIN_PTS, center=True,
                               min_periods=5).median()
            base = base.interpolate(limit_direction="both").to_numpy()
            return dz - base

        dev_p = _dev(z_prof)
        dev_r = _dev(z_raw)

        big = np.isfinite(dev_p) & (np.abs(dev_p) > nvt.DEFECT_THRESH_M)
        k = 0
        while k < len(big):
            if not big[k]:
                k += 1
                continue
            m2 = k
            while m2 + 1 < len(big) and big[m2 + 1]:
                m2 += 1
            if s[m2] - s[k] >= nvt.DEFECT_MIN_LEN_M:
                sl = slice(k, m2 + 1)
                rows.append(dict(
                    trip_id=tid, len_m=float(s[m2] - s[k]),
                    on_struct_share=float(on[sl].mean()),
                    dev_prof=float(np.nanmax(np.abs(dev_p[sl]))),
                    dev_raw=float(np.nanmax(np.abs(dev_r[sl]))),
                    dev_prof_mean=float(np.nanmean(dev_p[sl])),
                    dev_raw_mean=float(np.nanmean(dev_r[sl])),
                    lon=float(lon[sl].mean()), lat=float(lat[sl].mean())))
            k = m2 + 1
        if gi % 50 == 0:
            print(f"  {gi} Fahrten, {len(rows):,} Defekte", flush=True)

    D = pd.DataFrame(rows)
    if D.empty:
        print("keine Defekte")
        return
    D["struktur"] = np.where(D.on_struct_share >= 0.30, "Bauwerk",
                             np.where(D.on_struct_share > 0, "gemischt",
                                      "ohne Bauwerk"))
    # Ursache: erklaert das rohe DTM den Fehler?
    D["ursache"] = np.where(D.dev_raw >= 0.6 * D.dev_prof, "DTM",
                            np.where(D.dev_raw <= 0.3 * D.dev_prof, "Pipeline",
                                     "gemischt"))
    D.to_csv(out_dir / "defects_source.csv", index=False, encoding="utf-8")

    txt = [f"Defekte: {len(D):,} Abschnitte, {D.len_m.sum()/1000:.1f} km "
           f"(Wiederholfahrten mehrfach gezaehlt)", "",
           "== Ursache (rohes DTM als Schiedsrichter) =="]
    t = D.groupby("ursache").agg(
        n=("len_m", "size"), km=("len_m", lambda v: v.sum() / 1000.0),
        anteil_pct=("len_m", lambda v: 100.0 * v.sum() / D.len_m.sum()),
        dev_prof_p50=("dev_prof", "median"),
        dev_raw_p50=("dev_raw", "median")).round(2)
    txt += [t.to_string(), "", "== Ursache x Bauwerksbezug (Laenge in km) =="]
    piv = D.pivot_table(index="struktur", columns="ursache", values="len_m",
                        aggfunc=lambda v: v.sum() / 1000.0).round(1)
    txt += [piv.to_string(), "",
            "== die 12 groessten Pipeline-Defekte (DTM waere richtig) =="]
    pl = D[D.ursache == "Pipeline"].sort_values("dev_prof", ascending=False)
    txt += [pl.head(12)[["len_m", "struktur", "dev_prof", "dev_raw",
                         "dev_prof_mean", "dev_raw_mean", "lon", "lat"]]
            .round(2).to_string(index=False)]
    out = "\n".join(txt)
    (out_dir / "summary.txt").write_text(out, encoding="utf-8")
    print("\n" + out, flush=True)
    print(f"\nfertig: {out_dir}", flush=True)


if __name__ == "__main__":
    main()
