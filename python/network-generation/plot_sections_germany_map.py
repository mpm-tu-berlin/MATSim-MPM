# -*- coding: utf-8 -*-
"""
Deutschlandkarte der 20 Diskretisierungs-Sektionen (Paper Sec. V).

Zeichnet die 20 ~100-km-Sektionen (q5..q97) aus dem Quantil-Lauf auf eine
Deutschlandkarte, nummeriert 1..20 aufsteigend nach sigma_g (= Quantil-
Reihenfolge). Zwei Varianten:
  - topo:  Hoehenfarben + Hillshade (GMRT-DEM), auf Deutschland geclippt
  - plain: grauer Deutschland-Umriss ohne Hoehen

Datenquellen (Cache-Ordner, siehe --cache-dir):
  germany_dem_gmrt.tif   GMRT GridServer (https://www.gmrt.org), EPSG:4326
  germany_border.geojson Natural Earth 10m admin_0 (Deutschland-Feature)

Ausgabe: fig_route_map_topo_V1.{pdf,png}, fig_route_map_plain_V1.{pdf,png}
"""

import argparse
import gzip
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
import numpy as np
import pandas as pd
import rasterio
from matplotlib.colors import LightSource, LinearSegmentedColormap, Normalize
from matplotlib.cm import ScalarMappable
from matplotlib.path import Path as MplPath
from matplotlib.patches import PathPatch
from pyproj import Transformer

REPO = Path(__file__).resolve().parents[2]
DEFAULT_RUN = (REPO.parent / "MATSim-MPM-netgen" / "python" / "network-generation"
               / "data" / "sections_quantile_run_20260817_V3")
DEFAULT_OUT = REPO.parent / "IEEE-TTE-paper"

QUANTILES = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 97]

CITIES = {  # Orientierungspunkte (lon, lat)
    "Hamburg":   (9.993, 53.551),
    "Berlin":    (13.405, 52.520),
    "Cologne":   (6.960, 50.938),
    "Frankfurt": (8.682, 50.111),
    "Munich":    (11.582, 48.137),
}

# Manuelle Versatzvektoren (Grad lon/lat) fuer Nummern-Badges, um
# Ueberdeckungen zu vermeiden; Default ist (0.28, 0.18).
LABEL_OFFSETS = {
    2:  (-0.09, -0.26),
    3:  (-0.16, 0.57),
    4:  (0.42, -0.18),
    6:  (-0.43, -0.51),
    7:  (0.94, 0.07),
    8:  (-0.44, -0.14),
    9:  (-0.12, 0.29),
    10: (-0.97, -0.04),
    13: (-0.76, 0.36),
    16: (-0.74, -0.38),
    17: (-0.34, 0.04),
    19: (0.71, -0.23),
    20: (-0.02, -0.32),
}

# Sektionen 11 (q55) und 12 (q60) liegen auf demselben Korridor
# (560 von 561 Knoten gemeinsam): eine Linie, ein Badge "11/12".
LABEL_TEXT = {11: None, 12: "11/12"}

CITY_LABEL_OFFSETS = {  # Punkte relativ zum Stadtpunkt (dx, dy, ha)
    "Hamburg":   (-4.0, -2.5, "right"),
    "Frankfurt": (-3.0, -7.5, "right"),
}

# Klassische hypsometrische Farbanker (Patterson / Natural Earth), Meter -> Hex
HYPSO_ANCHORS = [
    (0, "#ACD0A5"), (100, "#94BF8B"), (200, "#A8C68F"), (300, "#BDCC96"),
    (400, "#D1D7AB"), (500, "#E1E4B5"), (600, "#EFEBC0"), (700, "#E8E1B6"),
    (800, "#DED6A3"), (900, "#D3CA9D"), (1000, "#CAB982"), (1250, "#C3A76B"),
    (1500, "#B9985A"), (1750, "#AA8753"), (2000, "#AC9A7C"), (2500, "#BAAE9A"),
    (3000, "#E0DED8"),
]
HYPSO_VMAX = 3000


def load_routes(run_dir: Path):
    """Liest die 20 Sektionen und liefert [(nr, quantil, lons, lats), ...]."""
    node_lists = pd.read_csv(run_dir / "selected_sections_node_lists.csv")
    order = {f"q{q}": i + 1 for i, q in enumerate(QUANTILES)}  # 1 = flachste
    tf = Transformer.from_crs("EPSG:4839", "EPSG:4326", always_xy=True)
    routes = []
    for _, row in node_lists.iterrows():
        sec = row["section"]
        xml_path = run_dir / f"section_{sec}_100km.xml.gz"
        coords = {}
        with gzip.open(xml_path, "rt", encoding="utf-8") as f:
            for _, elem in ET.iterparse(f, events=("end",)):
                if elem.tag == "node":
                    coords[elem.get("id")] = (float(elem.get("x")), float(elem.get("y")))
                elem.clear()
        ids = row["node_list"].split(";")
        xy = np.array([coords[i] for i in ids if i in coords])
        lon, lat = tf.transform(xy[:, 0], xy[:, 1])
        routes.append((order[sec], sec, np.asarray(lon), np.asarray(lat)))
    routes.sort(key=lambda r: r[0])
    return routes


def germany_paths(border_geojson: Path):
    """MultiPolygon -> (Liste der Ring-Arrays, Compound-MplPath zum Clippen)."""
    gj = json.loads(border_geojson.read_text(encoding="utf-8"))
    polys = gj["geometry"]["coordinates"]  # MultiPolygon
    rings, vertices, codes = [], [], []
    for poly in polys:
        for ring in poly:  # Ring 0 = aussen, weitere = Loecher
            arr = np.asarray(ring)
            rings.append(arr)
            vertices.append(arr)
            codes.append([MplPath.MOVETO] + [MplPath.LINETO] * (len(arr) - 2)
                         + [MplPath.CLOSEPOLY])
    path = MplPath(np.concatenate(vertices),
                   np.concatenate(codes).astype(np.uint8))
    return rings, path


def load_dem(dem_tif: Path):
    with rasterio.open(dem_tif) as src:
        dem = src.read(1).astype(float)
        b = src.bounds
    dem = np.where(np.isnan(dem), 0.0, dem)
    extent = (b.left, b.right, b.bottom, b.top)
    # Pixelgroesse in Metern fuer den Hillshade
    lat_mid = 0.5 * (b.bottom + b.top)
    dx = (b.right - b.left) / dem.shape[1] * 111_320 * math.cos(math.radians(lat_mid))
    dy = (b.top - b.bottom) / dem.shape[0] * 111_320
    return dem, extent, dx, dy


def make_map(routes, rings, clip_path, dem=None, dem_extent=None, dx=None, dy=None,
             out_stem=None):
    lon_min, lon_max = 5.6, 15.35
    lat_min, lat_max = 47.1, 55.25
    lat_mid = 0.5 * (lat_min + lat_max)

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "STIXGeneral", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 8,
        "pdf.fonttype": 42,
    })

    width = 3.5
    height = width * (lat_max - lat_min) / ((lon_max - lon_min)
                                            * math.cos(math.radians(lat_mid)))
    fig, ax = plt.subplots(figsize=(width, height))
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect(1.0 / math.cos(math.radians(lat_mid)))
    ax.axis("off")

    clip = PathPatch(clip_path, transform=ax.transData, facecolor="none",
                     edgecolor="none")
    ax.add_patch(clip)

    if dem is not None:
        # Klassische hypsometrische Farben + Hillshade
        cmap = LinearSegmentedColormap.from_list(
            "hypso", [(m / HYPSO_VMAX, c) for m, c in HYPSO_ANCHORS])
        norm = Normalize(vmin=0, vmax=HYPSO_VMAX)
        ls = LightSource(azdeg=315, altdeg=45)
        rgb = ls.shade(dem, cmap=cmap, norm=norm, blend_mode="soft",
                       vert_exag=8, dx=dx, dy=dy)
        im = ax.imshow(rgb, extent=dem_extent, origin="upper",
                       interpolation="bilinear", zorder=1, rasterized=True)
        im.set_clip_path(clip)
        cax = ax.inset_axes([0.02, 0.10, 0.028, 0.28])
        cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), cax=cax,
                          orientation="vertical")
        cb.set_ticks([0, 1000, 2000, 3000])
        cb.set_label("Elevation (m)", fontsize=6.5, labelpad=10)
        cb.ax.tick_params(labelsize=6, length=2, pad=1)
        cb.outline.set_linewidth(0.4)
    else:
        for arr in rings:
            ax.fill(arr[:, 0], arr[:, 1], facecolor="0.93", edgecolor="none",
                    zorder=1)

    for arr in rings:
        ax.plot(arr[:, 0], arr[:, 1], color="0.25", lw=0.6, zorder=3)

    for name, (lon, lat) in CITIES.items():
        dx_pt, dy_pt, ha = CITY_LABEL_OFFSETS.get(name, (2.5, -1.5, "left"))
        ax.plot(lon, lat, marker="o", ms=1.8, mfc="0.15", mec="none", zorder=4)
        ax.annotate(name, (lon, lat), xytext=(dx_pt, dy_pt),
                    textcoords="offset points", fontsize=5.5, style="italic",
                    color="0.25", ha=ha, zorder=4,
                    path_effects=[pe.withStroke(linewidth=1.2, foreground="white",
                                                alpha=0.75)])

    route_color = "#B2182B"
    for nr, sec, lon, lat in routes:
        ax.plot(lon, lat, color=route_color, lw=1.4, solid_capstyle="round",
                zorder=5,
                path_effects=[pe.Stroke(linewidth=2.4, foreground="white"),
                              pe.Normal()])
        text = LABEL_TEXT.get(nr, str(nr))
        if text is None:
            continue
        mid = len(lon) // 2
        off = LABEL_OFFSETS.get(nr, (0.28, 0.18))
        bx, by = lon[mid] + off[0], lat[mid] + off[1]
        # Anker = nahester Routenpunkt zum Badge; Leaderlinie nur bei Distanz
        cosl = math.cos(math.radians(by))
        d2 = ((lon - bx) * cosl) ** 2 + (lat - by) ** 2
        k = int(np.argmin(d2))
        dist = math.sqrt(d2[k])
        arrow = None
        if dist > 0.30:
            arrow = dict(arrowstyle="-", color=route_color, linewidth=0.5,
                         shrinkA=0, shrinkB=0)
        boxstyle = ("circle,pad=0.18" if "/" not in text
                    else "round,pad=0.15,rounding_size=0.6")
        ax.annotate(text, (lon[k], lat[k]), xytext=(bx, by),
                    textcoords="data", fontsize=6.0, fontweight="bold",
                    color=route_color, ha="center", va="center", zorder=6,
                    arrowprops=arrow,
                    bbox=dict(boxstyle=boxstyle, facecolor="white",
                              edgecolor=route_color, linewidth=0.6))

    # Massstabsbalken 100 km unten rechts (leere Ecke suedoestlich)
    bar_km = 100
    bar_deg = bar_km / (111.32 * math.cos(math.radians(lat_mid)))
    x0, y0 = lon_max - 0.4 - bar_deg, lat_min + 0.25
    ax.plot([x0, x0 + bar_deg], [y0, y0], color="0.15", lw=1.2,
            solid_capstyle="butt", zorder=6)
    ax.annotate(f"{bar_km} km", (x0 + bar_deg / 2, y0), xytext=(0, 2.5),
                textcoords="offset points", ha="center", fontsize=6,
                color="0.15", zorder=6)

    fig.tight_layout(pad=0.1)
    for ext in ("pdf", "png"):
        fig.savefig(f"{out_stem}.{ext}", dpi=600, bbox_inches="tight",
                    pad_inches=0.02)
    plt.close(fig)
    print(f"geschrieben: {out_stem}.pdf/.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache-dir", type=Path, required=True,
                    help="Ordner mit germany_dem_gmrt.tif + germany_border.geojson")
    ap.add_argument("--version", default="V1")
    args = ap.parse_args()

    routes = load_routes(args.run_dir)
    rings, clip_path = germany_paths(args.cache_dir / "germany_border.geojson")
    dem, extent, dx, dy = load_dem(args.cache_dir / "germany_dem_gmrt.tif")

    make_map(routes, rings, clip_path, dem, extent, dx, dy,
             out_stem=str(args.out_dir / f"fig_route_map_topo_{args.version}"))
    make_map(routes, rings, MplPath(clip_path.vertices, clip_path.codes),
             out_stem=str(args.out_dir / f"fig_route_map_plain_{args.version}"))


if __name__ == "__main__":
    main()
