import osmnx as ox
import pandas as pd
from tqdm import tqdm

def get_first_edge(gdf):
    """Gibt die erste Zeile und deren Index zurück."""
    index = gdf.index[0]
    row = gdf.loc[index]
    return index, row

def find_matching_osmids(first_coords, all_coords, gdf_detailed):
    return gdf_detailed[
        (gdf_detailed['start'] == first_coords) &
        (gdf_detailed['end'].isin(all_coords))
        ]

def filter_detailed_edges(gdf_detailed, osmid, reversed_val):
    if isinstance(reversed_val, list):
        return gdf_detailed[gdf_detailed['osmid'] == osmid]
    else:
        return gdf_detailed[
            (gdf_detailed['osmid'] == osmid) &
            (gdf_detailed['reversed'] == reversed_val)
            ]


if __name__ == "__main__":
    area = ("Brandenburg, Germany")
    highway_types = '["highway"~"motorway|trunk|primary"]'
    output_file_simplified = f"data/test_{area.split(',')[0].lower()}_simplified"
    output_file_detailed_sorted= f"data/test_{area.split(',')[0].lower()}_detailed_sorted"
    #------------------------------------------------------

    print(f"Lade vereinfachtes Straßennetz für: {area}")
    G = ox.graph.graph_from_place(
        query=area,
        network_type="drive",
        simplify=True, # Vereinfachte Kanten
        retain_all=False,
        truncate_by_edge=False,
        custom_filter=highway_types
    )
    print("Konvertiere vereinfachtes Straßennetzwerk zu GeoDataFrames...")
    gdf_nodes_simplified, gdf_edges_simplified = ox.convert.graph_to_gdfs(
        G,
        nodes=True,
        edges=True,
        node_geometry=True,
        fill_edge_geometry=True
    )

    # Projektion auf WGS84 (EPSG 4326)
    gdf_nodes_simplified = gdf_nodes_simplified.to_crs(epsg=4326)
    gdf_edges_simplified = gdf_edges_simplified.to_crs(epsg=4326)

    # Begrenze die Nachkommastellen der Geometrie-Koordinaten auf 5 und die Länge auf 1 Nachkommastelle
    gdf_nodes_simplified['geometry'] = gdf_nodes_simplified['geometry'].apply(
        lambda geom: geom.simplify(0) if geom.is_empty else geom
    ).apply(
        lambda geom: type(geom)([(round(x, 5), round(y, 5)) for x, y in geom.coords]) if geom.geom_type == "Point" else geom
    )

    gdf_edges_simplified['geometry'] = gdf_edges_simplified['geometry'].apply(
        lambda geom: geom.simplify(0) if geom.is_empty else geom
    ).apply(
        lambda geom: type(geom)([(round(x, 5), round(y, 5)) for x, y in geom.coords]) if geom.geom_type == "LineString" else geom
    )

    gdf_edges_simplified['length'] = gdf_edges_simplified['length'].apply(
        lambda length: round(length, 1)
    )

    # Ändere die Spalten 'oneway' & 'reversed' für Einträge mit 'oneway' == False und wenn sie eine Liste sind ([True, False]), weil sie doppelt geführt würden
    oneway_mask = (
            (gdf_edges_simplified['oneway'] == False) &
            gdf_edges_simplified['reversed'].apply(lambda x: isinstance(x, list))
    )
    matching_indices = gdf_edges_simplified.index[oneway_mask].tolist()
    gdf_edges_simplified.loc[oneway_mask, 'oneway'] = True
    gdf_edges_simplified.loc[oneway_mask, 'reversed'] = False

    processed_indices = []
    for index in matching_indices:
        if gdf_edges_simplified.at[index, 'osmid'] not in processed_indices:
            gdf_edges_simplified.loc[index, 'oneway'] = True
            gdf_edges_simplified.loc[index, 'reversed'] = False
            processed_indices.append(gdf_edges_simplified.at[index, 'osmid'])
        else:
            gdf_edges_simplified.loc[index, 'oneway'] = True
            gdf_edges_simplified.loc[index, 'reversed'] = True

    matching_indices.clear()

    gdf_edges_simplified_final = gdf_edges_simplified.copy()

    #------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #------------------------------------------------------------------------------------------------------------------------------------------------------------------

    print(f"Lade detailliertes Straßennetz für: {area}")
    G = ox.graph.graph_from_place(
        query=area,
        network_type="drive",
        simplify=False,  #Detailiert Kanten
        retain_all=True,
        truncate_by_edge=False,    # weil weitere Informationen zu jeder vereinfachten Kante benötigt werden
        custom_filter=highway_types
    )
    print("Konvertiere vereinfachtes Straßennetzwerk zu GeoDataFrames...")
    gdf_nodes_detailed, gdf_edges_detailed = ox.convert.graph_to_gdfs(
        G,
        nodes=True,
        edges=True,
        node_geometry=True,
        fill_edge_geometry=True
    )

    gdf_nodes_detailed = gdf_nodes_detailed.to_crs(epsg=4326)
    gdf_edges_detailed = gdf_edges_detailed.to_crs(epsg=4326)

    # Begrenze die Nachkommastellen der Geometrie-Koordinaten auf 5
    gdf_nodes_detailed['geometry'] = gdf_nodes_detailed['geometry'].apply(
        lambda geom: geom.simplify(0) if geom.is_empty else geom
    ).apply(
        lambda geom: type(geom)(
            [(round(x, 5), round(y, 5)) for x, y in geom.coords]) if geom.geom_type == "Point" else geom
    )

    gdf_edges_detailed['geometry'] = gdf_edges_detailed['geometry'].apply(
        lambda geom: geom.simplify(0) if geom.is_empty else geom
    ).apply(
        lambda geom: type(geom)(
            [(round(x, 5), round(y, 5)) for x, y in geom.coords]) if geom.geom_type == "LineString" else geom
    )

    gdf_edges_detailed['length'] = gdf_edges_detailed['length'].apply(
        lambda length: round(length, 1)
    )

    #------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #------------------------------------------------------------------------------------------------------------------------------------------------------------------
    total_detailed_length = gdf_edges_simplified['length'].sum()
    print(f"Gesamtlänge aller Elemente in gdf_edges_simplified: {total_detailed_length}")
    print(f"Gesamtlänge aller Elemente in gdf_edges_detailed: {gdf_edges_detailed['length'].sum()}")
    total_current_length = 0

    edge_mask_to_be_deleted = []  # Liste für zu löschende Indizes, weil nicht alle Punkte in der area liegen

    # Alle Start- und Endpunkte in den detailed edges extrahieren
    gdf_edges_detailed['start'] = gdf_edges_detailed['geometry'].apply(lambda geom: geom.coords[0])
    gdf_edges_detailed['end'] = gdf_edges_detailed['geometry'].apply(lambda geom: geom.coords[-1])

    # Fortschrittsbalken basierend auf Gesamtlänge
    pbar = tqdm(total=total_detailed_length, desc="Sorting progress", unit="m", mininterval=1, maxinterval=1)

    result_gdf_list = []
    while not gdf_edges_simplified.empty:

        index, row = get_first_edge(gdf_edges_simplified)
        geometry = row['geometry']

        if geometry.is_empty or len(geometry.coords) < 1:
            gdf_edges_simplified = gdf_edges_simplified.drop(index=index)
            continue

        first_coords = geometry.coords[0]
        all_coords = list(geometry.coords)

        matching_candidates = find_matching_osmids(first_coords, all_coords, gdf_edges_detailed)

        if matching_candidates.empty:
            edge_mask_to_be_deleted.append(index)
            gdf_edges_simplified = gdf_edges_simplified.drop(index=index)
            continue

        matching_osmid = matching_candidates['osmid'].iloc[0]
        detailed_edges = filter_detailed_edges(gdf_edges_detailed, matching_osmid, row['reversed'])

        # ------------------------------------------------------------------------------------------------------------------------------------------------------------------
        # ------------------------------------------------------------------------------------------------------------------------------------------------------------------

        if len(detailed_edges) == 1:
            if 'result_gdf' not in locals():
                result_gdf = detailed_edges.copy()
            else:
                result_gdf = pd.concat([result_gdf, detailed_edges], ignore_index=True)
            counter = 1
        else:
            simplified_geometries_coords = gdf_edges_simplified.loc[
                gdf_edges_simplified['osmid'].apply(
                    lambda x: matching_osmid in x if isinstance(x, list) else x == matching_osmid),
                'geometry'
            ]

            if not simplified_geometries_coords.empty:
                simplified_geometries_coords = simplified_geometries_coords.iloc[0].coords
            else:
                edge_mask_to_be_deleted.append(gdf_edges_simplified.index[0])
                gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen
                continue  # Beende den aktuellen Schleifendurchlauf, da nicht alle Punkte der simplified_edge innerhalb der geforderten area liegen

            # Erstelle ein Dictionary mit den Startkoordinaten der detaillierten Geometrien
            detailed_edges_coords = {idx: (geom.coords[0][0], geom.coords[0][1]) for idx, geom in
                                     detailed_edges['geometry'].items()}

            # Finde übereinstimmende Indizes basierend auf den Koordinaten
            matching_indices = []
            for coord in simplified_geometries_coords:
                if coord in detailed_edges_coords.values():
                    matching_indices.append(
                        list(detailed_edges_coords.keys())[list(detailed_edges_coords.values()).index(coord)]
                    )

            # Filtere die detaillierten Kanten basierend auf den übereinstimmenden Indizes
            detailed_edges = detailed_edges.loc[matching_indices]


            # Füge die gefilterten detaillierten Kanten zum Ergebnis-GeoDataFrame hinzu
            if 'result_gdf' not in locals():
                result_gdf = detailed_edges.copy()
            else:
                result_gdf = pd.concat([result_gdf, detailed_edges], ignore_index=True)

            counter = len(matching_indices)  # Setze counter auf die Länge von matching_indices

        # Aktualisiere die Geometrie der ersten Zeile, indem die ersten Anz. der `counter` Koordinaten entfernt werden
        if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list) and len(gdf_edges_simplified.iloc[0]['osmid']) == 1 or not isinstance(gdf_edges_simplified.iloc[0]['osmid'], list):
            gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Lösche die Zeile, wenn nur ein osmid vorhanden ist
        else:
            if not gdf_edges_simplified.iloc[0]['geometry'].is_empty and counter < len(gdf_edges_simplified.iloc[0]['geometry'].coords): #Wenn die Geometrie nicht leer ist und der counter kleiner als die Anzahl der Koordinaten in der Geometrie ist
                if counter + 1 != len(gdf_edges_simplified.iloc[0]['geometry'].coords): #counter+1 ist nicht gleich der Länge der Geometrie (Sonst bleibt ein Punkt übrig im LINESTRING == Error!)
                    gdf_edges_simplified.iloc[0, gdf_edges_simplified.columns.get_loc('geometry')] = \
                        type(gdf_edges_simplified.iloc[0]['geometry'])(list(gdf_edges_simplified.iloc[0]['geometry'].coords)[counter:])
                else:
                    gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen, wenn nur noch ein Punkt in geometry übrig bleibt

            if not gdf_edges_simplified.empty:
                gdf_edges_simplified.at[gdf_edges_simplified.index[0], 'osmid'] = (
                    [os for os in gdf_edges_simplified.iloc[0]['osmid'] if os != matching_osmid]
                    if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list)
                    else (None if gdf_edges_simplified.iloc[0]['osmid'] == matching_osmid else gdf_edges_simplified.iloc[0]['osmid'])
                )

            if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list):
                if gdf_edges_simplified.iloc[0]['osmid'] in [None, []]:  # Prüfen, ob in der ersten Zeile keine osmid oder eine leere Liste enthalten ist
                    gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen

        update_value = round(detailed_edges['length'].sum())
        pbar.update(update_value)

    pbar.close()

    print(f"Gesamtlänge aller Elemente in result_gdf: {result_gdf['length'].sum()}")

    # ------------------------------------------------------------------------------------------------------------------------------------------------------------------

    geom_to_index = {
        (round(geom.x, 6), round(geom.y, 6)): idx
        for idx, geom in zip(gdf_nodes_detailed.index, gdf_nodes_detailed.geometry)
    }


    # Funktionen zum Lookup des Start- und Endpunkts
    def get_u(geom):
        coords = geom.coords[0]
        return geom_to_index.get((round(coords[0], 6), round(coords[1], 6)), None)


    def get_v(geom):
        coords = geom.coords[1]
        return geom_to_index.get((round(coords[0], 6), round(coords[1], 6)), None)


    # Anwendung auf das GeoDataFrame
    result_gdf['u'] = result_gdf['geometry'].apply(get_u)
    result_gdf['v'] = result_gdf['geometry'].apply(get_v)

    print(f"Speichere Daten in {output_file_detailed_sorted}.gpkg ...")
    #result_gdf.to_file(f"{output_file_detailed_sorted}.gpkg", layer="nodes", driver="GPKG")
    gdf_nodes_detailed.to_file(f"{output_file_detailed_sorted}.gpkg", layer="nodes", driver="GPKG")
    result_gdf.to_file(f"{output_file_detailed_sorted}.gpkg", layer="edges", driver="GPKG")
    print("---Export 'detailliert' abgeschlossen.")

    print(f"Speichere Daten in {output_file_simplified}.gpkg ...")


    gdf_nodes_simplified.to_file(f"{output_file_simplified}.gpkg", layer="nodes", driver="GPKG")
    gdf_edges_simplified_final = gdf_edges_simplified_final.drop(index=edge_mask_to_be_deleted)
    gdf_edges_simplified_final.to_file(f"{output_file_simplified}.gpkg", layer="edges", driver="GPKG")

    print(f"Gesamtlänge aller Elemente in gdf_edges_simplified_final: {gdf_edges_simplified_final['length'].sum()}")