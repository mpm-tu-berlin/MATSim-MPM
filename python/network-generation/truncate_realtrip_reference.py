# -*- coding: utf-8 -*-
"""Referenzroute einer Realfahrt am hinteren Ende kuerzen.

Anlass (User 2026-08-18): bei der 43t-Fahrt ist der Abgleich zwischen GPS und
gefundener Route ab km 77,13 nicht mehr belastbar. Statt das Teilstueck in der
Auswertung zu ignorieren, wird die Referenz selbst gekuerzt, damit Netzbau,
Hoehenvalidierung und Simulation dieselbe, kuerzere Strecke sehen.

Die Kilometrierung bezieht sich auf die BOGENLAENGE DER GEFUNDENEN NETZROUTE
(so wie in karte_realfahrten.html und hoehenprofil_realfahrten.html gezeigt).
Der Schnittpunkt wird auf die Referenzlinie projiziert und diese dort getrennt.

Aufruf:
  python truncate_realtrip_reference.py --trip 43t --cut-km 77.13 \
      --route data/realtrip_networks_v2_20260817_V3b/section_43t_250m.xml.gz \
      --refs-dir data/realtrip_refs_v2 --out-dir data/realtrip_refs_v2_trunc
"""
import argparse
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

_SCRIPT_DIR = Path(__file__).parent


def read_chain(path):
    """(order, nodes-dict, root) eines Kettennetzes."""
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()
    nodes = {n.get("id"): n for n in root.find("nodes").findall("node")}
    adj = {}
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        if u == v:
            continue
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    ends = [n for n, nb in adj.items() if len(nb) == 1]
    start = ends[0] if ends else next(iter(adj))
    order, prev = [start], None
    while True:
        nxt = [n for n in adj[order[-1]] if n != prev and n not in order]
        if not nxt:
            break
        prev = order[-1]
        order.append(sorted(nxt)[0])
    return order, nodes, root


def xy_of(order, nodes):
    return np.array([[float(nodes[n].get("x")), float(nodes[n].get("y"))]
                     for n in order], float)


def arc(xy):
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])


def write_chain(order, nodes, out_path, template_root):
    """Kettennetz aus einer Knotenfolge schreiben (Links in Reihenfolge)."""
    net = ET.Element("network")
    nd = ET.SubElement(net, "nodes")
    for nid in order:
        src = nodes[nid]
        n = ET.SubElement(nd, "node", id=nid, x=src.get("x"), y=src.get("y"))
        if src.get("z") is not None:
            n.set("z", src.get("z"))
    src_links = template_root.find("links")
    lk = ET.SubElement(net, "links", **{k: v for k, v in src_links.attrib.items()})
    for i, (a, b) in enumerate(zip(order[:-1], order[1:]), start=1):
        xa, ya = float(nodes[a].get("x")), float(nodes[a].get("y"))
        xb, yb = float(nodes[b].get("x")), float(nodes[b].get("y"))
        length = float(np.hypot(xb - xa, yb - ya))
        ET.SubElement(lk, "link", id=str(i), **{"from": a, "to": b},
                      length=f"{length:.1f}", freespeed="22.22", capacity="2000",
                      permlanes="1", modes="car")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b'<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        ET.ElementTree(net).write(f, encoding="UTF-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip", required=True)
    ap.add_argument("--cut-km", type=float, required=True)
    ap.add_argument("--route", required=True,
                    help="gefundenes Routen-Subnetz, dessen Kilometrierung gilt")
    ap.add_argument("--refs-dir", default="data/realtrip_refs_v2")
    ap.add_argument("--out-dir", default="data/realtrip_refs_v2_trunc")
    a = ap.parse_args()

    route_path = _SCRIPT_DIR / a.route
    ref_path = _SCRIPT_DIR / a.refs_dir / ("section_" + a.trip + "_100km.xml.gz")
    out_path = _SCRIPT_DIR / a.out_dir / ("section_" + a.trip + "_100km.xml.gz")

    r_order, r_nodes, _ = read_chain(route_path)
    r_xy = xy_of(r_order, r_nodes)
    r_s = arc(r_xy)
    cut_m = a.cut_km * 1000.0
    if cut_m >= r_s[-1]:
        raise SystemExit("Schnitt bei %.2f km liegt hinter dem Routenende (%.2f km)."
                         % (a.cut_km, r_s[-1] / 1000.0))
    cut_xy = np.array([np.interp(cut_m, r_s, r_xy[:, 0]),
                       np.interp(cut_m, r_s, r_xy[:, 1])])

    f_order, f_nodes, f_root = read_chain(ref_path)
    f_xy = xy_of(f_order, f_nodes)
    # Referenz so orientieren, dass ihr Anfang zum Routenanfang gehoert
    if (np.hypot(*(f_xy[0] - r_xy[0])) > np.hypot(*(f_xy[-1] - r_xy[0]))):
        f_order = f_order[::-1]
        f_xy = f_xy[::-1]
    f_s = arc(f_xy)

    k = int(np.argmin(np.hypot(f_xy[:, 0] - cut_xy[0], f_xy[:, 1] - cut_xy[1])))
    dist = float(np.hypot(*(f_xy[k] - cut_xy)))
    kept = f_order[:k + 1]
    if len(kept) < 2:
        raise SystemExit("Schnitt liegt vor dem zweiten Referenzknoten.")

    write_chain(kept, f_nodes, out_path, f_root)
    print("Route  : %.2f km, Schnitt bei %.2f km" % (r_s[-1] / 1000.0, a.cut_km))
    print("Referenz: %.2f km -> %.2f km (%d von %d Knoten, Projektionsabstand %.1f m)"
          % (f_s[-1] / 1000.0, f_s[k] / 1000.0, len(kept), len(f_order), dist))
    print("Geschrieben: " + str(out_path))


if __name__ == "__main__":
    main()
