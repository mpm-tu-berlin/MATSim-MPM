# -*- coding: utf-8 -*-
"""Repariert die exportierten Referenz-Sektionen des Quantil-Runs zu reinen Ketten.

Befund 2026-07-06: export_subnetwork() exportierte alle Links, deren beide
Enden im Pfadknoten-SET liegen — also auch Querlinks zwischen nicht-
konsekutiven Routenknoten. Folge: 9/20 Sektionen ohne saubere Grad-1-
Endpunkte bzw. mit Grad-3-Knoten; Endpunkt-Erkennung und Pfadsuche im
Varianten-Generator liefen auf Abwege (q45: 78 statt 100 km).

Die wahre Route ist in den Dateien vollstaendig enthalten (alle Knoten sind
Routenknoten, CSV kennt Start/Ende). Rekonstruktion: Hamiltonpfad-DFS von
start_node nach end_node ueber ALLE Knoten; Export behaelt nur Links
konsekutiver Routenpaare. Validierung gegen L_m aus der Feature-CSV.

Output: <run_dir>_chainfix/ mit allen 20 Sektionen (reparierte + kopierte)
plus den beiden Feature-CSVs.
"""
import gzip
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

RUN_DIR = Path(__file__).parent / "data" / "sections_quantile_run_20260706_130433"
OUT_DIR = Path(str(RUN_DIR) + "_chainfix"
               )
QUANTILES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
             55, 60, 65, 70, 75, 80, 85, 90, 95, 97)
MAX_LEN_TOL = 0.01  # Rekonstruktion muss L_m der Auswahl auf 1 % treffen

sys.setrecursionlimit(100000)


def load_section(path):
    with gzip.open(path, "rb") as f:
        tree = ET.parse(f)
    root = tree.getroot()
    links = [(l.get("from"), l.get("to"), float(l.get("length")))
             for l in root.find("links").findall("link")]
    node_ids = {n.get("id") for n in root.find("nodes").findall("node")}
    return tree, root, node_ids, links


def hamiltonian_path(node_ids, links, start, end):
    """DFS-Pfad start->end, der ALLE Knoten genau einmal besucht."""
    adj = {}
    for u, v, _ in links:
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)

    n_total = len(node_ids)
    path = [start]
    visited = {start}

    def dfs(curr):
        if len(path) == n_total:
            return curr == end
        # Determinismus: Nachbarn sortiert; Grad-2-Ketten laufen ohne Verzweigung
        for nb in sorted(adj.get(curr, ())):
            if nb in visited or (nb == end and len(path) < n_total - 1):
                continue
            visited.add(nb)
            path.append(nb)
            if dfs(nb):
                return True
            visited.discard(nb)
            path.pop()
        return False

    if start not in adj or end not in adj:
        return None
    return path if dfs(start) else None


def repair(tree, root, ordered_path):
    """Entfernt alle Links, die kein konsekutives Routenpaar verbinden."""
    pairs = set()
    for i in range(len(ordered_path) - 1):
        pairs.add(frozenset((ordered_path[i], ordered_path[i + 1])))
    links_el = root.find("links")
    removed = 0
    for link in list(links_el.findall("link")):
        if frozenset((link.get("from"), link.get("to"))) not in pairs:
            links_el.remove(link)
            removed += 1
    return removed


def main():
    feat = pd.read_csv(RUN_DIR / "selected_sections_features.csv").set_index("section")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for csv in ("selected_sections_features.csv", "candidate_paths_features.csv"):
        shutil.copy2(RUN_DIR / csv, OUT_DIR / csv)

    n_repaired = 0
    for q in QUANTILES:
        label = f"q{q}"
        fname = f"section_{label}_100km.xml.gz"
        tree, root, node_ids, links = load_section(RUN_DIR / fname)

        start = str(feat.loc[label, "start_node"])
        end = str(feat.loc[label, "end_node"])
        path = hamiltonian_path(node_ids, links, start, end)
        if path is None:
            print(f"{label}: FEHLER — kein Hamiltonpfad {start}->{end} gefunden!")
            continue

        # Laengen-Validierung gegen die Auswahl-Features
        pair_len = {}
        for u, v, ln in links:
            key = frozenset((u, v))
            pair_len[key] = min(pair_len.get(key, float("inf")), ln)
        plen = sum(pair_len[frozenset((path[i], path[i + 1]))]
                   for i in range(len(path) - 1))
        ref = float(feat.loc[label, "L_m"])
        dev = abs(plen - ref) / ref
        if dev > MAX_LEN_TOL:
            print(f"{label}: FEHLER — rekonstruierte Laenge {plen/1000:.1f} km "
                  f"weicht {dev:.1%} von CSV {ref/1000:.1f} km ab!")
            continue

        removed = repair(tree, root, path)
        out_path = OUT_DIR / fname
        ET.indent(root, space="  ")
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<!DOCTYPE network SYSTEM '
                    '"http://www.matsim.org/files/dtd/network_v2.dtd">\n')
            f.write(ET.tostring(root, encoding="unicode"))
            f.write('\n')
        status = f"repariert ({removed} Querlinks entfernt)" if removed else "unveraendert"
        if removed:
            n_repaired += 1
        print(f"{label}: {status}, Route {plen/1000:.1f} km (CSV {ref/1000:.1f} km, "
              f"Abw. {dev:.2%}), {len(path)} Knoten")

    print(f"\nFertig: {n_repaired} Sektionen repariert -> {OUT_DIR}")


if __name__ == "__main__":
    main()
