# build_kdtree_from_gpkg_epsg4326.py
import os
# Oversubscription vermeiden (stabilere Auslastung)
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import numpy as np
import geopandas as gpd
from scipy.spatial import cKDTree

# Optional: schnellere IO
try:
    import pyogrio
    HAS_PYOGRIO = True
except Exception:
    HAS_PYOGRIO = False

# =========================
# Parameter
# =========================
# Beispiel: direkt das 3D-Straßen-GPKG verwenden:
gpkg_path    = r"data\Märkisch-Oderland_3d_raster_clamped.gpkg"  # Eingabe (Punkte ODER Linien)
layer_name   = None               # None = automatisch sinnvolle Layerwahl
output_path  = r"data\Märkisch-Oderland_kdtree_from_roads3d_epsg4326.npz"
height_col   = None               # Falls 2D-Geometrie: mögliche Höhen-Spalte (z.B. "z")

# KDTree-Parameter
leafsize      = 64
balanced_tree = False
compact_nodes = True

# Für 2D-Geometrie: Kandidaten für Höhen-Attribut
HEIGHT_COL_CANDIDATES = ["z", "Z", "elev", "elevation", "hoehe", "höhe", "h", "H"]

# =========================
# Hilfsfunktionen
# =========================
def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Nach EPSG:4326 reprojizieren. Z bleibt unverändert (GeoPandas transformiert XY).
    """
    if gdf.crs is None:
        raise RuntimeError("Eingabe hat kein CRS. Erwartet ein gesetztes CRS.")
    try:
        epsg = gdf.crs.to_epsg()
    except Exception:
        epsg = None
    if epsg == 4326:
        return gdf
    return gdf.to_crs("EPSG:4326")

def pick_height_col(columns, prefer=None):
    if prefer and prefer in columns:
        return prefer
    for c in HEIGHT_COL_CANDIDATES:
        if c in columns:
            return c
    return None

def read_any_layer(path, layer=None) -> gpd.GeoDataFrame:
    if HAS_PYOGRIO:
        gdf = pyogrio.read_dataframe(path, layer=layer, use_arrow=True)
    else:
        gdf = gpd.read_file(path, layer=layer)
    if gdf.empty:
        raise RuntimeError("Layer ist leer.")
    if gdf.crs is None:
        raise RuntimeError("Der Layer hat kein CRS – bitte im GPKG setzen.")
    return gdf

def auto_pick_layer(path) -> str | None:
    import fiona
    layers = fiona.listlayers(path)
    if not layers:
        return None
    # Bevorzugt: ein Layer mit Linien
    for lyr in layers:
        try:
            g = gpd.read_file(path, layer=lyr, rows=200)
            if not g.empty and g.geometry.geom_type.isin(["LineString","MultiLineString"]).any():
                return lyr
        except Exception:
            pass
    # Sonst: ein Layer mit Punkten
    for lyr in layers:
        try:
            g = gpd.read_file(path, layer=lyr, rows=200)
            if not g.empty and g.geometry.geom_type.isin(["Point"]).any():
                return lyr
        except Exception:
            pass
    return layers[0]

def extract_points_from_points_gdf(gdf: gpd.GeoDataFrame, height_col=None):
    """
    Erwartet EPSG:4326. Holt (lon,lat) und z aus Punkt-Geometrie (3D) oder Attribut.
    """
    # 3D-Geometrie?
    try:
        from shapely import get_coordinates
        coords_all = get_coordinates(gdf.geometry.values)  # (N,2) oder (N,3)
        geom_has_z = (coords_all.shape[1] == 3) and (coords_all.shape[0] == len(gdf))
    except Exception:
        geom_has_z = False
        coords_all = np.column_stack([gdf.geometry.x.to_numpy(),
                                      gdf.geometry.y.to_numpy()]).astype(np.float64)

    coords_xy = coords_all[:, :2].astype(np.float64, copy=False)

    if geom_has_z:
        heights = coords_all[:, 2].astype(np.float32, copy=False)
        src = "Geometrie-Z"
    else:
        hcol = pick_height_col(gdf.columns, prefer=height_col)
        if hcol is None:
            raise RuntimeError("Punktlayer ist 2D und es wurde keine passende Höhen-Spalte gefunden.")
        heights = gdf[hcol].to_numpy(dtype=np.float32, copy=False)
        ok = np.isfinite(heights)
        if not np.all(ok):
            coords_xy = coords_xy[ok]
            heights   = heights[ok]
        src = f"Attribut '{hcol}'"
    return coords_xy, heights, src

def extract_points_from_lines_gdf(gdf: gpd.GeoDataFrame):
    """
    Erwartet EPSG:4326. Nimmt ALLE Scheitelpunkte aus LineString/MultiLineString.
    Z wird aus der 3D-Geometrie genommen. Wenn 2D-Linien vorliegen -> Fehler (weil keine Höhen).
    """
    xs, ys, zs = [], [], []
    geom_cnt = 0
    line_cnt = 0

    for geom in gdf.geometry:
        geom_cnt += 1
        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "LineString":
            coords = list(geom.coords)
            line_cnt += 1
            for c in coords:
                if len(c) < 3:
                    raise RuntimeError("Liniengeometrie ist 2D; es fehlt Z. Bitte 3D-Geometrie liefern oder Höhen-Spalte bereitstellen.")
                x, y, z = c[0], c[1], c[2]
                xs.append(x); ys.append(y); zs.append(z)

        elif geom.geom_type == "MultiLineString":
            for ls in geom.geoms:
                coords = list(ls.coords)
                line_cnt += 1
                for c in coords:
                    if len(c) < 3:
                        raise RuntimeError("Liniengeometrie ist 2D; es fehlt Z. Bitte 3D-Geometrie liefern oder Höhen-Spalte bereitstellen.")
                    x, y, z = c[0], c[1], c[2]
                    xs.append(x); ys.append(y); zs.append(z)

        else:
            # andere Geometrietypen ignorieren
            continue

    if len(xs) == 0:
        raise RuntimeError("Aus den Linien konnten keine Stützpunkte extrahiert werden.")

    coords_xy = np.column_stack([np.asarray(xs, dtype=np.float64),
                                 np.asarray(ys, dtype=np.float64)])
    heights = np.asarray(zs, dtype=np.float32)
    return coords_xy, heights, geom_cnt, line_cnt

# =========================
# Main
# =========================
def main():
    # Layer wählen / lesen
    lyr = layer_name or auto_pick_layer(gpkg_path)
    print(f"Öffne: {gpkg_path}  |  Layer: {lyr or '(Standard)'}")
    gdf = read_any_layer(gpkg_path, layer=lyr)
    print(f"Features: {len(gdf):,}  |  Geometrietypen: {set(gdf.geometry.geom_type)}  |  CRS: {gdf.crs}")

    # Nach WGS84 reprojizieren (KDTree wird in EPSG:4326 gebaut)
    gdf = ensure_wgs84(gdf)
    print(f"Reprojiziert nach EPSG:4326 (falls nötig).")

    # Punkt- oder Linien-Layer?
    geom_types = set(gdf.geometry.geom_type)
    if geom_types <= {"Point"}:
        print("Erkenne Punkt-Layer → extrahiere Koordinaten …")
        coords_deg, heights, hsrc = extract_points_from_points_gdf(gdf, height_col=height_col)
        print(f"Punkte extrahiert: {coords_deg.shape[0]:,}  |  Höhenquelle: {hsrc}")

    elif geom_types & {"LineString", "MultiLineString"}:
        print("Erkenne Linien-Layer → extrahiere ALLE Scheitelpunkte (inkl. Z) …")
        coords_deg, heights, geom_cnt, line_cnt = extract_points_from_lines_gdf(gdf)
        print(f"Geometrien: {geom_cnt:,}  |  Liniensegmente: {line_cnt:,}  |  extrahierte Stützpunkte: {coords_deg.shape[0]:,}")
        print("Höhenquelle: Geometrie-Z")

    else:
        raise RuntimeError(f"Nicht unterstützte Geometrietypen: {geom_types}")

    # KDTree bauen (in Grad – für NN-Suche ausreichend)
    tree = cKDTree(coords_deg, leafsize=leafsize,
                   balanced_tree=balanced_tree, compact_nodes=compact_nodes)
    print(f"KDTree gebaut: {tree.n} Punkte, leafsize={leafsize}")

    # Speichern – Koordinaten + Höhen genügen; Tree bei Bedarf neu aufbauen
    np.savez_compressed(
        output_path,
        coords=coords_deg.astype(np.float32, copy=False),
        heights=heights.astype(np.float32, copy=False),
        crs="EPSG:4326"
    )
    print(f"Gespeichert: {output_path}")
    print("Hinweis: Zum Abfragen später den KDTree aus 'coords' neu bauen und den Index in 'heights' verwenden.")

if __name__ == "__main__":
    main()
