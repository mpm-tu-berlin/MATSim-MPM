import osmnx as ox
import pandas as pd

if __name__ == "__main__":
    area = ("Saarland, Germany")
    highway_types = '["highway"~"motorway|trunk|primary"]'
    output_file_simplified = f"test_{area.split(',')[0].lower()}_simplified"
    output_file_detailed = f"test_{area.split(',')[0].lower()}_detailed"
    output_file_detailed_sorted= f"test_{area.split(',')[0].lower()}_detailed_sorted"

#------------------------------------------------------

    print(f"Lade vereinfachtes Straßennetz für: {area}")
    G = ox.graph.graph_from_place(
        query=area,
        network_type="drive",
        simplify=True, # Vereinfachte Kanten
        retain_all=False,
        truncate_by_edge=True,
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
    # Brücken-Abschnitte als Geometrien extrahieren
    # bridge_gdf = ox.features_from_place(
    #    area,
    #    tags={"bridge": True}
    # )

    # Nur Liniengeometrien (keine Gebäude etc.)
    # bridge_lines = bridge_gdf[bridge_gdf.geom_type == "LineString"]
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
    for index, row in gdf_edges_simplified.iterrows():
        matching_indices = gdf_edges_simplified.index[
            (gdf_edges_simplified['oneway'] == False) & (gdf_edges_simplified['reversed'].apply(lambda x: isinstance(x, list)))
        ].tolist()
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

    print(f"Speichere Daten in {output_file_simplified}.gpkg ...")
    gdf_nodes_simplified.to_file(f"{output_file_simplified}.gpkg", layer="nodes", driver="GPKG")
    gdf_edges_simplified.to_file(f"{output_file_simplified}.gpkg", layer="edges", driver="GPKG")

    gdf_edges_simplified_final = gdf_edges_simplified.copy()

    print("---Export 'vereinfacht' abgeschlossen.")

# ------------------------------------------------------

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

    # Speichern in GeoPackage
    print(f"Speichere Daten in {output_file_detailed}.gpkg ...")
    gdf_nodes_detailed.to_file(f"{output_file_detailed}.gpkg", layer="nodes", driver="GPKG")
    gdf_edges_detailed.to_file(f"{output_file_detailed}.gpkg", layer="edges", driver="GPKG")
    print("---Export 'detailliert' abgeschlossen.")

#------------------------------------------------------
    print(f"Gesamtlänge aller Elemente in gdf_edges_simplified: {gdf_edges_simplified['length'].sum()}")
    print(f"Gesamtlänge aller Elemente in gdf_edges_detailed: {gdf_edges_detailed['length'].sum()}")
    total_detailed_length = gdf_edges_simplified['length'].sum()
    progress_counter = 0  # Zähler für Fortschrittsausgabe
    start_time = pd.Timestamp.now()
    print('Fortschritt')
    #debug_counter = 0
    while not gdf_edges_simplified.empty:
        # Finde die erste Geometrie in simplified edges
        first_geometry = gdf_edges_simplified.iloc[0]['geometry']
        first_coords = first_geometry.coords[0]  # Nur das erste x, y-Paar

        # Finde die osmid, deren Geometrie mit der ersten Geometrie übereinstimmt
        matching_osmid_candidates = gdf_edges_detailed[
            (gdf_edges_detailed['geometry'].apply(lambda geom: list(geom.coords)[0] == first_coords)) &
            (gdf_edges_detailed['geometry'].apply(lambda geom: list(geom.coords)[1] in [coord for coord in first_geometry.coords]))
        ]

        if matching_osmid_candidates.empty:
            #detailed_edges = find_point_out_of_scope(first_coords)
            gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen
            continue  # Beende den aktuellen Schleifendurchlauf, da der Punkt außerhalb der definierten area liegt  # Beende den aktuellen Schleifendurchlauf, da der Punkt außerhalb der definierten area liegt
        else:
            matching_osmid = matching_osmid_candidates['osmid'].iloc[0]
            if isinstance(gdf_edges_simplified.iloc[0]['reversed'], list):
                detailed_edges = gdf_edges_detailed[
                    gdf_edges_detailed['osmid'] == matching_osmid
                ]
            else:
                detailed_edges = gdf_edges_detailed[
                    (gdf_edges_detailed['osmid'] == matching_osmid) &
                    (gdf_edges_detailed['reversed'] == gdf_edges_simplified.iloc[0]['reversed'])
                ]
            # -----------------------------------------

        if len(detailed_edges) == 1:
            if 'result_gdf' not in locals():
                result_gdf = detailed_edges.copy()
            else:
                result_gdf = pd.concat([result_gdf, detailed_edges], ignore_index=True)
            counter = 1
        else:
            # Extrahiere die Koordinaten der Geometrien aus dem vereinfachten Straßennetzwerk
            #debug_counter+= 1
            #print(debug_counter)
            #simplified_geometries_coords = gdf_edges_simplified.loc[
            #    gdf_edges_simplified['osmid'].apply(lambda x: matching_osmid in x if isinstance(x, list) else x == matching_osmid),
            #    'geometry'
            #].iloc[0].coords

            simplified_geometries_coords = gdf_edges_simplified.loc[
                gdf_edges_simplified['osmid'].apply(
                    lambda x: matching_osmid in x if isinstance(x, list) else x == matching_osmid),
                'geometry'
            ]

            if not simplified_geometries_coords.empty:
                simplified_geometries_coords = simplified_geometries_coords.iloc[0].coords
            else:
                gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen
                continue  # Beende den aktuellen Schleifendurchlauf, da nicht alle Punkte der simplified_edge innerhalb der area liegen

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

            #test_counter = 0
        # Aktualisiere die Geometrie der ersten Zeile, indem die ersten `counter` Koordinaten entfernt werden
        if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list) and len(gdf_edges_simplified.iloc[0]['osmid']) == 1 or not isinstance(gdf_edges_simplified.iloc[0]['osmid'], list):
            gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Lösche die Zeile, wenn nur ein osmid vorhanden ist
        else:
            if not gdf_edges_simplified.iloc[0]['geometry'].is_empty and counter < len(gdf_edges_simplified.iloc[0]['geometry'].coords):
                gdf_edges_simplified.iloc[0, gdf_edges_simplified.columns.get_loc('geometry')] = \
                type(gdf_edges_simplified.iloc[0]['geometry'])(
                    list(gdf_edges_simplified.iloc[0]['geometry'].coords)[counter:]
                )
            if not gdf_edges_simplified.empty:
                gdf_edges_simplified.at[gdf_edges_simplified.index[0], 'osmid'] = (
                    [os for os in gdf_edges_simplified.iloc[0]['osmid'] if os != matching_osmid]
                    if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list)
                    else (None if gdf_edges_simplified.iloc[0]['osmid'] == matching_osmid else gdf_edges_simplified.iloc[0]['osmid'])
                )

            if isinstance(gdf_edges_simplified.iloc[0]['osmid'], list):
                if gdf_edges_simplified.iloc[0]['osmid'] in [None, []]:  # Prüfen, ob in der ersten Zeile keine osmid oder eine leere Liste enthalten ist
                    gdf_edges_simplified = gdf_edges_simplified.iloc[1:]  # Erste Zeile löschen

        if 'last_progress_time' not in locals() or (pd.Timestamp.now() - last_progress_time).seconds >= 10:  # Fortschritt alle 10 Sekunde ausgeben
            last_progress_time = pd.Timestamp.now()
            elapsed_time = pd.Timestamp.now() - start_time
            progress_percentage = result_gdf['length'].sum() / total_detailed_length
            progress_counter += 1
            if progress_counter >= 3:  # Verbleibende Zeit erst nach 3 Berechnungen ausgeben
                remaining_time = elapsed_time / progress_percentage * (1 - progress_percentage)
                print(f"{pd.Timestamp.now().strftime('%H:%M:%S')} - {progress_percentage:.0%}, Verbleibende Zeit: {remaining_time.components.hours:02}:{remaining_time.components.minutes:02}:{remaining_time.components.seconds:02}")

    print(f"Gesamtlänge aller Elemente in result_gdf: {result_gdf['length'].sum()}")
    #-----------------------
    #Für jede edge einen u und v (Start- un Endpunkt) hinzufügen


    #result_gdf['u'] = result_gdf['geometry'].apply(
    #    lambda geom: gdf_nodes_detailed.loc[
    #        gdf_nodes_detailed['geometry'].apply(
    #            lambda node_geom: node_geom.equals(Point(geom.coords[0]))
    #        )
    #    ].index[0] if not gdf_nodes_detailed.empty else None
    #)
    #result_gdf['v'] = result_gdf['geometry'].apply(
    #    lambda geom: gdf_nodes_detailed.loc[
    #        gdf_nodes_detailed['geometry'].apply(
    #            lambda node_geom: node_geom.equals(Point(geom.coords[1]))
    #        )
    #    ].index[0] if not gdf_nodes_detailed.empty else None
    #)

    # Mapping von Punkt-Koordinaten (als Tupel) zu Index
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
    result_gdf.to_file(f"{output_file_detailed_sorted}.gpkg", layer="nodes", driver="GPKG")
    result_gdf.to_file(f"{output_file_detailed_sorted}.gpkg", layer="edges", driver="GPKG")
    print("---Export 'detailliert' abgeschlossen.")