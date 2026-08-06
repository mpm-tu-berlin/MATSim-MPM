# -*- coding: utf-8 -*-
"""Referenz-Routen-Netze fuer die 6 telemetry-Validierungs-Trips (TTE Sec. VI,
Erweiterung 2026-08): GPS-Trace (25-m-Profil aus dem HoLa-Export) ->
EPSG:4839-Kettennetz section_<label>_100km.xml.gz — dasselbe Referenzformat,
das generate_section_link_length_variants.py fuer die WP4-Routen erwartet
(Kette mit 2 Grad-1-Endpunkten; genutzt werden nur Bbox/Korridor/Schlauch/
Endpunkte/Laenge, keine Hoehen).

Eingaben/Ausgaben bleiben saemtlich im gitignorten data/-Ordner (private
Realfahrtdaten); stdout nur Aggregate.

Aufruf:  ../../.venv/Scripts/python build_realtrip_reference_telemetry.py
"""
import gzip
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

_SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = _SCRIPT_DIR / "data" / "realtrip_telemetry_profiles"
REFS_DIR = _SCRIPT_DIR / "data" / "realtrip_refs_telemetry"
NODE_SPACING_M = 50.0  # wie die feinen 50-m-Sektionsreferenzen

LABELS = ["f22", "w24", "w43", "h27", "h19", "h30"]


def build_chain(lon, lat):
    """WGS84-Trace -> EPSG:4839-Punktkette mit >= NODE_SPACING_M Abstand."""
    tr = Transformer.from_crs("EPSG:4326", "EPSG:4839", always_xy=True)
    x, y = tr.transform(lon, lat)
    keep = [0]
    for i in range(1, len(x)):
        if np.hypot(x[i] - x[keep[-1]], y[i] - y[keep[-1]]) >= NODE_SPACING_M:
            keep.append(i)
    if keep[-1] != len(x) - 1:
        keep.append(len(x) - 1)
    return x[keep], y[keep]


def write_reference(label, x, y, out_path):
    seg = np.hypot(np.diff(x), np.diff(y))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE network SYSTEM '
             '"http://www.matsim.org/files/dtd/network_v2.dtd">',
             '<network>', '  <nodes>']
    for i, (xi, yi) in enumerate(zip(x, y)):
        lines.append(f'    <node id="n{i}" x="{xi:.2f}" y="{yi:.2f}" />')
    lines.append('  </nodes>')
    lines.append('  <links capperiod="01:00:00" effectivecellsize="7.5" '
                 'effectivelanewidth="3.75">')
    for i, ln in enumerate(seg):
        lines.append(f'    <link id="l{i}" from="n{i}" to="n{i + 1}" '
                     f'length="{ln:.2f}" freespeed="25.0" capacity="2000" '
                     f'permlanes="1" modes="car" />')
    lines.append('  </links>')
    lines.append('</network>')
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return float(seg.sum())


def main():
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for label in LABELS:
        df = pd.read_csv(PROFILES_DIR / f"trip_{label}.csv")
        ok = np.isfinite(df["lat"]) & np.isfinite(df["lon"])
        x, y = build_chain(df.loc[ok, "lon"].to_numpy(),
                           df.loc[ok, "lat"].to_numpy())
        out = REFS_DIR / f"section_{label}_100km.xml.gz"
        chain_m = write_reference(label, x, y, out)
        prof_km = df["s_m"].iloc[-1] / 1000.0
        rows.append({"label": label, "n_nodes": len(x),
                     "chain_km": round(chain_m / 1000.0, 1),
                     "profil_km": round(prof_km, 1),
                     "dev_pct": round(100.0 * (chain_m / 1000.0 / prof_km - 1.0), 2)})
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nReferenzen in: {REFS_DIR} (gitignored)")


if __name__ == "__main__":
    main()
