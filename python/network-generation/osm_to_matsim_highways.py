import numpy as np
import xml.etree.ElementTree as ET
import xml.dom.minidom as md
import gzip
import pandas as pd
import geopandas as gpd
from pyproj import Transformer
from scipy.spatial import cKDTree as KDTree # cKDTree ist schneller als KDTree und ausreichend für .query()
import matplotlib.pyplot as plt
from shapely.ops import linemerge


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
    coords = data["coords"]      # Koordinaten im Quell-KS (EPSG:32632)
    heights = data["heights"]    # Höhenwerte

    # Transformation: EPSG:32632 → EPSG:4326 (Notwendig für Zuordnung zu OSM-Knoten)
    transformer = Transformer.from_crs("EPSG:32632", "EPSG:4326", always_xy=True)
    lons, lats = transformer.transform(coords[:, 0], coords[:, 1])
    coords_latlon = np.column_stack((lats, lons))

    # Erstellung KDTree
    tree = KDTree(coords_latlon)

    print("KDTree erfolgreich geladen.")
    return tree, coords, heights

def load_local_osm_file(local_osm_input_path):
    gdf_nodes = gpd.read_file(local_osm_input_path, layer="nodes").set_crs(f"EPSG:{target_epsg}", allow_override=True)
    gdf_edges = gpd.read_file(local_osm_input_path, layer="edges").set_crs(f"EPSG:{target_epsg}", allow_override=True)
    #gdf_bridges = gpd.read_file(local_osm_input_path, layer="bridges")

    return gdf_nodes, gdf_edges

def plot_edge_length_distribution(gdf_edges):
    total_links = len(gdf_edges)
    min_length = gdf_edges['length'].min()
    max_length = gdf_edges['length'].max()
    sum_length = gdf_edges['length'].sum()
    print(f"Gesamtanzahl der Links: {total_links}")
    print(f"Minimale Länge: {min_length:.0f} m")
    print(f"Maximale Länge: {max_length:.0f} m")
    print(f"Gesamtlänge: {sum_length:.0f} m")
    print("------------------------------")

    #bins = range(0, min(400, int(gdf_edges['length'].max())), 50)
    #gdf_edges['length_bin'] = pd.cut(gdf_edges['length'], bins=bins, right=False)
    #pivot = gdf_edges.groupby('length_bin', observed=False).size()
    #labels = [f"<{bins[i + 1]}" for i in range(len(bins) - 1)]
    #pivot.index = labels
    #pivot.plot(kind='bar', legend=False)
    #plt.ylabel('Anzahl der Kanten')
    #plt.title('Anzahl der Kanten pro Längenintervall (<X m)')
    #plt.tight_layout()
    #plt.show()

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

def get_detailed_sequence(current_edge, gdf_edges_detailed, target_epsg):
    osmid_str = str(current_edge['osmid']).strip('[]').replace(' ', '')
    osmids = [int(id) for id in osmid_str.split(',') if id]
    detailed_edges = gpd.GeoDataFrame(columns=gdf_edges_detailed.columns).set_crs(f"EPSG:{target_epsg}")
    if len(osmids) > 1:
        for idx, edge in gdf_edges_detailed.iterrows():
           if edge['osmid'] in osmids:
               if all(coord in current_edge['geometry'].coords for coord in edge['geometry'].coords):
                   #edge 'reversed' ist vom Typ Boolean
                   #current_edge 'reversed' ist vom Typ String
                   if edge['geometry'] not in detailed_edges['geometry'].values and edge['geometry'] not in detailed_edges['geometry'].values and str(edge['reversed']) == str(current_edge['reversed']):
                       detailed_edges = pd.concat([detailed_edges, edge.to_frame().T])

    else:
        detailed_edges = gdf_edges_detailed.iloc[0:0]

    return detailed_edges


def short_edges(gdf_edges, gdf_nodes_detailed, gdf_edges_detailed, max_allowed_length, target_epsg):
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

        return create_split_edges(current_edge, forward_edges, backward_edges, target_epsg)

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

    def create_split_edges(original_edge, forward_edges, backward_edges, target_epsg):
        """Erstellt zwei neue Kanten basierend auf den aufgeteilten Kanten."""
        edge1 = gpd.GeoDataFrame([{
            **original_edge,
            'u': forward_edges.iloc[0]['u'],
            'v': forward_edges.iloc[-1]['v'],
            'length': forward_edges['length'].sum(),
            'osmid': '[' + ','.join(str(id) for id in forward_edges['osmid'].explode().unique()) + ']',
            'geometry': linemerge(list(forward_edges.geometry))
        }]).set_crs(f"EPSG:{target_epsg}", allow_override=True)

        edge2 = gpd.GeoDataFrame([{
            **original_edge,
            'u': backward_edges.iloc[0]['u'],
            'v': backward_edges.iloc[-1]['v'],
            'length': backward_edges['length'].sum(),
            'osmid': '[' + ','.join(str(id) for id in backward_edges['osmid'].explode().unique()) + ']',
            'geometry': linemerge(list(backward_edges.geometry))
        }]).set_crs(f"EPSG:{target_epsg}", allow_override=True)
        return edge1, edge2

    long_edges = gdf_edges[gdf_edges['length'] > max_allowed_length]
    if long_edges.empty:
        return gdf_edges

    indices_to_drop = []
    final_edges = gpd.GeoDataFrame(columns=gdf_edges.columns).set_crs(f"EPSG:{target_epsg}")  # Initialize empty GeoDataFrame
    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs(f"EPSG:{target_epsg}", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs(f"EPSG:{target_epsg}", allow_override=True)

    debug_counter = 0
    for idx, edge in long_edges.iterrows():
        edges_to_process = [edge]
        processed_edges = set()
        while edges_to_process:
            current_edge = edges_to_process.pop(0)
            edge_id = (current_edge['u'], current_edge['v'])
            if edge_id in processed_edges:
                continue
            processed_edges.add(edge_id)
            detailed_edges = get_detailed_sequence(current_edge, gdf_edges_detailed, target_epsg)
            if detailed_edges.empty:
                current_edge_gdf = gpd.GeoDataFrame([current_edge]).set_crs(f"EPSG:{target_epsg}", allow_override=True)
                final_edges = pd.concat([final_edges, current_edge_gdf], ignore_index=True)
                continue
            edge1, edge2 = split_edge(current_edge, detailed_edges)
            for edge in [edge1, edge2]:
                if float(edge['length'].iloc[0]) <= max_allowed_length:
                    final_edges = pd.concat([final_edges, edge], ignore_index=True)
                else:
                    edges_to_process.append(edge.iloc[0])

        indices_to_drop.append(idx)
        debug_counter+= 1
        print(debug_counter)
    gdf_edges = gdf_edges.drop(indices_to_drop)

    # Ensure final dataframes are valid and have consistent CRS
    if not final_edges.empty and final_edges.crs is None:
        final_edges = final_edges.set_crs(f"EPSG:{target_epsg}", allow_override=True)
    if not gdf_edges.crs:
        gdf_edges = gdf_edges.set_crs(f"EPSG:{target_epsg}", allow_override=True)

    # Update gdf_edges by concatenating final_edges
    if not final_edges.empty:
        final_edges = final_edges.loc[:, final_edges.notna().any()]  # Drop all-NA columns
        gdf_edges = pd.concat([gdf_edges, final_edges], ignore_index=True)
    
    return gdf_edges


def write_matsim_network(gdf_nodes, gdf_edges, epsg_code, output_path):
    print("Schreibe Matsim-Netzwerk...")
    gdf_nodes = gdf_nodes.to_crs(epsg=epsg_code)
    gdf_edges = gdf_edges.to_crs(epsg=epsg_code)

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
    #kdtree_input_path = "data/kdtree_germany_dtm50m_1m_acc.npz"
    local_osm_input_path_simplified = "test_Frankfurt oder_simplified.gpkg"
    local_osm_input_path_detailed = "test_Frankfurt oder_detailed_sorted.gpkg"
    output_path = "test.xml.gz"
    target_epsg = 4839  # EPSG:4839 is the EPSG code for Germany
    max_allowed_link_lengths = [1000 + i * 0 for i in range(1)]  # in meters

    #tree, coords, heights = load_kdtree(kdtree_input_path)
    gdf_nodes_simplified, gdf_edges_simplified = load_local_osm_file(local_osm_input_path_simplified)
    gdf_nodes_detailed, gdf_edges_detailed = load_local_osm_file(local_osm_input_path_detailed)

    #gdf_edges_simplified = gdf_edges_simplified.set_crs(f"EPSG:{target_epsg}", allow_override=True)

    plot_edge_length_distribution(gdf_edges_simplified)
    plot_edge_length_distribution(gdf_edges_detailed)

    for max_allowed_link_length in max_allowed_link_lengths:
        print(f"\nProcessing max allowed link length: {max_allowed_link_length}m")
        gdf_edges_shortened = short_edges(
            gdf_edges_simplified, gdf_nodes_detailed, gdf_edges_detailed, max_allowed_link_length, target_epsg
        )
        plot_edge_length_distribution(gdf_edges_shortened)

    #plot_edges(gdf_edges_simplified, title="Network Edges simplified")
    #plot_edges(gdf_edges_detailed, title="Network Edges detailed")
    #check_edges_for_bridges(gdf_edges, gdf_bridges)
    #gdf_nodes['height'] = gdf_nodes.apply(
    #    lambda row: get_nearest_height(tree, heights, [row.geometry.y, row.geometry.x]),
    #    axis=1
    #)
    #write_matsim_network(gdf_nodes, gdf_edges, target_epsg, output_path)