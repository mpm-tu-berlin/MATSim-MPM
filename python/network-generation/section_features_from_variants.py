# -*- coding: utf-8 -*-
"""
Topografie-Merkmale der Sektions-Varianten (Neuberechnung nach dem
Halbpixel-Fix im DTM-Sampling, 2026-08-17).

Hintergrund: knee_point_analysis.py braucht je Sektion die Topografie-Kennzahlen
(sigma_g, D+/km, g_abs_mean). Die bisherige selected_sections_features.csv stammt
aus der Auswahl vom 2026-07-06 und damit aus der V2-Hoehenbasis mit dem
fehlerhaften Halbpixel-Versatz. Die Sektions-AUSWAHL (Orte) bleibt unveraendert
(User-Entscheidung 2026-08-17), nur die Hoehen sind neu.

Die Kennzahlen sind aufloesungsabhaengig (Steigung = dz/ds ueber Links). Daher
werden sie fuer JEDE Linklaengen-Stufe der Variantenleiter berechnet und
zusaetzlich als 250-m-Schnitt ausgegeben, damit die Zahlen definitionsgleich zu
den im Paper berichteten sind (gleiche Formeln, gleiche Bezugsstufe, nur neue
Hoehen).

Ausgaben in --variants-dir:
  - section_features_by_link_length.csv   alle Sektionen x alle Stufen
  - selected_sections_features_V3.csv     250-m-Schnitt (Format wie die alte
                                          selected_sections_features.csv, direkt
                                          als --features-csv nutzbar)

Usage:
    python section_features_from_variants.py --variants-dir <dir> [--reference-length 250]
"""

import argparse
import gzip
import re
import xml.etree.ElementTree as ET
from math import hypot
from pathlib import Path

import numpy as np
import pandas as pd

REFERENCE_LINK_LENGTH_M = 250


def load_network(path):
    """Knoten (x, y, z) und Links (from, to) aus einem MATSim-Netz lesen."""
    with gzip.open(path, "rb") as f:
        root = ET.parse(f).getroot()
    nodes = {}
    for n in root.find("nodes").findall("node"):
        nodes[n.get("id")] = {
            "x": float(n.get("x")), "y": float(n.get("y")),
            "z": float(n.get("z")) if n.get("z") is not None else np.nan,
        }
    edges = [(l.get("from"), l.get("to")) for l in root.find("links").findall("link")]
    return nodes, edges


def order_chain(nodes, edges):
    """Knotenkette einer Sektion (Pfad-Subnetz) in Fahrtrichtung ordnen.

    Die Varianten enthalten nur die Sektions-Links, meist als Richtungspaare.
    Ueber die ungerichtete Nachbarschaft ergibt sich eine offene Kette; Start ist
    ein Knoten mit genau einem Nachbarn (bei Ringschluss ein beliebiger Knoten).
    """
    adj = {}
    for u, v in edges:
        if u == v:
            continue
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    if not adj:
        return []
    ends = [k for k, nb in adj.items() if len(nb) == 1]
    start = ends[0] if ends else next(iter(adj))
    chain, prev, cur = [start], None, start
    while True:
        nxt = [n for n in adj[cur] if n != prev]
        if not nxt:
            break
        nxt = nxt[0] if len(nxt) == 1 else sorted(nxt)[0]
        if nxt in chain:
            break
        chain.append(nxt)
        prev, cur = cur, nxt
    return chain


def compute_features(chain, nodes):
    """Identisch zu compute_features in extract_representative_sections_quantile.py
    (laengengewichtete Steigungsmomente), damit die Zahlen vergleichbar bleiben."""
    if len(chain) < 2:
        return None
    z = np.array([nodes[nid]["z"] for nid in chain], dtype=float)
    lengths = np.array([
        hypot(nodes[chain[i + 1]]["x"] - nodes[chain[i]]["x"],
              nodes[chain[i + 1]]["y"] - nodes[chain[i]]["y"])
        for i in range(len(chain) - 1)
    ], dtype=float)
    lengths_safe = np.where(lengths > 0.1, lengths, 0.1)
    L = float(np.sum(lengths))
    if L < 1.0:
        return None
    dz = np.diff(z)
    grades = dz / lengths_safe
    mu_g = float(np.sum(grades * lengths) / L)
    sign_changes = int(np.sum(np.diff(np.sign(grades)) != 0))
    return {
        "L_m": L,
        "g_abs_mean": float(np.sum(np.abs(grades) * lengths) / L),
        "mu_g": mu_g,
        "sigma_g": float(np.sqrt(np.sum(lengths * (grades - mu_g) ** 2) / L)),
        "D_plus_m": float(np.sum(np.maximum(0, dz))),
        "D_minus_m": float(np.sum(np.abs(np.minimum(0, dz)))),
        "D_plus_per_km": float(np.sum(np.maximum(0, dz))) / (L / 1000.0),
        "delta_z_m": float(np.max(z) - np.min(z)),
        "g_max": float(np.max(np.abs(grades))),
        "f_und_per_km": sign_changes / (L / 1000.0),
        "n_nodes": len(chain),
    }


def main():
    ap = argparse.ArgumentParser(description="Topografie-Merkmale je Sektion und Linklaenge.")
    ap.add_argument("--variants-dir", required=True)
    ap.add_argument("--reference-length", type=int, default=REFERENCE_LINK_LENGTH_M)
    args = ap.parse_args()

    vdir = Path(args.variants_dir)
    files = sorted(vdir.glob("section_*m.xml.gz"))
    if not files:
        raise SystemExit(f"Keine Varianten in {vdir}")

    rows = []
    for f in files:
        m = re.match(r"section_(.+)_(\d+)m\.xml\.gz$", f.name)
        if not m:
            continue
        section, length = m.group(1), int(m.group(2))
        nodes, edges = load_network(f)
        chain = order_chain(nodes, edges)
        feats = compute_features(chain, nodes)
        if feats is None:
            print(f"  WARNING: {f.name} uebersprungen (Kette < 2 Knoten).")
            continue
        feats.update(section=section, max_link_length=length,
                     chain_share=len(chain) / max(1, len(nodes)))
        rows.append(feats)

    df = pd.DataFrame(rows).sort_values(["section", "max_link_length"])
    out_all = vdir / "section_features_by_link_length.csv"
    df.to_csv(out_all, index=False)

    ref = df[df.max_link_length == args.reference_length].copy()
    out_ref = vdir / "selected_sections_features_V3.csv"
    ref.to_csv(out_ref, index=False)

    print(f"{len(df)} Zeilen -> {out_all.name}")
    print(f"{len(ref)} Sektionen bei {args.reference_length} m -> {out_ref.name}\n")

    def _q(s):
        return -1 if s == "flat" else int(s[1:])

    ref = ref.assign(_q=ref.section.map(_q)).sort_values("_q")
    print(ref[["section", "L_m", "sigma_g", "g_abs_mean", "D_plus_per_km",
               "delta_z_m", "g_max", "n_nodes"]]
          .to_string(index=False,
                     formatters={"L_m": "{:.0f}".format, "sigma_g": "{:.5f}".format,
                                 "g_abs_mean": "{:.5f}".format,
                                 "D_plus_per_km": "{:.2f}".format,
                                 "delta_z_m": "{:.1f}".format, "g_max": "{:.4f}".format}))


if __name__ == "__main__":
    main()
