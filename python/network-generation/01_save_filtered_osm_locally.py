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
def normalize_reversed_flags(gdf_edges_simplified, starts_by_osmid, ends_by_osmid):
    import pandas as pd
    from pandas.api.types import is_list_like

    def to_osmid_list(x):
        if is_list_like(x) and not isinstance(x, (str, bytes)):
            return list(x)
        return [x]

    def is_false_true_list(x):
        return (is_list_like(x) and not isinstance(x, (str, bytes))
                and len(x) == 2 and bool(x[0]) is False and bool(x[1]) is True)

    def xy5_tuple(pt):
        return (round(pt[0], 5), round(pt[1], 5))

    # Hilfsspalten
    if 'first5' not in gdf_edges_simplified.columns:
        gdf_edges_simplified['first5'] = gdf_edges_simplified['geometry'].apply(lambda g: xy5_tuple(g.coords[0]))
    if 'last5' not in gdf_edges_simplified.columns:
        gdf_edges_simplified['last5'] = gdf_edges_simplified['geometry'].apply(lambda g: xy5_tuple(g.coords[-1]))

    # Gegenstück-Lookup (OSMID-Satz + Start/Ende)
    def osmid_key_set(x):
        return frozenset(to_osmid_list(x))

    key_rows = []
    for idx, r in gdf_edges_simplified.iterrows():
        key_rows.append((idx, osmid_key_set(r['osmid']), r['first5'], r['last5']))
    key_df = pd.DataFrame(key_rows, columns=['idx', 'osmids', 's5', 'e5'])
    key_lookup = {(osmids, s5, e5): idx for idx, osmids, s5, e5 in key_df.itertuples(index=False)}

    # --- WICHTIG: live über Indizes iterieren, keinen Snapshot verarbeiten ---
    mask_ft = gdf_edges_simplified['reversed'].apply(is_false_true_list)
    idx_list = list(gdf_edges_simplified.index[mask_ft])

    for idx in idx_list:
        # Skip, wenn zwischenzeitlich schon festgelegt (weil Gegenstück uns schon gesetzt hat)
        cur_val = gdf_edges_simplified.at[idx, 'reversed']
        if not is_false_true_list(cur_val):
            continue

        row = gdf_edges_simplified.loc[idx]
        osmid_list = to_osmid_list(row['osmid'])
        s_start, s_end = row['first5'], row['last5']

        # Vergleichsbasis
        available_starts, available_ends = set(), set()
        for os in osmid_list:
            if os in starts_by_osmid:
                available_starts |= starts_by_osmid[os]
            if os in ends_by_osmid:
                available_ends   |= ends_by_osmid[os]

        if not available_starts and not available_ends:
            continue

        # 1) Bevorzugte, eindeutige Entscheidung über beide Enden:
        decision = None
        if (s_start in available_starts) and (s_end in available_ends):
            decision = False  # forward
        elif (s_start in available_ends) and (s_end in available_starts):
            decision = True   # reversed

        # 2) Falls noch unentschieden, alte Heuristik:
        if decision is None:
            if s_start in available_starts:
                decision = False
            elif s_start in available_ends:
                decision = True
            else:
                continue  # keine Änderung

        # aktuelle Edge festschreiben
        gdf_edges_simplified.at[idx, 'reversed'] = decision

        # Gegenstück finden und – wenn noch [False, True] – Gegenteil setzen
        key_cur = (osmid_key_set(row['osmid']), row['first5'], row['last5'])
        key_rev = (key_cur[0], key_cur[2], key_cur[1])
        opp_idx = key_lookup.get(key_rev)

        if opp_idx is not None:
            opp_val = gdf_edges_simplified.at[opp_idx, 'reversed']
            if is_false_true_list(opp_val):
                gdf_edges_simplified.at[opp_idx, 'reversed'] = (not decision)

    return gdf_edges_simplified



def build_region(area="Germany", bbox=None, out_prefix=None,
                 highway_types='["highway"~"motorway"]'):
    """Laedt OSM-Strassennetz (simplified + detailed) fuer eine Region und schreibt
    zwei GPKGs (simplified + detailed_sorted). Region entweder per Ortsname `area`
    ODER `bbox`=(west, south, east, north). `out_prefix` bestimmt die Ausgabepfade
    (Default: data/<area>). Damit ist die Pipeline regional wiederverwendbar."""
    if out_prefix is None:
        base = (area.split(',')[0].lower() if area else "region")
        out_prefix = f"data/{base}"
    output_file_simplified = f"{out_prefix}_simplified_DF"
    output_file_detailed_sorted = f"{out_prefix}_detailed_sorted_DF"

    def _fetch(simplify, retain_all, truncate_by_edge):
        if bbox is not None:
            return ox.graph.graph_from_bbox(
                bbox=bbox, network_type="drive", simplify=simplify,
                retain_all=retain_all, truncate_by_edge=truncate_by_edge,
                custom_filter=highway_types)
        return ox.graph.graph_from_place(
            query=area, network_type="drive", simplify=simplify,
            retain_all=retain_all, truncate_by_edge=truncate_by_edge,
            custom_filter=highway_types)
    #------------------------------------------------------

    print(f"Lade vereinfachtes Straßennetz für: {area or bbox}")
    G = _fetch(simplify=True, retain_all=False, truncate_by_edge=True)
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



    #------------------------------------------------------------------------------------------------------------------------------------------------------------------
    #------------------------------------------------------------------------------------------------------------------------------------------------------------------

    print(f"Lade detailliertes Straßennetz für: {area or bbox}")
    G = _fetch(simplify=False, retain_all=True, truncate_by_edge=False)
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


    # --- NORMALISIERUNG 'reversed' FÜR SIMPLIFIED-EDGES MIT MEHREREN OSMIDs ---
    # Einfügen NACH dem Runden beider GeoDataFrames und VOR der while-Schleife.

    from collections import defaultdict
    from pandas.api.types import is_list_like

    # 1) Start-/End-Koordinaten (5 Nachkommastellen) für detailed vorbereiten
    def xy5(pt):
        return (round(pt[0], 5), round(pt[1], 5))

    def start_xy5(g):
        return xy5(g.coords[0])

    def end_xy5(g):
        return xy5(g.coords[-1])

    gdf_edges_detailed['start5'] = gdf_edges_detailed['geometry'].apply(start_xy5)
    gdf_edges_detailed['end5']   = gdf_edges_detailed['geometry'].apply(end_xy5)

    # 2) Indizes für schnellen Lookup je OSM-ID:
    #    - starts_by_osmid[osmid] -> Menge aller Startpunkte
    starts_by_osmid = defaultdict(set)
    ends_by_osmid = defaultdict(set)

    for osmid_val, s5, e5 in zip(gdf_edges_detailed['osmid'],
                                 gdf_edges_detailed['start5'],
                                 gdf_edges_detailed['end5']):
        if is_list_like(osmid_val) and not isinstance(osmid_val, (str, bytes)):
            for os in osmid_val:
                starts_by_osmid[os].add(s5)
                ends_by_osmid[os].add(e5)
        else:
            os = osmid_val
            starts_by_osmid[os].add(s5)
            ends_by_osmid[os].add(e5)

    # 3) Für simplified-Edges die erste Koordinate (first5) und ALLE Koordinaten (als Set) vorbereiten
    def first_xy5_line(g):
        return xy5(g.coords[0])

    #def all_coords5_set(g):
    #    return {xy5(pt) for pt in g.coords}

    gdf_edges_simplified['first5']    = gdf_edges_simplified['geometry'].apply(first_xy5_line)
    #gdf_edges_simplified['coords5set'] = gdf_edges_simplified['geometry'].apply(all_coords5_set)

    gdf_edges_simplified = normalize_reversed_flags(gdf_edges_simplified, starts_by_osmid, ends_by_osmid)

    gdf_edges_simplified_final = gdf_edges_simplified.copy()


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

    result_gdf = None
    result_gdf_list = []
    while not gdf_edges_simplified.empty:

        index, row = get_first_edge(gdf_edges_simplified)
        geometry = row['geometry']

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
            if result_gdf is None:
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
            if result_gdf is None:
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

    # --- Konsistenter Node-Lookup: Rundung wie bei Kanten (5 Nachkommastellen) ---
    geom_to_index = {
        (round(geom.x, 5), round(geom.y, 5)): idx
        for idx, geom in zip(gdf_nodes_detailed.index, gdf_nodes_detailed.geometry)
    }

    def get_u(geom):
        x, y = geom.coords[0]
        return geom_to_index.get((round(x, 5), round(y, 5)), None)

    def get_v(geom):
        x, y = geom.coords[-1]  # echtes Ende statt coords[1]
        return geom_to_index.get((round(x, 5), round(y, 5)), None)

    # Falls noch nicht geschehen:
    result_gdf['u'] = result_gdf['geometry'].apply(get_u)
    result_gdf['v'] = result_gdf['geometry'].apply(get_v)

    # --- Reparatur: fehlende u/v über gegenläufige Kante in derselben osmid-Gruppe füllen ---
    from pandas.api.types import is_list_like

    def osmid_key(x):
        # robuste Gruppierung: list -> tuple; scalar bleibt scalar in tuple
        return tuple(x) if is_list_like(x) and not isinstance(x, (str, bytes)) else (x,)

    df = result_gdf.copy()

    # Nur bidirektionale OSMIDs bearbeiten (oneway == False)
    mask = (df['oneway'] == False)
    df = df.loc[mask].copy()

    # Start/End-Koordinaten (wie oben, 5 Nachkommastellen)
    df['start'] = df['geometry'].apply(lambda g: (round(g.coords[0][0], 5),  round(g.coords[0][1], 5)))
    df['end']   = df['geometry'].apply(lambda g: (round(g.coords[-1][0], 5), round(g.coords[-1][1], 5)))
    df['osmid_key'] = df['osmid'].apply(osmid_key)

    # Hilfs-DataFrame mit Index für Rückschreiben
    helper = df[['osmid_key', 'start', 'end', 'u', 'v']].reset_index().rename(columns={'index': 'idx'})

    # Gegenkante ist (end,start) in derselben osmid_key-Gruppe
    rev = helper.rename(columns={
        'start': 'rev_start', 'end': 'rev_end', 'u': 'opp_u', 'v': 'opp_v'
    })

    merged = helper.merge(
        rev,
        how='left',
        left_on=['osmid_key', 'start', 'end'],
        right_on=['osmid_key', 'rev_end', 'rev_start'],
        suffixes=('', '_opp')
    )

    # Fehlende u/v füllen: u <- opp_v, v <- opp_u
    merged['u_filled'] = merged['u'].where(merged['u'].notna(), merged['opp_v'])
    merged['v_filled'] = merged['v'].where(merged['v'].notna(), merged['opp_u'])

    # Änderungen zurück in result_gdf schreiben (nur wo oneway==False)
    result_gdf.loc[merged['idx'], 'u'] = merged['u_filled'].values
    result_gdf.loc[merged['idx'], 'v'] = merged['v_filled'].values

    ## --- Reihenfolge aller Einträge in result_gdf umkehren, sodass sie korrekt weiterverarbeitet werden können ---
    result_gdf = result_gdf.iloc[::-1].reset_index(drop=True)
    print("Reihenfolge von result_gdf wurde vollständig umgekehrt.")

# (Optional) kurze Diagnose
    #num_u_fixed = int((merged['u'].isna()) & (merged['u_filled'].notna())).sum()
    #num_v_fixed = int((merged['v'].isna()) & (merged['v_filled'].notna())).sum()
    #print(f"Repariert: u={num_u_fixed}, v={num_v_fixed} (nur oneway==False)")


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


if __name__ == "__main__":
    build_region(area="Germany", out_prefix="data/germany")