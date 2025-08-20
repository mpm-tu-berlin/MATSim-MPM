# merge_networks_points_only_100m_both.py
# Regeln:
# - Standard: simplified-Edges bleiben.
# - Bridge/Tunnel: Falls bridge=yes oder tunnel=yes:
#     * per Punkte-Matching (P_i -> P_{i+1}) alle detailed-Teilstücke einsammeln
#     * Summe der Längen der Segmente mit bridge/tunnel=yes berechnen (in Metern)
#     * > 100 m  -> gesamte simplified-Kante durch die gefundenen detailed-Segmente ersetzen
#     * <= 100 m -> simplified behalten UND die entsprechenden Tags (bridge/tunnel) entfernen
#
# Matching:
# - KEIN räumlicher Match, nur Punkt-zu-Punkt. Detailed-Edges sind bereits sortiert.
#
# u/v:
# - u/v werden gegen die detailed-Nodes gemappt.

import warnings
warnings.filterwarnings("ignore")

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from tqdm import tqdm

# ----------------------------
# Helper
# ----------------------------

def is_yes(x):
    return isinstance(x, str) and x.lower() == "yes"

def build_uv_from_nodes(edge_gdf, node_gdf, precision=5):
    node_map = {
        (round(pt.x, precision), round(pt.y, precision)): idx
        for idx, pt in zip(node_gdf.index, node_gdf.geometry)
        if isinstance(pt, Point)
    }
    def lookup_start(geom):
        x, y = geom.coords[0]
        return node_map.get((round(x, precision), round(y, precision)))
    def lookup_end(geom):
        x, y = geom.coords[-1]
        return node_map.get((round(x, precision), round(y, precision)))
    edge_gdf["u"] = edge_gdf["geometry"].apply(lookup_start)
    edge_gdf["v"] = edge_gdf["geometry"].apply(lookup_end)
    return edge_gdf

def coord_key(pt, precision=6):
    return (round(pt[0], precision), round(pt[1], precision))

# ----------------------------
# Konfiguration
# ----------------------------

SIMPLIFIED_GPKG = "data/test_brandenburg_simplified.gpkg"
DETAILED_GPKG   = "data/test_brandenburg_detailed_sorted.gpkg"

SIMPLIFIED_LAYER_EDGES = "edges"
SIMPLIFIED_LAYER_NODES = "nodes"

DETAILED_LAYER_EDGES = "edges"
DETAILED_LAYER_NODES = "nodes"

OUTPUT_GPKG = "data/test_brandenburg_merged.gpkg"
OUT_LAYER_EDGES = "edges"
OUT_LAYER_NODES = "nodes"

# Koordinaten-Rundung (muss zum Export passen)
PREC = 6

# Schwellwert (Meter) für Brücke/Tunnel
MIN_STRUCT_LEN_M = 100.0

# ----------------------------
# Laden
# ----------------------------

print("Lade simplified…")
simp_edges = gpd.read_file(SIMPLIFIED_GPKG, layer=SIMPLIFIED_LAYER_EDGES)
simp_nodes = gpd.read_file(SIMPLIFIED_GPKG, layer=SIMPLIFIED_LAYER_NODES)

print("Lade detailed…")
det_edges = gpd.read_file(DETAILED_GPKG, layer=DETAILED_LAYER_EDGES)
det_nodes = gpd.read_file(DETAILED_GPKG, layer=DETAILED_LAYER_NODES)

# CRS angleichen
if simp_edges.crs != det_edges.crs:
    det_edges = det_edges.to_crs(simp_edges.crs)
    det_nodes = det_nodes.to_crs(simp_nodes.crs)

# Metrisches CRS für Längen
try:
    LEN_CRS = simp_edges.estimate_utm_crs()
except Exception:
    LEN_CRS = None
if LEN_CRS is None:
    LEN_CRS = "EPSG:3857"  # Fallback

# ----------------------------
# Index auf detailed: (start_key, end_key) -> Indexliste
# (inkl. reversed für umgekehrte Richtung)
# ----------------------------

print("Baue Kanten-Index (detailed)…")
det_edges = det_edges.copy()
det_edges["_start_key"] = det_edges.geometry.apply(lambda g: coord_key(g.coords[0], PREC))
det_edges["_end_key"]   = det_edges.geometry.apply(lambda g: coord_key(g.coords[-1], PREC))

from collections import defaultdict
idx_fwd = defaultdict(list)   # (s,e) -> [rowidx,...]
idx_rev = defaultdict(list)   # (e,s) -> [rowidx,...]

for ridx, row in det_edges.iterrows():
    s, e = row["_start_key"], row["_end_key"]
    idx_fwd[(s, e)].append(ridx)
    idx_rev[(e, s)].append(ridx)  # umgekehrte Richtung

# ----------------------------
# Auswahl: welche simplified-Kanten sind Kandidaten?
# ----------------------------

simp_edges["__is_bridge"] = simp_edges.get("bridge", pd.Series([None]*len(simp_edges))).apply(is_yes)
simp_edges["__is_tunnel"] = simp_edges.get("tunnel", pd.Series([None]*len(simp_edges))).apply(is_yes)
simp_edges["__replace_candidate"] = simp_edges["__is_bridge"] | simp_edges["__is_tunnel"]

to_check   = simp_edges[simp_edges["__replace_candidate"]].copy()
to_keep    = simp_edges[~simp_edges["__replace_candidate"]].copy()

print(f"Simplified-Edges gesamt: {len(simp_edges)} | Kandidaten (bridge/tunnel=yes): {len(to_check)}")

# ----------------------------
# Prüfen & ggf. ersetzen
# ----------------------------

repl_rows = []           # gesammelte detailed-Teilketten
not_covered = 0          # Abschnitte ohne Match (Diagnose)
short_struct_cleaned = 0 # Anzahl kurzer Brücken/Tunnel, bei denen Tag entfernt wurde

detail_cols = [c for c in det_edges.columns if c not in {"_start_key","_end_key"}]

for sidx, srow in tqdm(to_check.iterrows(), total=len(to_check), desc="prüfe & ersetze"):
    geom = srow.geometry
    if geom is None or geom.is_empty or len(geom.coords) < 2:
        # keine gültige Geometrie -> einfach behalten
        to_keep = pd.concat([to_keep, srow.to_frame().T], ignore_index=True)
        continue

    coords = list(geom.coords)
    seg_parts = []
    complete = True

    for i in range(len(coords) - 1):
        a = coord_key(coords[i],   PREC)
        b = coord_key(coords[i+1], PREC)

        cand_idx = idx_fwd.get((a, b), [])
        if not cand_idx:
            cand_idx = idx_rev.get((a, b), [])

        if not cand_idx:
            complete = False
            not_covered += 1
            break

        seg_parts.append(det_edges.loc[cand_idx[0], detail_cols])

    if not complete or not seg_parts:
        # Wenn wir es nicht sicher rekonstruieren können: nicht ersetzen, Tags nicht anfassen
        to_keep = pd.concat([to_keep, srow.to_frame().T], ignore_index=True)
        continue

    seg_gdf = gpd.GeoDataFrame(seg_parts, geometry="geometry", crs=det_edges.crs)

    # Länge der Bridge-/Tunnel-Teile bestimmen (Meter)
    bt_mask = seg_gdf.get("bridge", pd.Series([None]*len(seg_gdf))).apply(is_yes) | \
              seg_gdf.get("tunnel", pd.Series([None]*len(seg_gdf))).apply(is_yes)

    bt_len_m = 0.0
    if bt_mask.any():
        bt_len_m = seg_gdf.loc[bt_mask].to_crs(LEN_CRS).geometry.length.sum()

    # Entscheidung:
    if bt_len_m > MIN_STRUCT_LEN_M:
        # ersetzen: gesamte Kette übernehmen
        repl_rows.append(seg_gdf)
    else:
        # kurz: simplified behalten, Tags entfernen (bridge/tunnel -> None)
        new_row = srow.copy()
        if "__is_bridge" in new_row and new_row["__is_bridge"]:
            new_row["bridge"] = None
        if "__is_tunnel" in new_row and new_row["__is_tunnel"]:
            new_row["tunnel"] = None
        to_keep = pd.concat([to_keep, new_row.to_frame().T], ignore_index=True)
        short_struct_cleaned += 1

# ----------------------------
# Vereinigen: behaltene simplified + ersetzende detailed
# ----------------------------

repl_detailed = (
    gpd.GeoDataFrame(pd.concat(repl_rows, ignore_index=True), geometry="geometry", crs=det_edges.crs)
    if repl_rows else
    det_edges.iloc[0:0].copy()
)

common_cols = list(to_keep.columns.intersection(repl_detailed.columns))
if "geometry" not in common_cols:
    common_cols.append("geometry")

out_edges = pd.concat(
    [to_keep[common_cols], repl_detailed[common_cols]],
    ignore_index=True,
    sort=False
)

# Hilfsspalten aufräumen
for col in ["__is_bridge","__is_tunnel","__replace_candidate","_start_key","_end_key"]:
    if col in out_edges.columns:
        out_edges.drop(columns=[col], inplace=True, errors="ignore")

# ----------------------------
# u/v anhand detailed-Nodes
# ----------------------------

out_edges = build_uv_from_nodes(out_edges, det_nodes, precision=PREC)

# ----------------------------
# Export
# ----------------------------

out_nodes = det_nodes.copy()

print(f"Exportiere nach {OUTPUT_GPKG} …")
out_nodes.to_file(OUTPUT_GPKG, layer=OUT_LAYER_NODES, driver="GPKG")
out_edges.to_file(OUTPUT_GPKG, layer=OUT_LAYER_EDGES, driver="GPKG")

try:
    total_len_m = out_edges.to_crs(LEN_CRS).geometry.length.sum()
    print(f"Fertig. Kanten: {len(out_edges)}  | Ersetzt-Ketten: {len(repl_rows)}  | kurze Strukturen getaggt-entfernt: {short_struct_cleaned}  | nicht gematchte Abschnitte: {not_covered}  | Gesamtlänge (m): {total_len_m:,.1f}")
except Exception:
    print(f"Fertig. Kanten: {len(out_edges)}  | Ersetzt-Ketten: {len(repl_rows)}  | kurze Strukturen getaggt-entfernt: {short_struct_cleaned}  | nicht gematchte Abschnitte: {not_covered}")
