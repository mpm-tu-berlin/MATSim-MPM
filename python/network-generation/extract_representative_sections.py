# -*- coding: utf-8 -*-
"""
Extract three representative ~100 km road sections (flat, medium, hilly) from
a smoothed MATSim network.  Selection is based on sigma_g (standard deviation of
link-wise grade), which captures both amplitude and frequency of grade changes.

Two networks are used:
  - A coarse network (500 m max link length) for path-finding and feature
    computation (fast, low RAM).
  - A fine network (50 m max link length) for the final MATSim sub-network
    export (high elevation resolution for simulation).

Multiple iterations with different random corridor-building orders are run in
parallel to discover more candidate paths.  All outputs go into a timestamped
folder under data/ for archival.
"""

import os
os.environ["PYTHONHASHSEED"] = "0"  # deterministic hashing in worker processes

import base64
import gzip
import io
import json
import math
import xml.etree.ElementTree as ET
import xml.dom.minidom as md
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from math import hypot, pi
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
from tqdm import tqdm

# ==============================================================================
# Configuration
# ==============================================================================
COARSE_NETWORK = r"data\Germany_max500m_V0_smoothed.xml.gz"   # for path search
FINE_NETWORK   = r"data\Germany_max50m_V0_smoothed.xml.gz"    # for final export

TARGET_LENGTH_M    = 100_000   # target section length
LENGTH_TOLERANCE_M = 2_000     # +/- tolerance around target
MAX_ANGLE_RAD      = pi / 2    # reject turns sharper than 90 deg
N_ITERATIONS       = 50        # number of randomised corridor-building iterations

COLORS = {"flat": "#2196F3", "medium": "#FF9800", "hilly": "#E53935"}
LABELS = {"flat": "Flat", "medium": "Medium", "hilly": "Hilly"}

# CRS of the MATSim network
NETWORK_CRS = "EPSG:4839"


# ==============================================================================
# Part 1: Load MATSim network
# ==============================================================================
def load_network(path):
    """Load a MATSim XML network -> nodes dict, links dict, raw link elements."""
    print(f"Loading network: {path}")
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()

    nodes = {}
    for node in root.find("nodes").findall("node"):
        nid = node.get("id")Bitte
        x = float(node.get("x"))
        y = float(node.get("y"))
        z_attr = node.get("z")
        z = float(z_attr) if z_attr is not None else 0.0
        nodes[nid] = {"x": x, "y": y, "z": z}

    links = {}
    link_elems = {}
    for link in root.find("links").findall("link"):
        lid = link.get("id")
        u = link.get("from")
        v = link.get("to")
        length = float(link.get("length", "0"))
        links[lid] = {"id": lid, "from": u, "to": v, "length": length}
        link_elems[lid] = link

    print(f"  {len(nodes)} nodes, {len(links)} links")
    return nodes, links, link_elems


# ==============================================================================
# Part 2: Build corridor graph (with seed for randomised iteration order)
# ==============================================================================
def build_graph_and_corridors(nodes, links, seed=None):
    """Build undirected graph, find corridors (chains of degree-2 nodes).

    When seed is not None, the crossing iteration order is shuffled so that
    different seeds produce different corridor decompositions.
    """
    G = nx.Graph()
    edge_link_ids = {}
    for lid in sorted(links):  # sorted for determinism across processes
        lk = links[lid]
        u, v = lk["from"], lk["to"]
        if u in nodes and v in nodes:
            key = tuple(sorted((u, v)))
            if key not in edge_link_ids:
                G.add_edge(u, v, length=lk["length"])
                edge_link_ids[key] = lid
            else:
                existing = links[edge_link_ids[key]]["length"]
                if lk["length"] < existing:
                    G[u][v]["length"] = lk["length"]
                    edge_link_ids[key] = lid

    deg = dict(G.degree())
    crossings = {n for n, d in deg.items() if d != 2 and d > 0}
    corridor_nodes = {n for n, d in deg.items() if 0 < d < 3}

    visited_edges = set()

    def edge_key(a, b):
        return tuple(sorted((a, b)))

    def walk_corridor(start, nb):
        path = [start, nb]
        prev, curr = start, nb
        while True:
            next_nodes = [x for x in G.neighbors(curr) if x != prev]
            if not next_nodes:
                break
            nxt = None
            for cand in next_nodes:
                if cand in corridor_nodes:
                    nxt = cand
                    break
            if nxt is None:
                endp = next_nodes[0]
                path.append(endp)
                visited_edges.add(edge_key(curr, endp))
                break
            if edge_key(curr, nxt) in visited_edges:
                break
            path.append(nxt)
            visited_edges.add(edge_key(curr, nxt))
            prev, curr = curr, nxt
        return path

    # Randomise iteration order so different seeds yield different corridors
    rng = np.random.default_rng(seed)
    start_order = sorted(crossings)  # sorted for determinism before shuffle
    rng.shuffle(start_order)

    corridors = []
    for start in start_order:
        neighbors = sorted(G.neighbors(start))  # sorted for determinism before shuffle
        rng.shuffle(neighbors)
        for nb in neighbors:
            ek = edge_key(start, nb)
            if ek in visited_edges:
                continue
            visited_edges.add(ek)
            path = walk_corridor(start, nb)
            if len(path) < 2:
                continue
            length = 0.0
            for i in range(len(path) - 1):
                a, b = path[i], path[i + 1]
                dx = nodes[a]["x"] - nodes[b]["x"]
                dy = nodes[a]["y"] - nodes[b]["y"]
                length += hypot(dx, dy)

            def direction_vec(n_from, n_to):
                dx = nodes[n_to]["x"] - nodes[n_from]["x"]
                dy = nodes[n_to]["y"] - nodes[n_from]["y"]
                mag = hypot(dx, dy)
                if mag < 1e-9:
                    return (1.0, 0.0)
                return (dx / mag, dy / mag)

            corridors.append({
                "nodes": path,
                "start": path[0],
                "end": path[-1],
                "length": length,
                "dir_start": direction_vec(path[0], path[1]),
                "dir_end": direction_vec(path[-2], path[-1]),
            })

    corridor_adj = {}
    for i, c in enumerate(corridors):
        corridor_adj.setdefault(c["start"], []).append(i)
        corridor_adj.setdefault(c["end"], []).append(i)

    return G, crossings, corridors, corridor_adj, edge_link_ids


# ==============================================================================
# Part 3: Generate ~100 km paths (greedy straight-ahead)
# ==============================================================================
def angle_between(dx1, dy1, dx2, dy2):
    """Angle in radians between two direction vectors (0 = same direction)."""
    dot = dx1 * dx2 + dy1 * dy2
    dot = max(-1.0, min(1.0, dot))
    return math.acos(dot)


def generate_paths(nodes, crossings, corridors, corridor_adj):
    """Generate ~100 km paths using greedy straight-ahead at each crossing."""
    start_nodes = sorted(crossings)

    min_len = TARGET_LENGTH_M - LENGTH_TOLERANCE_M
    max_len = TARGET_LENGTH_M + LENGTH_TOLERANCE_M

    paths = []
    seen_path_keys = set()

    for start in start_nodes:
        if start not in corridor_adj:
            continue
        for first_corr_idx in corridor_adj[start]:
            corr = corridors[first_corr_idx]

            if corr["start"] == start:
                next_node = corr["end"]
                heading = corr["dir_end"]
                corr_nodes = corr["nodes"]
            else:
                next_node = corr["start"]
                heading = (-corr["dir_start"][0], -corr["dir_start"][1])
                corr_nodes = list(reversed(corr["nodes"]))

            path_corr_indices = [first_corr_idx]
            path_nodes = list(corr_nodes)
            L_acc = corr["length"]
            used_corridors = {first_corr_idx}
            ok = True

            while L_acc < max_len:
                if next_node not in corridor_adj:
                    ok = False
                    break

                best_idx = None
                best_angle = pi
                best_next = None
                best_dir = None
                best_nodes = None

                for ci in corridor_adj[next_node]:
                    if ci in used_corridors:
                        continue
                    c = corridors[ci]
                    if c["start"] == next_node:
                        c_dir = c["dir_start"]
                        c_end = c["end"]
                        c_nodes = c["nodes"]
                    else:
                        c_dir = (-c["dir_end"][0], -c["dir_end"][1])
                        c_end = c["start"]
                        c_nodes = list(reversed(c["nodes"]))

                    ang = angle_between(heading[0], heading[1], c_dir[0], c_dir[1])
                    if ang < best_angle:
                        best_angle = ang
                        best_idx = ci
                        best_next = c_end
                        best_nodes = c_nodes
                        if c["start"] == next_node:
                            best_dir = c["dir_end"]
                        else:
                            best_dir = (-c["dir_start"][0], -c["dir_start"][1])

                if best_idx is None or best_angle > MAX_ANGLE_RAD:
                    ok = False
                    break

                used_corridors.add(best_idx)
                path_corr_indices.append(best_idx)
                path_nodes.extend(best_nodes[1:])
                L_acc += corridors[best_idx]["length"]
                heading = best_dir
                next_node = best_next

            if not ok and L_acc < min_len:
                continue
            if L_acc < min_len or L_acc > max_len:
                continue

            path_key = tuple(path_nodes)
            if path_key in seen_path_keys:
                continue
            seen_path_keys.add(path_key)

            paths.append({
                "node_list": path_nodes,
                "length_approx": L_acc,
            })

    return paths


# ==============================================================================
# Part 3b: Single-iteration worker for parallel execution
# ==============================================================================
def _run_iteration(args):
    """Worker function: build corridors with given seed, find paths."""
    coarse_nodes, coarse_links, seed = args
    _, crossings, corridors, corridor_adj, _ = \
        build_graph_and_corridors(coarse_nodes, coarse_links, seed=seed)
    paths = generate_paths(coarse_nodes, crossings, corridors, corridor_adj)
    return paths


# ==============================================================================
# Part 4: Compute features for each path
# ==============================================================================
def compute_features(path_nodes, nodes):
    """Compute all 11 metrics for a node sequence."""
    n = len(path_nodes)
    if n < 2:
        return None

    z = np.array([nodes[nid]["z"] for nid in path_nodes], dtype=float)
    lengths = np.array([
        hypot(nodes[path_nodes[i+1]]["x"] - nodes[path_nodes[i]]["x"],
              nodes[path_nodes[i+1]]["y"] - nodes[path_nodes[i]]["y"])
        for i in range(n - 1)
    ], dtype=float)

    lengths_safe = np.where(lengths > 0.1, lengths, 0.1)

    L = float(np.sum(lengths))
    if L < 1.0:
        return None

    dz = np.diff(z)
    grades = dz / lengths_safe

    g_abs_mean = float(np.sum(np.abs(grades) * lengths) / L)
    mu_g = float(np.sum(grades * lengths) / L)
    sigma_g = float(np.sqrt(np.sum(lengths * (grades - mu_g) ** 2) / L))
    D_plus = float(np.sum(np.maximum(0, dz)))
    D_minus = float(np.sum(np.abs(np.minimum(0, dz))))
    D_plus_per_km = D_plus / (L / 1000.0)
    delta_z = float(np.max(z) - np.min(z))
    g_max = float(np.max(np.abs(grades)))
    sign_changes = np.sum(np.diff(np.sign(grades)) != 0)
    f_und = float(sign_changes) / (L / 1000.0)

    return {
        "L_m": L,
        "g_abs_mean": g_abs_mean,
        "mu_g": mu_g,
        "sigma_g": sigma_g,
        "D_plus_m": D_plus,
        "D_minus_m": D_minus,
        "D_plus_per_km": D_plus_per_km,
        "delta_z_m": delta_z,
        "g_max": g_max,
        "f_und_per_km": f_und,
    }


# ==============================================================================
# Part 5: Select flat / medium / hilly
# ==============================================================================
def select_sections(df):
    """Select flat, medium, hilly sections by sigma_g."""
    idx_flat  = df["sigma_g"].idxmin()
    idx_hilly = df["sigma_g"].idxmax()
    median_sg = df["sigma_g"].median()
    idx_med   = (df["sigma_g"] - median_sg).abs().idxmin()

    print(f"\n--- Selected sections ---")
    for label, idx in [("FLAT", idx_flat), ("MEDIUM", idx_med), ("HILLY", idx_hilly)]:
        row = df.loc[idx]
        print(f"  {label:8s}: sigma_g={row['sigma_g']:.5f}  D+/km={row['D_plus_per_km']:.1f}  "
              f"f_und={row['f_und_per_km']:.1f}/km  L={row['L_m']/1000:.1f} km  "
              f"dz={row['delta_z_m']:.0f} m")

    return idx_flat, idx_med, idx_hilly


# ==============================================================================
# Part 6: Export sub-network from fine network
# ==============================================================================
def export_subnetwork(fine_nodes, fine_links, fine_link_elems, path_node_set, output_path):
    """Export a MATSim sub-network for nodes in path_node_set."""
    print(f"Exporting sub-network to {output_path}...")

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
    for lid, lk in fine_links.items():
        if lk["from"] in path_node_set and lk["to"] in path_node_set:
            orig_elem = fine_link_elems[lid]
            links_element.append(orig_elem)
            used_node_ids.add(lk["from"])
            used_node_ids.add(lk["to"])
            n_links += 1

    for nid in used_node_ids:
        nd = fine_nodes[nid]
        node_attrs = {"id": nid, "x": f"{nd['x']}", "y": f"{nd['y']}"}
        if nd["z"] is not None and math.isfinite(nd["z"]):
            node_attrs["z"] = f"{nd['z']}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    xml_string = ET.tostring(network, encoding="utf-8")
    pretty_xml = md.parseString(xml_string).toprettyxml()
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write("\n".join(pretty_xml.splitlines()[1:]))

    print(f"  {len(used_node_ids)} nodes, {n_links} links written")


# ==============================================================================
# Part 7: Find path nodes in fine network
# ==============================================================================
def find_fine_path_nodes(coarse_path_nodes, fine_nodes, fine_links):
    """Find all fine-network nodes along a coarse-network path via BFS."""
    adj = {}
    for lid, lk in fine_links.items():
        u, v = lk["from"], lk["to"]
        adj.setdefault(u, set()).add((v, lid))
        adj.setdefault(v, set()).add((u, lid))

    coarse_set = set(coarse_path_nodes)
    all_path_nodes = set(coarse_path_nodes)

    for i in range(len(coarse_path_nodes) - 1):
        start = coarse_path_nodes[i]
        end = coarse_path_nodes[i + 1]

        if start not in adj or end not in adj:
            continue

        visited = {start}
        queue = deque([(start, [start])])
        found = False

        while queue and not found:
            curr, curr_path = queue.popleft()
            if curr not in adj:
                continue
            for nb, _ in adj[curr]:
                if nb in visited:
                    continue
                if nb == end:
                    all_path_nodes.update(curr_path)
                    all_path_nodes.add(end)
                    found = True
                    break
                if nb not in coarse_set:
                    visited.add(nb)
                    queue.append((nb, curr_path + [nb]))

    return all_path_nodes


# ==============================================================================
# Part 8: HTML comparison report
# ==============================================================================
def _elevation_and_grades(path_nodes, nodes):
    """Return (cum_dist_km, z, grades) arrays for a node list."""
    n = len(path_nodes)
    z = np.array([nodes[nid]["z"] for nid in path_nodes], dtype=float)
    cum = [0.0]
    for i in range(1, n):
        a, b = path_nodes[i - 1], path_nodes[i]
        dx = nodes[a]["x"] - nodes[b]["x"]
        dy = nodes[a]["y"] - nodes[b]["y"]
        cum.append(cum[-1] + hypot(dx, dy))
    cum = np.array(cum) / 1000.0

    lengths = np.diff(cum) * 1000.0
    lengths_safe = np.where(lengths > 0.1, lengths, 0.1)
    dz = np.diff(z)
    grades = dz / lengths_safe * 100.0

    return cum, z, grades


def _fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _build_leaflet_polylines(sections, nodes):
    """Convert section paths to WGS84 polylines for Leaflet."""
    from pyproj import Transformer
    transformer = Transformer.from_crs(NETWORK_CRS, "EPSG:4326", always_xy=True)

    polylines = []
    for label, _row, path_nodes in sections:
        step = max(1, len(path_nodes) // 500)
        sampled = path_nodes[::step]
        if path_nodes[-1] not in sampled:
            sampled.append(path_nodes[-1])

        coords = []
        for nid in sampled:
            nd = nodes[nid]
            lon, lat = transformer.transform(nd["x"], nd["y"])
            coords.append([round(lat, 5), round(lon, 5)])
        polylines.append((label, coords))
    return polylines


def generate_html_report(sections, nodes, n_total_paths, n_iterations, output_path):
    """Generate HTML report with elevation profiles, grade distributions, and map."""
    print(f"Generating HTML report: {output_path}")

    # --- Figure 1: Elevation profiles overlaid ---
    fig1, ax1 = plt.subplots(figsize=(14, 5))
    for label, row, path_nodes in sections:
        cum, z, _ = _elevation_and_grades(path_nodes, nodes)
        ax1.plot(cum, z, color=COLORS[label], linewidth=1.2, label=LABELS[label])
    ax1.set_xlabel("Distance [km]")
    ax1.set_ylabel("Elevation [m a.s.l.]")
    ax1.set_title("Elevation Profiles")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    img1 = _fig_to_base64(fig1)

    # --- Figure 2: Grade distributions (shared x-axis for comparability) ---
    all_grades = []
    for _, _, path_nodes in sections:
        _, _, grades = _elevation_and_grades(path_nodes, nodes)
        all_grades.append(grades)
    shared_xlim = (-10, 10)
    shared_bins = np.linspace(shared_xlim[0], shared_xlim[1], 81)

    fig2, axes = plt.subplots(1, 3, figsize=(16, 4.5), sharey=True, sharex=True)
    for ax, (label, row, _pn), grades in zip(axes, sections, all_grades):
        ax.hist(grades, bins=shared_bins, color=COLORS[label], alpha=0.85,
                edgecolor="white", linewidth=0.3)
        ax.set_xlabel("Grade [%]")
        ax.set_title(f"{LABELS[label]}  ($\\sigma_g$={row['sigma_g']:.4f})")
        ax.axvline(0, color="black", linewidth=0.5, linestyle="--")
        ax.grid(True, alpha=0.3)
        ax.set_xlim(shared_xlim)
    axes[0].set_ylabel("Count")
    fig2.suptitle("Grade Distribution", y=1.02)
    fig2.tight_layout()
    img2 = _fig_to_base64(fig2)

    # --- Table rows ---
    table_rows = ""
    for label, row, _ in sections:
        table_rows += f"""
        <tr>
          <td><span style="color:{COLORS[label]}; font-weight:bold">{LABELS[label]}</span></td>
          <td>{row['L_m']/1000:.1f}</td>
          <td>{row['sigma_g']:.5f}</td>
          <td>{row['g_abs_mean']*100:.2f}</td>
          <td>{row['D_plus_m']:.0f}</td>
          <td>{row['D_minus_m']:.0f}</td>
          <td>{row['D_plus_per_km']:.1f}</td>
          <td>{row['delta_z_m']:.0f}</td>
          <td>{row['g_max']*100:.2f}</td>
          <td>{row['f_und_per_km']:.1f}</td>
        </tr>"""

    # --- Build Leaflet map data ---
    polylines = _build_leaflet_polylines(sections, nodes)
    polylines_js = ""
    for label, coords in polylines:
        polylines_js += (
            f'L.polyline({json.dumps(coords)}, '
            f'{{color: "{COLORS[label]}", weight: 4, opacity: 0.9}})'
            f'.addTo(map).bindPopup("{LABELS[label]}");\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Representative 100 km Sections</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px; margin: 40px auto; padding: 0 20px; background: #fafafa; }}
  h1 {{ color: #333; }}
  h2 {{ color: #555; margin-top: 40px; }}
  img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
  th, td {{ padding: 8px 12px; text-align: right; border-bottom: 1px solid #ddd; }}
  th {{ background: #f0f0f0; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  .info {{ color: #777; font-size: 0.9em; }}
  #map {{ width: 100%; height: 600px; border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
  .legend {{ background: white; padding: 10px 14px; border-radius: 5px; box-shadow: 0 1px 5px rgba(0,0,0,0.3); line-height: 1.8; }}
  .legend i {{ width: 18px; height: 4px; display: inline-block; margin-right: 6px; vertical-align: middle; }}
</style>
</head>
<body>
<h1>Representative 100 km Sections</h1>
<p class="info">Selected from <b>{n_total_paths}</b> unique candidate paths ({n_iterations} iterations). Primary criterion: &sigma;<sub>g</sub> (grade standard deviation).</p>

<h2>Key Metrics</h2>
<table>
  <tr>
    <th>Section</th><th>L [km]</th><th>&sigma;<sub>g</sub></th>
    <th>|g&#772;| [%]</th><th>D+ [m]</th><th>D- [m]</th>
    <th>D+/km [m/km]</th><th>&Delta;z [m]</th><th>g<sub>max</sub> [%]</th>
    <th>f<sub>und</sub> [1/km]</th>
  </tr>
  {table_rows}
</table>

<h2>Elevation Profiles</h2>
<img src="data:image/png;base64,{img1}" alt="Elevation profiles">

<h2>Grade Distributions</h2>
<img src="data:image/png;base64,{img2}" alt="Grade distributions">

<h2>Route Map</h2>
<div id="map"></div>
<script>
var map = L.map('map').setView([51.2, 10.4], 6);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 18
}}).addTo(map);

{polylines_js}

// Legend
var legend = L.control({{position: 'topright'}});
legend.onAdd = function(map) {{
  var div = L.DomUtil.create('div', 'legend');
  div.innerHTML =
    '<b>Sections</b><br>' +
    '<i style="background:{COLORS["flat"]}"></i> Flat<br>' +
    '<i style="background:{COLORS["medium"]}"></i> Medium<br>' +
    '<i style="background:{COLORS["hilly"]}"></i> Hilly';
  return div;
}};
legend.addTo(map);
</script>

</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML report saved to {output_path}")


# ==============================================================================
# Main
# ==============================================================================
if __name__ == "__main__":

    # --- Create timestamped output directory ---
    run_dir = Path(f"data/sections_run_{datetime.now():%Y%m%d_%H%M%S}")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {run_dir}")

    # --- Step 1: Load coarse network (once) ---
    coarse_nodes, coarse_links, _ = load_network(COARSE_NETWORK)

    # --- Step 2: Run iterations in parallel ---
    print(f"\nRunning {N_ITERATIONS} iterations in parallel...")
    args_list = [(coarse_nodes, coarse_links, i) for i in range(N_ITERATIONS)]

    with ProcessPoolExecutor() as pool:
        results = list(tqdm(
            pool.map(_run_iteration, args_list),
            total=N_ITERATIONS,
            desc="Iterations",
        ))

    # --- Step 3: Merge & deduplicate paths ---
    all_paths = []
    seen_keys = set()
    for iteration_paths in results:
        for p in iteration_paths:
            key = tuple(p["node_list"])
            if key not in seen_keys:
                seen_keys.add(key)
                all_paths.append(p)

    print(f"\nTotal unique paths: {len(all_paths)}")

    if len(all_paths) == 0:
        print("ERROR: No paths found. Try adjusting LENGTH_TOLERANCE_M or N_ITERATIONS.")
        exit(1)

    # --- Step 4: Compute features ---
    print("Computing features...")
    records = []
    for i, p in enumerate(tqdm(all_paths, desc="Feature computation")):
        feats = compute_features(p["node_list"], coarse_nodes)
        if feats is None:
            continue
        feats["path_id"] = i
        feats["start_node"] = p["node_list"][0]
        feats["end_node"] = p["node_list"][-1]
        feats["n_nodes"] = len(p["node_list"])
        feats["node_list"] = ";".join(p["node_list"])
        records.append(feats)

    df = pd.DataFrame(records)
    n_total_paths = len(df)
    print(f"Paths with valid features: {n_total_paths}")

    # --- Step 5: Select flat / medium / hilly ---
    idx_flat, idx_med, idx_hilly = select_sections(df)

    # --- Step 6: Load fine network & export sub-networks ---
    fine_nodes, fine_links, fine_link_elems = load_network(FINE_NETWORK)

    for label, idx, filename in [
        ("flat",   idx_flat,  "section_flat_100km.xml.gz"),
        ("medium", idx_med,   "section_medium_100km.xml.gz"),
        ("hilly",  idx_hilly, "section_hilly_100km.xml.gz"),
    ]:
        row = df.loc[idx]
        coarse_path_nodes = row["node_list"].split(";")
        print(f"\n--- {label.upper()} section ---")

        fine_path_nodes = find_fine_path_nodes(coarse_path_nodes, fine_nodes, fine_links)
        print(f"  Coarse nodes: {len(coarse_path_nodes)}, Fine nodes: {len(fine_path_nodes)}")

        export_subnetwork(fine_nodes, fine_links, fine_link_elems,
                          fine_path_nodes, str(run_dir / filename))

    # --- Step 7: Generate HTML comparison report ---
    sections_for_report = []
    for label, idx in [("flat", idx_flat), ("medium", idx_med), ("hilly", idx_hilly)]:
        row = df.loc[idx]
        path_nodes = row["node_list"].split(";")
        sections_for_report.append((label, row, path_nodes))

    generate_html_report(sections_for_report, coarse_nodes, n_total_paths,
                         N_ITERATIONS, str(run_dir / "sections_comparison.html"))

    print(f"\nDone! All outputs in: {run_dir.resolve()}")
