# -*- coding: utf-8 -*-
"""Haengt dem nationalen etruck-Netz (EPSG:25832, ohne Hoehen) DTM-Hoehen an.

Zweck: Wall-Clock-Benchmark des dynamischen Energiemodells auf dem
1-%-BET-Szenario (Discussion-Kapitel des IEEE-TTE-Papers). Das Netz aus
tietz_electric_2025 traegt keine z-Koordinaten; das dynamische Modell
braucht sie fuer die Steigung. Knoten ausserhalb des DTM (Grenzraum,
~340 Stueck) erhalten z=0.

Aufruf (aus dem Repo-Root):
    python python/network-generation/attach_z_etruck_network.py

Eingabe:  scenarios/german_etruck_network.xml.gz
          python/network-generation/data/DTM Germany 20m v3b by Sonny.tif
Ausgabe:  scenarios/german_etruck_network_z.xml.gz
"""
import gzip
import re
import time

import numpy as np
import rasterio
from pyproj import Transformer

NETZ_IN = "scenarios/german_etruck_network.xml.gz"
NETZ_OUT = "scenarios/german_etruck_network_z.xml.gz"
DTM = r"python/network-generation/data/DTM Germany 20m v3b by Sonny.tif"


def main():
    t0 = time.time()
    src = rasterio.open(DTM)
    band = src.read(1)
    nodata = src.nodata
    tf = Transformer.from_crs("EPSG:25832", "EPSG:32632", always_xy=True)

    node_re = re.compile(r'(<node id="[^"]+" x="([0-9eE+.\-]+)" y="([0-9eE+.\-]+)")')
    n = miss = 0
    with gzip.open(NETZ_IN, "rt", encoding="utf-8") as fin, \
         gzip.open(NETZ_OUT, "wt", encoding="utf-8") as fout:
        for line in fin:
            m = node_re.search(line)
            if m:
                gx, gy = tf.transform(float(m.group(2)), float(m.group(3)))
                try:
                    row, col = src.index(gx, gy)
                    z = float(band[row, col])
                    if (nodata is not None and z == nodata) or not np.isfinite(z):
                        z, miss = 0.0, miss + 1
                except IndexError:
                    z, miss = 0.0, miss + 1
                line = line.replace(m.group(1), f'{m.group(1)} z="{z:.1f}"', 1)
                n += 1
            fout.write(line)
    print(f"{n} Knoten mit z, {miss} ausserhalb/nodata -> z=0, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
