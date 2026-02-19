# -*- coding: utf-8 -*-
"""
Erzeugt eine OSM-HTML-Karte mit Messpunkten und MATSim-Netz(werken).

Nutzung (Beispiel):
make_osm_overlay_map(
    parquet_path=r"C:/.../983V80_week_2025_04_09.parquet",
    network_paths=[
        r"C:/.../Germany_max1000m_V0.xml.gz",
        r"C:/.../Germany_max10000m_V0.xml.gz",
    ],
    out_html=r"C:/.../matsim_measurements_map.html",
    raw_crs="EPSG:4326",      # Messdaten-Input
    net_input_crs="EPSG:4839",# Netz-Input (x/y in 4839)
    work_crs="EPSG:25833",    # metrisches Arbeits-CRS
    max_links_per_net=60000,  # für Performance ggf. begrenzen
    show_points=False         # zusätzlich einzelne Messpunkte anzeigen
)
"""

import gzip
import os
from typing import List, Optional

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from lxml import etree

import folium
from folium.plugins import Fullscreen, MeasureControl


# -------- Loader --------

def load_measurements_parquet_for_map(
        parquet_path: str,
        raw_crs: str = "EPSG:4326",
        work_crs: str = "EPSG:25833",
        filter_zero_velocity: bool = True,
) -> gpd.GeoDataFrame:
    df = pd.read_parquet(parquet_path)
    lc = {c.lower(): c for c in df.columns}
    lon_col = lc.get("lon") or lc.get("longitude")
    lat_col = lc.get("lat") or lc.get("latitude")
    mil_col = lc.get("mileage")
    alt_col = lc.get("altitude") or lc.get("elevation") or lc.get("height")
    vel_col = lc.get("velocity")

    if filter_zero_velocity and vel_col in df.columns:
        mask = pd.to_numeric(df[vel_col], errors="coerce").fillna(0) != 0
        df = df.loc[mask].copy()

    # Dezimaltrennzeichen defensiv normalisieren
    for c in [lon_col, lat_col]:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "."), errors="coerce")

    gdf = gpd.GeoDataFrame(
        df,
        geometry=[Point(xy) for xy in zip(df[lon_col], df[lat_col])],
        crs=raw_crs
    ).to_crs(work_crs)

    # optional: Pfadlinie (für Karte später nach WGS84)
    return gdf


def load_matsim_network_nodes_links(
        path: str,
        input_crs: str,
        work_crs: str
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    open_fn = gzip.open if path.lower().endswith(".gz") else open
    with open_fn(path, "rb") as f:
        tree = etree.parse(f)
    r = tree.getroot()
    ns = {"m": r.nsmap.get(None)} if None in r.nsmap else {}
    def findall(tag):
        return r.findall(f".//m:{tag}", namespaces=ns) if ns else r.findall(f".//{tag}")

    nodes = []
    for n in findall("node"):
        nid = n.get("id")
        x = float(n.get("x"))
        y = float(n.get("y"))
        z_attr = n.get("z") or n.get("elevation")
        z = float(z_attr) if z_attr is not None else np.nan
        nodes.append((nid, x, y, z))
    if not nodes:
        raise ValueError(f"Keine Nodes in {os.path.basename(path)}")

    gnodes = gpd.GeoDataFrame(
        pd.DataFrame(nodes, columns=["id","x","y","z"]),
        geometry=gpd.points_from_xy([n[1] for n in nodes], [n[2] for n in nodes]),
        crs=input_crs
    ).to_crs(work_crs)

    links = []
    for l in findall("link"):
        lid = l.get("id"); frm = l.get("from"); to = l.get("to")
        length = l.get("length")
        links.append((lid, frm, to, float(length) if length is not None else np.nan))
    links_df = pd.DataFrame(links, columns=["id","from","to","length"])
    return gnodes, links_df


def build_link_geometries(
        nodes: gpd.GeoDataFrame,
        links_df: pd.DataFrame
) -> gpd.GeoDataFrame:
    ndict = nodes.set_index("id")
    def make_geom(row):
        try:
            p = ndict.loc[row["from"], "geometry"]
            q = ndict.loc[row["to"], "geometry"]
            return LineString([p, q])
        except Exception:
            return None
    links_df = links_df.copy()
    links_df["geometry"] = links_df.apply(make_geom, axis=1)
    glinks = gpd.GeoDataFrame(links_df.dropna(subset=["geometry"]), crs=nodes.crs)
    # Länge nachtragen falls fehlt
    missing = glinks["length"].isna()
    if missing.any():
        glinks.loc[missing, "length"] = glinks.loc[missing, "geometry"].length.values
    return glinks


# -------- Map Export --------

def make_osm_overlay_map(
        parquet_path: str,
        network_paths: List[str],
        out_html: str,
        raw_crs: str = "EPSG:4326",
        net_input_crs: str = "EPSG:4839",
        work_crs: str = "EPSG:25833",
        max_links_per_net: int = 60000,
        show_points: bool = False,
) -> None:
    # Messdaten laden (im Arbeits-CRS)
    gmeas = load_measurements_parquet_for_map(parquet_path, raw_crs=raw_crs, work_crs=work_crs, filter_zero_velocity=True)

    # WGS84 für Folium
    gmeas_wgs = gmeas.to_crs(4326)
    center_lat = float(gmeas_wgs.geometry.y.mean())
    center_lon = float(gmeas_wgs.geometry.x.mean())

    m = folium.Map(location=[center_lat, center_lon], zoom_start=10, tiles="OpenStreetMap")
    Fullscreen().add_to(m)
    MeasureControl(position='topleft', primary_length_unit='kilometers').add_to(m)

    # Messpfad als Linie (durch sortierte Reihenfolge)
    meas_layer = folium.FeatureGroup(name="Messfahrt (Linie)", show=True)
    path_coords = [(pt.y, pt.x) for pt in gmeas_wgs.geometry]  # (lat, lon)
    if len(path_coords) >= 2:
        folium.PolyLine(path_coords, weight=3, opacity=0.9, color="#1d4ed8").add_to(meas_layer)
    # Punkte optional
    if show_points:
        from itertools import islice
        # sparsam: jeden n-ten Punkt
        step = max(1, len(path_coords)//5000)
        for (lat, lon) in islice(path_coords, 0, None, step):
            folium.CircleMarker(location=(lat, lon), radius=2, color="#60a5fa", fill=True, fill_opacity=0.7).add_to(meas_layer)
    meas_layer.add_to(m)

    # Start/Ende markieren
    if len(path_coords) >= 1:
        folium.Marker(path_coords[0], tooltip="Start", icon=folium.Icon(color="green")).add_to(m)
        folium.Marker(path_coords[-1], tooltip="Ende", icon=folium.Icon(color="red")).add_to(m)

    # Netze auflegen
    palette = ["#737373", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4", "#dc2626", "#0ea5e9"]
    for idx, net_path in enumerate(network_paths):
        try:
            nodes, links_df = load_matsim_network_nodes_links(net_path, input_crs=net_input_crs, work_crs=work_crs)
            glinks = build_link_geometries(nodes, links_df).to_crs(4326)

            # evtl. beschneiden: links in der Nähe der Messung (Performance)
            try:
                buffer_poly = gmeas_wgs.unary_union.convex_hull.buffer(0.2)  # ~0.2° ~ grob 20 km
                glinks = glinks.loc[glinks.intersects(buffer_poly)]
            except Exception:
                pass

            # viele Links? ausdünnen
            if len(glinks) > max_links_per_net:
                stride = int(np.ceil(len(glinks) / max_links_per_net))
                glinks = glinks.iloc[::stride, :].copy()

            col = palette[idx % len(palette)]
            layer = folium.FeatureGroup(name=f"MATSim: {os.path.basename(net_path)}", show=(idx == 0))

            # als GeoJSON, damit das Rendering effizient bleibt
            gj = folium.GeoJson(
                data=glinks.__geo_interface__,
                name=f"net_{idx}",
                style_function=lambda _: {"color": col, "weight": 2, "opacity": 0.7}
            )
            gj.add_to(layer)
            layer.add_to(m)
        except Exception as e:
            folium.map.CustomIcon  # no-op to keep folium imported
            print(f"[WARN] Konnte Netz nicht darstellen ({os.path.basename(net_path)}): {e}")

    folium.LayerControl(collapsed=False).add_to(m)
    m.save(out_html)
    print(f"[OK] Karte gespeichert: {out_html}")


# --------- Falls direkt starten (kleiner Dialog) ----------
if __name__ == "__main__":
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw()
        pq = filedialog.askopenfilename(title="Parquet wählen", filetypes=[("Parquet","*.parquet"),("Alle Dateien","*.*")])
        nets = filedialog.askopenfilenames(title="MATSim-Netze wählen", filetypes=[("MATSim","*.xml *.xml.gz"),("Alle Dateien","*.*")])
        if pq and nets:
            out = os.path.splitext(pq)[0] + "_map.html"
            make_osm_overlay_map(
                parquet_path=pq,
                network_paths=list(nets),
                out_html=out,
                raw_crs="EPSG:4326",
                net_input_crs="EPSG:4839",
                work_crs="EPSG:25833",
                max_links_per_net=60000,
                show_points=False
            )
    except Exception as e:
        print("[Fehler]", e)
