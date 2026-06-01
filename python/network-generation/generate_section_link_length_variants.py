# -*- coding: utf-8 -*-
"""
Generate MATSim network variants at different max link lengths for representative sections.

For each section (Q50/Q75/Q97) and each max_link_length, this script:
  1. Reads the fine (50m) section network to get the bounding box + buffer
  2. Spatially filters the Germany-wide gpkg data to that bounding box
  3. Runs short_edges() from script 04 with the target max_link_length
  4. Builds a MATSim network from the resulting edges + KDTree heights
  5. Finds the section path in the new network (start/end node matching)
  6. Exports only the section-path links as a MATSim sub-network

Usage:
    python generate_section_link_length_variants.py [--sections-dir <path>] [--output-dir <path>]
"""

import argparse
import gzip
import math
import sys
import xml.dom.minidom as md
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box
from tqdm import tqdm

# Import functions from script 04
from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent

def _import_script04():
    """Import script 04 as a module."""
    script_path = _SCRIPT_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    spec = spec_from_file_location("script04", str(script_path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ==============================
# Configuration
# ==============================
DEFAULT_SECTIONS_DIR = r"data\sections_quantile_run_20260307_090732"
DEFAULT_DATA_DIR = r"data"

KDTREE_PATH = r"data\germany_3d_raster_clamped_DF_kdtree_from_roads3d_epsg4326.npz"
SIMPLIFIED_GPKG = r"data\germany_simplified_DF.gpkg"
DETAILED_GPKG = r"data\germany_detailed_sorted_DF.gpkg"

LINK_LENGTHS = [50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000]

SECTION_FILES = {
    "q50": "section_q50_100km.xml.gz",
    "q75": "section_q75_100km.xml.gz",
    "q97": "section_q97_100km.xml.gz",
}

TARGET_EPSG = 4839
NETWORK_CRS = "EPSG:4839"
BUFFER_DEG = 0.1  # ~10 km buffer in degrees around section bounding box


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


def get_bounding_box_wgs84(nodes):
    """Get bounding box of nodes (in EPSG:4839) converted to WGS84, with buffer."""
    from pyproj import Transformer
    transformer = Transformer.from_crs(NETWORK_CRS, "EPSG:4326", always_xy=True)

    xs = [n["x"] for n in nodes.values()]
    ys = [n["y"] for n in nodes.values()]

    # Transform corners to WGS84
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    lon_min, lat_min = transformer.transform(min_x, min_y)
    lon_max, lat_max = transformer.transform(max_x, max_y)

    # Add buffer
    lon_min -= BUFFER_DEG
    lon_max += BUFFER_DEG
    lat_min -= BUFFER_DEG
    lat_max += BUFFER_DEG

    return lon_min, lat_min, lon_max, lat_max


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


def export_path_subnetwork(nodes, links, link_elems_root, path_node_set, output_path):
    """Export a MATSim sub-network for nodes in path_node_set."""
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

    # Find links where both endpoints are in the path
    for link in link_elems_root.find("links").findall("link"):
        u = link.get("from")
        v = link.get("to")
        if u in path_node_set and v in path_node_set:
            links_element.append(link)
            used_node_ids.add(u)
            used_node_ids.add(v)
            n_links += 1

    for nid in used_node_ids:
        nd = nodes[nid]
        node_attrs = {"id": nid, "x": f"{nd['x']}", "y": f"{nd['y']}"}
        if nd.get("z") is not None and math.isfinite(nd["z"]):
            node_attrs["z"] = f"{nd['z']}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    xml_string = ET.tostring(network, encoding="utf-8")
    pretty_xml = md.parseString(xml_string).toprettyxml()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write("\n".join(pretty_xml.splitlines()[1:]))

    return n_links


def generate_variants_for_section(
    section_label, section_path, link_lengths,
    script04, tree, coords, heights,
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

    # 2) Get bounding box in WGS84 for spatial filtering
    bbox = get_bounding_box_wgs84(section_nodes)
    print(f"  Bounding box (WGS84): lon=[{bbox[0]:.3f}, {bbox[2]:.3f}], lat=[{bbox[1]:.3f}, {bbox[3]:.3f}]")

    # 3) Spatial filter of gpkg edges
    bbox_geom = box(bbox[0], bbox[1], bbox[2], bbox[3])

    edges_simp_filtered = gdf_edges_simplified[gdf_edges_simplified.intersects(bbox_geom)].copy()
    edges_det_filtered = gdf_edges_detailed[gdf_edges_detailed.intersects(bbox_geom)].copy()

    # Also filter nodes that appear in filtered edges
    used_node_ids_simp = set(edges_simp_filtered["u"].astype(str)) | set(edges_simp_filtered["v"].astype(str))
    used_node_ids_det = set(edges_det_filtered["u"].astype(str)) | set(edges_det_filtered["v"].astype(str))

    print(f"  Filtered simplified edges: {len(edges_simp_filtered)} (from {len(gdf_edges_simplified)})")
    print(f"  Filtered detailed edges:   {len(edges_det_filtered)} (from {len(gdf_edges_detailed)})")

    if edges_simp_filtered.empty:
        print(f"  WARNING: No simplified edges in bounding box! Skipping section.")
        return []

    results = []

    for max_len in link_lengths:
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
        xs = gdf_nodes_export.geometry.x.to_numpy()
        ys = gdf_nodes_export.geometry.y.to_numpy()
        gdf_nodes_export["height"] = script04.kdtree_heights_vectorized(tree, heights, xs, ys)

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

        path_node_set = set(path)
        print(f"    Path: {len(path)} nodes")

        # 8) Export section sub-network
        out_file = output_dir / f"section_{section_label}_{max_len}m.xml.gz"
        n_exported = export_path_subnetwork(full_nodes, full_links, full_root, path_node_set, str(out_file))
        print(f"    Exported: {n_exported} links -> {out_file.name}")

        # Clean up temp file
        tmp_network_path.unlink(missing_ok=True)

        results.append({
            "section": section_label,
            "max_link_length": max_len,
            "n_nodes": len(path),
            "n_links": n_exported,
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
                        help="Comma-separated link lengths in meters (default: 50,100,...,5000)")
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

    # Load shared data (KDTree + gpkg)
    print("\nLoading KDTree...")
    kdtree_path = _SCRIPT_DIR / KDTREE_PATH
    tree, coords, heights = script04.load_kdtree(str(kdtree_path))

    print("Loading simplified gpkg...")
    simp_path = _SCRIPT_DIR / SIMPLIFIED_GPKG
    gdf_nodes_simplified, gdf_edges_simplified = script04.load_local_osm_file(str(simp_path))

    print("Loading detailed gpkg...")
    det_path = _SCRIPT_DIR / DETAILED_GPKG
    gdf_nodes_detailed, gdf_edges_detailed = script04.load_local_osm_file(str(det_path))

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
            script04=script04,
            tree=tree, coords=coords, heights=heights,
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
