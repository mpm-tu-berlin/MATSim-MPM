# -*- coding: utf-8 -*-
"""
A1-Messung: Vergleicht die in einem MATSim-Netz gebackenen Knoten-Hoehen (z)
gegen die LiDAR-Ground-Truth aus dem Sonny-DTM (20 m, EPSG:32632), direkt
bilinear an der Knotenposition gesampelt.

Damit wird der Effekt der fehlerhaften Grad-Raum-KD-Tree-Hoehenzuweisung
sichtbar gemacht, OHNE die (verlorene) npz-Punktwolke zu brauchen.

Interpretation:
  - ROHE Varianten-Netze (ohne Glaettung):  Delta_z = (buggy KD-Tree-z) - (DTM-Truth)
    -> isoliert den KD-Tree-Fehler (plus Road-Snap-Diskretisierung) als Obergrenze.
  - GEGLAETTETE Netze (Skript 05):           Delta_z enthaelt zusaetzlich den
    bewussten Glaettungs-Residualanteil (~RMS-Ziel). Hier zaehlen v.a. AUSREISSER.

Aufruf:
  python measure_dem_vs_network_z.py <netz1.xml.gz> [<netz2.xml.gz> ...] \
      [--dtm <pfad.tif>] [--net-crs EPSG:4839] [--spike-grade 0.12] [--worst 15]
"""

import argparse
import gzip
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

_SCRIPT_DIR = Path(__file__).parent
DEFAULT_DTM = _SCRIPT_DIR / "data" / "DTM Germany 20m v3b by Sonny.tif"


def load_nodes_and_links(path):
    """Liest Knoten (id->x,y,z) und Links (id,from,to,length) aus MATSim-XML(.gz)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        root = ET.parse(f).getroot()

    nodes = {}
    for n in root.find("nodes").findall("node"):
        z = n.get("z")
        nodes[n.get("id")] = {
            "x": float(n.get("x")),
            "y": float(n.get("y")),
            "z": float(z) if z is not None else None,
        }
    links = []
    for lk in root.find("links").findall("link"):
        links.append({
            "id": lk.get("id"),
            "from": lk.get("from"),
            "to": lk.get("to"),
            "length": float(lk.get("length", "0")),
        })
    return nodes, links


def sample_dtm_bilinear(ds, xs_utm, ys_utm):
    """Bilineares Sampling des DTM an UTM-Koordinaten. Gibt np.array zurueck (NaN=nodata/ausserhalb)."""
    inv = ~ds.transform                     # world -> (col,row) als float
    nodata = ds.nodata
    band = 1
    out = np.full(len(xs_utm), np.nan, dtype=float)

    for i, (x, y) in enumerate(zip(xs_utm, ys_utm)):
        col_f, row_f = inv * (x, y)         # affine: (x,y)->(col,row)
        # HALBPIXEL-KORREKTUR (Befund 2026-08-17, wie in Skript 04): (~transform)
        # zaehlt Pixel ab der ECKE, das Zentrum von Pixel 0 liegt bei 0,5. Ohne
        # die Verschiebung interpoliert das Sampling um ein halbes Pixel (10 m)
        # versetzt und misst gegen ein anderes Gelaende als die Netz-Hoehen.
        col_f, row_f = col_f - 0.5, row_f - 0.5
        col0, row0 = int(math.floor(col_f)), int(math.floor(row_f))
        if col0 < 0 or row0 < 0 or col0 + 1 >= ds.width or row0 + 1 >= ds.height:
            continue
        win = rasterio.windows.Window(col0, row0, 2, 2)
        a = ds.read(band, window=win).astype(float)   # shape (2,2): rows x cols
        if nodata is not None:
            a = np.where(a == nodata, np.nan, a)
        if np.isnan(a).any():
            continue
        fx, fy = col_f - col0, row_f - row0
        top = a[0, 0] * (1 - fx) + a[0, 1] * fx
        bot = a[1, 0] * (1 - fx) + a[1, 1] * fx
        out[i] = top * (1 - fy) + bot * fy
    return out


def analyse(path, ds, transformer, spike_grade, worst):
    nodes, links = load_nodes_and_links(path)
    ids = [nid for nid, nd in nodes.items() if nd["z"] is not None]
    if not ids:
        print(f"  [WARN] {Path(path).name}: keine z-Werte im Netz.")
        return

    xs = np.array([nodes[i]["x"] for i in ids])
    ys = np.array([nodes[i]["y"] for i in ids])
    z_net = np.array([nodes[i]["z"] for i in ids])

    xs_utm, ys_utm = transformer.transform(xs, ys)
    z_dem = sample_dtm_bilinear(ds, xs_utm, ys_utm)

    valid = np.isfinite(z_dem)
    n_miss = int((~valid).sum())
    dz = z_dem[valid] - z_net[valid]        # DEM-Truth minus Netz
    adz = np.abs(dz)

    print(f"\n=== {Path(path).name} ===")
    print(f"  Knoten: {len(ids)}  (DTM-gesampelt: {valid.sum()}, ausserhalb/nodata: {n_miss})")
    if dz.size == 0:
        print("  [WARN] keine gueltigen DTM-Samples.")
        return
    rms = float(np.sqrt(np.mean(dz**2)))
    print(f"  Delta_z (DEM - Netz) [m]:  mean={dz.mean():+.2f}  std={dz.std():.2f}  RMS={rms:.2f}")
    print(f"    |Delta_z| Perzentile:    p50={np.percentile(adz,50):.2f}  "
          f"p90={np.percentile(adz,90):.2f}  p95={np.percentile(adz,95):.2f}  "
          f"p99={np.percentile(adz,99):.2f}  max={adz.max():.2f}")
    for thr in (1, 3, 5, 10):
        c = int((adz > thr).sum())
        print(f"    |Delta_z| > {thr:2d} m: {c:4d} Knoten ({100*c/adz.size:.1f} %)")

    # --- Grad-Vergleich pro Link: Netz-z vs DEM-z ---
    zdem_by_id = {ids[k]: z_dem[k] for k in range(len(ids)) if valid[k]}
    znet_by_id = {ids[k]: z_net[k] for k in range(len(ids))}
    g_net, g_dem = [], []
    spikes_net = spikes_dem = 0
    for lk in links:
        L = lk["length"]
        a, b = lk["from"], lk["to"]
        if L <= 0:
            continue
        if a in znet_by_id and b in znet_by_id:
            gn = (znet_by_id[b] - znet_by_id[a]) / L
            g_net.append(gn)
            if abs(gn) > spike_grade:
                spikes_net += 1
        if a in zdem_by_id and b in zdem_by_id:
            gd = (zdem_by_id[b] - zdem_by_id[a]) / L
            g_dem.append(gd)
            if abs(gd) > spike_grade:
                spikes_dem += 1
    if g_net:
        print(f"  Steigung |g| > {spike_grade*100:.0f} %:   Netz={spikes_net} Links,  "
              f"DEM={spikes_dem} Links  (von {len(g_net)})")
        print(f"    std(grade):  Netz={np.std(g_net)*100:.2f} %   DEM={np.std(g_dem)*100:.2f} %")

    # --- groesste Ausreisser ---
    order = np.argsort(adz)[::-1][:worst]
    vids = [ids[k] for k in range(len(ids)) if valid[k]]
    vz_net = z_net[valid]
    vz_dem = z_dem[valid]
    print(f"  groesste {worst} Knoten-Abweichungen (UTM x,y | z_net -> z_dem | Delta):")
    for k in order:
        print(f"    node {vids[k]:>16s}  ({xs_utm[valid][k]:.0f},{ys_utm[valid][k]:.0f})  "
              f"{vz_net[k]:7.1f} -> {vz_dem[k]:7.1f}  {dz[k]:+6.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("networks", nargs="+", help="MATSim-Netz XML(.gz)")
    ap.add_argument("--dtm", default=str(DEFAULT_DTM))
    ap.add_argument("--net-crs", default="EPSG:4839")
    ap.add_argument("--spike-grade", type=float, default=0.12)
    ap.add_argument("--worst", type=int, default=15)
    args = ap.parse_args()

    dtm = Path(args.dtm)
    if not dtm.exists():
        print(f"DTM nicht gefunden: {dtm}", file=sys.stderr); sys.exit(1)

    transformer = Transformer.from_crs(args.net_crs, "EPSG:32632", always_xy=True)
    with rasterio.open(dtm) as ds:
        print(f"DTM: {dtm.name}  CRS={ds.crs}  res={ds.res}  net-CRS={args.net_crs}")
        for net in args.networks:
            analyse(net, ds, transformer, args.spike_grade, args.worst)


if __name__ == "__main__":
    main()