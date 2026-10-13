# -*- coding: utf-8 -*-
"""Gemessene Bauwerksfehler mit den Fehlermodi der Höhenzuweisung verknüpfen.

Kombiniert:
  - Ground Truth: `network_elevation_vs_telemetry.py` -> structures.csv
    (Fehler je Bauwerksstelle, aus 115.754 km Telemetrie, mit Wiederholungen)
  - Diagnose: `structure_anchor_audit.py` -> Fehlermodi F1b bis F7 je
    Linearisierungs-Run (Anker am Korridorrand, Kreuzung im Bauwerk, Spline-
    Verbiegung, Ganz-Kanten-Fallback, ...)

Damit wird die Frage beantwortet, die vorher nur heuristisch war: **welcher
Fehlermodus erklärt den tatsächlich gemessenen Höhenfehler?**

Vorgehen:
  1. Bauwerksstellen mit >= MIN_PASSES Durchfahrten nehmen und in Gitterzellen
     (CELL_DEG) einteilen.
  2. Die Zellen mit dem größten gemessenen Fehler UND eine gleich große
     Kontrollgruppe mit kleinem Fehler auswählen.
  3. Je Zelle das Audit fahren (Produktionspfad, selbstvalidiert).
  4. Audit-Runs und Messstellen räumlich verknüpfen (< JOIN_M).
  5. Gemessenen Fehler nach Fehlermodus auswerten.

Aufruf:
  python audit_groundtruth_join.py --gt data/network_elev_vs_telemetry/net_V2_<stamp>
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
OUT_ROOT = _SCRIPT_DIR / "data" / "audit_gt_join"

MIN_PASSES = 3
CELL_DEG = 0.15
JOIN_M = 150.0
N_CELLS_WORST = 6
N_CELLS_CONTROL = 4
CELL_PAD_DEG = 0.02


def _import_audit():
    p = _SCRIPT_DIR / "structure_anchor_audit.py"
    spec = spec_from_file_location("saa", str(p))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pick_cells(G):
    """Zellen mit den größten gemessenen Fehlern plus Kontrollzellen."""
    g = G[G["n_passes"] >= MIN_PASSES].copy()
    g["cx"] = (g["lon"] / CELL_DEG).round().astype(int)
    g["cy"] = (g["lat"] / CELL_DEG).round().astype(int)
    cells = g.groupby(["cx", "cy"]).agg(
        n_places=("group", "size"), n_passes=("n_passes", "sum"),
        dev_med=("dev_max_med", "median"), dev_max=("dev_max_med", "max"),
        lon=("lon", "mean"), lat=("lat", "mean")).reset_index()
    cells = cells[cells["n_places"] >= 2]
    worst = cells.sort_values("dev_max", ascending=False).head(N_CELLS_WORST)
    rest = cells[~cells.index.isin(worst.index)]
    control = rest.sort_values("dev_med").head(N_CELLS_CONTROL)
    worst = worst.assign(role="worst")
    control = control.assign(role="control")
    return pd.concat([worst, control], ignore_index=True), g


def run_audit_for_cell(saa, cell, tag):
    lon0 = (cell["cx"] - 0.5) * CELL_DEG - CELL_PAD_DEG
    lon1 = (cell["cx"] + 0.5) * CELL_DEG + CELL_PAD_DEG
    lat0 = (cell["cy"] - 0.5) * CELL_DEG - CELL_PAD_DEG
    lat1 = (cell["cy"] + 0.5) * CELL_DEG + CELL_PAD_DEG
    bbox = (lon0, lat0, lon1, lat1)
    e_simp, e_det, n_det = saa.load_region(bbox)
    if e_simp.empty or e_det.empty:
        return None, None
    edges_short, node_lonlat = saa.build_shortened_network(
        saa._import_script04(), e_simp, e_det, n_det, saa.MAX_LINK_LENGTH_M)
    s04 = saa._import_script04()
    dtm = s04.load_dtm(str(saa.DTM_PATH))
    df, prof, cache, z_mirror = saa.audit_region(
        s04, edges_short, node_lonlat, dtm, bbox, tag)
    maxdz, _, _ = saa.cross_check(s04, edges_short, node_lonlat, dtm, z_mirror)
    return df, maxdz


def join(audit_df, gt_places):
    """Audit-Runs den Messstellen zuordnen (nächster Nachbar < JOIN_M)."""
    if audit_df is None or audit_df.empty or gt_places.empty:
        return pd.DataFrame()
    tf = Transformer.from_crs("EPSG:4326", "EPSG:4839", always_xy=True)
    ax, ay = tf.transform(audit_df["lon"].to_numpy(float),
                          audit_df["lat"].to_numpy(float))
    gx, gy = tf.transform(gt_places["lon"].to_numpy(float),
                          gt_places["lat"].to_numpy(float))
    tree = cKDTree(np.column_stack([np.asarray(ax), np.asarray(ay)]))
    d, idx = tree.query(np.column_stack([np.asarray(gx), np.asarray(gy)]))
    ok = np.isfinite(d) & (d <= JOIN_M)
    out = gt_places.loc[ok].reset_index(drop=True).copy()
    a = audit_df.iloc[idx[ok]].reset_index(drop=True)
    for c in ["span_id", "span_len_m", "n_samples", "kind", "source",
              "F1b_anchor_dev_left_m", "F1b_anchor_dev_right_m",
              "F2_anchor_at_edge", "F3_junctions_in_span", "F4_spline_dev_m",
              "F5_too_short", "F6_fallback", "F7_grade_implausible",
              "dtm_dive_m", "residual_climb_m", "severity_m", "grade_pct"]:
        if c in a.columns:
            out["a_" + c] = a[c].to_numpy()
    out["join_dist_m"] = d[ok]
    return out


def report(J):
    lines = [f"verknüpfte Stellen: {len(J)}", ""]
    if J.empty:
        return "\n".join(lines)
    J = J.copy()
    J["gt_abs"] = J["dev_max_med"].abs()
    lines.append("gemessener Fehler (median |dev| je Stelle) nach Fehlermodus:")
    for col, txt in [("a_F2_anchor_at_edge", "F2 Anker am Korridorrand"),
                     ("a_F5_too_short", "F5 Run < 2 Samples"),
                     ("a_F6_fallback", "F6 Ganz-Kanten-Fallback"),
                     ("a_F7_grade_implausible", "F7 Neigung unplausibel")]:
        if col not in J:
            continue
        for val in (True, False):
            m = J[col].astype(bool) == val
            if m.sum() == 0:
                continue
            lines.append(f"  {txt} = {str(val):5s}: n={int(m.sum()):4d}  "
                         f"gemessen p50 {J.loc[m,'gt_abs'].median():6.2f} m  "
                         f"p90 {J.loc[m,'gt_abs'].quantile(0.9):6.2f} m")
    if "a_F3_junctions_in_span" in J:
        for lab, m in [("F3 Kreuzung im Bauwerk = True",
                        J["a_F3_junctions_in_span"] > 0),
                       ("F3 Kreuzung im Bauwerk = False",
                        J["a_F3_junctions_in_span"] == 0)]:
            if m.sum():
                lines.append(f"  {lab}: n={int(m.sum()):4d}  "
                             f"gemessen p50 {J.loc[m,'gt_abs'].median():6.2f} m  "
                             f"p90 {J.loc[m,'gt_abs'].quantile(0.9):6.2f} m")
    lines.append("")
    lines.append("Rangkorrelation Audit-Kennzahl gegen gemessenen Fehler (Spearman):")
    for c in ["a_F1b_anchor_dev_left_m", "a_F1b_anchor_dev_right_m",
              "a_F4_spline_dev_m", "a_dtm_dive_m", "a_residual_climb_m",
              "a_severity_m", "a_span_len_m"]:
        if c in J and J[c].notna().sum() > 5:
            r = J[[c, "gt_abs"]].dropna().corr(method="spearman").iloc[0, 1]
            lines.append(f"  {c:32s} rho = {r:+.3f}")
    lines.append("")
    lines.append("die 15 gemessen schlechtesten Stellen mit ihren Flags:")
    cols = ["group", "kind", "n_passes", "len_m", "dev_max_med", "dev_mean_med",
            "a_span_len_m", "a_F1b_anchor_dev_left_m", "a_F1b_anchor_dev_right_m",
            "a_F2_anchor_at_edge", "a_F3_junctions_in_span", "a_F4_spline_dev_m",
            "a_dtm_dive_m", "join_dist_m", "lon", "lat"]
    cols = [c for c in cols if c in J.columns]
    lines.append(J.sort_values("gt_abs", ascending=False).head(15)[cols]
                 .round(2).to_string(index=False))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True, help="Ordner eines Ground-Truth-Laufs")
    args = ap.parse_args()

    gt_dir = Path(args.gt)
    G = pd.read_csv(gt_dir / "structures.csv")
    saa = _import_audit()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Ground Truth: {gt_dir.name}\nAusgabe: {out_dir}", flush=True)

    cells, places = pick_cells(G)
    print(f"{len(cells)} Zellen ausgewählt "
          f"({int((cells['role']=='worst').sum())} worst, "
          f"{int((cells['role']=='control').sum())} Kontrolle)", flush=True)

    joined = []
    for i, cell in cells.iterrows():
        tag = f"C{i:02d}{cell['role'][0].upper()}"
        print(f"\n[{tag}] lon~{cell['lon']:.2f} lat~{cell['lat']:.2f}  "
              f"{int(cell['n_places'])} Stellen, gemessen median "
              f"{cell['dev_med']:.2f} m, max {cell['dev_max']:.2f} m", flush=True)
        try:
            adf, maxdz = run_audit_for_cell(saa, cell, tag)
        except Exception as e:
            print(f"  Audit fehlgeschlagen: {type(e).__name__}: {e}", flush=True)
            continue
        if adf is None or adf.empty:
            print("  keine Bauwerks-Runs im Audit", flush=True)
            continue
        print(f"  Audit: {len(adf)} Runs, Selbstvalidierung max|dz| {maxdz:.1e} m",
              flush=True)
        sub = places[((places["lon"] / CELL_DEG).round().astype(int) == cell["cx"]) &
                     ((places["lat"] / CELL_DEG).round().astype(int) == cell["cy"])]
        J = join(adf, sub)
        if not J.empty:
            J["cell"] = tag
            J["role"] = cell["role"]
            joined.append(J)
            print(f"  verknüpft: {len(J)} von {len(sub)} Messstellen", flush=True)

    if not joined:
        print("nichts verknüpft")
        return
    J = pd.concat(joined, ignore_index=True)
    J.to_csv(out_dir / "joined.csv", index=False, encoding="utf-8")
    txt = report(J)
    (out_dir / "summary.txt").write_text(txt, encoding="utf-8")
    print("\n" + txt, flush=True)
    print(f"\nfertig: {out_dir}", flush=True)


if __name__ == "__main__":
    main()
