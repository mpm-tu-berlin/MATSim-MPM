import osmnx as ox
import xml.etree.ElementTree as ET
import xml.dom.minidom as md
import gzip
import pandas as pd
import rasterio

# GeoTIFF öffnen
with rasterio.open("data/DTM Germany 50m v3b by Sonny.tif") as src:
    data = src.read(1)  # Kanal 1 = Höhenwerte
    transform = src.transform  # Affin-Transformation: Pixel <-> Weltkoordinaten
    nodata = src.nodata

    # Shape des Bildes
    height, width = data.shape

    # Beispiel: Alle Punkte durchgehen
    for row in range(height):
        for col in range(width):
            z = data[row, col]
            if z == nodata:
                continue  # Leerer Pixel
            x, y = rasterio.transform.xy(transform, row, col)
            print(f"x: {x:.2f}, y: {y:.2f}, z: {z:.2f}")

# Parameter
area = "Spandau, Berlin, Deutschland"
#highway_types = '["highway"~"motorway"]'  # motorway = Autobahn, trunk/primary = Bundesstraße
highway_types = '["highway"~"motorway|trunk|primary"]'  # motorway = Autobahn, trunk/primary = Bundesstraße

# Download street network for "area"
print("Lade OSM-Daten herunter...")
G = ox.graph.graph_from_place(query=area, network_type="drive", retain_all=True, truncate_by_edge=False, custom_filter=highway_types)
print("Netzwerk erfolgreich geladen.")

gdf_nodes, gdf_edges = ox.convert.graph_to_gdfs(G, nodes=True, edges=True, node_geometry=True, fill_edge_geometry=True)
# Koordinaten in EPSG:4839 umwandeln
gdf_nodes = gdf_nodes.to_crs(epsg=4839)
gdf_edges = gdf_edges.to_crs(epsg=4839)
print("gdf_nodes.head():\n", gdf_nodes.head())
print("gdf_edges.head():\n", gdf_edges.head())

# Root-Element und Grundstruktur erstellen (Diese Zeilen müssen VOR der Link-Verarbeitung stehen)
network = ET.Element("network")

# Trennlinien hinzufügen
comment1 = ET.Comment("======================================================================")
network.insert(1, comment1)

nodes_element = ET.SubElement(network, "nodes")

comment2 = ET.Comment("======================================================================")
network.append(comment2)

links_element = ET.SubElement(network, "links", capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75")

comment3 = ET.Comment("======================================================================")
network.append(comment3)

# Links (Edges) sammeln
links_data = []
for index, row in gdf_edges.iterrows():
    u, v, key = index
    from_node = str(u)
    to_node = str(v)
    link_id = f"{from_node}-{to_node}"
    length = str(round(row['length']))

    maxspeed = row.get('maxspeed', 130)
    if isinstance(maxspeed, list):
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
    if isinstance(lanes, list):
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

# Doppelte Links entfernen: nur den mit der höchsten Geschwindigkeit behalten
unique_links = {}
for link in links_data:
    link_id = link["id"]
    if (link_id not in unique_links) or (link["freespeed"] > unique_links[link_id]["freespeed"]):
        unique_links[link_id] = link

# Links in XML schreiben
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

# Knoten (Nodes) hinzufügen
for node_id, row in gdf_nodes.iterrows():
    node = ET.SubElement(nodes_element, "node", id=str(node_id), x=str(row['geometry'].x), y=str(row['geometry'].y))

# XML-Datei formatieren und speichern
tree = ET.ElementTree(network)
xml_string = ET.tostring(network, encoding='utf-8')
dom = md.parseString(xml_string)
pretty_xml = dom.toprettyxml()

with gzip.open("matsim_network_test.xml.gz", "wt", encoding="utf-8") as f:
    # Schreibe die XML-Deklaration
    f.write('<?xml version="1.0" ?>\n')
    # Schreibe die DOCTYPE-Zeile
    f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
    # Schreibe das eigentliche XML (ohne die XML-Deklaration, da schon geschrieben)
    pretty_xml_no_decl = "\n".join(pretty_xml.splitlines()[1:])
    f.write(pretty_xml_no_decl)

print("MATSim Netzwerk XML erfolgreich erstellt und komprimiert!")