#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evaluate *slope* reconstruction quality of a MATSim network
against a high-resolution elevation reference (e.g., 3D LINESTRING Z or
point cloud stored in a GeoPackage).

This variant outputs **only slope-related metrics** (no elevation errors).

Definitions
----------
- Reference profile z*(s):
    * nearest_points / per_link_line: per NN gegen Referenz
    * by_uv: direkt aus der echten 3D-Liniengeometrie (u,v-Match)
- Reconstructed profile ẑ_L(s): linear zwischen den MATSim-Knotenhöhen.
- Discretisation: konstantes Δs (default 5 m).
- Slope via central differences:
    g*(s_i)   = [ z*(s_{i+1}) - z*(s_{i-1}) ] / (2 Δs)
    ĝ_L(s_i)  = [ ẑ_L(s_{i+1}) - ẑ_L(s_{i-1}) ] / (2 Δs)

Outputs
-------
- CSV mit per-link **slope** Metriken: MAE/RMSE/Bias (in m/m).
- Optional CSV/Excel mit per-sample **slope** Werten (Debug).

Usage
-----
python evaluate_slope_only.py \
  --network /path/to/network.xml[.gz] \
  --gpkg /path/to/reference.gpkg \
  --layer roads_3d \
  --ds 5 \
  --mode by_uv \
  --limit_links 0 \
  --out /path/to/output.csv

Parameters
----------
--network        MATSim network (.xml oder .xml.gz). Nodes brauchen x,y und
                 eine Höhen-Attribut {z, elevation, height, h}.
--gpkg           GeoPackage mit:
                 - LINESTRING Z (3D-Zentrierlinien) ODER
                 - POINT mit Höhenattribut
--layer          Layername im GPKG (z. B. roads_3d).
--ds             Schrittweite Δs in m (default 5).
--mode           Referenzmodus:
                    - by_uv: (from,to) ↔ (u,v) → echte 3D-Linie
                    - per_link_line: nächste Referenzlinie (Centroid-NN) → densify & NN
                    - nearest_points: globale Punktwolke → NN
--limit_links    Wenn >0: nur erste N Links (Test).
--bbox_pad       Puffer (m) beim Aufbau der Referenz-Punktwolke (nur nearest_points).
--save_samples   Pfad zu CSV mit per-Sample Werten (optional; groß).
--out            Output CSV Pfad.

Dependencies
------------
Python 3.9+
    numpy, pandas, lxml, geopandas, shapely, scipy
Optional: tqdm

Notes
-----
- CRS: MATSim- und GPKG-Koordinaten in Metern und **gleichem CRS**.
"""

from __future__ import annotations
import argparse
import gzip
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from lxml import etree
import geopandas as gpd
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union
from scipy.spatial import cKDTree

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(x, **kwargs):
        return x


# ---------------------------- I/O: MATSim network ----------------------------

def parse_matsim_nodes_links(xml_path: Path) -> Tuple[Dict[str, dict], List[dict]]:
    """Parse MATSim network XML(.gz) -> nodes, links.
    Nodes must have attributes x,y and optionally elevation in {z,elevation,height,h}.
    """
    if str(xml_path).endswith(".gz"):
        with gzip.open(xml_path, "rb") as f:
            tree = etree.parse(f)
    else:
        tree = etree.parse(str(xml_path))

    root = tree.getroot()
    ns = {}
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}")[0].strip("{")
        ns["m"] = ns_uri

    nodes_xpath = ".//m:nodes/m:node" if ns else ".//nodes/node"
    links_xpath = ".//m:links/m:link" if ns else ".//links/link"

    nodes: Dict[str, dict] = {}
    for nd in root.xpath(nodes_xpath, namespaces=ns):
        nid = nd.get("id")
        x = float(nd.get("x"))
        y = float(nd.get("y"))
        z_attr = None
        for cand in ("z", "elevation", "height", "h"):
            if nd.get(cand) is not None:
                try:
                    z_attr = float(nd.get(cand))
                except Exception:
                    z_attr = None
                break
        nodes[nid] = {"x": x, "y": y, "z": z_attr}

    links: List[dict] = []
    for lk in root.xpath(links_xpath, namespaces=ns):
        links.append({
            "id": lk.get("id"),
            "from": lk.get("from"),
            "to": lk.get("to"),
            "length": float(lk.get("length")) if lk.get("length") else None,
        })

    return nodes, links


# ------------------------- Geometry helpers / sampling ------------------------

def sample_straight(x1: float, y1: float, x2: float, y2: float, ds: float) -> List[Tuple[float, float, float]]:
    """Sample straight line (x1,y1)-(x2,y2) every ds meters.
    Returns list of tuples (s, x, y), with s in [0, L].
    """
    line = LineString([(x1, y1), (x2, y2)])
    L = line.length
    if L == 0:
        return [(0.0, x1, y1)]
    n = int(math.floor(L / ds))
    s_vals = [i * ds for i in range(n + 1)]
    if s_vals[-1] < L:
        s_vals.append(L)
    out = []
    for s in s_vals:
        p = line.interpolate(s)
        out.append((s, p.x, p.y))
    return out


def central_diff(z_values: Sequence[float], ds: float) -> List[float]:
    """Central differences (endpoints via forward/backward)."""
    n = len(z_values)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    g = [math.nan] * n
    for i in range(n):
        zi = z_values[i]
        if math.isnan(zi):
            g[i] = math.nan
            continue
        if i == 0:
            z0, z1 = z_values[0], z_values[1]
            g[i] = (z1 - z0) / ds if not (math.isnan(z1) or math.isnan(z0)) else math.nan
        elif i == n - 1:
            z0, z1 = z_values[-2], z_values[-1]
            g[i] = (z1 - z0) / ds if not (math.isnan(z1) or math.isnan(z0)) else math.nan
        else:
            zm1, zp1 = z_values[i - 1], z_values[i + 1]
            g[i] = (zp1 - zm1) / (2.0 * ds) if not (math.isnan(zm1) or math.isnan(zp1)) else math.nan
    return g


# ---------------------- Reference: LINESTRING Z densify -----------------------

def densify_linestring_z(line: LineString, ds: float) -> np.ndarray:
    """Return Nx3 array of (x,y,z) sampled along LINESTRING Z at step ds."""
    coords = np.asarray(line.coords)  # (N, 3) expected
    xy = coords[:, :2]
    has_z = coords.shape[1] >= 3
    z = coords[:, 2] if has_z else None
    seg = xy[1:] - xy[:-1]
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    L = cum[-1]
    if L == 0:
        x, y = xy[0]
        z0 = float(z[0]) if has_z else math.nan
        return np.array([[x, y, z0]], dtype=float)
    s_vals = np.arange(0, L + ds, ds)
    if s_vals[-1] > L + 1e-6:
        s_vals = s_vals[:-1]
    idxs = np.searchsorted(cum, s_vals, side="right") - 1
    idxs = np.clip(idxs, 0, len(seg_len) - 1)
    s0 = cum[idxs]
    l = seg_len[idxs]
    with np.errstate(divide='ignore', invalid='ignore'):
        t = (s_vals - s0) / l
        t[~np.isfinite(t)] = 0.0
    p0 = xy[idxs]
    p1 = xy[idxs + 1]
    xy_s = p0 + (p1 - p0) * t[:, None]
    if has_z:
        z0 = z[idxs]
        z1 = z[idxs + 1]
        z_s = z0 + (z1 - z0) * t
    else:
        z_s = np.full_like(s_vals, np.nan, dtype=float)
    return np.column_stack([xy_s, z_s.astype(float)])


def sample_along_real_line_with_z(line: LineString, ds: float):
    """Return (s, x, y, z) sampled along real 3D line with step ds."""
    arr = densify_linestring_z(line, ds)  # -> Nx3: x,y,z
    xy = arr[:, :2]; z = arr[:, 2]
    if len(xy) <= 1:
        return np.array([0.0], dtype=float), xy[:, 0], xy[:, 1], z.astype(float)
    seg = xy[1:] - xy[:-1]
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    s = np.concatenate([[0.0], np.cumsum(seg_len)])
    return s, xy[:, 0], xy[:, 1], z.astype(float)


def reconstruct_linear_between_nodes_over_curve(s: np.ndarray, z_from, z_to) -> np.ndarray:
    """Linear interpolation between node elevations over curve-length axis s."""
    if z_from is None or z_to is None:
        return np.full_like(s, np.nan, dtype=float)
    Ls = float(s[-1]) if s.size > 0 else 0.0
    if Ls <= 1e-9:
        return np.full_like(s, float(z_from), dtype=float)
    return float(z_from) + (float(z_to) - float(z_from)) * (s / Ls)


# ---------------------- Reference builders (GPKG input) ----------------------

@dataclass
class ReferencePoints:
    xy: np.ndarray  # (N, 2)
    z: np.ndarray   # (N,)
    tree: cKDTree


def find_height_col(columns: List[str]) -> Optional[str]:
    lowers = [c.lower() for c in columns]
    for cand in ("z", "elev", "elevation", "height", "h", "z_m", "zval"):
        if cand in lowers:
            return columns[lowers.index(cand)]
    for c in columns:
        cl = c.lower()
        if cl.startswith("band") or cl.startswith("elev"):
            return c
    return None


def build_reference_points_from_gpkg(
        gpkg: Path,
        layer: str,
        ds: float,
        bbox: Optional[Tuple[float, float, float, float]] = None,
) -> ReferencePoints:
    """Create a NN-ready cloud of reference points from a GPKG layer."""
    gdf = gpd.read_file(gpkg, layer=layer)
    if bbox is not None:
        gdf = gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]

    height_col = find_height_col(list(gdf.columns))

    # POINT layer with height column
    if (gdf.geom_type == "Point").any() and (height_col is not None):
        pts = gdf[gdf.geometry.type == "Point"].copy()
        xy = np.column_stack([pts.geometry.x.values, pts.geometry.y.values]).astype(float)
        z = pts[height_col].astype(float).values
        tree = cKDTree(xy)
        return ReferencePoints(xy=xy, z=z, tree=tree)

    # LINESTRING Z -> densify
    pts_xy: List[np.ndarray] = []
    pts_z: List[np.ndarray] = []
    for geom in gdf.geometry:
        if isinstance(geom, LineString) and getattr(geom, "has_z", False):
            arr = densify_linestring_z(geom, ds)
            pts_xy.append(arr[:, :2])
            pts_z.append(arr[:, 2])
    if not pts_xy:
        raise RuntimeError("No suitable reference geometries found (POINT with height or LINESTRING Z)")
    xy = np.vstack(pts_xy)
    z = np.concatenate(pts_z)
    tree = cKDTree(xy)
    return ReferencePoints(xy=xy, z=z, tree=tree)


# --------- Per-link line matching (each link -> nearest reference line) -------

@dataclass
class LineCacheEntry:
    xy: np.ndarray  # (M,2)
    z: np.ndarray   # (M,)
    tree: cKDTree


def build_line_centroid_tree(gdf_lines: gpd.GeoDataFrame) -> Tuple[np.ndarray, cKDTree]:
    centroids = np.array([(geom.centroid.x, geom.centroid.y) for geom in gdf_lines.geometry])
    return centroids, cKDTree(centroids)


def get_line_cache_entry(gdf_lines: gpd.GeoDataFrame, idx: int, ds: float, cache: Dict[int, LineCacheEntry]) -> LineCacheEntry:
    if idx in cache:
        return cache[idx]
    geom = gdf_lines.geometry.iloc[int(idx)]
    if not (isinstance(geom, LineString) and getattr(geom, "has_z", False)):
        raise RuntimeError("per_link_line mode requires LINESTRING Z references.")
    arr = densify_linestring_z(geom, ds)
    xy = arr[:, :2]
    z = arr[:, 2]
    tree = cKDTree(xy)
    entry = LineCacheEntry(xy=xy, z=z, tree=tree)
    cache[idx] = entry
    return entry


# --------------------------- by_uv: match (u,v) -------------------------------

def _to_str_id(x) -> str:
    """Robust cast of MATSim/OSM ids (floats like 6.3e+09 -> '6307174000')."""
    try:
        xi = int(round(float(x)))
        return str(xi)
    except Exception:
        return str(x)


def build_uv_lines(gdf_lines: gpd.GeoDataFrame) -> Dict[Tuple[str, str], LineString]:
    """
    Group roads_3d by (u,v), merge segments, store forward and reverse keys.
    Requires LINESTRING Z and columns 'u','v'.
    """
    if "u" not in gdf_lines.columns or "v" not in gdf_lines.columns:
        raise RuntimeError("by_uv mode requires 'u' and 'v' columns in the GPKG layer.")
    df = gdf_lines[gdf_lines.geometry.type == "LineString"].copy()
    df["u_str"] = df["u"].map(_to_str_id)
    df["v_str"] = df["v"].map(_to_str_id)
    uv2geom: Dict[Tuple[str, str], LineString] = {}
    for (u_str, v_str), grp in df.groupby(["u_str", "v_str"]):
        geoms = list(grp.geometry)
        if len(geoms) == 1:
            merged = geoms[0]
        else:
            merged = linemerge(unary_union(geoms))
            if merged.geom_type == "MultiLineString":
                merged = max(list(merged.geoms), key=lambda g: g.length)
        if not isinstance(merged, LineString) or not getattr(merged, "has_z", False):
            continue
        uv2geom[(u_str, v_str)] = merged
        # reverse direction as separate key (coords reversed so s runs from u->v or v->u accordingly)
        uv2geom[(v_str, u_str)] = LineString(list(merged.coords)[::-1])
    if not uv2geom:
        raise RuntimeError("by_uv: no usable (u,v) LINESTRING Z geometries found.")
    return uv2geom


# --------------------------------- Metrics -----------------------------------

def metrics_pair(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> Tuple[int, float, float, float]:
    if mask.sum() == 0:
        return 0, math.nan, math.nan, math.nan
    diff = a[mask] - b[mask]
    n = int(mask.sum())
    mae = float(np.mean(np.abs(diff)))
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    bias = float(np.mean(diff))
    return n, mae, rmse, bias


# --------------------------------- Runner ------------------------------------

def run(
        network: Path,
        gpkg: Path,
        layer: str,
        ds: float,
        mode: str,
        limit_links: int,
        bbox_pad: float,
        out_csv: Path,
        save_samples: Optional[Path] = None,
) -> None:
    nodes, links = parse_matsim_nodes_links(network)
    if limit_links and limit_links > 0:
        links = links[:limit_links]

    # Gather overall bbox to optionally trim the reference (nearest_points only)
    xs: List[float] = []
    ys: List[float] = []
    for lk in links:
        a = nodes.get(lk["from"])
        b = nodes.get(lk["to"])
        if a is None or b is None:
            continue
        xs.extend([a["x"], b["x"]])
        ys.extend([a["y"], b["y"]])
    if not xs:
        raise RuntimeError("No valid links found.")
    minx, maxx = min(xs) - bbox_pad, max(xs) + bbox_pad
    miny, maxy = min(ys) - bbox_pad, max(ys) + bbox_pad

    # Build reference structures
    if mode == "nearest_points":
        ref = build_reference_points_from_gpkg(gpkg, layer, ds=ds, bbox=(minx, miny, maxx, maxy))
        gdf_lines = None
        cent_tree = None
        line_cache = None
        uv2geom = None
    elif mode == "per_link_line":
        gdf_lines = gpd.read_file(gpkg, layer=layer)
        gdf_lines = gdf_lines[gdf_lines.geometry.type == "LineString"].reset_index(drop=True)
        centroids, cent_tree = build_line_centroid_tree(gdf_lines)
        line_cache: Dict[int, LineCacheEntry] = {}
        ref = None
        uv2geom = None
    elif mode == "by_uv":
        gdf_lines = gpd.read_file(gpkg, layer=layer)
        gdf_lines = gdf_lines[gdf_lines.geometry.type == "LineString"].reset_index(drop=True)
        uv2geom = build_uv_lines(gdf_lines)
        ref = None
        cent_tree = None
        line_cache = None
    else:
        raise ValueError("mode must be one of {'nearest_points','per_link_line','by_uv'}")

    rows = []
    sample_rows = [] if save_samples else None
    skipped_by_uv = 0

    for lk in tqdm(links, desc="Links"):
        a = nodes.get(lk["from"])
        b = nodes.get(lk["to"])
        if a is None or b is None:
            continue

        if mode == "by_uv":
            from_id = _to_str_id(lk["from"])
            to_id   = _to_str_id(lk["to"])
            line = uv2geom.get((from_id, to_id))
            if line is None:
                skipped_by_uv += 1
                continue  # kein Match -> Link überspringen (alternativ: Fallback einbauen)
            s, xs_, ys_, z_star = sample_along_real_line_with_z(line, ds)
        else:
            # Sample along straight line between MATSim nodes
            samples = sample_straight(a["x"], a["y"], b["x"], b["y"], ds)
            s = np.array([t[0] for t in samples], dtype=float)
            xs_ = np.array([t[1] for t in samples], dtype=float)
            ys_ = np.array([t[2] for t in samples], dtype=float)

            # Reference z*(s)
            if mode == "nearest_points":
                _, nn_idx = ref.tree.query(np.column_stack([xs_, ys_]), k=1)
                z_star = ref.z[nn_idx]
            else:  # per_link_line
                mx, my = (a["x"] + b["x"]) / 2.0, (a["y"] + b["y"]) / 2.0
                _, li = cent_tree.query([mx, my], k=1)
                entry = get_line_cache_entry(gdf_lines, int(li), ds, line_cache)
                _, nn_idx = entry.tree.query(np.column_stack([xs_, ys_]), k=1)
                z_star = entry.z[nn_idx]
            z_star = np.asarray(z_star, dtype=float)

        # Reconstructed ẑ_L(s) — linear between node elevations over curve length
        z_from, z_to = a.get("z"), b.get("z")
        z_L = reconstruct_linear_between_nodes_over_curve(s, z_from, z_to)

        # Slopes
        g_star = np.asarray(central_diff(z_star, ds), dtype=float)
        g_L    = np.asarray(central_diff(z_L,    ds), dtype=float)

        # Metrics (slope only)
        mask_g = ~np.isnan(g_star) & ~np.isnan(g_L)
        n_g, mae_g, rmse_g, bias_g = metrics_pair(g_star, g_L, mask_g)

        rows.append({
            "link_id": lk["id"],
            "from": lk["from"],
            "to": lk["to"],
            "n_pts": int(s.size),
            "n_g": n_g,
            "g_mae_mperm": mae_g,
            "g_rmse_mperm": rmse_g,
            "g_bias_mperm": bias_g,
            "length_reported": lk.get("length"),
            "mode": mode,
        })

        if sample_rows is not None:
            for i in range(s.size):
                sample_rows.append({
                    "link_id": lk["id"],
                    "s": float(s[i]),
                    "x": float(xs_[i]),
                    "y": float(ys_[i]),
                    "g_star": float(g_star[i]) if not math.isnan(g_star[i]) else math.nan,
                    "g_L": float(g_L[i]) if not math.isnan(g_L[i]) else math.nan,
                })

    df = pd.DataFrame(rows)

    # --- Summaries (slope only) ---
    summary = {
        "links_evaluated": int(len(df)),
        "samples_total": int(df["n_pts"].fillna(0).sum()),
        "g_mae_mperm_mean": float(df["g_mae_mperm"].mean(skipna=True)) if not df.empty else math.nan,
        "g_rmse_mperm_mean": float(df["g_rmse_mperm"].mean(skipna=True)) if not df.empty else math.nan,
        "g_bias_mperm_mean": float(df["g_bias_mperm"].mean(skipna=True)) if not df.empty else math.nan,
        "by_uv_skipped": int(skipped_by_uv) if mode == "by_uv" else 0,
    }

    def wavg(col, wcol):
        c = df[col].astype(float)
        w = df[wcol].fillna(0).astype(float)
        sw = w.sum()
        if sw <= 0:
            return math.nan
        return float(np.average(c, weights=w))

    summary_w = {
        "g_mae_mperm_weighted": wavg("g_mae_mperm", "n_g") if not df.empty else math.nan,
        "g_rmse_mperm_weighted": wavg("g_rmse_mperm", "n_g") if not df.empty else math.nan,
        "g_bias_mperm_weighted": wavg("g_bias_mperm", "n_g") if not df.empty else math.nan,
    }

    # Write per-link CSV
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)

    # Try Excel with summaries
    xlsx_path = out_csv.with_suffix(".xlsx")
    wrote_xlsx = False
    try:
        with pd.ExcelWriter(xlsx_path) as writer:
            df.to_excel(writer, sheet_name="per_link_slope_metrics", index=False)
            pd.DataFrame([summary]).to_excel(writer, sheet_name="summary_mean", index=False)
            pd.DataFrame([summary_w]).to_excel(writer, sheet_name="summary_weighted", index=False)
            if sample_rows is not None:
                pd.DataFrame(sample_rows).to_excel(writer, sheet_name="samples", index=False)
        wrote_xlsx = True
    except Exception as e:
        print("Hinweis: Konnte Excel nicht schreiben (benötigt openpyxl/xlsxwriter).", e)

    # Optional: Samples additionally as CSV if Excel failed
    if sample_rows is not None and not wrote_xlsx and save_samples is not None:
        pd.DataFrame(sample_rows).to_csv(save_samples, index=False)

    # Console output
    print(f"Saved per-link slope metrics (CSV) to: {out_csv}")
    if wrote_xlsx:
        print(f"Saved Excel with slope summaries to: {xlsx_path}")
    else:
        print("Excel-Export übersprungen – siehe Hinweis oben. Du kannst 'pip install openpyxl' ausführen.")

    # Print summaries
    print("\n=== Gesamtdurchschnitte (ungewichtet, nur Steigung) ===")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print("\n=== Gesamtdurchschnitte (gewichtet, nur Steigung) ===")
    for k, v in summary_w.items():
        print(f"{k}: {v}")

    if sample_rows is not None and wrote_xlsx:
        print("Per-sample Steigungswerte wurden in der Excel (Tab 'samples') abgelegt.")
    elif sample_rows is not None and not wrote_xlsx and save_samples is not None:
        print(f"Saved per-sample slope values (CSV) to: {save_samples}")


# ---------------------------------- Batch & Plot ---------------------------------

def evaluate_network_return_summary(
        network: Path,
        gpkg: Path,
        layer: str,
        ds: float,
        mode: str,
        limit_links: int,
        bbox_pad: float,
) -> dict:
    """Runs the slope-only evaluation and returns compact summary (no files)."""
    nodes, links = parse_matsim_nodes_links(network)
    if limit_links and limit_links > 0:
        links = links[:limit_links]

    xs: List[float] = []
    ys: List[float] = []
    for lk in links:
        a = nodes.get(lk["from"]); b = nodes.get(lk["to"])
        if a is None or b is None:
            continue
        xs.extend([a["x"], b["x"]]); ys.extend([a["y"], b["y"]])
    if not xs:
        raise RuntimeError("No valid links found.")
    minx, maxx = min(xs) - bbox_pad, max(xs) + bbox_pad
    miny, maxy = min(ys) - bbox_pad, max(ys) + bbox_pad

    if mode == "nearest_points":
        ref = build_reference_points_from_gpkg(gpkg, layer, ds=ds, bbox=(minx, miny, maxy, maxy))
        gdf_lines = None; cent_tree = None; line_cache = None; uv2geom = None
    elif mode == "per_link_line":
        gdf_lines = gpd.read_file(gpkg, layer=layer)
        gdf_lines = gdf_lines[gdf_lines.geometry.type == "LineString"].reset_index(drop=True)
        centroids, cent_tree = build_line_centroid_tree(gdf_lines)
        line_cache: Dict[int, LineCacheEntry] = {}
        ref = None; uv2geom = None
    elif mode == "by_uv":
        gdf_lines = gpd.read_file(gpkg, layer=layer)
        gdf_lines = gdf_lines[gdf_lines.geometry.type == "LineString"].reset_index(drop=True)
        uv2geom = build_uv_lines(gdf_lines)
        ref = None; cent_tree = None; line_cache = None
    else:
        raise ValueError("mode must be one of {'nearest_points','per_link_line','by_uv'}")

    rows = []
    max_link_len_m = 0.0
    skipped_by_uv = 0

    for lk in links:
        a = nodes.get(lk["from"]); b = nodes.get(lk["to"])
        if a is None or b is None:
            continue

        # geometric length for summary
        geom_line = LineString([(a["x"], a["y"]), (b["x"], b["y"])])
        Lgeom = float(geom_line.length)
        Lreported = float(lk.get("length")) if lk.get("length") is not None else Lgeom
        Lmax_this = max(Lgeom, Lreported)
        if Lmax_this > max_link_len_m:
            max_link_len_m = Lmax_this

        if mode == "by_uv":
            from_id = _to_str_id(lk["from"]); to_id = _to_str_id(lk["to"])
            line = uv2geom.get((from_id, to_id))
            if line is None:
                skipped_by_uv += 1
                continue
            s, xs_, ys_, z_star = sample_along_real_line_with_z(line, ds)
        else:
            samples = sample_straight(a["x"], a["y"], b["x"], b["y"], ds)
            s = np.array([t[0] for t in samples], dtype=float)
            xs_ = np.array([t[1] for t in samples], dtype=float)
            ys_ = np.array([t[2] for t in samples], dtype=float)
            if mode == "nearest_points":
                _, nn_idx = ref.tree.query(np.column_stack([xs_, ys_]), k=1)
                z_star = ref.z[nn_idx]
            else:
                mx, my = (a["x"] + b["x"]) / 2.0, (a["y"] + b["y"]) / 2.0
                _, li = cent_tree.query([mx, my], k=1)
                entry = get_line_cache_entry(gdf_lines, int(li), ds, line_cache)
                _, nn_idx = entry.tree.query(np.column_stack([xs_, ys_]), k=1)
                z_star = entry.z[nn_idx]
            z_star = np.asarray(z_star, dtype=float)

        z_L = reconstruct_linear_between_nodes_over_curve(s, a.get("z"), b.get("z"))
        g_star = np.asarray(central_diff(np.asarray(z_star, dtype=float), ds), dtype=float)
        g_L    = np.asarray(central_diff(z_L, ds), dtype=float)
        mask_g = ~np.isnan(g_star) & ~np.isnan(g_L)
        n_g, mae_g, rmse_g, bias_g = metrics_pair(g_star, g_L, mask_g)

        rows.append({
            "n_pts": int(s.size),
            "n_g": n_g,
            "g_mae_mperm": mae_g,
            "g_rmse_mperm": rmse_g,
            "g_bias_mperm": bias_g,
        })

    df = pd.DataFrame(rows)
    g_mae_mean  = float(df["g_mae_mperm"].mean(skipna=True)) if not df.empty else math.nan
    g_rmse_mean = float(df["g_rmse_mperm"].mean(skipna=True)) if not df.empty else math.nan
    g_bias_mean = float(df["g_bias_mperm"].mean(skipna=True)) if not df.empty else math.nan

    def wavg(col, wcol):
        c = df[col].astype(float)
        w = df[wcol].fillna(0).astype(float)
        sw = w.sum()
        if sw <= 0:
            return math.nan
        return float(np.average(c, weights=w))

    summary = {
        "network": str(network),
        "links_evaluated": int(len(df)),
        "samples_total": int(df["n_pts"].fillna(0).sum()),
        "max_link_len_m": float(max_link_len_m),
        "g_mae_mperm_weighted": wavg("g_mae_mperm", "n_g") if not df.empty else math.nan,
        "g_rmse_mperm_weighted": wavg("g_rmse_mperm", "n_g") if not df.empty else math.nan,
        "g_bias_mperm_weighted": wavg("g_bias_mperm", "n_g") if not df.empty else math.nan,
        "g_mae_mperm_mean": g_mae_mean,
        "g_rmse_mperm_mean": g_rmse_mean,
        "g_bias_mperm_mean": g_bias_mean,
        "by_uv_skipped": int(skipped_by_uv) if mode == "by_uv" else 0,
    }
    return summary


def batch_evaluate_and_plot(
        network_glob: str,
        gpkg: Path,
        layer: str,
        ds: float,
        mode: str,
        limit_links: int,
        bbox_pad: float,
        out_csv: Path,
        out_plot: Optional[Path] = None,
) -> Tuple[pd.DataFrame, Optional[Path]]:
    """Evaluate all networks matching a glob and create a scatter plot."""
    import glob
    import matplotlib.pyplot as plt

    files = sorted(glob.glob(network_glob))
    if not files:
        raise FileNotFoundError(f"No networks matched glob: {network_glob}")

    summaries: List[dict] = []
    for f in tqdm(files, desc="Networks"):
        try:
            s = evaluate_network_return_summary(
                network=Path(f), gpkg=gpkg, layer=layer, ds=ds,
                mode=mode, limit_links=limit_links, bbox_pad=bbox_pad,
            )
            summaries.append(s)
        except Exception as e:
            print(f"WARN: Failed to evaluate {f}: {e}")

    df = pd.DataFrame(summaries)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)

    plot_path = None
    if out_plot is not None:
        out_plot.parent.mkdir(parents=True, exist_ok=True)
        plt.figure()
        plt.scatter(df["max_link_len_m"], df["g_rmse_mperm_weighted"], marker="o", label="RMSE weighted")
        plt.scatter(df["max_link_len_m"], df["g_rmse_mperm_mean"], marker="x", label="RMSE unweighted")
        if "g_mae_mperm_weighted" in df.columns:
            plt.scatter(df["max_link_len_m"], df["g_mae_mperm_weighted"], marker="^", label="MAE weighted")
        if "g_mae_mperm_mean" in df.columns:
            plt.scatter(df["max_link_len_m"], df["g_mae_mperm_mean"], marker="+", label="MAE unweighted")
        plt.xlabel("Max link length in network [m]")
        plt.ylabel("g_RMSE [m/m]")
        plt.title("Slope error vs. Max Link Length (RMSE & MAE; weighted & unweighted)")
        plt.legend()
        plt.grid(True, which="both", linestyle=":", linewidth=0.5)
        plt.tight_layout()
        plt.savefig(out_plot, dpi=180)
        plot_path = out_plot
        plt.close()

    return df, plot_path


# -------------------------- Programmatic entry points -------------------------

def run_programmatically(
        single_network: Optional[Path] = Path("data/Saarland_max1000m_V0.xml.gz"),
        batch_glob: Optional[str] = None,
        gpkg: Path = Path("data/Saarland_3d_raster_clamped.gpkg.gpkg"),
        layer: str = "roads_3d",
        ds: float = 5.0,
        mode: str = "by_uv",
        limit_links: int = 0,
        bbox_pad: float = 0.0,
        out_csv_single: Path = Path("data/benchmark_Saarland_slope_only.csv"),
        out_csv_batch: Path = Path("data/batch_slope_vs_maxlen.csv"),
        out_plot: Optional[Path] = Path("data/slope_vs_maxlink.png"),
        save_samples: Optional[Path] = None,
):
    """Programmatic runner without using CLI."""
    results = {}

    if single_network is not None:
        run(
            network=single_network,
            gpkg=gpkg,
            layer=layer,
            ds=ds,
            mode=mode,
            limit_links=limit_links,
            bbox_pad=bbox_pad,
            out_csv=out_csv_single,
            save_samples=save_samples,
        )
        results['single'] = out_csv_single

    if batch_glob is not None:
        df, plot_path = batch_evaluate_and_plot(
            network_glob=batch_glob,
            gpkg=gpkg,
            layer=layer,
            ds=ds,
            mode=mode,
            limit_links=limit_links,
            bbox_pad=bbox_pad,
            out_csv=out_csv_batch,
            out_plot=out_plot,
        )
        results['batch_df'] = df
        results['batch_csv'] = out_csv_batch
        results['plot'] = plot_path

    return results


# ---------------------------------- CLI --------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slope-only evaluation for MATSim networks")
    parser.add_argument("--network", type=Path, default=Path("data/Saarland_max1000m_V0.xml.gz"))
    parser.add_argument("--gpkg", type=Path, default=Path("data/Saarland_3d_raster_clamped.gpkg.gpkg"))
    parser.add_argument("--layer", type=str, default="roads_3d")
    parser.add_argument("--ds", type=float, default=5.0)
    parser.add_argument("--mode", type=str, default="by_uv",
                        choices=["by_uv", "per_link_line", "nearest_points"])
    parser.add_argument("--limit_links", type=int, default=0)
    parser.add_argument("--bbox_pad", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=Path("data/benchmark_Saarland_slope_only.csv"))
    parser.add_argument("--save_samples", type=Path, default=None)

    # Batch/glob mode
    parser.add_argument("--batch_glob", type=str, default=None, help="e.g. 'data/Saarland_max*V0.xml.gz'")
    parser.add_argument("--batch_out", type=Path, default=Path("data/batch_slope_vs_maxlen.csv"))
    parser.add_argument("--plot", type=Path, default=Path("data/slope_vs_maxlink.png"))

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()

    if args.batch_glob:
        df, plot_path = batch_evaluate_and_plot(
            network_glob=args.batch_glob,
            gpkg=args.gpkg,
            layer=args.layer,
            ds=args.ds,
            mode=args.mode,
            limit_links=args.limit_links,
            bbox_pad=args.bbox_pad,
            out_csv=args.batch_out,
            out_plot=args.plot,
        )
        print(f"Saved batch summary to: {args.batch_out}")
        if plot_path:
            print(f"Saved scatter plot to: {plot_path}")
    else:
        run(
            network=args.network,
            gpkg=args.gpkg,
            layer=args.layer,
            ds=args.ds,
            mode=args.mode,
            limit_links=args.limit_links,
            bbox_pad=args.bbox_pad,
            out_csv=args.out,
            save_samples=args.save_samples,
        )
