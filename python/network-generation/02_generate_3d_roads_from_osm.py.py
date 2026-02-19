# -*- coding: utf-8 -*-
# Erzeugt 3D-Straßen aus OSM + DTM und eine OSM-HTML-Karte mit Punkt-Höhen (vor/nach Glättung).

from pathlib import Path
import os, math, json
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point, box
import rasterio
from scipy.interpolate import UnivariateSpline
from collections import defaultdict

# --- Pfade (Defaults; können per CLI-Argument überschrieben werden) ---
TIF_PATH      = Path(r"data\DTM Germany 50m v3b by Sonny.tif")
OSM_PATH      = Path(r"data\germany_detailed_sorted_DF.gpkg")
OUTPUT_GPKG   = Path(r"data\germany_3d_raster_clamped_DF.gpkg")
OUTPUT_LAYER  = "roads_3d"
OUTPUT_HTML   = Path(r"data\roads_3d_interactive_germany.html")
OUTPUT_HTML_OSM = Path(r"data\roads_3d_points_over_osm.html")

# --- Parameter ---
SPACING_ALONG_M = 12
ROUND_Z_DECIMALS = 1
SMOOTH_LAMBDA = 0.1
MAX_GRADE = 0.08                        # Worst-Case für Landstraßen = 8% (Forschungsgesellschaft für Straßen- und Verkehrswesen: Richtlinien für die Anlage von Landstraßen – RAL. FGSV-Verlag, Köln 2012)
NODE_PRECISION = 5

MAKE_3D_PLOT = False
STRICT_NO_DEM_FOR_BR_TU = False   # Wenn True, werden für Brücke/Tunnel auch für die HTML-Visualisierung KEINE DEM-Werte gesampelt.
MAKE_OSM_HTML = False
HTML_POINT_STRIDE = 10                 # 10 ⇒ ungefähr alle 80 m ein Punkt in der HTML
MAX_HTML_POINTS = 150_000              # hard cap für die HTML (dynamisch heruntergesampelt)
HTML_RANDOM_SEED = 42                  # für reproduzierbares Downsampling
BBOX_FOR_HTML = None                   # (minx, miny, maxx, maxy) im Raster-CRS oder None

MAKE_POINTS_GPKG   = False
POINTS_GPKG_PATH   = Path(r"data\roads_3d_points.gpkg")
POINTS_GPKG_LAYER  = "sample_points"

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

# ---------- Läufe ohne Brücke/Tunnel zusammenfassen ----------
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

    def ck(pt):
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
                sv = (round(ls.coords[-1][0][0], key_precision), round(ls.coords[-1][0][1], key_precision)) if isinstance(ls.coords[-1], (list, tuple)) else (round(ls.coords[-1][0], key_precision), round(ls.coords[-1][1], key_precision))
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
    z_dem = sample_z_from_raster(src, xy)                         # ROH aus TIF
    z_base = spline_smooth(s, z_dem, smooth_lambda)               # Glättung
    z_cl = clamp_grade(s, z_base, max_grade, iters=3)             # Gefälle begrenzen
    z_cl[0], z_cl[-1] = z_base[0], z_base[-1]
    z_cl = clamp_grade(s, z_cl, max_grade, iters=1)
    ls3d = make3d(pts, z_cl, ROUND_Z_DECIMALS)
    return s, pts, z_dem, z_cl, ls3d                              # NEU: z_dem zurückgeben

def process_struct_linestring(line: LineString, anchors, src, ds):
    s, pts = densify_linestring(line, ds)

    start_key = coord_key_point(pts[0], NODE_PRECISION)
    end_key   = coord_key_point(pts[-1], NODE_PRECISION)
    z0 = anchors.get(start_key, None)
    z1 = anchors.get(end_key, None)

    if z0 is None or z1 is None:
        if STRICT_NO_DEM_FOR_BR_TU:
            if z0 is None: z0 = 0.0
            if z1 is None: z1 = 0.0
        else:
            need_xy = []
            if z0 is None: need_xy.append((pts[0].x, pts[0].y))
            if z1 is None: need_xy.append((pts[-1].x, pts[-1].y))
            if need_xy:
                zs_fb = sample_z_from_raster(src, need_xy)
                k = 0
                if z0 is None: z0 = float(zs_fb[k]); k += 1
                if z1 is None: z1 = float(zs_fb[k-1 if len(need_xy)==1 else k])

    z_linear = np.linspace(float(z0), float(z1), len(pts))
    ls3d = make3d(pts, z_linear, ROUND_Z_DECIMALS)

    # Für Visualisierung optional DEM auch auf Strukturen sampeln (nur wenn nicht strikt untersagt)
    z_dem_struct = None
    if not STRICT_NO_DEM_FOR_BR_TU:
        xy = [(p.x, p.y) for p in pts]
        z_dem_struct = sample_z_from_raster(src, xy)
    return s, pts, z_dem_struct, z_linear, ls3d                   # NEU: z_dem_struct (oder None)

# --- OSM HTML Karte bauen ---
def build_osm_html(points_records, rast_crs, out_html_path,
                   point_stride=1, max_points=150_000,
                   random_seed=42, bbox=None,
                   dz_only_from_plain=True):
    """
    Punktekarte:
      - Layer "DEM (TIF)": blaue Punkte (z_dem)
      - Layer "Δz (3D - DEM)": Punkte farblich skaliert nach Δz = z_after - z_dem
        (wenn dz_only_from_plain=True: ausschließlich Punkte mit kind=="plain")
    """
    import folium
    import numpy as np
    import branca.colormap as cm
    from pyproj import Transformer

    n_total = len(points_records)
    if n_total == 0:
        print("[Hinweis] Keine Punkte für HTML-Karte vorhanden.")
        return

    print(f"[HTML] Rohpunkte: {n_total:,}")

    # 1) Optional: BBOX-Filter
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        points_records = [r for r in points_records if (minx <= r["x"] <= maxx and miny <= r["y"] <= maxy)]
        print(f"[HTML] Nach BBOX: {len(points_records):,}")

    # 2) Striding
    if point_stride > 1:
        points_records = points_records[::point_stride]
        print(f"[HTML] Nach Stride ({point_stride}): {len(points_records):,}")

    # 3) Downsampling (hartes Limit)
    if len(points_records) > max_points:
        rng = np.random.default_rng(random_seed)
        idx = rng.choice(len(points_records), size=max_points, replace=False)
        points_records = [points_records[i] for i in idx]
        print(f"[HTML] Nach Downsampling (max {max_points:,}): {len(points_records):,}")

    # 4) Nach WGS84 transformieren
    xs = np.fromiter((r["x"] for r in points_records), dtype=float, count=len(points_records))
    ys = np.fromiter((r["y"] for r in points_records), dtype=float, count=len(points_records))
    kinds = np.array([r.get("kind","plain") for r in points_records])
    transformer = Transformer.from_crs(rast_crs, "EPSG:4326", always_xy=True)
    lons, lats = transformer.transform(xs, ys)
    print("[HTML] Koordinaten nach WGS84 transformiert.")

    # Δz vektoriell berechnen (NaNs bleiben NaN)
    z_dem_arr   = np.array([r.get("z_dem", np.nan)   for r in points_records], dtype=float)
    z_after_arr = np.array([r.get("z_after", np.nan) for r in points_records], dtype=float)
    dz_arr = z_after_arr - z_dem_arr

    # Nur Δz von plain-Punkten berücksichtigen (gehen in Glättung ein)
    if dz_only_from_plain:
        dz_valid_mask = np.isfinite(dz_arr) & (kinds == "plain")
    else:
        dz_valid_mask = np.isfinite(dz_arr)

    # Robuste Farbbereichsgrenzen (nur aus den "validen" Δz-Werten)
    if np.any(dz_valid_mask):
        q_lo, q_hi = np.nanpercentile(dz_arr[dz_valid_mask], [2, 98])
        vmax = max(abs(q_lo), abs(q_hi))
        if not np.isfinite(vmax) or vmax == 0:
            vmax = 1.0
        vmin = -vmax
    else:
        vmin, vmax = -1.0, 1.0

    print(f"[HTML] Δz-Farbskala: vmin={vmin:.3f} m, vmax={vmax:.3f} m (symmetrisch)")

    # Karte + Layer
    lon_min, lon_max = float(np.nanmin(lons)), float(np.nanmax(lons))
    lat_min, lat_max = float(np.nanmin(lats)), float(np.nanmax(lats))
    center = [(lat_min + lat_max) / 2.0, (lon_min + lon_max) / 2.0]

    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")
    fg_dem   = folium.FeatureGroup(name="DEM (TIF)")
    fg_dz    = folium.FeatureGroup(name="Δz (3D - DEM)")

    # Farbskala (divergierend, symmetrisch)
    cmap = cm.LinearColormap(
        colors=["#313695", "#74add1", "#ffffbf", "#f46d43", "#a50026"],
        vmin=vmin, vmax=vmax
    )
    cmap.caption = "Δz (3D - DEM) [m]"
    cmap.add_to(m)

    # Marker setzen
    for i, r in enumerate(points_records):
        lat = float(lats[i]); lon = float(lons[i])
        z_dem   = z_dem_arr[i]
        z_after = z_after_arr[i]
        dz      = dz_arr[i]

        # DEM-Layer: nur z_dem (blau)
        if np.isfinite(z_dem):
            popup_dem = folium.Popup(
                html=(f"<b>Quelle:</b> TIF (DEM)<br>"
                      f"<b>Lon/Lat:</b> {lon:.6f}, {lat:.6f}<br>"
                      f"<b>z_dem:</b> {z_dem:.2f} m"),
                max_width=280
            )
            folium.CircleMarker(
                location=(lat, lon), radius=3, color="#1f77b4",
                fill=True, fill_opacity=0.7, weight=1, popup=popup_dem
            ).add_to(fg_dem)

        # Δz-Layer: NUR plain-Punkte (gehen in Glättung ein), nur mit gültiger Δz
        if dz_only_from_plain and kinds[i] != "plain":
            continue
        if not np.isfinite(dz):
            continue

        color = cmap(float(dz))
        popup_dz = folium.Popup(
            html=(f"<b>Quelle:</b> 3D (geglättet/clamped)<br>"
                  f"<b>Lon/Lat:</b> {lon:.6f}, {lat:.6f}<br>"
                  f"<b>z_3d:</b> {z_after:.2f} m<br>"
                  f"<b>z_dem:</b> {z_dem:.2f} m<br>"
                  f"<b>Δz (3D - DEM):</b> {dz:.2f} m"),
            max_width=320
        )
        folium.CircleMarker(
            location=(lat, lon), radius=3, color=color,
            fill=True, fill_opacity=0.8, weight=1, popup=popup_dz
        ).add_to(fg_dz)

    fg_dem.add_to(m)
    fg_dz.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]])
    m.save(str(out_html_path))
    print(f"[OK] OSM-HTML-Karte gespeichert: {out_html_path}")

# --- Export ---
def export_points_to_gpkg(points_records, crs, out_path: Path, layer_name: str, only_plain=True):
    if not points_records:
        print("[Hinweis] Keine Punkte zum Export.")
        return
    df = pd.DataFrame(points_records).copy()

    # Nur die Punkte exportieren, die in die Glättung eingegangen sind
    if only_plain:
        df = df[df.get("kind", "plain") == "plain"].copy()

    if df.empty:
        print("[Hinweis] Nach Filter keine Punkte zum Export.")
        return

    # Δz berechnen (wo möglich)
    df["dz"] = np.where(
        df["z_dem"].notna() & df["z_after"].notna(),
        df["z_after"] - df["z_dem"],
        np.nan
    )

    gdf = gpd.GeoDataFrame(
        df,
        geometry=[Point(xy) for xy in zip(df["x"], df["y"])],
        crs=crs
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, layer=layer_name, driver="GPKG")
    print(f"[OK] Punkte exportiert: {out_path} (Layer: {layer_name}, {len(gdf)} Features, only_plain={only_plain})")

# --- Hauptfunktion ---
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Erzeuge 3D-Straßen aus OSM + DTM")
    parser.add_argument("--tif",    default=str(TIF_PATH),    help="Pfad zur DTM-TIF-Datei")
    parser.add_argument("--osm",    default=str(OSM_PATH),    help="Pfad zur OSM-GPKG-Datei (detailed_sorted)")
    parser.add_argument("--output", default=str(OUTPUT_GPKG), help="Ausgabepfad für 3D-GPKG")
    args = parser.parse_args()

    tif_path    = Path(args.tif)
    osm_path    = Path(args.osm)
    output_gpkg = Path(args.output)

    with rasterio.open(tif_path) as src:
        rast_crs = src.crs
        if not ensure_meter_crs(rast_crs):
            raise ValueError(f"Raster-CRS {rast_crs} ist nicht metrisch.")

        lines = load_osm_lines_gpkg(str(osm_path)).to_crs(rast_crs)

        bounds_poly = box(*src.bounds)
        try:
            lines = gpd.overlay(lines, gpd.GeoDataFrame(geometry=[bounds_poly], crs=rast_crs), how="intersection")
        except Exception:
            pass

        print("Erzeuge lange Glättungsabschnitte – Brücken/Tunnel bleiben separat …")
        lines = coalesce_non_structural_runs(lines, key_precision=NODE_PRECISION)

        def row_is_struct(r):
            return is_truthy_osm(get_tag(r,'tunnel')) or is_truthy_osm(get_tag(r,'bridge'))
        is_struct_mask = lines.apply(row_is_struct, axis=1)
        lines_struct = lines[is_struct_mask].copy()
        lines_plain  = lines[~is_struct_mask].copy()

        anchors_sum = defaultdict(float)
        anchors_cnt = defaultdict(int)
        out_plain_geoms = []
        out_plain_meta  = {"len_m":[], "max_abs_grade":[], "is_bridge":[], "is_tunnel":[]}

        # Punkte für HTML/GPKG
        points_for_html = []

        for _, row in lines_plain.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                out_plain_geoms.append(None)
                for k in out_plain_meta: out_plain_meta[k].append(np.nan)
                continue

            def handle_ls(ls):
                s, pts, z_dem, z_cl, g3d = process_plain_linestring(ls, row, src, SPACING_ALONG_M, MAX_GRADE, SMOOTH_LAMBDA)
                # HTML-/Export-Punkte sammeln
                for i, p in enumerate(pts):
                    points_for_html.append({
                        "x": p.x, "y": p.y,
                        "z_dem": float(z_dem[i]),
                        "z_after": float(z_cl[i]),
                        "kind": "plain"
                    })
                # Anker sammeln
                anchors_sum[coord_key_point(pts[0])]  += float(z_cl[0]);  anchors_cnt[coord_key_point(pts[0])]  += 1
                anchors_sum[coord_key_point(pts[-1])] += float(z_cl[-1]); anchors_cnt[coord_key_point(pts[-1])] += 1
                return s, z_cl, g3d

            if isinstance(geom, MultiLineString):
                parts3d=[]; lengths=[]; grades=[]
                for ls in geom.geoms:
                    s, z_cl, g3d = handle_ls(ls)
                    parts3d.append(g3d)
                    lengths.append(float(s[-1]))
                    dz = np.diff(z_cl); ds_arr = np.diff(s)
                    gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                    grades.append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_plain_geoms.append(MultiLineString(parts3d))
                out_plain_meta["len_m"].append(sum(lengths) if lengths else np.nan)
                out_plain_meta["max_abs_grade"].append(max(grades) if grades else np.nan)
                out_plain_meta["is_bridge"].append(False)
                out_plain_meta["is_tunnel"].append(False)
            else:
                s, z_cl, g3d = handle_ls(geom)
                out_plain_geoms.append(g3d)
                dz = np.diff(z_cl); ds_arr = np.diff(s)
                gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                out_plain_meta["len_m"].append(float(s[-1]))
                out_plain_meta["max_abs_grade"].append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_plain_meta["is_bridge"].append(False)
                out_plain_meta["is_tunnel"].append(False)

        anchors = {k: anchors_sum[k]/anchors_cnt[k] for k in anchors_cnt.keys()}

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

            def handle_struct_ls(ls):
                s, pts, z_dem_struct, z_lin, g3d = process_struct_linestring(ls, anchors, src, SPACING_ALONG_M)
                # Punkte sammeln (für DEM-Layer sichtbar, Δz ggf. ausgeblendet)
                for i, p in enumerate(pts):
                    points_for_html.append({
                        "x": p.x, "y": p.y,
                        "z_dem": (None if z_dem_struct is None else float(z_dem_struct[i])),
                        "z_after": float(z_lin[i]),
                        "kind": ("bridge" if is_bridge else "tunnel" if is_tunnel else "struct")
                    })
                return s, z_lin, g3d

            if isinstance(geom, MultiLineString):
                parts3d=[]; lengths=[]; grades=[]
                for ls in geom.geoms:
                    s, z_lin, g3d = handle_struct_ls(ls)
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
                s, z_lin, g3d = handle_struct_ls(geom)
                out_struct_geoms.append(g3d)
                dz = np.diff(z_lin); ds_arr = np.diff(s)
                gr = np.where(ds_arr>0, dz/ds_arr, 0.0)
                out_struct_meta["len_m"].append(float(s[-1]))
                out_struct_meta["max_abs_grade"].append(float(np.max(np.abs(gr))) if gr.size else 0.0)
                out_struct_meta["is_bridge"].append(bool(is_bridge))
                out_struct_meta["is_tunnel"].append(bool(is_tunnel))

        out_plain = lines_plain.copy()
        out_plain["geometry"] = out_plain_geoms
        for k,v in out_plain_meta.items(): out_plain[k] = v

        out_struct = lines_struct.copy()
        out_struct["geometry"] = out_struct_geoms
        for k,v in out_struct_meta.items(): out_struct[k] = v

        out = pd.concat([out_plain, out_struct], ignore_index=True)
        out = gpd.GeoDataFrame(out, geometry="geometry", crs=rast_crs)

    # speichern GPKG
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(output_gpkg, layer=OUTPUT_LAYER, driver="GPKG")
    print(f"[OK] 3D-Linien gespeichert: {output_gpkg}")

    # Optional: Plotly 3D
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
        print(f"[OK] Interaktive 3D-Karte (Plotly) gespeichert: {OUTPUT_HTML}")

    # OSM-HTML mit Punkt-Höhen (Δz nur plain)
    if MAKE_OSM_HTML:
        OUTPUT_HTML_OSM.parent.mkdir(parents=True, exist_ok=True)
        build_osm_html(
            points_for_html, out.crs, OUTPUT_HTML_OSM,
            point_stride=HTML_POINT_STRIDE,
            max_points=MAX_HTML_POINTS,
            random_seed=HTML_RANDOM_SEED,
            bbox=BBOX_FOR_HTML,
            dz_only_from_plain=True
        )

    # Punkte-GPKG (nur plain exportieren)
    if MAKE_POINTS_GPKG:
        export_points_to_gpkg(points_for_html, out.crs, POINTS_GPKG_PATH, POINTS_GPKG_LAYER, only_plain=True)

if __name__=="__main__":
    os.environ.setdefault("GDAL_CACHEMAX","512")
    main()
