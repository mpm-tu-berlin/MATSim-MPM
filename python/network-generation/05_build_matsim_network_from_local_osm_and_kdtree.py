from collections import defaultdict

import numpy as np
import xml.etree.ElementTree as ET
import xml.dom.minidom as md
import gzip
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree as KDTree # cKDTree ist schneller als KDTree und ausreichend für .query()
import matplotlib.pyplot as plt
from shapely.geometry.linestring import LineString
from tqdm import tqdm
import contextily as ctx


def load_kdtree(input_path):
    """
    Lädt einen KDTree sowie Koordinaten und Höhen aus einer .npz-Datei.

    Args:
        input_path (str): Pfad zur .npz-Datei mit 'coords' und 'heights'.

    Returns:
        tuple: (KDTree, coords, heights)
    """
    # Daten laden
    data = np.load(input_path)
    coords = data["coords"]      # Koordinaten im Quell-KS (EPSG:4326)
    heights = data["heights"]    # Höhenwerte

    # Erstellung KDTree
    tree = KDTree(coords)

    print("KDTree erfolgreich geladen.")
    return tree, coords, heights

def load_local_osm_file(local_osm_input_path):
    gdf_nodes = gpd.read_file(local_osm_input_path, layer="nodes").set_crs(f"EPSG:4326", allow_override=True)
    gdf_edges = gpd.read_file(local_osm_input_path, layer="edges").set_crs(f"EPSG:4326", allow_override=True)

    #Plot der Kanten und Knoten in OSM
    #fig, ax = plt.subplots(figsize=(12, 12))
    #gdf_edges.to_crs(epsg=3857).plot(ax=ax, linewidth=1, color='blue', label="Kanten")
    #gdf_nodes.to_crs(epsg=3857).plot(ax=ax, color='red', markersize=10, label="Knoten")
    #ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik)
    #plt.title("Network Edges mit OSM-Hintergrund")
    #plt.xlabel('Longitude')
    #plt.ylabel('Latitude')
    #plt.legend()
    #plt.tight_layout()
    #plt.show()

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
        # Entferne eckige Klammern und Leerzeichen, dann splitte an Kommas
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

    # Leeres GeoDataFrame mit passender CRS
    detailed_edges = gpd.GeoDataFrame(columns=gdf_edges_detailed.columns).set_crs(f"EPSG:4326")

    #if len(osmids) > 1:
    candidate_indices = set()
    for osmid in osmids:
        candidate_indices.update(osmid_index.get(osmid, []))

    # Nur die relevanten Zeilen aus dem detaillierten Netzwerk holen
    candidates = gdf_edges_detailed.loc[sorted(candidate_indices)]

    for _, edge in candidates.iterrows():
        if all(coord in current_edge['geometry'].coords for coord in edge['geometry'].coords):
            if (
                    edge['geometry'] not in detailed_edges['geometry'].values
                    and str(edge['reversed']) == str(current_edge['reversed'])
            ):
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
        }]).set_crs(f"EPSG:4326", allow_override=True)

        edge2 = gpd.GeoDataFrame([{
            **original_edge,
            'u': backward_edges.iloc[0]['u'],
            'v': backward_edges.iloc[-1]['v'],
            'length': backward_edges['length'].sum(),
            'osmid': '[' + ','.join(str(id) for id in backward_edges['osmid'].explode().unique()) + ']',
            'geometry': LineString([pt for geom in backward_edges.geometry for pt in geom.coords]),
        }]).set_crs(f"EPSG:4326", allow_override=True)

        return edge1, edge2

    #debug_counter = 0
    long_edges = gdf_edges[gdf_edges['length'] > max_allowed_length]
    total_length_to_process = long_edges['length'].sum()
    if long_edges.empty:
        return gdf_edges
    # Fortschrittsbalken basierend auf Gesamtlänge
    pbar = tqdm(total=total_length_to_process, desc="Edge Shortening", unit="m", mininterval=1, maxinterval=1)
    indices_to_drop = []
    final_edges = gpd.GeoDataFrame(columns=gdf_edges.columns).set_crs(f"EPSG:4326")  # Initialize empty GeoDataFrame
    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs(f"EPSG:4326", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs(f"EPSG:4326", allow_override=True)

    osmid_index = defaultdict(list)



    # Kürze alle Brücken in gdf_edges_detailed auf ein Element
    if 'bridge' in gdf_edges_detailed.columns:
        # Schritt 1: Brücken markieren
        is_bridge = gdf_edges_detailed['bridge'] == 'yes'

        # Schritt 2: Gruppenbildung durch Erkennung von Unterbrechungen
        group_id = (is_bridge != is_bridge.shift(1)).cumsum()
        gdf_edges_detailed['bridge_group'] = group_id.where(is_bridge)

        # Schritt 3: Finde alle Gruppen mit mehr als einem Element
        group_sizes = gdf_edges_detailed.groupby('bridge_group').size()
        valid_groups = group_sizes[group_sizes > 1].index

        # Neue Zeilen speichern
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

        # Drop alte Gruppen-Zeilen (alle außer die erste der Gruppe)
        drop_indices = gdf_edges_detailed[gdf_edges_detailed['bridge_group'].isin(valid_groups)].index
        keep_indices = [idx for idx, _ in merged_rows]
        drop_indices = drop_indices.difference(keep_indices)
        gdf_edges_detailed = gdf_edges_detailed.drop(drop_indices)

        # Füge zusammengefasste Zeilen ein
        for idx, row in merged_rows:
            gdf_edges_detailed.loc[idx] = row

        # Aufräumen
        gdf_edges_detailed = gdf_edges_detailed.drop(columns='bridge_group')





    # Kürze alle Tunnel in gdf_edges_detailed auf ein Element
    if 'tunnel' in gdf_edges_detailed.columns:
        # Schritt 1: Tunnel markieren
        is_tunnel = gdf_edges_detailed['tunnel'] == 'yes'

        # Schritt 2: Gruppenbildung durch Erkennung von Unterbrechungen
        group_id = (is_tunnel != is_tunnel.shift(1)).cumsum()
        gdf_edges_detailed['tunnel_group'] = group_id.where(is_tunnel)

        # Schritt 3: Finde alle Gruppen mit mehr als einem Element
        group_sizes = gdf_edges_detailed.groupby('tunnel_group').size()
        valid_groups = group_sizes[group_sizes > 1].index

        # Neue Zeilen speichern
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

        # Drop alte Gruppen-Zeilen (alle außer die erste der Gruppe)
        drop_indices = gdf_edges_detailed[gdf_edges_detailed['tunnel_group'].isin(valid_groups)].index
        keep_indices = [idx for idx, _ in merged_rows]
        drop_indices = drop_indices.difference(keep_indices)
        gdf_edges_detailed = gdf_edges_detailed.drop(drop_indices)

        # Füge zusammengefasste Zeilen ein
        for idx, row in merged_rows:
            gdf_edges_detailed.loc[idx] = row

        # Aufräumen
        gdf_edges_detailed = gdf_edges_detailed.drop(columns='tunnel_group')








    for idx, row in gdf_edges_detailed.iterrows():
        # osmid ist z. B. "[12345, 67890]"
        osmids = [int(x) for x in str(row['osmid']).strip('[]').replace(' ', '').split(',') if x]
        for osmid in osmids:
            osmid_index[osmid].append(idx)


    for idx, edge in long_edges.iterrows():
        edges_to_process = [edge]
        processed_edges = set()
        processed_length = 0
        while edges_to_process:
            current_edge = edges_to_process.pop(0)
            edge_id = (current_edge['u'], current_edge['v'])
            if edge_id in processed_edges:
                final_edges = pd.concat([final_edges, gpd.GeoDataFrame([current_edge])], ignore_index=True)
                processed_length = processed_length + edge['length'].iloc[0]
                edges_to_process = [e for e in edges_to_process if not e.equals(current_edge)]
                continue
            processed_edges.add(edge_id)
            detailed_edges = get_detailed_sequence(current_edge, gdf_edges_detailed, osmid_index)
            if detailed_edges.empty:
                current_edge_gdf = gpd.GeoDataFrame([current_edge]).set_crs(f"EPSG:4326", allow_override=True)
                final_edges = pd.concat([final_edges, current_edge_gdf], ignore_index=True)
                continue
            edge1, edge2 = split_edge(current_edge, detailed_edges)
            for edge in [edge1, edge2]:
                if float(edge['length'].iloc[0]) <= max_allowed_length:
                    final_edges = pd.concat([final_edges, edge], ignore_index=True)
                    processed_length = processed_length + edge['length'].iloc[0]
                else:
                    edges_to_process.append(edge.iloc[0])

        indices_to_drop.append(idx)

        pbar.update(round(processed_length))
    gdf_edges = gdf_edges.drop(indices_to_drop)
    pbar.close()

    # Ensure final dataframes are valid and have consistent CRS
    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs(f"EPSG:4326", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs(f"EPSG:4326", allow_override=True)

    # Update gdf_edges by concatenating final_edges
    if not final_edges.empty:
        final_edges = final_edges.loc[:, final_edges.notna().any()]  # Drop all-NA columns
        gdf_edges = pd.concat([gdf_edges, final_edges], ignore_index=True)

    return gdf_edges

def write_matsim_network(gdf_nodes, gdf_edges, epsg_code, output_path):
    print("Schreibe Matsim-Netzwerk...")

    gdf_nodes = gdf_nodes.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)
    gdf_edges = gdf_edges.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)

    network = ET.Element("network")
    comment1 = ET.Comment("======================================================================")
    network.insert(1, comment1)
    nodes_element = ET.SubElement(network, "nodes")
    comment2 = ET.Comment("======================================================================")
    network.append(comment2)
    links_element = ET.SubElement(network, "links", capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75")
    comment3 = ET.Comment("======================================================================")
    network.append(comment3)

    links_data = []
    for _, row in gdf_edges.iterrows():
        # Verwenden Sie die 'u' und 'v' Spalten aus dem DataFrame
        from_node = str(row['u'])
        to_node = str(row['v'])
        link_id = f"{from_node}-{to_node}"
        length = str(round(row['length']))

        maxspeed = row.get('maxspeed', 130)
        if maxspeed is None:
            maxspeed = 400
        elif isinstance(maxspeed, list):
            try:
                maxspeed = max(float(speed) for speed in maxspeed if str(speed).replace('.','',1).isdigit())
            except ValueError:
                maxspeed = 130
        elif isinstance(maxspeed, str):
            try:
                maxspeed = float(maxspeed)
            except ValueError:
                maxspeed = 130

        freespeed = round(float(maxspeed) / 3.6, 2)

        capacity = str(int(row.get('capacity', 3000)))
        lanes = row.get('lanes', 1)
        if lanes is None:
            lanes = 1
        elif isinstance(lanes, list):
            valid_lanes = [l for l in lanes if l is not None and str(l).replace('.','',1).isdigit()]
            lanes = max(valid_lanes) if valid_lanes else 1
        elif isinstance(lanes, (float, str)):
            try:
                lanes = float(lanes)
                if pd.isna(lanes):
                    lanes = 1
                else:
                    lanes = int(lanes)
            except (ValueError, TypeError):
                lanes = 1
        lanes = str(max(1, int(lanes)))

        highway_type = str(row.get('highway', 'unknown'))

        links_data.append({
            "id": link_id,
            "from": from_node,
            "to": to_node,
            "length": length,
            "freespeed": freespeed,
            "capacity": capacity,
            "permlanes": lanes,
            "highway_type": highway_type
        })


    unique_links = {}
    for link in links_data:
        link_id = link["id"]
        if (link_id not in unique_links) or (link["freespeed"] > unique_links[link_id]["freespeed"]):
            unique_links[link_id] = link

    for link in unique_links.values():
        link_elem = ET.SubElement(
            links_element, "link",
            id=link["id"],
            **{"from": link["from"], "to": link["to"], "length": link["length"],
               "freespeed": str(link["freespeed"]), "capacity": link["capacity"],
               "permlanes": link["permlanes"], "oneway": "1", "modes": "car"}
        )
        attributes = ET.SubElement(link_elem, "attributes")
        attribute_speed = ET.SubElement(attributes, "attribute", name="allowed_speed", **{"class": "java.lang.Double"})
        attribute_speed.text = str(link["freespeed"])
        attribute_type = ET.SubElement(attributes, "attribute", name="type", **{"class": "java.lang.String"})
        attribute_type.text = link["highway_type"]

    for node_id, row in gdf_nodes.iterrows():
        node = ET.SubElement(nodes_element, "node", id=str(node_id), x=str(row['geometry'].x), y=str(row['geometry'].y), z=str(row['height']))

    xml_string = ET.tostring(network, encoding='utf-8')
    dom = md.parseString(xml_string)
    pretty_xml = dom.toprettyxml()

    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        pretty_xml_no_decl = "\n".join(pretty_xml.splitlines()[1:])
        f.write(pretty_xml_no_decl)

    pass


if __name__ == "__main__":
    kdtree_input_path = "data/kdtree_germany_20m_epsg4326.npz"
    area = "brandenburg"
    local_osm_input_path_simplified = f"data/{area}_simplified.gpkg"
    local_osm_input_path_detailed = f"data/{area}_detailed_sorted.gpkg"
    output_path = f"data/{area}_max_100m_long.xml.gz"
    target_epsg = 4839  # EPSG:4839 is the EPSG code for Germany
    max_allowed_link_lengths = [1000 + i * 0 for i in range(1)]  # in meters

    tree, coords, heights = load_kdtree(kdtree_input_path)
    gdf_nodes_simplified, gdf_edges_simplified = load_local_osm_file(local_osm_input_path_simplified)
    gdf_nodes_detailed, gdf_edges_detailed = load_local_osm_file(local_osm_input_path_detailed)

    #plot_edge_length_distribution(gdf_edges_simplified)
    #plot_edge_length_distribution(gdf_edges_detailed)

    for max_allowed_link_length in max_allowed_link_lengths:
        print(f"\nProcessing max allowed link length: {max_allowed_link_length}m")
        gdf_edges_shortened = short_edges(
            gdf_edges_simplified, gdf_edges_detailed, max_allowed_link_length
        )
        #plot_edge_length_distribution(gdf_edges_shortened)
        #plot_edges(gdf_edges_shortened)

        #plot_edges(gdf_edges_simplified, title="Network Edges simplified")
        #plot_edges(gdf_edges_detailed, title="Network Edges detailed")

        # Höheninformationen hinzufügen
        # # Entferne alle Knoten, deren osmid nicht in 'u' oder 'v' von gdf_edges_shortened vorkommt
        #print(gdf_edges_shortened.dtypes)
        #print(gdf_nodes_detailed.dtypes)
        gdf_edges_shortened["u"] = gdf_edges_shortened["u"].astype(int)
        gdf_edges_shortened["v"] = gdf_edges_shortened["v"].astype(int)
        #print(gdf_edges_shortened.dtypes)
        nodes_in_shortened_edges = set(gdf_edges_shortened['u']).union(set(gdf_edges_shortened['v']))

        nodes_set = set(nodes_in_shortened_edges)
        mask = gdf_nodes_detailed['osmid'].isin(nodes_in_shortened_edges)
        gdf_nodes_detailed_reduced = gdf_nodes_detailed[mask]

        # Plot der Kanten und Knoten in OSM zur Überprüfung
        #fig, ax = plt.subplots(figsize=(12, 12))
        #gdf_edges_shortened.to_crs(epsg=3857).plot(ax=ax, linewidth=1, color='blue', label="Kanten")
        #gdf_nodes_detailed_reduced.to_crs(epsg=3857).plot(ax=ax, color='red', markersize=10, label="Knoten")
        #ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik)
        #plt.title("Network Edges mit OSM-Hintergrund")
        #plt.xlabel('Longitude')
        #plt.ylabel('Latitude')
        #plt.legend()
        #plt.tight_layout()
        #plt.show()


        gdf_nodes_detailed_reduced['height'] = gdf_nodes_detailed_reduced.apply(
            lambda row: get_nearest_height(tree, heights, [row.geometry.x, row.geometry.y]),
            axis=1
        )
        # Plot der Kanten und Knoten in OSM zur Überprüfung
        fig, ax = plt.subplots(figsize=(12, 12))
        gdf_edges_shortened.to_crs(epsg=3857).plot(ax=ax, linewidth=1, color='blue', label="Kanten")
        gdf_nodes_detailed_reduced.to_crs(epsg=3857).plot(ax=ax, color='red', markersize=10, label="Knoten")
        ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik)
        plt.title("Network Edges mit OSM-Hintergrund")
        plt.xlabel('Longitude')
        plt.ylabel('Latitude')
        plt.legend()
        plt.tight_layout()
        plt.show()

        plot_edges(gdf_edges_shortened, title="Netzwerk (gekürzte Kanten)")
        #MATSim-Netzwerk schreiben
        write_matsim_network(gdf_nodes_detailed_reduced, gdf_edges_shortened, target_epsg, output_path)