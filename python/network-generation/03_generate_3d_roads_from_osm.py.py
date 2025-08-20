# -*- coding: utf-8 -*-
# Erzeugt 3D-Straßen aus OSM + DTM.
# Neu:
# 1) Nicht-Struktur-Läufe (ohne bridge/tunnel) werden zu maximalen Abschnitten
#    zusammengefasst, die an Kreuzungen (Grad != 2) enden. Dadurch wirkt die Glättung
#    (SMOOTH_LAMBDA) über längere Strecken.
# 2) Für Brücken/Tunnel werden KEINE DEM-Höhen gesampelt (weder innen noch an Endpunkten).
#    Endhöhen kommen aus benachbarten Nicht-Struktur-Abschnitten (Anker). Fallback optional.

from pathlib import Path
import os, math, json
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point, box
import rasterio
from scipy.interpolate import UnivariateSpline
from collections import defaultdict

# --- Pfade ---
TIF_PATH      = Path(r"data\DTM Germany 20m v3b by Sonny.tif")
OSM_PATH      = Path(r"data\test_brandenburg_merged.gpkg")
OUTPUT_GPKG   = Path(r"data\roads_3d_raster_clamped.gpkg")
OUTPUT_LAYER  = "roads_3d"
OUTPUT_HTML   = Path(r"data\roads_3d_interactive_brandenburg.html")  # optionaler Plot

# --- Parameter ---
SPACING_ALONG_M = 8          # Abtastung entlang der Straße
ROUND_Z_DECIMALS = 1
SMOOTH_LAMBDA = 0.1
MAX_GRADE = 0.065
NODE_PRECISION = 5           #Rundung für genauigkeit der Kooedinaten om .gpkg (hier 5=10cm)

MAKE_3D_PLOT = False
LINEARIZE_BRIDGE_TUNNEL = True        # Brücken/Tunnel strikt linear
CLAMP_BRIDGE_TUNNEL = False           # Brücken/Tunnel nicht clampen
STRICT_NO_DEM_FOR_BR_TU = False       # Falls True: bei fehlenden Ankern KEIN Fallback aufs DEM (setzt 0.0)

# --- Utilitys ---
def ensure_meter_crs(crs):
    from pyproj import CRS
    u = CRS.from_user_input(crs)
    return any(ai.unit_name and "metre" in (ai.unit_name or "").lower() for ai in u.axis_info)

def pick_lines_layer(gpkg_path_str):
    import fiona
    layers = fiona.listlayers(gpkg_path_str)
    if "lines" in layers:
        return "lines"
    for lyr in layers:
        g = gpd.read_file(gpkg_path_str, layer=lyr, rows=200)
        if not g.empty and g.geometry.geom_type.isin(["LineString","MultiLineString"]).mean() > 0.5:
            return lyr
    return layers[0] if layers else None

def load_osm_lines_gpkg(gpkg_path_str, layer=None):
    if layer is None:
        layer = pick_lines_layer(gpkg_path_str)
    g = gpd.read_file(gpkg_path_str, layer=layer)
    g = g[g.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    if g.empty:
        raise ValueError("Keine Linien gefunden.")
    if g.crs is None:
        raise ValueError("Kein CRS im OSM-Layer gesetzt.")
    return g

def get_tag(row, key: str, default=None):
    if key in row and row[key] not in (None, np.nan, ''):
        return row[key]
    for c in row.index:
        if c.lower()=='tags':
            v = row[c]
            if isinstance(v, dict):
                return v.get(key, default)
            try:
                d = json.loads(v)
                return d.get(key, default)
            except Exception:
                return default
    return default

def is_truthy_osm(val) -> bool:
    if val is None: return False
    s = str(val).strip().lower()
    return s in ("yes","true","1","viaduct","bridge","tunnel")

def coord_key_xy(x, y, prec=NODE_PRECISION):
    return (round(float(x), prec), round(float(y), prec))

def coord_key_point(pt: Point, prec=NODE_PRECISION):
    return coord_key_xy(pt.x, pt.y, prec)

def reverse_ls(ls: LineString) -> LineString:
    return LineString(list(ls.coords)[::-1])

def densify_linestring(ls: LineString, ds: float):
    L = ls.length
    if L<=0: return np.array([0.0]), [Point(ls.coords[0])]
    n = max(2, int(math.ceil(L/ds))+1)
    s = np.linspace(0.0, L, n)
    pts = [ls.interpolate(float(d)) for d in s]
    return s, pts

def spline_smooth(s, z, lmbda):
    if lmbda<=0: return z
    try:
        spl = UnivariateSpline(s, z, s=lmbda*len(s))
        return spl(s)
    except Exception:
        return z

def clamp_grade(s, z, max_grade, iters=2):
    z_new = z.copy()
    for _ in range(iters):
        for i in range(1, len(s)):
            ds_i = s[i]-s[i-1]
            z_new[i] = min(z_new[i], z_new[i-1]+max_grade*ds_i)
            z_new[i] = max(z_new[i], z_new[i-1]-max_grade*ds_i)
        for i in range(len(s)-2,-1,-1):
            ds_i = s[i+1]-s[i]
            z_new[i] = min(z_new[i], z_new[i+1]+max_grade*ds_i)
            z_new[i] = max(z_new[i], z_new[i+1]-max_grade*ds_i)
    return z_new

def make3d(points, z, round_decimals=1):
    if round_decimals is not None:
        z = np.round(z.astype(float), round_decimals)
    return LineString([(p.x,p.y,float(zz)) for p,zz in zip(points,z)])

def sample_z_from_raster(src, pts_xy):
    vals = list(src.sample(pts_xy))
    zs = np.array([v[0] if (v is not None and np.isfinite(v[0])) else np.nan for v in vals], dtype=float)
    if np.any(~np.isfinite(zs)):
        idx = np.where(np.isfinite(zs))[0]
        if idx.size == 0:
            zs[:] = 0.0
        else:
            for i in range(len(zs)):
                if not np.isfinite(zs[i]):
                    j = idx[(np.abs(idx - i)).argmin()]
                    zs[i] = zs[j]
    return zs

# ---------- Läufe ohne Brücke/Tunnel zusammenfassen (stoppt an Kreuzungen) ----------
def coalesce_non_structural_runs(gdf: gpd.GeoDataFrame, key_precision=NODE_PRECISION) -> gpd.GeoDataFrame:
    def _get_tag_row(row, key):
        if key in row and row[key] not in (None, "", float("nan")):
            return row[key]
        for c in row.index:
            if c.lower() == "tags":
                v = row[c]
                if isinstance(v, dict):
                    return v.get(key)
                try:
                    d = json.loads(v); return d.get(key)
                except Exception:
                    return None
        return None

    def ck(pt):  # coord key
        return (round(pt[0], key_precision), round(pt[1], key_precision))

    g = gdf[gdf.geometry.type.isin(["LineString","MultiLineString"])].copy()
    if "MultiLineString" in set(g.geometry.type):
        g = g.explode(ignore_index=True)

    is_struct = g.apply(lambda r: is_truthy_osm(_get_tag_row(r,"bridge")) or
                                  is_truthy_osm(_get_tag_row(r,"tunnel")), axis=1)
    g_struct = g[is_struct].copy()
    g_plain  = g[~is_struct].copy()
    if g_plain.empty:
        return g

    # u/v bestimmen (fallback: Koordinaten)
    have_uv_plain = ("u" in g_plain.columns) and ("v" in g_plain.columns)
    uv_plain = {}
    for idx, row in g_plain.iterrows():
        if have_uv_plain and pd.notna(row.get("u")) and pd.notna(row.get("v")):
            uv_plain[idx] = (row["u"], row["v"])
        else:
            ls = row.geometry
            uv_plain[idx] = (ck(ls.coords[0]), ck(ls.coords[-1]))

    have_uv_all = ("u" in g.columns) and ("v" in g.columns)
    uv_all = {}
    for idx, row in g.iterrows():
        if have_uv_all and pd.notna(row.get("u")) and pd.notna(row.get("v")):
            uv_all[idx] = (row["u"], row["v"])
        else:
            ls = row.geometry
            uv_all[idx] = (ck(ls.coords[0]), ck(ls.coords[-1]))

    # Adjazenzen
    from collections import defaultdict
    adj_plain = defaultdict(list)
    for eidx, (u, v) in uv_plain.items():
        adj_plain[u].append(eidx)
        adj_plain[v].append(eidx)
    adj_all = defaultdict(list)
    for eidx, (u, v) in uv_all.items():
        adj_all[u].append(eidx)
        adj_all[v].append(eidx)
    deg_all = {node: len(edgs) for node, edgs in adj_all.items()}

    def reverse(ls): return LineString(list(ls.coords)[::-1])

    visited = set()
    merged_rows = []

    def extend(node, prev_edge, visited_local):
        seq = []
        curr_node = node
        last_edge = prev_edge
        while True:
            inc = [e for e in adj_plain.get(curr_node, []) if e != last_edge and e not in visited_local]
            if deg_all.get(curr_node, 0) != 2 or not inc:
                break
            e = inc[0]
            seq.append(e)
            visited_local.add(e)
            u, v = uv_plain[e]
            next_node = v if curr_node == u else u
            last_edge = e
            curr_node = next_node
        return seq, curr_node

    for e0 in g_plain.index:
        if e0 in visited: continue
        u0, v0 = uv_plain[e0]
        visited_local = {e0}

        if deg_all.get(u0, 0) != 2:
            start_node, other_node = u0, v0
        elif deg_all.get(v0, 0) != 2:
            start_node, other_node = v0, u0
        else:
            start_node, other_node = u0, v0

        seq_back, start_end = extend(start_node, e0, visited_local)
        seq_fwd , end_end   = extend(other_node, e0, visited_local)
        ordered = list(reversed(seq_back)) + [e0] + seq_fwd

        coords = []
        current_node = start_end
        for e in ordered:
            ls = g_plain.loc[e].geometry
            u, v = uv_plain[e]
            if current_node == u:
                seg = ls; nxt = v
            elif current_node == v:
                seg = reverse(ls); nxt = u
            else:
                su = (round(ls.coords[0][0], key_precision), round(ls.coords[0][1], key_precision))
                sv = (round(ls.coords[-1][0], key_precision), round(ls.coords[-1][1], key_precision))
                if current_node == su:
                    seg, nxt = ls, sv
                else:
                    seg, nxt = reverse(ls), su
            segc = list(seg.coords)
            if not coords: coords.extend(segc)
            else:
                if segc and coords[-1] == segc[0]:
                    coords.extend(segc[1:])
                else:
                    coords.extend(segc)
            current_node = nxt

        new_geom = LineString(coords)
        first = g_plain.loc[ordered[0]].copy()
        first.geometry = new_geom
        if "bridge" in first.index: first["bridge"] = None
        if "tunnel" in first.index: first["tunnel"] = None
        if ("u" in first.index) and ("v" in first.index):
            if start_end == end_end:
                first["u"] = None; first["v"] = None
            else:
                first["u"] = start_end; first["v"] = end_end
        merged_rows.append(first)
        visited.update(ordered)

    merged_plain = gpd.GeoDataFrame(merged_rows, geometry="geometry", crs=gdf.crs)
    try: merged_plain["length"] = merged_plain.geometry.length
    except Exception: pass

    out = pd.concat([g_struct, merged_plain], ignore_index=True)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)

# ---------- Höhenberechnung ----------
def process_plain_linestring(line: LineString, row, src, ds, max_grade, smooth_lambda):
    s, pts = densify_linestring(line, ds)
    xy = [(p.x, p.y) for p in pts]
    z_dem = sample_z_from_raster(src, xy)
    z_base = spline_smooth(s, z_dem, smooth_lambda)
    z_cl = clamp_grade(s, z_base, max_grade, iters=3)
    z_cl[0], z_cl[-1] = z_base[0], z_base[-1]
    z_cl = clamp_grade(s, z_cl, max_grade, iters=1)
    ls3d = make3d(pts, z_cl, ROUND_Z_DECIMALS)
    return s, pts, z_cl, ls3d

def process_struct_linestring(line: LineString, anchors, src, ds):
    # Keine DEM-Samples innen und — per Anforderung — auch nicht an Endpunkten.
    s, pts = densify_linestring(line, ds)

    # Anker an Endpunkten suchen
    start_key = coord_key_point(pts[0], NODE_PRECISION)
    end_key   = coord_key_point(pts[-1], NODE_PRECISION)
    z0 = anchors.get(start_key, None)
    z1 = anchors.get(end_key, None)

    # Fallbacks nur wenn unbedingt nötig
    if z0 is None or z1 is None:
        if STRICT_NO_DEM_FOR_BR_TU:
            if z0 is None: z0 = 0.0
            if z1 is None: z1 = 0.0
        else:
            # Nur den fehlenden Endpunkt aus DEM holen (Endpunkte gelten NICHT als „innerhalb“,
            # aber wenn du das strikt vermeiden willst, setze STRICT_NO_DEM_FOR_BR_TU=True)
            need_xy = []
            if z0 is None: need_xy.append((pts[0].x, pts[0].y))
            if z1 is None: need_xy.append((pts[-1].x, pts[-1].y))
            if need_xy:
                zs_fb = sample_z_from_raster(src, need_xy)
                k = 0
                if z0 is None: z0 = float(zs_fb[k]); k += 1
                if z1 is None: z1 = float(zs_fb[k-1 if len(need_xy)==1 else k])

    # Linear zwischen Ankern
    z_linear = np.linspace(float(z0), float(z1), len(pts))
    ls3d = make3d(pts, z_linear, ROUND_Z_DECIMALS)
    return s, pts, z_linear, ls3d

# --- Hauptfunktion ---
def main():
    with rasterio.open(TIF_PATH) as src:
        rast_crs = src.crs
        if not ensure_meter_crs(rast_crs):
            raise ValueError(f"Raster-CRS {rast_crs} ist nicht metrisch.")

        lines = load_osm_lines_gpkg(str(OSM_PATH)).to_crs(rast_crs)

        # auf Raster-Ausdehnung beschneiden
        bounds_poly = box(*src.bounds)
        try:
            lines = gpd.overlay(lines, gpd.GeoDataFrame(geometry=[bounds_poly], crs=rast_crs), how="intersection")
        except Exception:
            pass

        # Lange Nicht-Struktur-Läufe erzeugen
        print("Erzeuge lange Glättungsabschnitte – Brücken/Tunnel bleiben separat …")
        lines = coalesce_non_structural_runs(lines, key_precision=NODE_PRECISION)

        # Aufteilen in plain vs. struct
        def row_is_struct(r):
            return is_truthy_osm(get_tag(r,'tunnel')) or is_truthy_osm(get_tag(r,'bridge'))
        is_struct_mask = lines.apply(row_is_struct, axis=1)
        lines_struct = lines[is_struct_mask].copy()
        lines_plain  = lines[~is_struct_mask].copy()

        # --- PASS 1: Plain verarbeiten + Anker sammeln ---
        anchors_sum = defaultdict(float)  # node_key -> sum(z)
        anchors_cnt = defaultdict(int)    # node_key -> count
        out_plain_geoms = []
        out_plain_meta  = {"len_m":[], "max_abs_grade":[], "is_bridge":[], "is_tunnel":[]}

        for _, row in lines_plain.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                out_plain_geoms.append(None)
                for k in out_plain_meta: out_plain_meta[k].append(np.nan)
                continue

            if isinstance(geom, MultiLineString):
                parts3d=[]; lengths=[]; grades=[]; anyb=False; anyt=False
                for ls in geom.geoms:
                    s, pts, z_cl, g3d = process_plain_linestring(ls, row, src, SPACING_ALONG_M, MAX_GRADE, SMOOTH_LAMBDA)
                    parts3d.append(g3d)
                    lengths.append(float(s[-1]))
                    dz = np.diff(z_cl); ds_arr = np.diff(s)
                    gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                    grades.append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                    # Anker sammeln
                    anchors_sum[coord_key_point(pts[0])] += float(z_cl[0]); anchors_cnt[coord_key_point(pts[0])] += 1
                    anchors_sum[coord_key_point(pts[-1])] += float(z_cl[-1]); anchors_cnt[coord_key_point(pts[-1])] += 1
                out_plain_geoms.append(MultiLineString(parts3d))
                out_plain_meta["len_m"].append(sum(lengths) if lengths else np.nan)
                out_plain_meta["max_abs_grade"].append(max(grades) if grades else np.nan)
                out_plain_meta["is_bridge"].append(False)
                out_plain_meta["is_tunnel"].append(False)
            else:
                s, pts, z_cl, g3d = process_plain_linestring(geom, row, src, SPACING_ALONG_M, MAX_GRADE, SMOOTH_LAMBDA)
                out_plain_geoms.append(g3d)
                dz = np.diff(z_cl); ds_arr = np.diff(s)
                gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                out_plain_meta["len_m"].append(float(s[-1]))
                out_plain_meta["max_abs_grade"].append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_plain_meta["is_bridge"].append(False)
                out_plain_meta["is_tunnel"].append(False)
                anchors_sum[coord_key_point(pts[0])]  += float(z_cl[0]);  anchors_cnt[coord_key_point(pts[0])]  += 1
                anchors_sum[coord_key_point(pts[-1])] += float(z_cl[-1]); anchors_cnt[coord_key_point(pts[-1])] += 1

        # Anker finalisieren (Mittelwert bei konkurrierenden Werten)
        anchors = {k: anchors_sum[k]/anchors_cnt[k] for k in anchors_cnt.keys()}

        # --- PASS 2: Strukturen (Brücke/Tunnel) linear zwischen Ankern ---
        out_struct_geoms = []
        out_struct_meta  = {"len_m":[], "max_abs_grade":[], "is_bridge":[], "is_tunnel":[]}

        for _, row in lines_struct.iterrows():
            geom = row.geometry
            is_bridge = is_truthy_osm(get_tag(row,'bridge'))
            is_tunnel = is_truthy_osm(get_tag(row,'tunnel'))
            if geom is None or geom.is_empty:
                out_struct_geoms.append(None)
                for k in out_struct_meta: out_struct_meta[k].append(np.nan)
                continue

            if isinstance(geom, MultiLineString):
                parts3d=[]; lengths=[]; grades=[]
                for ls in geom.geoms:
                    s, pts, z_lin, g3d = process_struct_linestring(ls, anchors, src, SPACING_ALONG_M)
                    parts3d.append(g3d)
                    lengths.append(float(s[-1]))
                    dz = np.diff(z_lin); ds_arr = np.diff(s)
                    gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                    grades.append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_struct_geoms.append(MultiLineString(parts3d))
                out_struct_meta["len_m"].append(sum(lengths) if lengths else np.nan)
                out_struct_meta["max_abs_grade"].append(max(grades) if grades else np.nan)
                out_struct_meta["is_bridge"].append(bool(is_bridge))
                out_struct_meta["is_tunnel"].append(bool(is_tunnel))
            else:
                s, pts, z_lin, g3d = process_struct_linestring(geom, anchors, src, SPACING_ALONG_M)
                out_struct_geoms.append(g3d)
                dz = np.diff(z_lin); ds_arr = np.diff(s)
                gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                out_struct_meta["len_m"].append(float(s[-1]))
                out_struct_meta["max_abs_grade"].append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_struct_meta["is_bridge"].append(bool(is_bridge))
                out_struct_meta["is_tunnel"].append(bool(is_tunnel))

        # --- Zusammenführen in Original-Reihenfolge ---
        # (Wir behalten die Attributtabellen von lines_plain und lines_struct und setzen die neuen Geometrien/Metadaten)
        out_plain = lines_plain.copy()
        out_plain["geometry"] = out_plain_geoms
        for k,v in out_plain_meta.items(): out_plain[k] = v

        out_struct = lines_struct.copy()
        out_struct["geometry"] = out_struct_geoms
        for k,v in out_struct_meta.items(): out_struct[k] = v

        out = pd.concat([out_plain, out_struct], ignore_index=True)
        out = gpd.GeoDataFrame(out, geometry="geometry", crs=rast_crs)

    # speichern
    OUTPUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(OUTPUT_GPKG, layer=OUTPUT_LAYER, driver="GPKG")
    print(f"[OK] 3D-Linien gespeichert: {OUTPUT_GPKG}")

    # Optional: einfacher 3D-Plot (Plotly)
    if MAKE_3D_PLOT:
        import plotly.graph_objects as go
        fig = go.Figure()
        for geom in out.geometry:
            if geom is None: continue
            if isinstance(geom, LineString):
                xs, ys, zs = zip(*list(geom.coords))
                fig.add_trace(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(width=2), opacity=0.8))
            elif isinstance(geom, MultiLineString):
                for ls in geom.geoms:
                    xs, ys, zs = zip(*list(ls.coords))
                    fig.add_trace(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(width=2), opacity=0.8))
        fig.update_layout(title="3D-Straßennetz", showlegend=False)
        fig.write_html(str(OUTPUT_HTML))
        print(f"[OK] Interaktive 3D-Karte gespeichert: {OUTPUT_HTML}")

if __name__=="__main__":
    os.environ.setdefault("GDAL_CACHEMAX","512")
    main()
