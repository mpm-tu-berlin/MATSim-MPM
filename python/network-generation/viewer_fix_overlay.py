# -*- coding: utf-8 -*-
"""Bauwerks-Fix-Overlay fuer den Korridor-Viewer.

Berechnet fuer die Korridore einer corridor_viewer-Auswahl das dichte
Fahrbahnprofil MIT eingeschaltetem korridoruebergreifendem Bauwerks-Fix
(global_structures=True, jetzt mit ADAPTIVER Ankerzone) und legt je
Telemetriepunkt die Fix-Hoehe ab. corridor_viewer.py haengt sie ueber
--fix-overlay als zusaetzliche Linie ein.

Selektion identisch zu corridor_viewer.py (gleiche defects.csv, gleiches
Dedup-Raster, gleiche Sortierung) — die Punktreihen passen 1:1 aufeinander.

Aufruf:
  python viewer_fix_overlay.py --eval-dir data/network_elev_vs_telemetry/net_V3_<stamp> \
      [--top 30] [--window-km 5]
"""
import argparse
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.ops import transform as shapely_transform

_SCRIPT_DIR = Path(__file__).parent
_NETGEN = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
CORPUS = _SCRIPT_DIR / "data" / "telemetry_elev_corpus" / "corpus.parquet"
DTM_PATH = _SCRIPT_DIR / "data" / "DTM Germany 20m v3b by Sonny.tif"
SIMPLIFIED_GPKG = _NETGEN / "data" / "germany_simplified_DF.gpkg"
DETAILED_GPKG = _NETGEN / "data" / "germany_detailed_sorted_DF.gpkg"
MATCH_M = 15.0
BUFFER_M = 500.0
TR_TO = Transformer.from_crs("EPSG:4326", "EPSG:4839", always_xy=True)
TR_BACK = Transformer.from_crs("EPSG:4839", "EPSG:4326", always_xy=True)


def _import(name, path):
    spec = spec_from_file_location(name, str(path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def select_corridors(ev, top, dedup_km, min_defect_trips=2):
    """Exakt dieselbe Auswahl wie corridor_viewer.py (inkl. Pass-Konsistenz)."""
    df = pd.read_csv(ev / "defects.csv")
    if "dev_consens_m" in df.columns:
        df = df.sort_values("dev_consens_m", key=lambda v: v.abs(),
                            ascending=False)
    else:
        df = df.sort_values("dev_max_abs_m", ascending=False)
    if min_defect_trips > 0 and "n_trips_defect" in df.columns:
        df = df[df.n_trips_defect >= min_defect_trips]
    grid = dedup_km * 1000.0
    df["cell"] = (df.x // grid).astype(int).astype(str) + "_" + (df.y // grid).astype(int).astype(str)
    return df.drop_duplicates("cell").head(top).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-dir", required=True)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--window-km", type=float, default=5.0)
    ap.add_argument("--dedup-km", type=float, default=2.0)
    ap.add_argument("--min-defect-trips", type=int, default=2)
    a = ap.parse_args()
    ev = Path(a.eval_dir) if Path(a.eval_dir).is_absolute() else _SCRIPT_DIR / a.eval_dir

    df = select_corridors(ev, a.top, a.dedup_km, a.min_defect_trips)
    print(f"{len(df)} Korridore")

    s04 = _import("script04", _NETGEN / "04_build_matsim_network_from_local_osm_and_kdtree.py")
    print("Lade DTM + GPKGs ...", flush=True)
    dtm = s04.load_dtm(str(DTM_PATH))
    _, gdf_edges_simp = s04.load_local_osm_file(str(SIMPLIFIED_GPKG))
    gdf_nodes_det, gdf_edges_det = s04.load_local_osm_file(str(DETAILED_GPKG))

    corpus = pd.read_parquet(CORPUS)
    win = a.window_km * 1000.0
    rows = []
    for rank, row in df.iterrows():
        trip = corpus[corpus.trip_id == row.trip_id].sort_values("s_m")
        w = trip[(trip.s_m >= row.s0_m - win) & (trip.s_m <= row.s1_m + win)].reset_index(drop=True)
        if len(w) < 30:
            continue
        name = f"K{rank+1:02d}"
        try:
            line = LineString(list(zip(w.lon.values, w.lat.values)))
            line_m = shapely_transform(TR_TO.transform, line)
            poly = shapely_transform(TR_BACK.transform, line_m.buffer(BUFFER_M))

            e_simp = gdf_edges_simp[gdf_edges_simp.intersects(poly)].copy()
            e_det = gdf_edges_det[gdf_edges_det.intersects(poly)].copy()
            if e_simp.empty:
                print(f"  {name}: keine Kanten im Korridor — skip")
                continue
            edges, split_xy = s04.short_edges(
                gdf_edges_simplified=e_simp, gdf_edges_detailed=e_det,
                max_allowed_length=250.0)

            # Knoten-Koordinaten (wie generate_section_link_length_variants)
            used = set(map(str, edges["u"])) | set(map(str, edges["v"]))
            det = gdf_nodes_det.copy()
            det["osmid_norm"] = pd.to_numeric(
                det["osmid"].apply(lambda v: v[0] if isinstance(v, (list, tuple, np.ndarray)) and len(v) else v),
                errors="coerce")
            det = det.dropna(subset=["osmid_norm"])
            osm_xy = {str(int(r["osmid_norm"])): (float(r.geometry.x), float(r.geometry.y))
                      for _, r in det.iterrows() if str(int(r["osmid_norm"])) in used}
            node_lonlat = {}
            for nid in used:
                if nid in osm_xy:
                    node_lonlat[nid] = osm_xy[nid]
                elif nid in split_xy:
                    node_lonlat[nid] = split_xy[nid]
            # Rest aus Kantenenden
            for _, r in edges.iterrows():
                for nid, ix in ((str(r["u"]), 0), (str(r["v"]), -1)):
                    if nid not in node_lonlat:
                        c = r.geometry.coords[ix]
                        node_lonlat[nid] = (float(c[0]), float(c[1]))

            _, dense = s04.assign_heights_along_corridors(
                edges, node_lonlat, dtm, target_epsg=4839,
                sample_step_m=5.0, smooth_rms_m=1.0,
                collect_dense=True, global_structures=True)
            if dense.shape[0] == 0:
                print(f"  {name}: kein dichtes Profil — skip")
                continue
            dx, dy = TR_TO.transform(dense[:, 0], dense[:, 1])
            tree = cKDTree(np.column_stack([dx, dy]))
            px, py = TR_TO.transform(w.lon.values, w.lat.values)
            dd, ii = tree.query(np.column_stack([px, py]))
            zfix = np.where(dd <= MATCH_M, dense[ii, 2], np.nan)
            for s_m, z in zip(w.s_m.values, zfix):
                rows.append({"corridor": name, "s_m": float(s_m),
                             "z_fix": (float(z) if np.isfinite(z) else None)})
            print(f"  {name}: {np.isfinite(zfix).sum()}/{len(w)} Punkte mit Fix-Hoehe")
        except Exception as e:
            print(f"  {name}: FEHLER {type(e).__name__}: {e}")

    out = ev / "fix_overlay.parquet"
    pd.DataFrame(rows).to_parquet(out, index=False)
    print(f"\n{out}")


if __name__ == "__main__":
    main()
