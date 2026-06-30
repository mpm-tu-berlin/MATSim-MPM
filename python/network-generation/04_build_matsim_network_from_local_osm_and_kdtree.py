# -*- coding: utf-8 -*-
"""
MATSim Edge Shortener & Network Exporter

This script builds a MATSim network from local (simplified + detailed) OSM data and a height
KDTree (lon/lat EPSG:4326), while enforcing a maximum link length constraint by replacing long
simplified edges with the corresponding directed sequence of detailed segments and recursively
splitting at the nearest detailed segment boundary around the simplified half-length.

Key features
------------
- Only simplified edges with length > `max_allowed_link_length` are replaced.
- Replacement follows the matched *directed* detailed sequence between (u, v).
- The split point is chosen by summing from both ends and taking the first detailed boundary
  that exceeds half of the simplified edge length, minimizing imbalance.
- Recursive halving continues until all parts are <= `max_allowed_link_length`.
- Robust attribute handling and geometry length fallbacks (EPSG:3857) for safety.
- Outputs a MATSim network_v2 DTD XML (gzipped) with optional node Z (height).

Dependencies
------------
- numpy, pandas, geopandas, shapely, scipy (KDTree), tqdm

Inputs (example from __main__)
-----------------------------
- data/{area}_kdtree_from_roads3d_epsg4326.npz  (npz with 'coords' [lon, lat] & 'heights')
- data/{area}_simplified.gpkg                    (layers: 'nodes', 'edges')
- data/{area}_detailed_sorted.gpkg               (layers: 'nodes', 'edges')

Author
------
Refactored and documented in English.
"""

import math
from collections import defaultdict
import gzip
import xml.etree.ElementTree as ET
import xml.dom.minidom as md

import os
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from tqdm import tqdm
import rasterio
from pyproj import Transformer

# --------------------------- Hoehen aus LiDAR-DTM ---------------------------
# Frueher kamen die Knotenhoehen aus einer npz-Punktwolke per KD-Tree-Nearest-
# Neighbor im GRAD-Raum (lon/lat). Das ist anisotrop (1 deg lon != 1 deg lat) und
# erzeugte ~2 m Hoehenrauschen mit vereinzelten Mehrmeter-Ausreissern an Bruecken/
# Parallelfahrbahnen, zusaetzlich float32-Quantisierung der Koordinaten.
# Ersetzt durch direktes, CRS-korrektes bilineares Sampling des LiDAR-DTM an der
# Knotenposition. Damit haengt die Pipeline nur noch an OSM (Topologie) + DTM (Hoehe).

DTM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "DTM Germany 20m v3b by Sonny.tif")
_DTM_QUERY_CRS = "EPSG:4326"        # Knotenkoordinaten beim Hoehenabruf (lon/lat)
_DTM_MAX_BLOCK_PX = 150_000_000     # max. Fenster fuer Block-Sampling (~600 MB), sonst knotenweise

# OSM 'none' (unlimitierte Autobahn) -> Auslegungs-/Richtgeschwindigkeit. Der LKW wird
# im Sim ohnehin durch seine eigene maximumVelocity begrenzt; entscheidend ist nur, dass
# diese Links NICHT faelschlich auf 50 km/h Default fallen.
NONE_MAXSPEED_KMH = 130.0


def load_dtm(dtm_path: str = DTM_PATH) -> str:
    """Prueft die DTM-Datei und gibt einen leichtgewichtigen Handle (den Pfad) zurueck.
    Das Raster wird in sample_heights() PRO AUFRUF geoeffnet -> thread-sicher fuer die
    parallele Varianten-Generierung."""
    if not os.path.exists(dtm_path):
        raise FileNotFoundError(f"LiDAR-DTM nicht gefunden: {dtm_path}")
    return dtm_path


def sample_heights(dtm_path: str, lons, lats) -> np.ndarray:
    """Bilineare DTM-Hoehe an (lon, lat) in EPSG:4326. NaN ausserhalb/nodata.

    Oeffnet das Raster selbst (thread-sicher), transformiert ins DTM-CRS und
    interpoliert bilinear. Regionale Netze: umschliessendes Fenster einmal laden
    (vektorisiert, schnell). Sehr grosse Bounding-Box: knotenweise 2x2-Fenster."""
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    out = np.full(lons.shape, np.nan, dtype=float)

    with rasterio.open(dtm_path) as ds:
        transformer = Transformer.from_crs(_DTM_QUERY_CRS, ds.crs, always_xy=True)
        xs, ys = transformer.transform(lons, lats)
        cols, rows = (~ds.transform) * (np.asarray(xs, float), np.asarray(ys, float))
        cols = np.where(np.isfinite(cols), cols, -1e9)
        rows = np.where(np.isfinite(rows), rows, -1e9)
        c0 = np.floor(cols).astype("int64")
        r0 = np.floor(rows).astype("int64")
        inb = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < ds.width) & (r0 + 1 < ds.height)
        if not inb.any():
            return out

        nodata = ds.nodata
        ci, ri = c0[inb], r0[inb]
        fx, fy = cols[inb] - ci, rows[inb] - ri
        cmin, cmax = int(ci.min()), int(ci.max())
        rmin, rmax = int(ri.min()), int(ri.max())
        win_w, win_h = cmax - cmin + 2, rmax - rmin + 2

        if win_w * win_h <= _DTM_MAX_BLOCK_PX:
            block = ds.read(1, window=rasterio.windows.Window(cmin, rmin, win_w, win_h)).astype(float)
            if nodata is not None:
                block = np.where(block == nodata, np.nan, block)
            rr, cc = ri - rmin, ci - cmin
            top = block[rr, cc] * (1 - fx) + block[rr, cc + 1] * fx
            bot = block[rr + 1, cc] * (1 - fx) + block[rr + 1, cc + 1] * fx
            vals = top * (1 - fy) + bot * fy
        else:
            # Sehr grosse Bounding-Box (z.B. deutschlandweites Feinnetz): in Kacheln
            # <= _DTM_MAX_BLOCK_PX aufteilen, pro belegter Kachel EIN Fenster lesen und
            # vektorisiert bilinear interpolieren (memory-bounded, schnell; nur Kacheln
            # mit Knoten werden gelesen -> bei linienhaften Strassennetzen wenig Daten).
            vals = np.full(ci.shape, np.nan, dtype=float)
            tile = int(max(256, math.floor(math.sqrt(_DTM_MAX_BLOCK_PX))))  # Kachelkante [px]
            n_tcol = ((cmax - cmin) // tile) + 1
            tile_id = ((ri - rmin) // tile) * n_tcol + ((ci - cmin) // tile)
            for tid in np.unique(tile_id):
                m = tile_id == tid
                cm0, cm1 = int(ci[m].min()), int(ci[m].max())
                rm0, rm1 = int(ri[m].min()), int(ri[m].max())
                w, h = cm1 - cm0 + 2, rm1 - rm0 + 2
                blk = ds.read(1, window=rasterio.windows.Window(cm0, rm0, w, h)).astype(float)
                if nodata is not None:
                    blk = np.where(blk == nodata, np.nan, blk)
                rr, cc, fxm, fym = ri[m] - rm0, ci[m] - cm0, fx[m], fy[m]
                top = blk[rr, cc] * (1 - fxm) + blk[rr, cc + 1] * fxm
                bot = blk[rr + 1, cc] * (1 - fxm) + blk[rr + 1, cc + 1] * fxm
                vals[m] = top * (1 - fym) + bot * fym

        out[inb] = vals
    return out


def _normalize_maxspeed(v):
    """OSM-maxspeed -> km/h float. 'none' (unlimitierte Autobahn) -> NONE_MAXSPEED_KMH,
    'walk' -> 7. Unbekannt/leer -> np.nan (Aufrufer setzt dann seinen Default)."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return np.nan
    s = str(v).strip().lower()
    if s in ("none", "de:motorway", "signals", "variable"):
        return float(NONE_MAXSPEED_KMH)
    if s in ("walk", "de:walk", "de:living_street"):
        return 7.0
    try:
        return float(s.replace(",", "."))
    except Exception:
        return np.nan


# --- Rueckwaerts-kompatible Shims fuer bestehende Aufrufer -------------------
# Die alte npz/KD-Tree-Schnittstelle bleibt aufrufbar, liefert aber jetzt DTM-Hoehen.
# load_kdtree(pfad) ignoriert den (nicht mehr noetigen) npz-Pfad.

def load_kdtree(input_path=None):
    """DEPRECATED: Hoehen kommen jetzt direkt aus dem LiDAR-DTM. Gibt
    (dtm_handle, None, None) zurueck, damit altes 3-Tupel-Entpacken weiter funktioniert."""
    return load_dtm(), None, None


def kdtree_heights_vectorized(tree, heights, xs, ys):
    """DEPRECATED: ignoriert die alten KD-Tree-Argumente und sampelt das DTM direkt.
    `tree` ist der dtm_handle aus load_kdtree(); `heights` wird ignoriert."""
    return sample_heights(tree, xs, ys)

def load_local_osm_file(local_osm_input_path):
    """Load nodes and edges from a local GeoPackage with layers 'nodes' and 'edges' (EPSG:4326)."""
    gdf_nodes = gpd.read_file(local_osm_input_path, layer="nodes").set_crs("EPSG:4326", allow_override=True)
    gdf_edges = gpd.read_file(local_osm_input_path, layer="edges").set_crs("EPSG:4326", allow_override=True)
    return gdf_nodes, gdf_edges

def _truthy_flag(val) -> bool:
    """Interpret a variety of truthy/falsy inputs as boolean."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip().lower()
    return s not in ("", "no", "false", "0")

def _num(x, default=None, as_int=False):
    """Parse robust numeric value: return float/int or default. Removes NaN/inf."""
    try:
        v = float(x)
        if not math.isfinite(v):
            return default
        return int(round(v)) if as_int else v
    except Exception:
        return default

def _clean_text(val, default="unknown"):
    """Return a clean string value without empty/'nan'."""
    if val is None:
        return default
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return default
    return s

def _maybe_bool(x):
    """Tri-state bool parser: True/False or None if unknown."""
    s = str(x).strip().lower()
    if s in ("1", "true", "yes"):  return True
    if s in ("0", "false", "no"):  return False
    return None

def _flatten_osmids_from_block(block: gpd.GeoDataFrame) -> list[int]:
    """Extract ordered, de-duplicated OSMIDs from block['osmid']."""
    out, seen = [], set()
    for val in block.get('osmid', []):
        for osm in _osmid_list(val):
            if osm not in seen:
                seen.add(osm)
                out.append(osm)
    return out


# --------------------------- Detailed sequence (directed) ---------------------------
def _osmid_list(val):
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [int(x) for x in s.replace(" ", "").split(",") if x]

def build_osmid_index_for_detailed(gdf_edges_detailed: gpd.GeoDataFrame,
                                   reversed_col: str = "reversed"):
    """
    Build indices for quick candidate lookup by OSMID and optional 'reversed' flag.
    Returns a dict with:
      - 'any': osmid -> [row_idx]
      - 'rev': (osmid, reversed_bool) -> [row_idx]
      - 'has_rev': whether the column exists
    """
    idx_any = defaultdict(list)              # osmid -> [row_idx]
    idx_rev = defaultdict(list)              # (osmid, rev_bool) -> [row_idx]
    has_rev = reversed_col in gdf_edges_detailed.columns

    for i, row in gdf_edges_detailed.iterrows():
        # collect OSMIDs for this detailed edge row
        for osmid in str(row.get('osmid', '')).strip('[]').replace(' ', '').split(','):
            if not osmid:
                continue
            try:
                osm = int(osmid)
            except Exception:
                continue

            idx_any[osm].append(i)

            if has_rev:
                rev = _maybe_bool(row.get(reversed_col))
                if rev is not None:
                    idx_rev[(osm, rev)].append(i)

    return {"any": idx_any, "rev": idx_rev, "has_rev": has_rev}

def _empty_like(df, crs="EPSG:4326"):
    cols = list(df.columns) if isinstance(df, (pd.DataFrame, gpd.GeoDataFrame)) else []
    return gpd.GeoDataFrame(columns=cols).set_crs(crs)

def get_directed_detailed_sequence(edge, gdf_edges_detailed, osmid_index, DEBUG=False) -> gpd.GeoDataFrame:
    """
    Build a *directed* sequence of detailed segments from u0 -> v0.

    Priority when extending from the *current* set (primary/alt):
      1) same OSMID,   u == current (forward within OSMID block)
      2) same OSMID,   v == current (flip within OSMID block)
      3) other OSMID,  u == current (forward at OSMID boundary)
      4) other OSMID,  v == current (flip at OSMID boundary)

    Switching between primary/alt is kept as fallback. When reconstructing:
      - Segments from 'alt' toggle the 'reversed' flag (if present).
      - Steps taken via 'v' (reverse direction) flip geometry and swap u/v.
    """
    try:
        # --- Simplified edge meta ---
        u0, v0 = int(edge["u"]), int(edge["v"])
        osmid_str = str(edge.get("osmid", "")).strip("[]").replace(" ", "")
        osmids = [int(x) for x in osmid_str.split(",") if x]

        edge_rev = _maybe_bool(edge.get("reversed")) if ("reversed" in edge) else None
        has_rev = bool(osmid_index.get("has_rev"))

        # --- Candidate selection via OSMIDs (+ optional reversed) ---
        cand_idx_set = set()
        if has_rev and (edge_rev is not None):
            for osm in osmids:
                cand_idx_set.update(osmid_index["rev"].get((osm, edge_rev), []))
            if DEBUG and not cand_idx_set:
                print(f"[osmid+rev] no candidates for {u0}->{v0} (osmids={osmids}, reversed={edge_rev})")
        else:
            for osm in osmids:
                cand_idx_set.update(osmid_index["any"].get(osm, []))
            if DEBUG and not cand_idx_set:
                print(f"[osmid] no candidates for {u0}->{v0} (osmids={osmids})")

        if not cand_idx_set:
            return _empty_like(gdf_edges_detailed)

        cand = gdf_edges_detailed.loc[sorted(cand_idx_set)].copy()
        if cand.empty:
            if DEBUG:
                print("[cand] empty after OSMID(+reversed) filter")
            return _empty_like(gdf_edges_detailed)

        # --- Cleanup u/v ---
        def _cleanup_uv(df):
            df = df.copy()
            df["u"] = pd.to_numeric(df["u"], errors="coerce")
            df["v"] = pd.to_numeric(df["v"], errors="coerce")
            df = df.dropna(subset=["u", "v"]).copy()
            df["u"] = df["u"].astype(int)
            df["v"] = df["v"].astype(int)
            return df

        cand = _cleanup_uv(cand)

        # --- Build lookups ---
        def build_by_start(df):
            m = {}
            for i, r in df.iterrows():
                m.setdefault(int(r["u"]), []).append(i)
            return m

        def build_by_end(df):
            m = {}
            for i, r in df.iterrows():
                m.setdefault(int(r["v"]), []).append(i)
            return m

        by_u_primary   = build_by_start(cand)
        by_end_primary = build_by_end(cand)

        # --- Alt set: reversed == not edge_rev ---
        cand_alt = _empty_like(gdf_edges_detailed)
        by_u_alt, by_end_alt = {}, {}
        if has_rev and (edge_rev is not None):
            alt_idx_set = set()
            for osm in osmids:
                alt_idx_set.update(osmid_index["rev"].get((osm, not edge_rev), []))
            if alt_idx_set:
                cand_alt = _cleanup_uv(gdf_edges_detailed.loc[sorted(alt_idx_set)].copy())
                by_u_alt   = build_by_start(cand_alt)
                by_end_alt = build_by_end(cand_alt)

        # --- Stable first-OSMID accessor per row ---
        def row_osmid_first(r):
            try:
                ids = _osmid_list(r.get('osmid', ''))
                return ids[0] if ids else None
            except Exception:
                return None

        # --- Select start: prefer primary u==u0, else alt u==u0 ---
        used_primary, used_alt = set(), set()
        seq = []  # list of (src, idx, how) with how in {'u','v','start'}

        original_source = "primary"
        current_source = "primary"
        cand_cur, by_u_cur, by_end_cur = cand, by_u_primary, by_end_primary
        cand_other, by_u_other, by_end_other, other_label = cand_alt, by_u_alt, by_end_alt, "alt"

        start_list = by_u_cur.get(u0, [])
        if not start_list:
            alt_list = by_u_alt.get(u0, [])
            if alt_list:
                current_source = "alt"
                cand_cur, by_u_cur, by_end_cur = cand_alt, by_u_alt, by_end_alt
                cand_other, by_u_other, by_end_other, other_label = cand, by_u_primary, by_end_primary, "primary"
                start_list = alt_list
            else:
                if DEBUG:
                    print(f"[start] no start segment via u==u0 in primary/alt | u0={u0}")
                return _empty_like(gdf_edges_detailed)

        start_idx = start_list[0]
        (used_primary if current_source == "primary" else used_alt).add(start_idx)
        seq.append((current_source, start_idx, "start"))

        cur = int(cand_cur.loc[start_idx]["v"])
        last_osm = row_osmid_first(cand_cur.loc[start_idx])  # active OSMID block

        if DEBUG:
            ru = int(cand_cur.loc[start_idx]["u"]); rv = int(cand_cur.loc[start_idx]["v"])
            print(f"[start] idx={start_idx} seg=({ru}->{rv}) source={current_source} cur={cur} target v0={v0} osmid={last_osm}")

        # For legacy fallback (once) when switching back to original_source
        allow_v_once = False

        # --- Try to extend within current source with OSMID-aware priority ---
        def try_current(cur_node, last_osm):
            """
            Priorities:
              1) same OSMID, u==cur
              2) same OSMID, v==cur (flip)
              3) other OSMID, u==cur
              4) other OSMID, v==cur (flip)
            If all fail:
              - optional legacy once-flip only in primary (allow_v_once)
            """
            nonlocal allow_v_once
            df = cand_cur
            used_set = used_primary if current_source == "primary" else used_alt

            lst_u = by_u_cur.get(cur_node, [])
            lst_v = by_end_cur.get(cur_node, [])

            def pick(candidates, prefer_same_osm: bool, via: str):
                # via: 'u' (forward) or 'v' (flip)
                for i in candidates:
                    if i in used_set:
                        continue
                    osm_i = row_osmid_first(df.loc[i])
                    same = (osm_i == last_osm) if (last_osm is not None and osm_i is not None) else False
                    if (prefer_same_osm and same) or (not prefer_same_osm and not same):
                        new_cur = int(df.loc[i]["v"] if via == 'u' else df.loc[i]["u"])
                        used_set.add(i)
                        seq.append((current_source, i, via))
                        if DEBUG:
                            u_i = int(df.loc[i]["u"]); v_i = int(df.loc[i]["v"])
                            how = "via u==cur" if via == 'u' else "via v==cur (flip)"
                            print(f"[step/{current_source}] idx={i} seg=({u_i}->{v_i}) {how} cur={cur_node} -> {new_cur} osm_same={same} osm={osm_i}")
                        if via == 'v':
                            allow_v_once = False  # flip consumed
                        return new_cur, osm_i, True
                return cur_node, last_osm, False

            # 1) same OSMID, u==cur
            new_cur, new_osm, ok = pick(lst_u, prefer_same_osm=True, via='u')
            if ok: return new_cur, new_osm, True

            # 2) same OSMID, v==cur (flip)
            new_cur, new_osm, ok = pick(lst_v, prefer_same_osm=True, via='v')
            if ok: return new_cur, new_osm, True

            # 3) other OSMID, u==cur
            new_cur, new_osm, ok = pick(lst_u, prefer_same_osm=False, via='u')
            if ok: return new_cur, new_osm, True

            # 4) other OSMID, v==cur (flip)
            new_cur, new_osm, ok = pick(lst_v, prefer_same_osm=False, via='v')
            if ok: return new_cur, new_osm, True

            # last resort: legacy once-flip only in primary
            if current_source == "primary" and allow_v_once:
                for i in lst_v:
                    if i not in used_set:
                        new_cur = int(df.loc[i]["u"])
                        used_set.add(i)
                        seq.append((current_source, i, "v"))
                        if DEBUG:
                            u_i = int(df.loc[i]["u"]); v_i = int(df.loc[i]["v"])
                            print(f"[step/{current_source}] idx={i} seg=({u_i}->{v_i}) via v==cur (legacy once) cur={cur_node} -> {new_cur}")
                        allow_v_once = False
                        return new_cur, row_osmid_first(df.loc[i]), True

            return cur_node, last_osm, False

        # --- Main chaining loop ---
        guard = 0
        while guard < (len(cand) + len(cand_alt) + 10):
            guard += 1

            new_cur, last_osm_new, ok = try_current(cur, last_osm)
            if ok:
                cur, last_osm = new_cur, last_osm_new
                if cur == v0:
                    break
                continue

            # switch primary <-> alt
            current_source, other_label = other_label, current_source
            cand_cur,  cand_other  = cand_other,  cand_cur
            by_u_cur,  by_u_other  = by_u_other,  by_u_cur
            by_end_cur, by_end_other = by_end_other, by_end_cur

            # When switching back to the original source, allow one legacy flip
            allow_v_once = (current_source == original_source)

            if DEBUG:
                print(f"[switch] now={current_source} allow_v_once={allow_v_once}  cur={cur} last_osm={last_osm}")

            new_cur, last_osm_new, ok = try_current(cur, last_osm)
            if ok:
                cur, last_osm = new_cur, last_osm_new
                if cur == v0:
                    break
                continue

            if DEBUG:
                print(f"[chain] no continuation at cur={cur} (now={current_source})")
            break

        if not seq:
            if DEBUG:
                print("[end] empty sequence")
            return _empty_like(gdf_edges_detailed)

        # --- Reconstruct output rows with harmonized orientation ---
        rows = []
        for src, idx, how in seq:
            # choose source row
            if src == "primary":
                if idx not in cand.index:
                    continue
                row = cand.loc[idx].copy()
            else:  # 'alt'
                if idx not in cand_alt.index:
                    continue
                row = cand_alt.loc[idx].copy()

            # 1) toggle 'reversed' for segments from 'alt'
            if src == "alt" and "reversed" in row:
                r = _maybe_bool(row["reversed"])
                if r is not None:
                    row["reversed"] = (not r)

            # 2) flip geometry + swap u/v if chosen via v==cur (reverse)
            if how == "v":
                u_old, v_old = row["u"], row["v"]
                row["u"], row["v"] = v_old, u_old
                try:
                    row["geometry"] = LineString(list(row.geometry.coords)[::-1])
                except Exception:
                    pass

            rows.append(row)

        if not rows:
            return _empty_like(gdf_edges_detailed)

        out = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        return out.set_crs("EPSG:4326", allow_override=True)

    except Exception as e:
        if DEBUG:
            print("[get_directed_detailed_sequence:osmid-aware] EXCEPTION:", repr(e))
        return _empty_like(gdf_edges_detailed)

def split_once_at_half(edge, seq_det: gpd.GeoDataFrame):
    """
    Split 'edge' at its half simplified length *on a detailed segment boundary*.

    Returns:
      (left_edge_df, right_edge_df, split_meta, left_seq, right_seq)
    or (None, None, None, None, None).
    """
    L = float(edge['length'])
    if L <= 0 or seq_det.empty:
        return None, None, None, None, None

    n = len(seq_det)
    if n < 2:
        return None, None, None, None, None

    seq = seq_det.copy()
    seq['len_det'] = pd.to_numeric(seq['length'], errors='coerce').fillna(0.0).astype(float)

    half = 0.5 * L
    cum_fwd = seq['len_det'].cumsum().to_numpy()
    cum_bwd = seq['len_det'][::-1].cumsum().to_numpy()

    k_fwd = int(np.searchsorted(cum_fwd, half, side='left'))
    k_bwd = int(np.searchsorted(cum_bwd, half, side='left'))

    over_fwd = cum_fwd[k_fwd] - half if k_fwd < n else float('inf')
    over_bwd = cum_bwd[k_bwd] - half if k_bwd < n else float('inf')

    use_front = (over_fwd <= over_bwd)
    cut_idx = (k_fwd if use_front else (n - k_bwd))

    if cut_idx <= 0:
        cut_idx = 1
    elif cut_idx >= n:
        cut_idx = n - 1

    left_seg  = seq.iloc[:cut_idx].copy()
    right_seg = seq.iloc[cut_idx:].copy()
    if left_seg.empty or right_seg.empty:
        return None, None, None, None, None

    # Split node + XY
    if use_front:
        split_nid = str(int(left_seg.iloc[-1]['v']))
        split_xy  = left_seg.iloc[-1].geometry.coords[-1]
    else:
        split_nid = str(int(right_seg.iloc[0]['u']))
        split_xy  = right_seg.iloc[0].geometry.coords[0]
    split_meta = {"nid": split_nid, "xy": (float(split_xy[0]), float(split_xy[1]))}

    # Helper: build a simplified sub-edge from a detailed block
    def make_block(block):
        row = edge.copy()
        row['u'] = str(int(block.iloc[0]['u']))
        row['v'] = str(int(block.iloc[-1]['v']))

        coords = []
        prev_end = None
        for _, s in block.iterrows():
            g = s.geometry
            if prev_end is not None and g.coords[0] != prev_end:
                g = LineString(list(g.coords)[::-1])
            if not coords:
                coords.extend(list(g.coords))
            else:
                coords.extend(list(g.coords)[1:])
            prev_end = g.coords[-1]
        row['geometry'] = LineString(coords)

        # length
        L_sum = pd.to_numeric(block['len_det'], errors='coerce').sum()
        if not np.isfinite(L_sum) or L_sum <= 0:
            try:
                L_sum = float(gpd.GeoSeries([row['geometry']], crs="EPSG:4326").to_crs(3857).length.iloc[0])
            except Exception:
                L_sum = 1.0
        row['length'] = float(max(1.0, L_sum))

        # attributes (conservative)
        row['highway']  = block.iloc[0].get('highway', row.get('highway', 'unknown'))
        if 'maxspeed' in block:
            try:
                row['maxspeed'] = float(pd.to_numeric(block['maxspeed'], errors='coerce').max())
            except Exception:
                row['maxspeed'] = row.get('maxspeed', 50.0)
        else:
            row['maxspeed'] = row.get('maxspeed', 50.0)
        if 'oneway' in block:
            try:
                flags = block['oneway'].astype(str).str.lower().isin(['1', 'true', 'yes'])
                row['oneway'] = bool(flags.any())
            except Exception:
                row['oneway'] = False
        else:
            row['oneway'] = False

        # OSMIDs taken exactly from the block, filtered by original set (if present)
        orig_ids = set(_osmid_list(edge.get('osmid', '')))
        block_ids = _flatten_osmids_from_block(block)
        filtered_ids = [osm for osm in block_ids if (not orig_ids) or (osm in orig_ids)]
        row['osmid'] = "[" + ",".join(str(x) for x in filtered_ids) + "]"

        row['origin'] = 'split'
        return gpd.GeoDataFrame([row]).set_crs("EPSG:4326", allow_override=True)

    left_edge  = make_block(left_seg)
    right_edge = make_block(right_seg)

    # Provide sequence halves as well
    left_seq = left_seg.drop(columns=[c for c in left_seg.columns if c not in seq_det.columns], errors='ignore').copy()
    right_seq = right_seg.drop(columns=[c for c in right_seg.columns if c not in seq_det.columns], errors='ignore').copy()
    left_seq  = left_seq.set_crs("EPSG:4326", allow_override=True)
    right_seq = right_seq.set_crs("EPSG:4326", allow_override=True)

    return left_edge, right_edge, split_meta, left_seq, right_seq

import sys

def short_edges(gdf_edges_simplified: gpd.GeoDataFrame,
                gdf_edges_detailed: gpd.GeoDataFrame,
                max_allowed_length: float):
    """
    Replace overly long simplified edges with directed sequences of detailed segments and
    recursively split until each part is <= max_allowed_length.
    Returns a tuple (edges_out_gdf, split_nodes_xy_dict).
    """

    # Ensure CRS
    if not gdf_edges_simplified.crs:
        gdf_edges_simplified = gdf_edges_simplified.set_crs("EPSG:4326", allow_override=True)
    if not gdf_edges_detailed.crs:
        gdf_edges_detailed = gdf_edges_detailed.set_crs("EPSG:4326", allow_override=True)

    osmid_index = build_osmid_index_for_detailed(gdf_edges_detailed)

    is_long = pd.to_numeric(gdf_edges_simplified['length'], errors='coerce').fillna(0.0) > float(max_allowed_length)
    long_edges = gdf_edges_simplified[is_long]
    keep_edges = gdf_edges_simplified[~is_long].copy()
    keep_edges['origin'] = 'keep'

    result_parts = [keep_edges]
    split_nodes_xy = {}
    to_process = []

    # -------- Progress target estimation --------
    def _needed_segments(L, maxlen):
        try:
            Lf = float(L)
        except Exception:
            return 0
        if not np.isfinite(Lf) or Lf <= 0:
            return 0
        return int(np.ceil(Lf / float(maxlen)))

    needed_total = (
            sum(_needed_segments(e['length'], max_allowed_length) for _, e in long_edges.iterrows()) +
            len(keep_edges)
    )

    # -------- Progress bars --------
    pbar_tasks = tqdm(total=0, desc="Splitting work", position=0,
                      unit="task", mininterval=0.3,
                      dynamic_ncols=True, leave=False, file=sys.stdout)

    pbar_final = tqdm(total=needed_total, desc="Final edges", position=1,
                      unit="edge", mininterval=0.3,
                      dynamic_ncols=True, leave=False, file=sys.stdout)

    # -------- Initial sequences --------
    for _, e in long_edges.iterrows():
        seq = get_directed_detailed_sequence(e, gdf_edges_detailed, osmid_index)
        if seq.empty:
            ee = gpd.GeoDataFrame([e], crs="EPSG:4326"); ee['origin'] = 'keep'
            result_parts.append(ee)
            pbar_final.update(1)
            if pbar_final.n > pbar_final.total:
                pbar_final.total = pbar_final.n
                pbar_final.refresh()
        else:
            to_process.append({'edge_row': e, 'seq_det': seq})
            pbar_tasks.total += 1
            pbar_tasks.refresh()

    # guard against infinite loops
    def _part_key(edge_row, seq_det):
        return (str(edge_row['u']), str(edge_row['v']),
                int(round(float(edge_row['length']))),
                str(edge_row.get('osmid')), int(len(seq_det)))

    seen_parts = set()

    # -------- Main processing loop --------
    while to_process:
        item = to_process.pop(0)
        edge = item['edge_row']
        seq  = item['seq_det']
        L = float(edge['length'])

        if L <= max_allowed_length:
            out_df = gpd.GeoDataFrame([edge], crs="EPSG:4326")
            out_df['origin'] = 'split'
            result_parts.append(out_df)
            pbar_final.update(1)
            pbar_tasks.update(1)
            continue

        left, right, split_meta, left_seq, right_seq = split_once_at_half(edge, seq)
        if left is None or right is None:
            ee = gpd.GeoDataFrame([edge], crs="EPSG:4326"); ee['origin'] = 'keep'
            result_parts.append(ee)
            pbar_final.update(1)
            pbar_tasks.update(1)
            continue

        split_nodes_xy[split_meta['nid']] = split_meta['xy']

        new_tasks = 0

        # --- left ---
        L_edge = left.iloc[0]
        if float(L_edge['length']) > max_allowed_length:
            key = _part_key(L_edge, left_seq)
            if key not in seen_parts:
                seen_parts.add(key)
                to_process.append({'edge_row': L_edge, 'seq_det': left_seq})
                new_tasks += 1
        else:
            df_out = gpd.GeoDataFrame([L_edge], crs="EPSG:4326"); df_out['origin'] = 'split'
            result_parts.append(df_out)
            pbar_final.update(1)

        # --- right ---
        R_edge = right.iloc[0]
        if float(R_edge['length']) > max_allowed_length:
            key = _part_key(R_edge, right_seq)
            if key not in seen_parts:
                seen_parts.add(key)
                to_process.append({'edge_row': R_edge, 'seq_det': right_seq})
                new_tasks += 1
        else:
            df_out = gpd.GeoDataFrame([R_edge], crs="EPSG:4326"); df_out['origin'] = 'split'
            result_parts.append(df_out)
            pbar_final.update(1)

        # finished one task
        pbar_tasks.update(1)

        if new_tasks:
            pbar_tasks.total += new_tasks
            pbar_tasks.refresh()

        if pbar_final.n > pbar_final.total:
            pbar_final.total = pbar_final.n
            pbar_final.refresh()

    pbar_tasks.close()
    pbar_final.close()

    out = pd.concat(result_parts, ignore_index=True)
    if not out.crs:
        out = out.set_crs("EPSG:4326", allow_override=True)

    return out, split_nodes_xy

def write_matsim_network(gdf_nodes, gdf_edges, epsg_code, output_path, nodes_without_z: set = None):
    """
    Write a MATSim network (network_v2 DTD). For non-oneway links, produce two directed links.

    Parameters
    ----------
    gdf_nodes : GeoDataFrame (EPSG:4326)
        Must contain column 'height' for node Z (optional) and 'geometry' points.
    gdf_edges : GeoDataFrame (EPSG:4326)
        Must contain columns 'u', 'v', 'length', 'maxspeed' (km/h), 'capacity', 'lanes',
        'highway', 'oneway', and LineString geometry.
    epsg_code : int
        Target planar CRS for x/y coordinates in the MATSim file.
    output_path : str
        Path to write gzipped MATSim network XML.
    nodes_without_z : set[str], optional
        Node IDs for which 'z' should be omitted even if height is present.
    """
    print("Writing MATSim network...")
    # unify CRS (x/y in target EPSG)
    gdf_nodes = gdf_nodes.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)
    gdf_edges = gdf_edges.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)

    # Node-ID series (robust, ensure single scalar value)
    if "osmid" in gdf_nodes.columns:
        osmid_series = gdf_nodes["osmid"].copy()
        osmid_series = osmid_series.apply(lambda v: (v[0] if isinstance(v, (list, tuple, np.ndarray)) else v))
    else:
        osmid_series = gdf_nodes.index.to_series()
    osmid_series = osmid_series.astype(str)

    # Node lookup: id -> (x, y, z)
    has_height = "height" in gdf_nodes.columns
    node_lookup = {}
    for idx, row in gdf_nodes.iterrows():
        osm_id = osmid_series.loc[idx]
        x = float(row.geometry.x)
        y = float(row.geometry.y)
        z = None
        if has_height:
            try:
                z_val = row["height"]
                if pd.notna(z_val):
                    z = float(z_val)
            except Exception:
                z = None
        node_lookup[osm_id] = (x, y, z)

    # Build links (possibly bidirectional)
    links_data = []

    def parse_maxspeed(val, default=130.0):
        if isinstance(val, (list, tuple, np.ndarray)):
            cand = [_normalize_maxspeed(x) for x in val]
            cand = [c for c in cand if c is not None and math.isfinite(c)]
            return max(cand) if cand else default
        r = _normalize_maxspeed(val)
        return r if (r is not None and math.isfinite(r)) else default

    for _, row in gdf_edges.iterrows():
        u = _clean_text(row.get("u"), None)
        v = _clean_text(row.get("v"), None)
        # Skip if u/v missing
        if (u is None) or (v is None):
            continue

        length_m = _num(row.get("length"), default=None)
        if length_m is None or length_m <= 0:
            # Fallback: Geometrielaenge im (metrischen) Ziel-CRS des Netzes.
            # gdf_edges ist hier bereits nach epsg_code (z.B. 4839) reprojiziert,
            # daher ist geometry.length direkt in Metern. (Frueher: to_crs(3857),
            # was in DE die Laenge um ~50-75 % ueberschaetzte.)
            try:
                length_m = float(row.geometry.length)
            except Exception:
                length_m = 1.0
        length = str(int(round(max(1.0, length_m))))

        maxspeed = parse_maxspeed(row.get("maxspeed", 50.0), default=50.0)
        freespeed = round((maxspeed or 50.0) / 3.6, 2)

        cap = _num(row.get("capacity"), default=3000, as_int=True) or 3000
        permlanes = _num(row.get("lanes"), default=1, as_int=True) or 1

        highway_type = _clean_text(row.get("highway"), "unknown")
        one_way_flag = _truthy_flag(row.get("oneway", None))
        if (not one_way_flag) and highway_type in ("motorway", "motorway_link"):
            one_way_flag = True

        def add_link(u_, v_):
            lid = f"{u_}-{v_}"
            links_data.append({
                "id": lid, "from": u_, "to": v_,
                "length": length,
                "freespeed": float(freespeed),
                "capacity": str(int(cap)),
                "permlanes": str(int(max(1, permlanes))),
                "highway_type": highway_type,
                "oneway_attr": "1" if one_way_flag else "0",
            })

        add_link(u, v)
        if not one_way_flag:
            add_link(v, u)


    # Deduplicate by ID (keep the faster freespeed if duplicates exist)
    unique_links = {}
    for link in links_data:
        lid = link["id"]
        if (lid not in unique_links) or (link["freespeed"] > unique_links[lid]["freespeed"]):
            unique_links[lid] = link

    # XML structure
    network = ET.Element("network")
    network.insert(1, ET.Comment("======================================================================"))
    nodes_element = ET.SubElement(network, "nodes")
    network.append(ET.Comment("======================================================================"))
    links_element = ET.SubElement(
        network, "links",
        capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75"
    )
    network.append(ET.Comment("======================================================================"))

    # Write links & collect used nodes
    used_node_ids = set()
    for link in unique_links.values():
        if link["from"] not in node_lookup or link["to"] not in node_lookup:
            continue
        le = ET.SubElement(
            links_element, "link",
            id=link["id"],
            **{
                "from": link["from"],
                "to": link["to"],
                "length": link["length"],
                "freespeed": str(link["freespeed"]),
                "capacity": link["capacity"],
                "permlanes": link["permlanes"],
                "modes": "car",
            }
        )
        attrs = ET.SubElement(le, "attributes")
        a_speed = ET.SubElement(attrs, "attribute", name="allowed_speed", **{"class": "java.lang.Double"})
        a_speed.text = str(link["freespeed"])
        a_type = ET.SubElement(attrs, "attribute", name="type", **{"class": "java.lang.String"})
        a_type.text = _clean_text(link["highway_type"], "unknown")
        a_oneway = ET.SubElement(attrs, "attribute", name="oneway_source", **{"class": "java.lang.String"})
        a_oneway.text = "1" if link.get("oneway_attr") == "1" else "0"

        used_node_ids.add(link["from"]); used_node_ids.add(link["to"])

    # Write only used nodes; optionally omit z
    nodes_without_z = nodes_without_z or set()
    for osm_id in used_node_ids:
        x, y, z = node_lookup[osm_id]
        node_attrs = {"id": str(osm_id), "x": f"{x}", "y": f"{y}"}
        # only write z if it is a finite number and not in the omit-list
        if (z is not None) and math.isfinite(z) and (str(osm_id) not in nodes_without_z):
            node_attrs["z"] = f"{z}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    xml_string = ET.tostring(network, encoding="utf-8")
    pretty_xml = md.parseString(xml_string).toprettyxml()
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write("\n".join(pretty_xml.splitlines()[1:]))
    print("Done:", output_path)

def sanitize_edges_for_export(gdf_edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Clean edges before export:
    - Length: fix invalid/missing values from geometry (EPSG:3857), min=1 m
    - lanes/capacity/maxspeed/highway/oneway with robust defaults without NaN
    - keep u/v as strings (as required by MATSim)
    """
    df = gdf_edges.copy()

    # ensure CRS
    if not df.crs:
        df = df.set_crs("EPSG:4326", allow_override=True)

    # length
    if 'length' not in df.columns:
        df['length'] = np.nan
    len_num = pd.to_numeric(df['length'], errors='coerce')
    bad_len = len_num.isna() | (len_num <= 0)
    if bad_len.any():
        df3857 = df.to_crs(3857)
        df.loc[bad_len, 'length'] = df3857.loc[bad_len, 'geometry'].length.values
    df['length'] = pd.to_numeric(df['length'], errors='coerce').fillna(1.0).clip(lower=1.0)

    # lanes
    if 'lanes' not in df.columns:
        df['lanes'] = 1
    df['lanes'] = pd.to_numeric(df['lanes'], errors='coerce').fillna(1).clip(lower=1).round().astype(int)

    # capacity
    if 'capacity' not in df.columns:
        df['capacity'] = 3000
    df['capacity'] = pd.to_numeric(df['capacity'], errors='coerce').fillna(3000).clip(lower=100).round().astype(int)

    # maxspeed
    if 'maxspeed' not in df.columns:
        df['maxspeed'] = 50.0
    else:
        def _mx(v):
            if isinstance(v, (list, tuple, np.ndarray, pd.Series)):
                try:
                    vals = pd.to_numeric(
                        pd.Series([_normalize_maxspeed(x) for x in v]), errors='coerce')
                    vals = vals[vals.notna()]
                    return float(vals.max()) if not vals.empty else np.nan
                except Exception:
                    return np.nan
            return _normalize_maxspeed(v)
        df['maxspeed'] = df['maxspeed'].apply(_mx)
        df['maxspeed'] = pd.to_numeric(df['maxspeed'], errors='coerce').fillna(50.0).clip(lower=1.0)

    # highway
    if 'highway' not in df.columns:
        df['highway'] = 'unknown'
    df['highway'] = df['highway'].astype(str)
    df.loc[df['highway'].str.strip().isin(['', 'nan', 'None']), 'highway'] = 'unknown'

    # oneway -> bool
    if 'oneway' not in df.columns:
        df['oneway'] = False
    df['oneway'] = df['oneway'].apply(lambda x: str(x).strip().lower() in ('1', 'true', 'yes'))

    # u/v as strings
    df['u'] = df['u'].astype(str)
    df['v'] = df['v'].astype(str)

    return df

def generate_network(
        area: str,
        max_allowed_link_length: float,
        target_epsg: int = 4839,
        version: str = "V0",
):
    # --- Pfade & Namen ---
    # Hoehen kommen jetzt direkt aus dem LiDAR-DTM (siehe load_dtm/sample_heights),
    # nicht mehr aus einer npz/KD-Tree-Punktwolke.
    local_osm_input_path_simplified = f"data/germany_simplified_DF.gpkg" #f"data/{area}_simplified.gpkg"
    local_osm_input_path_detailed   = f"data/germany_detailed_sorted_DF.gpkg" #f"data/{area}_detailed_sorted.gpkg"
    output_path = f"data/{area}_max{int(max_allowed_link_length)}m_{version}.xml.gz"

    # --- Daten laden ---
    dtm = load_dtm()
    gdf_nodes_simplified, gdf_edges_simplified = load_local_osm_file(local_osm_input_path_simplified)
    gdf_nodes_detailed,   gdf_edges_detailed   = load_local_osm_file(local_osm_input_path_detailed)

    # --- Kanten kürzen ---
    print(f"\nShortening edges for area={area} with max_allowed_link_length={max_allowed_link_length} m ...")
    gdf_edges_shortened, split_nodes_xy = short_edges(
        gdf_edges_simplified=gdf_edges_simplified,
        gdf_edges_detailed=gdf_edges_detailed,
        max_allowed_length=max_allowed_link_length
    )

    # --- genutzte Nodes bestimmen ---
    used_nodes = set(map(str, gdf_edges_shortened['u'])) | set(map(str, gdf_edges_shortened['v']))

    from shapely.geometry import Point
    def _first_scalar(v):
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) > 0:
            return v[0]
        return v

    gdf_nodes_detailed = gdf_nodes_detailed.copy()
    gdf_nodes_detailed["osmid_norm"] = pd.to_numeric(
        gdf_nodes_detailed["osmid"].apply(_first_scalar), errors='coerce'
    )
    gdf_nodes_detailed = gdf_nodes_detailed.dropna(subset=["osmid_norm"]).copy()
    gdf_nodes_detailed["osmid_norm"] = gdf_nodes_detailed["osmid_norm"].astype(int)

    osm_xy = {
        str(int(r["osmid_norm"])): (float(r.geometry.x), float(r.geometry.y))
        for _, r in gdf_nodes_detailed.iterrows()
    }

    def _xy_from_edge(nid: str):
        rows = gdf_edges_shortened[(gdf_edges_shortened['u'].astype(str) == nid) |
                                   (gdf_edges_shortened['v'].astype(str) == nid)]
        if rows.empty:
            return None
        r = rows.iloc[0]
        line: LineString = r.geometry
        if str(r['u']) == nid:
            return (float(line.coords[0][0]), float(line.coords[0][1]))
        else:
            return (float(line.coords[-1][0]), float(line.coords[-1][1]))

    node_rows, seen = [], set()
    for nid in used_nodes:
        if nid in seen:
            continue
        if nid in osm_xy:
            x, y = osm_xy[nid]
        elif nid in split_nodes_xy:
            x, y = split_nodes_xy[nid]
        else:
            fb = _xy_from_edge(nid)
            if fb is None:
                continue
            x, y = fb
        node_rows.append({'osmid': nid, 'geometry': Point(x, y)})
        seen.add(nid)

    gdf_nodes_export = gpd.GeoDataFrame(node_rows, crs="EPSG:4326")
    xs = gdf_nodes_export.geometry.x.to_numpy()
    ys = gdf_nodes_export.geometry.y.to_numpy()
    gdf_nodes_export['height'] = sample_heights(dtm, xs, ys)

    # --- Export ---
    write_matsim_network(
        gdf_nodes=gdf_nodes_export,
        gdf_edges=gdf_edges_shortened,
        epsg_code=target_epsg,
        output_path=output_path,
        nodes_without_z=set()
    )
    return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build MATSim networks with different max link lengths.")
    parser.add_argument("--area", type=str, required=True, help="z.B. 'Germany'")
    parser.add_argument("--max-length", type=float, required=True, help="Max. Linklänge in Metern")
    parser.add_argument("--version", type=str, default="V0")
    parser.add_argument("--epsg", type=int, default=4839)
    args = parser.parse_args()

    generate_network(
        area=args.area,
        max_allowed_link_length=args.max_length,
        target_epsg=args.epsg,
        version=args.version,
    )

