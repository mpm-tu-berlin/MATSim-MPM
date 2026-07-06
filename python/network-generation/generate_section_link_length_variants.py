# -*- coding: utf-8 -*-
"""
Generate MATSim network variants at different max link lengths for representative sections.

For each section (Q75/Q97) and each max_link_length, this script:
  1. Reads the fine (50m) section network to get the bounding box + buffer
  2. Spatially filters the Germany-wide gpkg data to that bounding box
  3. Runs short_edges() from script 04 with the target max_link_length
  4. Builds a MATSim network; Hoehen via assign_heights_along_corridors aus
     Skript 04 (DTM-direkt, korridor-geglaettet, bruecken-linearisiert —
     aufloesungsUNabhaengig, d.h. alle Varianten teilen dieselbe Hoehenbasis;
     ersetzt das alte KD-Tree-npz-Sampling, A3-Konsistenz 2026-07)
  5. Finds the section path in the new network (start/end node matching)
  6. Exports only the section-path links as a MATSim sub-network

Skript 04 und die Germany-GPKGs liegen im Netzgen-Worktree (../MATSim-MPM-netgen,
Branch feature/network-generation) und werden von dort per Dateipfad importiert/geladen
(bewusste Entscheidung 2026-07-01: kein Branch-Merge).

Usage:
    python generate_section_link_length_variants.py [--sections-dir <path>] [--output-dir <path>]
"""

import argparse
import copy
import gzip
import math
import sys
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import transform as shapely_transform
from tqdm import tqdm

# Import functions from script 04
from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent

# Netzgen-Worktree (Geschwister-Checkout, Branch feature/network-generation):
# dort liegen Skript 04 und die aktuellen Germany-GPKGs (Skript-01-Lauf 2026-07-03).
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"


def _import_script04():
    """Import script 04 as a module (lokal, sonst aus dem Netzgen-Worktree)."""
    script_path = _SCRIPT_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    if not script_path.exists():
        script_path = _NETGEN_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    spec = spec_from_file_location("script04", str(script_path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ==============================
# Configuration
# ==============================
DEFAULT_SECTIONS_DIR = str(_NETGEN_DIR / "data" / "sections_quantile_run_20260706_130433")
DEFAULT_DATA_DIR = r"data"

DTM_PATH = _SCRIPT_DIR / "data" / "DTM Germany 20m v3b by Sonny.tif"
SIMPLIFIED_GPKG = _NETGEN_DIR / "data" / "germany_simplified_DF.gpkg"
DETAILED_GPKG = _NETGEN_DIR / "data" / "germany_detailed_sorted_DF.gpkg"

# Hoehenzuweisung (wie generate_network-Defaults in Skript 04, A3 = geglaettet)
SAMPLE_STEP_M = 5.0
SMOOTH_RMS_M = 1.0

# Reduzierte Leiter fuer die 20-Sektionen-Studie (identisch zur Analyse);
# dicht genug fuer Kneedle je Sektion, 12 statt 22 Stufen (Rechenzeit).
LINK_LENGTHS = [50, 100, 150, 200, 250, 300, 350, 400, 500, 600, 750, 1000]

# 20 Sektionen ueber die sigma_g-Quantile (Auswahl 2026-07-06 auf V2)
SECTION_FILES = {
    f"q{q}": f"section_q{q}_100km.xml.gz"
    for q in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
              55, 60, 65, 70, 75, 80, 85, 90, 95, 97)
}

TARGET_EPSG = 4839
NETWORK_CRS = "EPSG:4839"
CORRIDOR_BUFFER_M = 500  # meters buffer around reference route for spatial filtering
MAX_LENGTH_DEVIATION = 0.05  # 5% max allowed deviation from reference route length


# ==============================
# Load a MATSim network XML
# ==============================
def load_matsim_network(path):
    """Load nodes and links from a MATSim XML network."""
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()

    nodes = {}
    for node in root.find("nodes").findall("node"):
        nid = node.get("id")
        x = float(node.get("x"))
        y = float(node.get("y"))
        z_attr = node.get("z")
        z = float(z_attr) if z_attr is not None else None
        nodes[nid] = {"x": x, "y": y, "z": z}

    links = {}
    for link in root.find("links").findall("link"):
        lid = link.get("id")
        u = link.get("from")
        v = link.get("to")
        length = float(link.get("length", "0"))
        links[lid] = {"id": lid, "from": u, "to": v, "length": length}

    return nodes, links


def build_route_corridor(section_nodes, section_links, buffer_m=CORRIDOR_BUFFER_M):
    """Build a corridor polygon around the reference route in WGS84.

    1. Find ordered path through the section network (BFS between endpoints)
    2. Create LineString from ordered node coordinates (EPSG:4839)
    3. Buffer by buffer_m meters in EPSG:4839
    4. Transform to WGS84 (EPSG:4326) for filtering gpkg data
    """
    from pyproj import Transformer

    start, end = find_endpoints(section_nodes, section_links)
    path = find_ordered_path(section_nodes, section_links, start, end)
    if path is None:
        raise ValueError("Cannot find ordered path in reference section network")

    # Build LineString in EPSG:4839 (metric CRS)
    coords_4839 = [(section_nodes[nid]["x"], section_nodes[nid]["y"]) for nid in path]
    line_4839 = LineString(coords_4839)

    # Buffer in metric CRS
    corridor_4839 = line_4839.buffer(buffer_m)

    # Transform to WGS84
    transformer = Transformer.from_crs(NETWORK_CRS, "EPSG:4326", always_xy=True)
    corridor_wgs84 = shapely_transform(transformer.transform, corridor_4839)

    return corridor_wgs84


def find_endpoints(nodes, links):
    """Find the two endpoint nodes (degree=1 in undirected view) of a section network."""
    adj = {}
    for lid, lk in links.items():
        u, v = lk["from"], lk["to"]
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)

    endpoints = [nid for nid, neighbors in adj.items() if len(neighbors) == 1]
    if len(endpoints) < 2:
        # Fallback: use nodes with lowest degree
        by_degree = sorted(adj.items(), key=lambda x: len(x[1]))
        endpoints = [by_degree[0][0], by_degree[-1][0]]

    return endpoints[0], endpoints[1]


def find_ordered_path(nodes, links, start, end):
    """BFS to find ordered path from start to end through the network."""
    adj = {}
    for lid, lk in links.items():
        u, v = lk["from"], lk["to"]
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)

    visited = {start}
    parent = {start: None}
    queue = deque([start])

    while queue:
        curr = queue.popleft()
        if curr == end:
            break
        for nb in adj.get(curr, set()):
            if nb not in visited:
                visited.add(nb)
                parent[nb] = curr
                queue.append(nb)

    if end not in parent:
        return None

    path = []
    node = end
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()
    return path


def find_nearest_node(target_x, target_y, candidate_nodes):
    """Find the nearest node to (target_x, target_y) in candidate_nodes dict."""
    best_nid = None
    best_dist = float("inf")
    for nid, nd in candidate_nodes.items():
        dx = nd["x"] - target_x
        dy = nd["y"] - target_y
        d = math.hypot(dx, dy)
        if d < best_dist:
            best_dist = d
            best_nid = nid
    return best_nid, best_dist


def export_path_subnetwork(nodes, links, link_elems_root, ordered_path, output_path):
    """Export a bidirectional MATSim sub-network for links along the ordered path.

    Creates exactly two links per consecutive node pair (A->B and B->A),
    producing 2*(N-1) links for N path nodes. Each undirected edge appears
    exactly once as a forward/reverse pair, deduplicated via the path order.
    """
    # Build set of allowed (from, to) pairs from consecutive path nodes (both directions
    # so we can find the source link regardless of its original direction)
    allowed_edges = set()
    for i in range(len(ordered_path) - 1):
        allowed_edges.add((ordered_path[i], ordered_path[i + 1]))
        allowed_edges.add((ordered_path[i + 1], ordered_path[i]))

    network = ET.Element("network")
    network.insert(1, ET.Comment("=" * 70))
    nodes_element = ET.SubElement(network, "nodes")
    network.append(ET.Comment("=" * 70))
    links_element = ET.SubElement(
        network, "links",
        capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75"
    )
    network.append(ET.Comment("=" * 70))

    used_node_ids = set()
    n_links = 0

    # Build lookup: frozenset({u,v}) -> link element (first match from source)
    pair_to_source = {}
    for link in link_elems_root.find("links").findall("link"):
        u = link.get("from")
        v = link.get("to")
        if (u, v) not in allowed_edges:
            continue
        pair = frozenset((u, v))
        if pair not in pair_to_source:
            pair_to_source[pair] = link

    # Create two links per consecutive path pair (forward + reverse = bidirectional)
    for i in range(len(ordered_path) - 1):
        from_id = ordered_path[i]
        to_id = ordered_path[i + 1]
        pair = frozenset((from_id, to_id))
        src = pair_to_source.get(pair)
        if src is None:
            continue
        for a, b in [(from_id, to_id), (to_id, from_id)]:
            lid = f"{a}-{b}"
            le = ET.SubElement(links_element, "link",
                id=lid, **{"from": a, "to": b},
                length=src.get("length"),
                freespeed=src.get("freespeed"),
                capacity=src.get("capacity"),
                permlanes=src.get("permlanes"),
                modes="car")
            attrs = ET.SubElement(le, "attributes")
            src_attrs = src.find("attributes")
            if src_attrs is not None:
                for attr in src_attrs.findall("attribute"):
                    if attr.get("name") == "oneway_source":
                        continue
                    attrs.append(copy.deepcopy(attr))
            a_oneway = ET.SubElement(attrs, "attribute",
                name="oneway_source", **{"class": "java.lang.String"})
            a_oneway.text = "0"
            n_links += 1
        used_node_ids.add(from_id)
        used_node_ids.add(to_id)

    for nid in used_node_ids:
        nd = nodes[nid]
        node_attrs = {"id": nid, "x": f"{nd['x']}", "y": f"{nd['y']}"}
        if nd.get("z") is not None and math.isfinite(nd["z"]):
            node_attrs["z"] = f"{nd['z']}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    ET.indent(network, space="  ")
    xml_string = ET.tostring(network, encoding="unicode")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write(xml_string)
        f.write('\n')

    return n_links


def generate_variants_for_section(
    section_label, section_path, link_lengths, dtm,
    script04,
    gdf_nodes_simplified, gdf_edges_simplified,
    gdf_nodes_detailed, gdf_edges_detailed,
    output_dir
):
    """Generate network variants at different link lengths for one section."""
    print(f"\n{'='*60}")
    print(f"Processing section: {section_label}")
    print(f"{'='*60}")

    # 1) Load section network to get bounding box and endpoints
    section_nodes, section_links = load_matsim_network(section_path)
    print(f"  Section has {len(section_nodes)} nodes, {len(section_links)} links")

    start_node, end_node = find_endpoints(section_nodes, section_links)
    start_xy = (section_nodes[start_node]["x"], section_nodes[start_node]["y"])
    end_xy = (section_nodes[end_node]["x"], section_nodes[end_node]["y"])
    print(f"  Start node: {start_node} at ({start_xy[0]:.0f}, {start_xy[1]:.0f})")
    print(f"  End node:   {end_node} at ({end_xy[0]:.0f}, {end_xy[1]:.0f})")

    # 2) Build corridor polygon around reference route for spatial filtering
    corridor_polygon = build_route_corridor(section_nodes, section_links)
    corridor_bounds = corridor_polygon.bounds  # (minx, miny, maxx, maxy)
    print(f"  Corridor bounds (WGS84): lon=[{corridor_bounds[0]:.3f}, {corridor_bounds[2]:.3f}], "
          f"lat=[{corridor_bounds[1]:.3f}, {corridor_bounds[3]:.3f}]")

    # Reference route total length for validation
    ref_total_length = sum(lk["length"] for lk in section_links.values())
    print(f"  Reference route length: {ref_total_length:.1f} m")

    # 3) Spatial filter of gpkg edges using corridor polygon
    edges_simp_filtered = gdf_edges_simplified[gdf_edges_simplified.intersects(corridor_polygon)].copy()
    edges_det_filtered = gdf_edges_detailed[gdf_edges_detailed.intersects(corridor_polygon)].copy()

    # Also filter nodes that appear in filtered edges
    used_node_ids_simp = set(edges_simp_filtered["u"].astype(str)) | set(edges_simp_filtered["v"].astype(str))
    used_node_ids_det = set(edges_det_filtered["u"].astype(str)) | set(edges_det_filtered["v"].astype(str))

    print(f"  Filtered simplified edges: {len(edges_simp_filtered)} (from {len(gdf_edges_simplified)})")
    print(f"  Filtered detailed edges:   {len(edges_det_filtered)} (from {len(gdf_edges_detailed)})")

    if edges_simp_filtered.empty:
        print(f"  WARNING: No simplified edges in corridor! Skipping section.")
        return []

    results = []

    for max_len in link_lengths:
        out_file = output_dir / f"section_{section_label}_{max_len}m.xml.gz"
        if out_file.exists():
            print(f"\n  --- max_link_length = {max_len} m --- SKIP (already exists: {out_file.name})")
            results.append({
                "section": section_label,
                "max_link_length": max_len,
                "n_nodes": -1,
                "n_links": -1,
                "output_file": str(out_file),
            })
            continue

        print(f"\n  --- max_link_length = {max_len} m ---")

        # 4) Run short_edges
        edges_shortened, split_nodes_xy = script04.short_edges(
            gdf_edges_simplified=edges_simp_filtered.copy(),
            gdf_edges_detailed=edges_det_filtered.copy(),
            max_allowed_length=float(max_len)
        )

        # 5) Build node GeoDataFrame with heights
        used_nodes_set = set(map(str, edges_shortened["u"])) | set(map(str, edges_shortened["v"]))

        def _first_scalar(v):
            if isinstance(v, (list, tuple, np.ndarray)) and len(v) > 0:
                return v[0]
            return v

        # Build node coordinate lookup from detailed nodes
        gdf_nodes_det_copy = gdf_nodes_detailed.copy()
        gdf_nodes_det_copy["osmid_norm"] = pd.to_numeric(
            gdf_nodes_det_copy["osmid"].apply(_first_scalar), errors="coerce"
        )
        gdf_nodes_det_copy = gdf_nodes_det_copy.dropna(subset=["osmid_norm"]).copy()
        gdf_nodes_det_copy["osmid_norm"] = gdf_nodes_det_copy["osmid_norm"].astype(int)

        osm_xy = {
            str(int(r["osmid_norm"])): (float(r.geometry.x), float(r.geometry.y))
            for _, r in gdf_nodes_det_copy.iterrows()
        }

        def _xy_from_edge(nid):
            rows = edges_shortened[
                (edges_shortened["u"].astype(str) == nid) |
                (edges_shortened["v"].astype(str) == nid)
            ]
            if rows.empty:
                return None
            r = rows.iloc[0]
            line = r.geometry
            if str(r["u"]) == nid:
                return (float(line.coords[0][0]), float(line.coords[0][1]))
            else:
                return (float(line.coords[-1][0]), float(line.coords[-1][1]))

        node_rows, seen = [], set()
        for nid in used_nodes_set:
            if nid in seen:
                continue
            if nid in osm_xy:
                x, y = osm_xy[nid]
            elif nid in split_nodes_xy:
                x, y = split_nodes_xy[nid]
            else:
                fb = _xy_from_edge(nid)
                if fb is None:
                    continue
                x, y = fb
            node_rows.append({"osmid": nid, "geometry": Point(x, y)})
            seen.add(nid)

        gdf_nodes_export = gpd.GeoDataFrame(node_rows, crs="EPSG:4326")
        # Hoehen wie in script04.generate_network: korridor-geglaettetes DTM-Profil
        # (aufloesungsunabhaengig -> alle max_len-Varianten teilen dieselbe Hoehenbasis).
        node_lonlat = {str(r["osmid"]): (float(r["geometry"].x), float(r["geometry"].y))
                       for r in node_rows}
        z_by_node = script04.assign_heights_along_corridors(
            edges_shortened, node_lonlat, dtm,
            target_epsg=TARGET_EPSG, sample_step_m=SAMPLE_STEP_M, smooth_rms_m=SMOOTH_RMS_M)
        gdf_nodes_export["height"] = [z_by_node.get(str(nid), np.nan)
                                      for nid in gdf_nodes_export["osmid"]]

        # 6) Write full regional MATSim network
        edges_clean = script04.sanitize_edges_for_export(edges_shortened)

        tmp_network_path = output_dir / f"_tmp_{section_label}_{max_len}m.xml.gz"
        script04.write_matsim_network(
            gdf_nodes=gdf_nodes_export,
            gdf_edges=edges_clean,
            epsg_code=TARGET_EPSG,
            output_path=str(tmp_network_path),
            nodes_without_z=set()
        )

        # 7) Load the generated network and find the section path
        with gzip.open(tmp_network_path, "rb") as f:
            full_tree = ET.parse(f)
        full_root = full_tree.getroot()

        full_nodes = {}
        for node in full_root.find("nodes").findall("node"):
            nid = node.get("id")
            x = float(node.get("x"))
            y = float(node.get("y"))
            z_attr = node.get("z")
            z = float(z_attr) if z_attr is not None else None
            full_nodes[nid] = {"x": x, "y": y, "z": z}

        full_links = {}
        for link in full_root.find("links").findall("link"):
            lid = link.get("id")
            u = link.get("from")
            v = link.get("to")
            length = float(link.get("length", "0"))
            full_links[lid] = {"id": lid, "from": u, "to": v, "length": length}

        # Find nearest nodes to section start/end in the new network
        new_start, dist_s = find_nearest_node(start_xy[0], start_xy[1], full_nodes)
        new_end, dist_e = find_nearest_node(end_xy[0], end_xy[1], full_nodes)

        if new_start is None or new_end is None:
            print(f"    WARNING: Could not find start/end nodes. Skipping.")
            tmp_network_path.unlink(missing_ok=True)
            continue

        print(f"    Matched start: {new_start} (dist={dist_s:.1f}m), end: {new_end} (dist={dist_e:.1f}m)")

        # BFS to find path
        path = find_ordered_path(full_nodes, full_links, new_start, new_end)
        if path is None:
            print(f"    WARNING: No path found between start and end. Skipping.")
            tmp_network_path.unlink(missing_ok=True)
            continue

        print(f"    Path: {len(path)} nodes")

        # Length validation: compute path total length and compare to reference
        path_node_set = set(path)
        path_length = sum(
            lk["length"] for lk in full_links.values()
            if lk["from"] in path_node_set and lk["to"] in path_node_set
        )
        length_deviation = abs(path_length - ref_total_length) / ref_total_length if ref_total_length > 0 else 0
        print(f"    Path length: {path_length:.1f} m (ref: {ref_total_length:.1f} m, "
              f"deviation: {length_deviation:.1%})")

        if length_deviation > MAX_LENGTH_DEVIATION:
            print(f"    WARNING: Length deviation {length_deviation:.1%} exceeds "
                  f"{MAX_LENGTH_DEVIATION:.0%} threshold. Skipping variant.")
            tmp_network_path.unlink(missing_ok=True)
            continue

        # 8) Export section sub-network
        out_file = output_dir / f"section_{section_label}_{max_len}m.xml.gz"
        n_exported = export_path_subnetwork(full_nodes, full_links, full_root, path, str(out_file))
        print(f"    Exported: {n_exported} links -> {out_file.name}")

        # Clean up temp file
        tmp_network_path.unlink(missing_ok=True)

        results.append({
            "section": section_label,
            "max_link_length": max_len,
            "n_nodes": len(path),
            "n_links": n_exported,
            "total_length_m": path_length,
            "length_deviation": length_deviation,
            "output_file": str(out_file),
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Generate section network variants at different link lengths.")
    parser.add_argument("--sections-dir", type=str, default=DEFAULT_SECTIONS_DIR,
                        help="Directory containing section_q*.xml.gz files")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory (default: auto-timestamped under data/)")
    parser.add_argument("--link-lengths", type=str, default=None,
                        help="Comma-separated link lengths in meters "
                             "(Default: knie-orientierte Leiter 50..1000, s. LINK_LENGTHS)")
    args = parser.parse_args()

    sections_dir = Path(args.sections_dir)
    if not sections_dir.is_absolute():
        sections_dir = _SCRIPT_DIR / sections_dir

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = _SCRIPT_DIR / f"data/section_variants_{datetime.now():%Y%m%d_%H%M%S}"
    output_dir.mkdir(parents=True, exist_ok=True)

    link_lengths = LINK_LENGTHS
    if args.link_lengths:
        link_lengths = [int(x.strip()) for x in args.link_lengths.split(",")]

    print(f"Sections directory: {sections_dir}")
    print(f"Output directory:   {output_dir}")
    print(f"Link lengths:       {link_lengths}")

    # Load script 04 functions
    print("\nLoading script 04 module...")
    script04 = _import_script04()

    # Load shared data (DTM + gpkg)
    print("\nLoading DTM...")
    dtm = script04.load_dtm(str(DTM_PATH))

    print("Loading simplified gpkg...")
    gdf_nodes_simplified, gdf_edges_simplified = script04.load_local_osm_file(str(SIMPLIFIED_GPKG))

    print("Loading detailed gpkg...")
    gdf_nodes_detailed, gdf_edges_detailed = script04.load_local_osm_file(str(DETAILED_GPKG))

    all_results = []

    for label, filename in SECTION_FILES.items():
        section_path = sections_dir / filename
        if not section_path.exists():
            print(f"\nWARNING: Section file not found: {section_path}. Skipping.")
            continue

        results = generate_variants_for_section(
            section_label=label,
            section_path=str(section_path),
            link_lengths=link_lengths,
            dtm=dtm,
            script04=script04,
            gdf_nodes_simplified=gdf_nodes_simplified,
            gdf_edges_simplified=gdf_edges_simplified,
            gdf_nodes_detailed=gdf_nodes_detailed,
            gdf_edges_detailed=gdf_edges_detailed,
            output_dir=output_dir,
        )
        all_results.extend(results)

    # Save summary CSV
    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_path = output_dir / "variants_summary.csv"
        summary_df.to_csv(summary_path, index=False)
        print(f"\nSummary saved to: {summary_path}")

    print(f"\nDone! Generated {len(all_results)} network variants in {output_dir}")
    return str(output_dir)


if __name__ == "__main__":
    main()
