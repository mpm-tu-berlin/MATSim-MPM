from collections import defaultdict

import numpy as np
import xml.etree.ElementTree as ET
import xml.dom.minidom as md
import gzip
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree as KDTree  # cKDTree ist schneller als KDTree und ausreichend für .query()
import matplotlib.pyplot as plt
from shapely.geometry import LineString
from shapely.ops import linemerge
from tqdm import tqdm
import contextily as ctx
from pyproj import Transformer

# --- Schnellere Projektion & KDTree-Vektorabfrage ---
TF_4326_TO_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
TF_3857_TO_4326 = Transformer.from_crs(3857, 4326, always_xy=True)

def _to_4326_xy(X, Y):
    x, y = TF_3857_TO_4326.transform(X, Y)
    return x, y

def kdtree_heights_vectorized(tree: KDTree, heights: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Holt Höhen für viele Punkte auf einmal (EPSG:4326), xs/ys = lon/lat!"""
    pts = np.column_stack([xs, ys])
    _, idx = tree.query(pts, k=1)
    return heights[idx].astype(float)

# ---------- Gemeinsame Utilities ----------

def _truthy_flag(val) -> bool:
    """Interpretiert OSM-ähnliche Flag-Werte robust."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip().lower()
    return s not in ("", "no", "false", "0")

def _collect_neighbors_contiguous(df3857: gpd.GeoDataFrame,
                                  df4326: gpd.GeoDataFrame,
                                  start_idx: int,
                                  direction: int,
                                  max_dist_m: float):
    """
    Sammelt Nachbarsegmente NEBEN der Gruppe ausschließlich dann,
    wenn sie topologisch anliegen (passende u/v-Knoten).
    direction: -1 (vor der Gruppe), +1 (nach der Gruppe).
    Gibt eine Liste von Indexen in richtiger Reihenfolge zurück.
    """
    acc = 0.0
    res = []
    j = start_idx + direction

    expected = str(df4326.loc[start_idx, "u"] if direction == -1 else df4326.loc[start_idx, "v"])

    # Wir laufen im DataFrame, brechen aber ab, wenn Topologie nicht passt.
    while (j in df3857.index) and (acc < max_dist_m):
        row_m = df3857.loc[j]
        row_g = df4326.loc[j]

        if direction == -1:
            # rückwärts: require v == expected, dann expected = u
            if str(row_g.get("v")) != expected:
                break
            expected = str(row_g.get("u"))
        else:
            # vorwärts: require u == expected, dann expected = v
            if str(row_g.get("u")) != expected:
                break
            expected = str(row_g.get("v"))

        seg_len = float(row_m.get("length", row_m.geometry.length))
        res.append(j)
        acc += seg_len
        j += direction

    return res if direction == +1 else res[::-1]

def _concat_by_pos(df: gpd.GeoDataFrame, pos_list: list[int]) -> LineString:
    if not pos_list:
        return LineString()
    coords = []
    first = True
    for p in pos_list:
        line: LineString = df.loc[p, "geometry"]
        if line is None or line.is_empty:
            continue
        c = list(line.coords)
        if first:
            coords.extend(c); first = False
        else:
            if coords and c and coords[-1] == c[0]:
                coords.extend(c[1:])
            else:
                coords.extend(c)
    return LineString(coords) if coords else LineString()

def _sample_height_from_point(tree: KDTree, heights: np.ndarray, lon: float, lat: float) -> float:
    """KDTree-Abfrage mit (lon,lat)."""
    _, idx = tree.query([lon, lat], k=1)
    return float(heights[idx])

# ---------- Z-Overrides (robust) ----------

def compute_bridge_tunnel_z_overrides_strict(
        gdf_edges_detailed_4326: gpd.GeoDataFrame,
        gdf_edges_detailed_3857: gpd.GeoDataFrame,
        tree: KDTree,
        heights: np.ndarray,
        offset_m: float = 50.0,
        max_delta_m: float = 20.0,   # maximal erlaubte Abweichung ggü. DEM am Endknoten
        inner_eps_m: float = 1.0     # Fallback-Abstand innerhalb der Gruppe
) -> dict[str, float]:
    """
    Ermittelt Z-Overrides für Start/Ende jeder Bridge-/Tunnel-Gruppe.
    Nutzt topologisch angrenzende Vor-/Nachbarsegmente. Sanity-Check gegen DEM am Endknoten + 1m-Fallback.
    """
    df4326 = gdf_edges_detailed_4326.reset_index(drop=True)
    df3857 = gdf_edges_detailed_3857.reset_index(drop=True)

    overrides: dict[str, float] = {}

    def collect_groups_pos(flag_col: str):
        if flag_col not in df4326.columns: return []
        mask = df4326[flag_col].apply(_truthy_flag).to_numpy()
        if not mask.any(): return []
        true_pos = np.flatnonzero(mask)
        if true_pos.size == 0: return []
        cuts = np.where(np.diff(true_pos) != 1)[0] + 1
        return [block for block in np.split(true_pos, cuts) if block.size > 0]

    for flag in ["bridge", "tunnel"]:
        for pos_arr in collect_groups_pos(flag):
            i0 = int(pos_arr[0])
            i1 = int(pos_arr[-1])

            # Gruppen-Endpunkte (Node-IDs als Strings)
            u_start = str(df4326.loc[i0, "u"])
            v_end   = str(df4326.loc[i1, "v"])

            # Topologisch angrenzende Segmente sammeln
            before_pos = _collect_neighbors_contiguous(df3857, df4326, i0, -1, offset_m)
            after_pos  = _collect_neighbors_contiguous(df3857, df4326, i1, +1, offset_m)

            # Pfad zusammenbauen
            before_line = _concat_by_pos(df3857, before_pos)
            group_line  = _concat_by_pos(df3857, list(pos_arr))
            after_line  = _concat_by_pos(df3857, after_pos)

            parts = [l for l in (before_line, group_line, after_line) if (l and not l.is_empty)]
            if not parts:
                continue

            merged = linemerge(parts)
            if merged.geom_type == "MultiLineString":
                merged = LineString([pt for geom in merged.geoms for pt in geom.coords])

            before_len = before_line.length
            group_len  = group_line.length
            if group_len <= 0:
                continue

            # Zielpositionen
            s_start   = before_len
            s_end     = before_len + group_len
            s_before  = max(0.0,          s_start - offset_m)
            s_after   = min(merged.length, s_end   + offset_m)

            # Fallbacks, wenn keine Nachbarn vorhanden: nutze 1 m innerhalb der Gruppe
            if before_len == 0.0:
                s_before = max(0.0, s_start + min(inner_eps_m, group_len) - s_start)  # == inner_eps_m innerhalb Gruppe
            if after_line.length == 0.0:
                s_after = min(merged.length, s_end - min(inner_eps_m, group_len))

            P_before = merged.interpolate(s_before)
            P_after  = merged.interpolate(s_after)

            xb, yb = _to_4326_xy(P_before.x, P_before.y)
            xa, ya = _to_4326_xy(P_after.x,  P_after.y)

            # Sanity: DEM direkt am Gruppen-Endknoten (Start/Ende der Gruppe in 4326)
            ux, uy = list(df4326.loc[i0, "geometry"].coords)[0]
            vx, vy = list(df4326.loc[i1, "geometry"].coords)[-1]
            z_u_dem = _sample_height_from_point(tree, heights, ux, uy)
            z_v_dem = _sample_height_from_point(tree, heights, vx, vy)

            zs = kdtree_heights_vectorized(tree, heights, np.array([xb, xa]), np.array([yb, ya]))
            z_u_off, z_v_off = float(zs[0]), float(zs[1])

            def guard(z_off, z_dem, max_delta=max_delta_m):
                return z_off if abs(z_off - z_dem) <= max_delta else z_dem

            overrides[u_start] = guard(z_u_off, z_u_dem)
            overrides[v_end]   = guard(z_v_off, z_v_dem)

    return overrides


def compute_bridge_tunnel_z_overrides_fast(
        gdf_edges_detailed_4326: gpd.GeoDataFrame,
        gdf_edges_detailed_3857: gpd.GeoDataFrame,
        tree: KDTree,
        heights: np.ndarray,
        offset_m: float = 50.0,
        max_delta_m: float = 20.0,
        inner_eps_m: float = 1.0
) -> dict[str, float]:
    """
    Schnellere Variante (arbeitet in 3857), ergänzt um:
    - topologische Nachbarschaft,
    - Sanity-Check vs. DEM am Endknoten,
    - 1-m-Fallback innerhalb der Gruppe.
    """
    overrides: dict[str, float] = {}

    def collect_groups(flag_col: str):
        if flag_col not in gdf_edges_detailed_4326.columns:
            return []
        mask = gdf_edges_detailed_4326[flag_col].apply(_truthy_flag)
        if mask.sum() == 0:
            return []
        group_id = (mask != mask.shift(1)).cumsum()
        tmp = gdf_edges_detailed_4326.copy()
        tmp["_grp"] = group_id.where(mask)
        groups = []
        for gid, grp in tmp.groupby("_grp"):
            if pd.isna(gid) or len(grp) == 0:
                continue
            groups.append(grp.index.to_list())
        return groups

    def concat_lines(rows_iter):
        coords = []
        first = True
        for r in rows_iter:
            line: LineString = r.geometry
            if line is None or line.is_empty:
                continue
            c = list(line.coords)
            if first:
                coords.extend(c); first = False
            else:
                if coords and c and coords[-1] == c[0]:
                    coords.extend(c[1:])
                else:
                    coords.extend(c)
        return LineString(coords) if coords else LineString()

    for flag in ["bridge", "tunnel"]:
        for idx_list in collect_groups(flag):
            idx_list = sorted(idx_list)

            grp4326 = gdf_edges_detailed_4326.loc[idx_list]
            grp3857 = gdf_edges_detailed_3857.loc[idx_list]

            u_start = str(grp4326.iloc[0]["u"])
            v_end   = str(grp4326.iloc[-1]["v"])

            first_idx = idx_list[0]
            last_idx  = idx_list[-1]

            # topologische Nachbarn statt blindem Index-Wandern
            before_pos = _collect_neighbors_contiguous(gdf_edges_detailed_3857, gdf_edges_detailed_4326, first_idx, -1, offset_m)
            after_pos  = _collect_neighbors_contiguous(gdf_edges_detailed_3857, gdf_edges_detailed_4326, last_idx,  +1, offset_m)

            before_line = concat_lines([gdf_edges_detailed_3857.loc[j] for j in before_pos])
            group_line  = concat_lines([r for _, r in grp3857.iterrows()])
            after_line  = concat_lines([gdf_edges_detailed_3857.loc[j] for j in after_pos])

            parts = [l for l in (before_line, group_line, after_line) if (l and not l.is_empty)]
            if not parts:
                continue

            merged = linemerge(parts)
            if merged.geom_type == "MultiLineString":
                merged = LineString([pt for geom in merged.geoms for pt in geom.coords])

            before_len = before_line.length
            group_len  = group_line.length
            if group_len <= 0:
                continue

            s_start = before_len
            s_end   = before_len + group_len
            s_before = max(0.0, s_start - offset_m)
            s_after  = min(merged.length, s_end + offset_m)

            # Fallbacks innerhalb der Gruppe
            if before_len == 0.0:
                s_before = min(s_start + min(inner_eps_m, group_len), s_end)
            if after_line.length == 0.0:
                s_after = max(s_end - min(inner_eps_m, group_len), s_start)

            P_before = merged.interpolate(s_before)
            P_after  = merged.interpolate(s_after)

            xb, yb = _to_4326_xy(P_before.x, P_before.y)
            xa, ya = _to_4326_xy(P_after.x,  P_after.y)

            # DEM direkt am Gruppen-Endknoten
            ux, uy = list(grp4326.iloc[0].geometry.coords)[0]
            vx, vy = list(grp4326.iloc[-1].geometry.coords)[-1]
            z_u_dem = _sample_height_from_point(tree, heights, ux, uy)
            z_v_dem = _sample_height_from_point(tree, heights, vx, vy)

            zs = kdtree_heights_vectorized(tree, heights, np.array([xb, xa]), np.array([yb, ya]))
            z_u_off, z_v_off = float(zs[0]), float(zs[1])

            def guard(z_off, z_dem, max_delta=max_delta_m):
                return z_off if abs(z_off - z_dem) <= max_delta else z_dem

            overrides[u_start] = guard(z_u_off, z_u_dem)
            overrides[v_end]   = guard(z_v_off, z_v_dem)

    return overrides

# ---------- Laden / Plot / Split etc. (unverändert außer kleinen Kosmetik) ----------

def load_kdtree(input_path):
    """
    Lädt einen KDTree sowie Koordinaten und Höhen aus einer .npz-Datei.
    Erwartet coords in EPSG:4326 als (lon, lat)!
    """
    data = np.load(input_path)
    coords = data["coords"]      # Koordinaten im Quell-KS (EPSG:4326), Reihenfolge: lon,lat
    heights = data["heights"]    # Höhenwerte
    tree = KDTree(coords)
    print("KDTree erfolgreich geladen.")
    return tree, coords, heights

def load_local_osm_file(local_osm_input_path):
    gdf_nodes = gpd.read_file(local_osm_input_path, layer="nodes").set_crs("EPSG:4326", allow_override=True)
    gdf_edges = gpd.read_file(local_osm_input_path, layer="edges").set_crs("EPSG:4326", allow_override=True)
    return gdf_nodes, gdf_edges

def plot_edge_length_distribution(gdf_edges):
    total_links = len(gdf_edges)
    min_length = gdf_edges['length'].min()
    max_length = gdf_edges['length'].max()
    sum_length = gdf_edges['length'].sum()
    print(f"Gesamtanzahl der Links: {total_links}")
    print(f"Minimale Länge: {min_length:.0f} m")
    print(f"Maximale Länge: {max_length:.0f} m")
    print(f"Durchschnittliche Länge: {gdf_edges['length'].mean():.0f} m")
    print(f"Gesamtlänge: {sum_length:.0f} m")
    print("------------------------------")

    bins = range(0, min(5000, int(gdf_edges['length'].max())), 100)
    gdf_edges['length_bin'] = pd.cut(gdf_edges['length'], bins=bins, right=False)
    pivot = gdf_edges.groupby('length_bin', observed=False).size()
    labels = [f"<{bins[i + 1]}" for i in range(len(bins) - 1)]
    pivot.index = labels
    pivot.plot(kind='bar', legend=False)
    plt.ylabel('Anzahl der Kanten')
    plt.title('Verteilung der Kantenlängen')
    plt.tight_layout()
    plt.show()

def plot_edges(gdf_edges, title="Network Edges"):
    fig, ax = plt.subplots(figsize=(10, 10))
    gdf_edges.plot(ax=ax, linewidth=1, color='blue')
    plt.title(title)
    plt.xlabel('Longitude')
    plt.ylabel('Latitude')
    plt.grid(True)
    plt.tight_layout()
    plt.show()

def check_edges_for_bridges(gdf_edges, gdf_bridges):
    bridge_mask = gdf_edges['bridge'] == 'yes'
    bridge_edges = gdf_edges[bridge_mask]

    for idx, edge in bridge_edges.iterrows():
        osmid_str = str(edge['osmid']).strip('[]').replace(' ', '')
        osmids = [int(id) for id in osmid_str.split(',') if id]
        is_bridge = any(id in gdf_bridges['id'].values for id in osmids)
        gdf_edges.loc[idx, 'is_confirmed_bridge'] = is_bridge

    return gdf_edges

def get_nearest_height(tree, heights, point):
    distances, indices = tree.query(point, k=1)
    nearest_height = heights[indices]
    print("Nächste Höhe gefunden:", nearest_height)
    return nearest_height

def get_detailed_sequence(current_edge, gdf_edges_detailed, osmid_index):
    osmid_str = str(current_edge['osmid']).strip('[]').replace(' ', '')
    osmids = [int(id) for id in osmid_str.split(',') if id]

    detailed_edges = gpd.GeoDataFrame(columns=gdf_edges_detailed.columns).set_crs("EPSG:4326")

    candidate_indices = set()
    for osmid in osmids:
        candidate_indices.update(osmid_index.get(osmid, []))

    candidates = gdf_edges_detailed.loc[sorted(candidate_indices)]

    for _, edge in candidates.iterrows():
        if all(coord in current_edge['geometry'].coords for coord in edge['geometry'].coords):
            if (edge['geometry'] not in detailed_edges['geometry'].values and
                    str(edge['reversed']) == str(current_edge['reversed'])):
                detailed_edges = pd.concat([detailed_edges, edge.to_frame().T])

    return detailed_edges

def short_edges(gdf_edges, gdf_edges_detailed, max_allowed_length):
    def split_edge(current_edge, detailed_edges):
        """Teilt eine Kante in zwei kürzere Kanten."""
        half_length = current_edge['length'] / 2

        forward_edges = calculate_cumulative_edges(detailed_edges, half_length, forward=True)
        backward_edges = calculate_cumulative_edges(detailed_edges, half_length, forward=False)

        smaller_cum_length = min(forward_edges['length'].sum(), backward_edges['length'].sum())
        if smaller_cum_length == forward_edges['length'].sum():
            if len(backward_edges) > 1:
                backward_edges = backward_edges.iloc[:-1]
        else:
            if len(forward_edges) > 1:
                forward_edges = forward_edges.iloc[:-1]

        backward_edges = backward_edges.iloc[::-1]
        return create_split_edges(current_edge, forward_edges, backward_edges)

    def calculate_cumulative_edges(detailed_edges, target_length, forward):
        """Berechnet kumulative Kantenlängen vorwärts oder rückwärts."""
        cum_length = 0
        selected_edges = []
        edges_iter = detailed_edges.iterrows() if forward else detailed_edges.iloc[::-1].iterrows()

        for _, det_edge in edges_iter:
            selected_edges.append(det_edge)
            cum_length += det_edge['length']
            if cum_length >= target_length:
                break
        return gpd.GeoDataFrame(selected_edges)

    def create_split_edges(original_edge, forward_edges, backward_edges):
        """Erstellt zwei neue Kanten basierend auf den aufgeteilten Kanten."""
        edge1 = gpd.GeoDataFrame([{
            **original_edge,
            'u': forward_edges.iloc[0]['u'],
            'v': forward_edges.iloc[-1]['v'],
            'length': forward_edges['length'].sum(),
            'osmid': '[' + ','.join(str(id) for id in forward_edges['osmid'].explode().unique()) + ']',
            'geometry': LineString([pt for geom in forward_edges.geometry for pt in geom.coords]),
        }]).set_crs("EPSG:4326", allow_override=True)

        edge2 = gpd.GeoDataFrame([{
            **original_edge,
            'u': backward_edges.iloc[0]['u'],
            'v': backward_edges.iloc[-1]['v'],
            'length': backward_edges['length'].sum(),
            'osmid': '[' + ','.join(str(id) for id in backward_edges['osmid'].explode().unique()) + ']',
            'geometry': LineString([pt for geom in backward_edges.geometry for pt in geom.coords]),
        }]).set_crs("EPSG:4326", allow_override=True)

        return edge1, edge2

    long_edges = gdf_edges[gdf_edges['length'] > max_allowed_length]
    total_length_to_process = long_edges['length'].sum()
    if long_edges.empty:
        return gdf_edges

    pbar = tqdm(total=len(long_edges), desc="Edge Shortening", unit="edge", mininterval=0.5)
    indices_to_drop = []
    final_edges = gpd.GeoDataFrame(columns=gdf_edges.columns).set_crs("EPSG:4326")
    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs("EPSG:4326", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs("EPSG:4326", allow_override=True)

    osmid_index = defaultdict(list)

    # --- Brücken-Gruppen zusammenfassen (nur in detailed) ---
    if 'bridge' in gdf_edges_detailed.columns:
        is_bridge = gdf_edges_detailed['bridge'] == 'yes'
        group_id = (is_bridge != is_bridge.shift(1)).cumsum()
        gdf_edges_detailed['bridge_group'] = group_id.where(is_bridge)

        group_sizes = gdf_edges_detailed.groupby('bridge_group').size()
        valid_groups = group_sizes[group_sizes > 1].index

        merged_rows = []
        for gid in valid_groups:
            group = gdf_edges_detailed[gdf_edges_detailed['bridge_group'] == gid]
            merged = group.iloc[0].copy()
            merged['start'] = group.iloc[0]['start']
            merged['end'] = group.iloc[-1]['end']
            merged['geometry'] = LineString([
                group.iloc[0]['geometry'].coords[0],
                group.iloc[-1]['geometry'].coords[-1]
            ])
            merged['length'] = group['length'].sum()
            merged['osmid'] = group.iloc[0]['osmid']
            merged['v'] = group.iloc[-1]['v']
            merged_rows.append((group.index[0], merged))

        drop_indices = gdf_edges_detailed[gdf_edges_detailed['bridge_group'].isin(valid_groups)].index
        keep_indices = [idx for idx, _ in merged_rows]
        drop_indices = drop_indices.difference(keep_indices)
        gdf_edges_detailed = gdf_edges_detailed.drop(drop_indices)

        for idx, row in merged_rows:
            gdf_edges_detailed.loc[idx] = row

        gdf_edges_detailed = gdf_edges_detailed.drop(columns='bridge_group')

    # --- Tunnel-Gruppen zusammenfassen (nur in detailed) ---
    if 'tunnel' in gdf_edges_detailed.columns:
        is_tunnel = gdf_edges_detailed['tunnel'] == 'yes'
        group_id = (is_tunnel != is_tunnel.shift(1)).cumsum()
        gdf_edges_detailed['tunnel_group'] = group_id.where(is_tunnel)

        group_sizes = gdf_edges_detailed.groupby('tunnel_group').size()
        valid_groups = group_sizes[group_sizes > 1].index

        merged_rows = []
        for gid in valid_groups:
            group = gdf_edges_detailed[gdf_edges_detailed['tunnel_group'] == gid]
            merged = group.iloc[0].copy()
            merged['start'] = group.iloc[0]['start']
            merged['end'] = group.iloc[-1]['end']
            merged['geometry'] = LineString([
                group.iloc[0]['geometry'].coords[0],
                group.iloc[-1]['geometry'].coords[-1]
            ])
            merged['length'] = group['length'].sum()
            merged['osmid'] = group.iloc[0]['osmid']
            merged['v'] = group.iloc[-1]['v']
            merged_rows.append((group.index[0], merged))

        drop_indices = gdf_edges_detailed[gdf_edges_detailed['tunnel_group'].isin(valid_groups)].index
        keep_indices = [idx for idx, _ in merged_rows]
        drop_indices = drop_indices.difference(keep_indices)
        gdf_edges_detailed = gdf_edges_detailed.drop(drop_indices)

        for idx, row in merged_rows:
            gdf_edges_detailed.loc[idx] = row

        gdf_edges_detailed = gdf_edges_detailed.drop(columns='tunnel_group')

    # --- Index für detailed osmid -> Zeilen ---
    for idx, row in gdf_edges_detailed.iterrows():
        osmids = [int(x) for x in str(row['osmid']).strip('[]').replace(' ', '').split(',') if x]
        for osmid in osmids:
            osmid_index[osmid].append(idx)

    # --- Lange Kanten iterativ halbieren ---
    for idx, edge in long_edges.iterrows():
        edges_to_process = [edge]
        processed_edges = set()
        processed_length = 0
        while edges_to_process:
            current_edge = edges_to_process.pop(0)
            edge_id = (current_edge['u'], current_edge['v'])
            if edge_id in processed_edges:
                final_edges = pd.concat(
                    [final_edges, gpd.GeoDataFrame([current_edge]).set_crs("EPSG:4326", allow_override=True)],
                    ignore_index=True
                )
                processed_length += float(current_edge['length'])
                edges_to_process = [e for e in edges_to_process if not e.equals(current_edge)]
                continue

            processed_edges.add(edge_id)
            detailed_edges = get_detailed_sequence(current_edge, gdf_edges_detailed, osmid_index)
            if detailed_edges.empty:
                current_edge_gdf = gpd.GeoDataFrame([current_edge]).set_crs("EPSG:4326", allow_override=True)
                final_edges = pd.concat([final_edges, current_edge_gdf], ignore_index=True)
                continue
            edge1, edge2 = split_edge(current_edge, detailed_edges)
            for e2 in [edge1, edge2]:
                if float(e2['length'].iloc[0]) <= max_allowed_length:
                    final_edges = pd.concat([final_edges, e2], ignore_index=True)
                    processed_length = processed_length + e2['length'].iloc[0]
                else:
                    edges_to_process.append(e2.iloc[0])

        indices_to_drop.append(idx)
        pbar.update(1)  # pro fertig behandelter Original-Kante

    gdf_edges = gdf_edges.drop(indices_to_drop)
    pbar.close()

    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs("EPSG:4326", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs("EPSG:4326", allow_override=True)

    if not final_edges.empty:
        final_edges = final_edges.reindex(columns=gdf_edges.columns, fill_value=pd.NA)
        final_edges = final_edges.set_crs(gdf_edges.crs or "EPSG:4326", allow_override=True)
        gdf_edges = pd.concat([gdf_edges, final_edges], ignore_index=True)

    return gdf_edges

# --------------------------- No-Z-Knoten ---------------------------

def _collect_endpoints_for_flag(gdf_edges_detailed: gpd.GeoDataFrame, flag_col: str) -> set:
    """Start/Ende von Brücken-/Tunnelgruppen als Set von Node-IDs (Strings)."""
    if flag_col not in gdf_edges_detailed.columns:
        return set()

    mask = gdf_edges_detailed[flag_col].apply(_truthy_flag)
    if mask.sum() == 0:
        return set()

    group_id = (mask != mask.shift(1)).cumsum()
    gtmp = gdf_edges_detailed.copy()
    gtmp["_grp"] = group_id.where(mask)

    endpoints = set()
    for gid, grp in gtmp.groupby("_grp"):
        if pd.isna(gid):
            continue
        first_u = grp.iloc[0]["u"]
        last_v = grp.iloc[-1]["v"]
        endpoints.add(str(first_u))
        endpoints.add(str(last_v))
    return endpoints

def get_nodes_without_z_from_detailed(gdf_edges_detailed: gpd.GeoDataFrame) -> set:
    """Start/Ende von Brücken- oder Tunnelabschnitten → diese Knoten ohne z exportieren."""
    bridge_endpoints = _collect_endpoints_for_flag(gdf_edges_detailed, "bridge")
    tunnel_endpoints = _collect_endpoints_for_flag(gdf_edges_detailed, "tunnel")
    return set(bridge_endpoints).union(tunnel_endpoints)

# --------------------------- Export ---------------------------

from lxml import etree as ET2  # optional
def write_matsim_network(gdf_nodes, gdf_edges, epsg_code, output_path, nodes_without_z: set = None):
    """
    Schreibt ein MATSim-Netzwerk.
    Knoten in nodes_without_z erhalten KEIN z-Attribut.
    """
    print("Schreibe Matsim-Netzwerk...")

    # CRS vereinheitlichen (Geometrien in Projektion für x/y)
    gdf_nodes = gdf_nodes.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)
    gdf_edges = gdf_edges.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)

    # OSM-IDs der Nodes bereitstellen
    if "osmid" in gdf_nodes.columns:
        osmid_series = gdf_nodes["osmid"].copy()
        osmid_series = osmid_series.apply(lambda v: (v[0] if isinstance(v, (list, tuple, np.ndarray)) else v))
    else:
        osmid_series = gdf_nodes.index.to_series()

    osmid_series = osmid_series.astype(str)

    # Lookup: osmid -> (x, y, z)
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

    # Kante-Attribute vorbereiten
    links_data = []
    for _, row in gdf_edges.iterrows():
        from_node = str(row["u"])
        to_node = str(row["v"])
        link_id = f"{from_node}-{to_node}"

        length = str(int(round(float(row.get("length", 0.0)))))

        maxspeed = row.get("maxspeed", 130)
        if maxspeed is None:
            maxspeed = 130
        elif isinstance(maxspeed, (list, tuple, np.ndarray)):
            cand = []
            for s in maxspeed:
                try:
                    cand.append(float(str(s).replace(",", ".")))
                except Exception:
                    pass
            maxspeed = max(cand) if cand else 130
        else:
            try:
                maxspeed = float(str(maxspeed).replace(",", "."))
            except Exception:
                maxspeed = 130
        freespeed = round(float(maxspeed) / 3.6, 2)

        capacity = row.get("capacity", 3000)
        try:
            capacity = str(int(capacity))
        except Exception:
            capacity = "3000"

        lanes = row.get("lanes", 1)
        if lanes is None:
            lanes = 1
        elif isinstance(lanes, (list, tuple, np.ndarray)):
            valid = []
            for l in lanes:
                try:
                    if l is not None and str(l) != "" and not pd.isna(l):
                        valid.append(float(l))
                except Exception:
                    pass
            lanes = int(max(valid)) if valid else 1
        else:
            try:
                val = float(lanes)
                lanes = 1 if pd.isna(val) else int(round(val))
            except Exception:
                lanes = 1
        permlanes = str(max(1, lanes))

        highway_type = str(row.get("highway", "unknown"))

        links_data.append({
            "id": link_id,
            "from": from_node,
            "to": to_node,
            "length": length,
            "freespeed": freespeed,
            "capacity": capacity,
            "permlanes": permlanes,
            "highway_type": highway_type
        })

    # Duplikate nach id entfernen: behalte die mit höchster freespeed
    unique_links = {}
    for link in links_data:
        lid = link["id"]
        if (lid not in unique_links) or (link["freespeed"] > unique_links[lid]["freespeed"]):
            unique_links[lid] = link

    # Netz-XML
    network = ET.Element("network")
    network.insert(1, ET.Comment("======================================================================"))
    nodes_element = ET.SubElement(network, "nodes")
    network.append(ET.Comment("======================================================================"))
    links_element = ET.SubElement(
        network, "links",
        capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75"
    )
    network.append(ET.Comment("======================================================================"))

    # Links schreiben & verwendete Nodes sammeln
    used_node_ids = set()
    for link in unique_links.values():
        if link["from"] not in node_lookup or link["to"] not in node_lookup:
            continue

        link_elem = ET.SubElement(
            links_element, "link",
            id=link["id"],
            **{
                "from": link["from"],
                "to": link["to"],
                "length": link["length"],
                "freespeed": str(link["freespeed"]),
                "capacity": link["capacity"],
                "permlanes": link["permlanes"],
                "oneway": "1",
                "modes": "car",
            }
        )
        attributes = ET.SubElement(link_elem, "attributes")
        a_speed = ET.SubElement(attributes, "attribute", name="allowed_speed", **{"class": "java.lang.Double"})
        a_speed.text = str(link["freespeed"])
        a_type = ET.SubElement(attributes, "attribute", name="type", **{"class": "java.lang.String"})
        a_type.text = link["highway_type"]

        used_node_ids.add(link["from"])
        used_node_ids.add(link["to"])

    # --- Nur verwendete Nodes schreiben; z ggf. weglassen ---
    nodes_without_z = nodes_without_z or set()

    for osm_id in used_node_ids:
        x, y, z = node_lookup[osm_id]
        node_attrs = {"id": str(osm_id), "x": f"{x}", "y": f"{y}"}
        if (z is not None) and (str(osm_id) not in nodes_without_z):
            node_attrs["z"] = f"{z}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    # pretty print & schreiben (inkl. DTD)
    xml_string = ET.tostring(network, encoding="utf-8")
    pretty_xml = md.parseString(xml_string).toprettyxml()
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write("\n".join(pretty_xml.splitlines()[1:]))

    print("Fertig:", output_path)

# --------------------------- Main ---------------------------

if __name__ == "__main__":
    kdtree_input_path = r"data\germany_kdtree_from_roads3d_epsg4326.npz"
    area = "germany"
    local_osm_input_path_simplified = f"data/{area}_simplified.gpkg"
    local_osm_input_path_detailed = f"data/{area}_detailed_sorted.gpkg"
    output_path = f"data/{area}_max_200m_long.xml.gz"
    target_epsg = 4839  # EPSG:4839 (Germany)
    max_allowed_link_lengths = [200 + i * 0 for i in range(1)]  # in meters
    BR_TU_OFFSET_M = 50.0  # Offset zur Höhenzuweisung vor&nach Brücken und Tunneln

    tree, coords, heights = load_kdtree(kdtree_input_path)
    gdf_nodes_simplified, gdf_edges_simplified = load_local_osm_file(local_osm_input_path_simplified)
    gdf_nodes_detailed, gdf_edges_detailed = load_local_osm_file(local_osm_input_path_detailed)

    # nur einmal projizieren
    gdf_edges_detailed_3857 = gdf_edges_detailed.to_crs(epsg=3857)

    # Originale detailed-Edges/Nodes einfrieren (für XY-Referenz und Gruppenbildung)
    gdf_nodes_detailed_orig  = gdf_nodes_detailed.copy(deep=True)
    gdf_edges_detailed_orig  = gdf_edges_detailed.copy(deep=True)
    gdf_edges_detailed_orig_3857 = gdf_edges_detailed_orig.to_crs(epsg=3857)

    for max_allowed_link_length in max_allowed_link_lengths:
        print(f"\nProcessing max allowed link length: {max_allowed_link_length}m")
        gdf_edges_shortened = short_edges(
            gdf_edges_simplified, gdf_edges_detailed, max_allowed_link_length
        )

        # Höheninformationen hinzufügen (nur für verwendete Nodes)
        gdf_edges_shortened["u"] = gdf_edges_shortened["u"].astype(int)
        gdf_edges_shortened["v"] = gdf_edges_shortened["v"].astype(int)
        nodes_in_shortened_edges = set(gdf_edges_shortened['u']).union(set(gdf_edges_shortened['v']))

        mask = gdf_nodes_detailed['osmid'].isin(nodes_in_shortened_edges)
        gdf_nodes_detailed_reduced = gdf_nodes_detailed[mask].copy()

        print("Start compute z_overrides …")
        z_overrides = compute_bridge_tunnel_z_overrides_strict(
            gdf_edges_detailed_4326=gdf_edges_detailed_orig,
            gdf_edges_detailed_3857=gdf_edges_detailed_orig_3857,
            tree=tree,
            heights=heights,
            offset_m=BR_TU_OFFSET_M
        )
        print(f"Done z_overrides: {len(z_overrides)} Knoten")

        # Standardhöhen für alle verwendeten Knoten
        xs = gdf_nodes_detailed_reduced.geometry.x.to_numpy()
        ys = gdf_nodes_detailed_reduced.geometry.y.to_numpy()
        gdf_nodes_detailed_reduced['height'] = kdtree_heights_vectorized(tree, heights, xs, ys)

        # Overrides anwenden (nur u/v-Knoten der kurzen Kanten)
        if "osmid" in gdf_nodes_detailed_reduced.columns:
            gdf_nodes_detailed_reduced["osmid_str"] = gdf_nodes_detailed_reduced["osmid"].astype(str)
        else:
            gdf_nodes_detailed_reduced["osmid_str"] = gdf_nodes_detailed_reduced.index.astype(str)

        mask_override = gdf_nodes_detailed_reduced["osmid_str"].isin(z_overrides.keys())
        gdf_nodes_detailed_reduced.loc[mask_override, "height"] = (
            gdf_nodes_detailed_reduced.loc[mask_override, "osmid_str"].map(z_overrides).astype(float)
        )

        # Optional: Knoten ohne z (Start/Ende Brücke/Tunnel) bestimmen und beim Export z weglassen
        # nodes_without_z = get_nodes_without_z_from_detailed(gdf_edges_detailed_orig)
        nodes_without_z = set()

        # MATSim-Netzwerk schreiben
        write_matsim_network(
            gdf_nodes_detailed_reduced,
            gdf_edges_shortened,
            target_epsg,
            output_path,
            nodes_without_z=nodes_without_z
        )
