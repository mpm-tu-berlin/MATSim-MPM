import gzip
import xml.etree.ElementTree as ET
import networkx as nx
import numpy as np
from math import hypot
from pathlib import Path
import xml.dom.minidom as md
import warnings

# ==============================
# Konfiguration
# ==============================
INPUT_FILE  = r"data\Germany_max_unlimited_long_V0.xml.gz"
OUTPUT_FILE = r"data\Germany_max_unlimitied_long_smoothened_not_merged_0_05.xml.gz"

# Glättung (Spline)
SMOOTH_METHOD = "spline"
SPLINE_S = None            # None = auto (siehe SPLINE_TARGET_RMS), sonst fester Wert
SPLINE_TARGET_RMS = 3      # Ziel-RMS in m

DEBUG_PATH_PRINTS = False

# --- Zusammenführen von Links wenn Unterschied sehr klein (optional) ---
MERGE_LINKS  = False   # <— Schalter: True = zusammenfügen, False = überspringen
GRADE_EPS    = 0.0005  # max. mittlere Steigung |Δz|/L (z. B. 0.01 %)
PRESERVE_IDS = False  # neue ID = "L1+L2+..."; bei True: nimm ID des ersten Links

# --- Map-Export (optional) ---
EXPORT_ALL_SECTIONS_HTML = r"data\smoothed_sections_all.html"
COLOR_MODE = "signed"      # "signed" (blau/weiß/rot) oder "abs" (weiß->rot)
PERCENTILE_CLIP = 100
LINE_WEIGHT = 4
ZOOM_START = 11
CRS_INPUT = "auto"         # "auto" | "epsg:4326" | "epsg:25833"
MAX_POINTS_PER_SECTION = 1000
SHOW_SECTION_POPUPS = False
# ==============================

# ------------------------------
# Datei einlesen
# ------------------------------
with gzip.open(INPUT_FILE, 'rb') as f:
    tree = ET.parse(f)
root = tree.getroot()

nodes_elem = root.find("nodes")
links_elem = root.find("links")
if nodes_elem is None or links_elem is None:
    raise RuntimeError("Konnte <nodes> oder <links> im Netzwerk nicht finden.")

# ------------------------------
# Knoten einlesen
# ------------------------------
nodes = {}
for node in nodes_elem.findall("node"):
    nid = node.get("id")
    x = float(node.get("x"))
    y = float(node.get("y"))
    z_attr = node.get("z")
    z = float(z_attr) if z_attr is not None else None
    nodes[nid] = {"x": x, "y": y, "z": z, "elem": node}

# ------------------------------
# Graph aufbauen
# ------------------------------
G = nx.Graph()
missing_endpoints = 0
for link in links_elem.findall("link"):
    u = str(link.get("from"))
    v = str(link.get("to"))
    if u in nodes and v in nodes:
        G.add_edge(u, v)
    else:
        missing_endpoints += 1

deg = dict(G.degree())
crossings = {n for n, d in deg.items() if d != 2 and d > 0}
corridor_nodes = {n for n, d in deg.items() if 0 < d < 3}

# ------------------------------
# Hilfsfunktionen für Glättung
# ------------------------------
def edge_key(a, b):
    return tuple(sorted((a, b)))

def cumulative_lengths(path, nodes):
    cum = [0.0]
    for i in range(1, len(path)):
        a, b = path[i-1], path[i]
        dx = nodes[a]["x"] - nodes[b]["x"]
        dy = nodes[a]["y"] - nodes[b]["y"]
        cum.append(cum[-1] + hypot(dx, dy))
    return np.array(cum, dtype=float)

def _interp_missing_along_path(path, z_arr, cum) -> np.ndarray | None:
    known = np.isfinite(z_arr)
    if known.sum() == 0:
        return None
    if known.sum() == 1:
        z_arr = np.where(known, z_arr, z_arr[known][0])
        return z_arr
    z_filled = z_arr.copy()
    z_filled[~known] = np.interp(cum[~known], cum[known], z_arr[known])
    return z_filled

def smooth_elevations_along_path(path, nodes, lock_start: bool, lock_end: bool):
    try:
        from scipy.interpolate import UnivariateSpline
    except Exception as e:
        raise ImportError("Spline-Glättung benötigt SciPy. Installiere scipy (pip install scipy).") from e

    z_vals = [nodes[n]["z"] for n in path]
    z_arr = np.array([float(z) if z is not None else np.nan for z in z_vals], dtype=float)

    if len(path) <= 2:
        return z_arr

    cum = cumulative_lengths(path, nodes)
    total = cum[-1]
    if total <= 0:
        return None

    z_base = _interp_missing_along_path(path, z_arr.copy(), cum)
    if z_base is None:
        return None

    seglens = np.diff(cum, prepend=cum[0])
    w = np.clip(seglens, 1e-6, None)
    sumw = float(np.sum(w))
    m = len(z_base)
    k = min(3, m - 1)
    s_eff = (SPLINE_S if SPLINE_S is not None else (SPLINE_TARGET_RMS ** 2) * sumw)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".*maxit.*s too small.*")
        spl = UnivariateSpline(cum, z_base, w=w, s=s_eff, k=k)

    out = spl(cum)

    if lock_start and np.isfinite(z_arr[0]):
        out[0] = float(z_arr[0])
    if lock_end and np.isfinite(z_arr[-1]):
        out[-1] = float(z_arr[-1])

    if DEBUG_PATH_PRINTS:
        print(f"[smooth] n={m}, k={k}, L={total:.1f} m, sumw={sumw:.1f}, s={s_eff:.2f}, "
              f"lock_start={lock_start}, lock_end={lock_end}")

    return out

# ------------------------------
# Alle Korridore finden & glätten
# ------------------------------
visited_edges = set()
chains_found = 0
chains_smoothed = 0
nodes_changed = 0
chains_skipped_zero_length = 0

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

print("Anzahl Knoten:", len(nodes))
print("Anzahl Kanten:", G.number_of_edges())
print("Korridor-Knoten (Grad < 3):", len(corridor_nodes))
print("Kreuzungspunkte (Grad != 2):", len(crossings))
if missing_endpoints:
    print("Warnung: Links mit fehlenden Endknoten:", missing_endpoints)

start_points = [n for n in G.nodes if n in crossings]

for start in start_points:
    for nb in G.neighbors(start):
        if nb not in corridor_nodes:
            continue
        ek = edge_key(start, nb)
        if ek in visited_edges:
            continue

        visited_edges.add(ek)
        path = walk_corridor(start, nb)

        if len(path) < 3:
            continue

        chains_found += 1
        if DEBUG_PATH_PRINTS:
            print(f"Pfad-Länge: {len(path)} | Start: {path[0]} | Ende: {path[-1]}")

        start_id = path[0]
        end_id   = path[-1]

        start_z_missing = (nodes[start_id]["z"] is None)
        end_z_missing   = (nodes[end_id]["z"] is None)

        lock_start = not start_z_missing
        lock_end   = not end_z_missing

        smoothed = smooth_elevations_along_path(path, nodes, lock_start=lock_start, lock_end=lock_end)
        if smoothed is None:
            continue

        cum = cumulative_lengths(path, nodes)
        if cum[-1] <= 0:
            chains_skipped_zero_length += 1
            continue

        before = np.array([nodes[n]["z"] if nodes[n]["z"] is not None else np.nan for n in path], dtype=float)
        after  = smoothed

        for nid, new_z in zip(path[1:-1], after[1:-1]):
            old_z = nodes[nid]["z"]
            if old_z is None or abs(float(old_z) - float(new_z)) > 1e-9:
                nodes[nid]["z"] = float(new_z)
                nodes_changed += 1

        if start_z_missing and np.isfinite(after[0]):
            nodes[start_id]["z"] = float(after[0])
            nodes_changed += 1
        if end_z_missing and np.isfinite(after[-1]):
            nodes[end_id]["z"] = float(after[-1])
            nodes_changed += 1

        chains_smoothed += 1

        if DEBUG_PATH_PRINTS:
            print("Vorher :", before[:10])
            print("Nachher:", after[:10])

# ==============================
# Korridor-Merging (ohne Knickwinkel, ohne Mapping)
# ==============================
import math
from collections import defaultdict

def _length(a, b):
    dx = nodes[a]["x"] - nodes[b]["x"]
    dy = nodes[a]["y"] - nodes[b]["y"]
    return math.hypot(dx, dy)

dir_in, dir_out, link_by_id = defaultdict(list), defaultdict(list), {}
for link in links_elem.findall("link"):
    lid = link.get("id"); u = link.get("from"); v = link.get("to")
    link_by_id[lid] = link
    dir_out[u].append(lid)
    dir_in[v].append(lid)

def _modes_str(elem):
    m = elem.get("modes")
    return " ".join(sorted((m or "").split()))

def _attrs_compatible_chain(lids):
    modes = {_modes_str(link_by_id[l]) for l in lids}
    if len(modes) > 1:
        return False
    types = {(link_by_id[l].get("type") or "") for l in lids if link_by_id[l].get("type")}
    if len(types) > 1:
        return False
    return True

def _link_length(lid):
    le = link_by_id[lid]
    L = le.get("length")
    if L is not None:
        try:
            return float(L)
        except:
            pass
    return _length(le.get("from"), le.get("to"))

def _is_mid(n):
    return len(dir_in[n]) == 1 and len(dir_out[n]) == 1

def _walk_corridor(start, first_out_lid):
    path_nodes = [start]
    path_links = []
    lid = first_out_lid
    while True:
        path_links.append(lid)
        l = link_by_id[lid]
        nxt = l.get("to")
        path_nodes.append(nxt)
        if not _is_mid(nxt):
            break
        outs = dir_out[nxt]
        if not outs:
            break
        lid = outs[0]
    return path_nodes, path_links

def _grade_ok(path_nodes):
    u = path_nodes[0]; w = path_nodes[-1]
    zu, zw = nodes[u]["z"], nodes[w]["z"]
    if None in (zu, zw):
        return True
    Ltot = 0.0
    for i in range(1, len(path_nodes)):
        Ltot += _length(path_nodes[i-1], path_nodes[i])
    if Ltot <= 0:
        return True
    return abs((zw - zu) / Ltot) <= GRADE_EPS

def _merge_corridor(path_nodes, lids):
    new_id = (link_by_id[lids[0]].get("id") if PRESERVE_IDS else "+".join(link_by_id[l].get("id") for l in lids))
    u = path_nodes[0]; w = path_nodes[-1]
    Lsum = sum(_link_length(l) for l in lids)

    new_link = ET.Element("link")
    new_link.set("id", new_id)
    new_link.set("from", u); new_link.set("to", w)
    new_link.set("length", f"{Lsum:.3f}")

    def _min_num_over(keys):
        for k in keys:
            vals = []
            for lid in lids:
                val = link_by_id[lid].get(k)
                try:
                    if val is not None:
                        vals.append(float(val))
                except:
                    pass
            if vals:
                new_link.set(k, str(min(vals)))
    _min_num_over(["freespeed", "capacity", "permlanes"])

    types = [link_by_id[l].get("type") for l in lids if link_by_id[l].get("type")]
    if types and len(set(types)) == 1:
        new_link.set("type", types[0])
    m = _modes_str(link_by_id[lids[0]])
    if m:
        new_link.set("modes", m)

    for lid in lids:
        links_elem.remove(link_by_id[lid])
    links_elem.append(new_link)

    for lid in lids:
        l = link_by_id.pop(lid, None)
        if l:
            a = l.get("from"); b = l.get("to")
            if lid in dir_out[a]:
                dir_out[a].remove(lid)
            if lid in dir_in[b]:
                dir_in[b].remove(lid)
    link_by_id[new_id] = new_link
    dir_out[u].append(new_id)
    dir_in[w].append(new_id)

    for v in path_nodes[1:-1]:
        if not dir_in[v] and not dir_out[v]:
            nodes_elem.remove(nodes[v]["elem"])
            nodes.pop(v, None)

links_before = len(links_elem.findall("link"))
merged_corridors = 0
merged_links_total = 0

if MERGE_LINKS:
    visited_link = set()
    start_nodes = [n for n in nodes.keys() if not _is_mid(n)]
    for s in start_nodes:
        for lid in list(dir_out[s]):
            if lid in visited_link or lid not in link_by_id:
                continue
            path_nodes, lids = _walk_corridor(s, lid)
            visited_link.update(lids)
            if len(lids) <= 1:
                continue
            if not _attrs_compatible_chain(lids):
                continue
            if not _grade_ok(path_nodes):
                continue
            _merge_corridor(path_nodes, lids)
            merged_corridors += 1
            merged_links_total += len(lids) - 1

links_after = len(links_elem.findall("link"))

print(f"Merging aktiviert: {MERGE_LINKS}")
print(f"Links vor dem Merging:  {links_before}")
print(f"Links nach dem Merging: {links_after}")
if MERGE_LINKS:
    print(f"Zusammengefasste Korridore: {merged_corridors}")
    print(f"Eingesparte Link-Kanten:    {merged_links_total}")

# ------------------------------
# XML mit neuen Höhen überschreiben
# ------------------------------
for nid, data in nodes.items():
    elem = data["elem"]
    z = data["z"]
    if z is not None:
        elem.set("z", f"{z:.6f}")
    else:
        if "z" in elem.attrib:
            del elem.attrib["z"]

out_path = Path(OUTPUT_FILE)
out_path.parent.mkdir(parents=True, exist_ok=True)

xml_bytes = ET.tostring(root, encoding="utf-8")
pretty_xml = md.parseString(xml_bytes).toprettyxml()

with gzip.open(out_path, "wt", encoding="utf-8") as f:
    f.write('<?xml version="1.0" ?>\n')
    f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
    f.write("\n".join(pretty_xml.splitlines()[1:]))

print("Fertig!")
print(f"Gefundene Abschnitte: {chains_found}")
print(f"Geglättete Abschnitte: {chains_smoothed}")
print(f"Geänderte Knoten (inkl. Start/Ende bei fehlendem z): {nodes_changed}")
print(f"Übersprungen (Null-Länge): {chains_skipped_zero_length}")
print(f"Ausgabe: {out_path.resolve()}")
