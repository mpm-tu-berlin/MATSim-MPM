# -*- coding: utf-8 -*-
"""Deutschland-Höhenprofil gegen den gesamten Telemetrie-Korpus prüfen.

Stufe 2/3 nach telemetry_elevation_corpus.py: statt elf ausgewählter Energie-
Routen wird das Höhenergebnis der Pipeline an ALLEN befahrenen Stellen gegen die
gemessenen Höhen gehalten, mit Schwerpunkt Brücken und Tunnel.

Referenzflächen (--ref):
  dense_V2  germany_dense_heights_V2.zip  (Standard; dichtes, geglättetes,
            bauwerks-linearisiertes Fahrbahnprofil der Pipeline, ~5 m Raster)
  net_V2 / net_V1 / net_V0   die MATSim-Netze (250-m-Links) — zeigen zusätzlich
            den Diskretisierungsverlust, net_V1/V0 sind die Stände VOR dem
            Bauwerks-Fix vom 2026-07-05 (Vintage-Vergleich).

Bauwerkserkennung: Brücken-/Tunnel-Segmente aus dem Detail-GPKG (OSM-
Granularität). Ein Telemetriepunkt gilt als "auf Bauwerk", wenn ein
Bauwerkssegment näher als STRUCT_M liegt UND dessen Richtung mit der lokalen
FAHRTRICHTUNG übereinstimmt. Damit zählen querende Überführungen nicht mit.

GPS-Drift wird je Fahrt über einen rollierenden Median entfernt, dessen
Stützstellen ausschließlich bauwerksfrei sind. Mehrfachbefahrungen derselben
Stelle liefern die Wiederholstreuung = empirischer Messrauschboden.

DATENSCHUTZ: Ein-/Ausgaben nur in gitignorten data/-Ordnern, stdout nur Aggregate.

Aufruf:
  python network_elevation_vs_telemetry.py --ref dense_V2 --plot-top 12
  python network_elevation_vs_telemetry.py --ref net_V1     # Vintage-Vergleich
"""
import argparse
import gzip
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio
from pyproj import Transformer
from scipy.spatial import cKDTree

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).parent
_NETGEN = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
DETAILED_GPKG = _NETGEN / "data" / "germany_detailed_sorted_DF.gpkg"
CORPUS = _SCRIPT_DIR / "data" / "telemetry_elev_corpus" / "corpus.parquet"
OUT_ROOT = _SCRIPT_DIR / "data" / "network_elev_vs_telemetry"

REFS = {
    "dense_V2": ("dense", _NETGEN / "data" / "germany_dense_heights_V2.zip"),
    # V3 = V2 + Halbpixel-Fix der DTM-Abtastung (Commit 153fa3d). Der Vergleich
    # net_V2 gegen net_V3 quantifiziert den Fix am produktiven Netz.
    "net_V3": ("net", _NETGEN / "data" / "germany_network_250m_V3.xml.gz"),
    "net_V2": ("net", _NETGEN / "data" / "germany_network_250m_V2.xml.gz"),
    "net_V1": ("net", _NETGEN / "data" / "germany_network_250m_V1.xml.gz"),
    "net_V0": ("net", _NETGEN / "data" / "germany_network_250m_V0.xml.gz"),
}

DENS_M = 20.0            # Verdichtung der Netzkanten (nur ref=net)
MATCH_M = 15.0           # max. Abstand Telemetriepunkt <-> Referenzpunkt
STRUCT_M = 12.0          # max. Abstand Telemetriepunkt <-> Bauwerkssegment
STRUCT_BEARING_DEG = 30.0
BASELINE_WIN_PTS = 150   # rollierender Median, 150 x 20 m = 3 km
APPROACH_M = 150.0
MIN_PASS_POINTS = 3
# Gueltigkeitsschranke (Befund 2026-08-17): Die Drift-Basislinie braucht genuegend
# bauwerksfreie Stuetzstellen. In Verflechtungsbereichen mit vielen parallelen
# Rampenbruecken sind bis zu 30 % der Punkte als Bauwerk geflaggt, nach Abzug der
# 150-m-Zufahrtszonen bleiben unter 40 % nutzbar -> die Basislinie wird aus der
# Ferne interpoliert und dev enthaelt den Antennenversatz statt des Bauwerksfehlers.
# Deutschlandweit sind 85,5 % nutzbar (Median), keine Fahrt unter 40 %.
MIN_BASELINE_SHARE = 0.40
GROUP_GRID_M = 200.0
# Lokale Basislinie je Bauwerk (Befund 2026-08-17): der rollierende 3-km-Median
# rechnet grossraeumige Profildefekte (z.B. ein 20-m-Absturz ohne Bauwerksbezug
# bei 7.09/51.23) den Bauwerken zu, die zufaellig darin liegen. Der Versatz wird
# deshalb aus einem Fenster UNMITTELBAR vor und hinter dem Bauwerk bestimmt.
LOCAL_BASE_GAP_M = 50.0     # Abstand zum Bauwerk (Widerlager/Damm aussparen)
LOCAL_BASE_WIN_M = 300.0    # Fensterlaenge dahinter
LOCAL_BASE_MIN_PTS = 5
# Defekterkennung unabhaengig von Bauwerken
DEFECT_THRESH_M = 3.0
DEFECT_MIN_LEN_M = 100.0
ALT_MIN, ALT_MAX = -50.0, 3000.0
STRUCT_VALS = ("yes", "true", "1", "viaduct", "bridge", "tunnel")
TARGET_EPSG = 4839


def _tf():
    return Transformer.from_crs("EPSG:4326", f"EPSG:{TARGET_EPSG}", always_xy=True)


# ---------------------------------------------------------------- Referenzfläche

def load_dense(path):
    """Dichtes Fahrbahnprofil aus dem ZIP (lon, lat, z) -> projizierte Arrays."""
    with zipfile.ZipFile(path) as z:
        name = z.infolist()[0].filename
        with z.open(name) as f:
            d = pd.read_csv(f, usecols=["lon", "lat", "z"],
                            dtype={"lon": "float64", "lat": "float64",
                                   "z": "float32"})
    x, y = _tf().transform(d["lon"].to_numpy(), d["lat"].to_numpy())
    return np.asarray(x), np.asarray(y), d["z"].to_numpy(np.float64)


def load_network_dense(path):
    """MATSim-Netz laden und jede Kante auf DENS_M verdichten."""
    with gzip.open(path, "rb") as f:
        root = ET.parse(f).getroot()
    nodes = {}
    for n in root.find("nodes").findall("node"):
        zt = n.get("z")
        nodes[n.get("id")] = (float(n.get("x")), float(n.get("y")),
                              float(zt) if zt else np.nan)
    seen, X, Y, Z = set(), [], [], []
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        key = frozenset((u, v))
        if key in seen or u not in nodes or v not in nodes:
            continue
        seen.add(key)
        x0, y0, z0 = nodes[u]
        x1, y1, z1 = nodes[v]
        L = float(np.hypot(x1 - x0, y1 - y0))
        if not np.isfinite(L) or L <= 0:
            continue
        k = max(2, int(L / DENS_M) + 1)
        t = np.linspace(0.0, 1.0, k)
        X.append(x0 + t * (x1 - x0)); Y.append(y0 + t * (y1 - y0))
        Z.append(z0 + t * (z1 - z0))
    return np.concatenate(X), np.concatenate(Y), np.concatenate(Z)


def load_reference(ref):
    kind, path = REFS[ref]
    print(f"  Referenz {ref}: {path.name}", flush=True)
    if kind == "dense":
        return load_dense(path)
    return load_network_dense(path)


# ------------------------------------------------------------- Bauwerkssegmente

def load_structure_segments():
    """Segmentmitten + Richtung + Typ aller getaggten Brücken/Tunnel."""
    where = (" OR ".join([f"bridge = '{v}'" for v in STRUCT_VALS])
             + " OR " + " OR ".join([f"tunnel = '{v}'" for v in STRUCT_VALS]))
    det = pyogrio.read_dataframe(DETAILED_GPKG, layer="edges",
                                 columns=["osmid", "bridge", "tunnel"],
                                 where=where)
    tf = _tf()
    mx, my, mb, mk = [], [], [], []
    for geom, tun in zip(det.geometry, det["tunnel"]):
        if geom is None or geom.is_empty:
            continue
        c = np.asarray(list(geom.coords), float)
        if len(c) < 2:
            continue
        gx, gy = tf.transform(c[:, 0], c[:, 1])
        gx = np.asarray(gx, float); gy = np.asarray(gy, float)
        mx.append(0.5 * (gx[:-1] + gx[1:])); my.append(0.5 * (gy[:-1] + gy[1:]))
        b = np.degrees(np.arctan2(np.diff(gy), np.diff(gx)))
        mb.append(b)
        is_tun = str(tun).strip().lower() in STRUCT_VALS
        mk.append(np.full(len(b), 1 if is_tun else 0, np.int8))
    print(f"  Bauwerkssegmente: {len(det)} Kanten -> "
          f"{sum(len(a) for a in mx):,} Teilsegmente", flush=True)
    return (np.concatenate(mx), np.concatenate(my),
            np.concatenate(mb), np.concatenate(mk))


def trace_bearing(x, y):
    """Lokale Fahrtrichtung [deg] je Punkt (zentrale Differenz)."""
    dx = np.gradient(x); dy = np.gradient(y)
    return np.degrees(np.arctan2(dy, dx))


# Kontinuitaetsbewusstes Matching (Befund K05/K09 2026-08-18): der naechste
# Referenzpunkt in 2D springt an Ueber-/Unterfuehrungen auf die kreuzende
# Strasse (Moseltalbruecke: Talstrasse 1,3 m neben der Trajektorie, Deck 4,4 m
# -> 133-m-Scheindefekt). Stattdessen waehlt eine Viterbi-Kette aus je MATCH_K
# Kandidaten die Folge mit minimalen Hoehenspruengen plus gewichtetem
# Querabstand. Die Auswahl nutzt NUR Netzhoehen (nie die Messung), damit echte
# Netzdefekte nicht weggematcht werden.
MATCH_K = 8
MATCH_GAP_S_M = 300.0    # s-Luecke, ab der die Kette neu startet
MATCH_DIST_W = 0.2       # Kosten je Meter Querabstand vs. Meter Hoehensprung


def match_continuity(rtree, rz, x, y, s):
    """Liefert (dist, index) je Punkt; dist=inf, wo kein Kandidat <= MATCH_M."""
    n = len(x)
    D, I = rtree.query(np.column_stack([x, y]), k=MATCH_K,
                       distance_upper_bound=MATCH_M, workers=-1)
    D = np.atleast_2d(D); I = np.atleast_2d(I)
    ok = np.isfinite(D) & (I < len(rz))
    Z = np.where(ok, rz[np.clip(I, 0, len(rz) - 1)], np.nan)
    ok &= np.isfinite(Z)
    D = np.where(ok, D, np.inf)
    Z = np.where(ok, Z, np.inf)

    best_d = np.full(n, np.inf)
    best_i = np.zeros(n, dtype=np.int64)
    idxs = np.flatnonzero(ok.any(axis=1))
    if len(idxs) == 0:
        return best_d, best_i
    breaks = np.flatnonzero(np.diff(s[idxs]) > MATCH_GAP_S_M) + 1
    for chain in np.split(idxs, breaks):
        cost = MATCH_DIST_W * D[chain[0]]
        back = np.zeros((len(chain), MATCH_K), dtype=np.int8)
        for a in range(1, len(chain)):
            with np.errstate(invalid="ignore"):
                jump = np.abs(Z[chain[a]][:, None] - Z[chain[a - 1]][None, :])
            jump[np.isnan(jump)] = np.inf          # inf - inf
            tot = jump + cost[None, :]
            back[a] = np.argmin(tot, axis=1)
            cost = tot[np.arange(MATCH_K), back[a]] + MATCH_DIST_W * D[chain[a]]
        c = int(np.argmin(cost))
        for a in range(len(chain) - 1, -1, -1):
            i = chain[a]
            best_d[i] = D[i, c]
            best_i[i] = I[i, c]
            c = int(back[a, c])
    return best_d, best_i


# ------------------------------------------------------------------- Auswertung

def evaluate(corpus, rx, ry, rz, sx, sy, sb, sk):
    print("  Referenz-KD-Tree ...", flush=True)
    rtree = cKDTree(np.column_stack([rx, ry]), balanced_tree=False,
                    compact_nodes=False)
    stree = cKDTree(np.column_stack([sx, sy]))
    tf = _tf()

    passes, ctrl, defects = [], [], []
    cell_trips = {}
    n_match, n_pts, n_struct = 0, 0, 0
    n_skipped = [0]
    groups = corpus.groupby("trip_id", sort=False)
    print(f"  {len(groups)} Fahrten auswerten ...", flush=True)
    for gi, (tid, g) in enumerate(groups, 1):
        g = g.sort_values("s_m")
        alt = g["alt_m"].to_numpy(float)
        good = np.isfinite(alt) & (alt > ALT_MIN) & (alt < ALT_MAX)
        if good.sum() < 20:
            continue
        s = g["s_m"].to_numpy(float)[good]
        lat = g["lat"].to_numpy(float)[good]
        lon = g["lon"].to_numpy(float)[good]
        alt = alt[good]
        x, y = tf.transform(lon, lat)
        x = np.asarray(x, float); y = np.asarray(y, float)
        n_pts += len(x)

        d, idx = match_continuity(rtree, rz, x, y, s)
        m = np.isfinite(d) & (d <= MATCH_M) & np.isfinite(rz[np.clip(idx, 0, len(rz) - 1)])
        if m.sum() < 20:
            continue
        s, x, y, alt, lat, lon = s[m], x[m], y[m], alt[m], lat[m], lon[m]
        z_ref = rz[idx[m]]
        n_match += len(s)


        # Bauwerksflag: Naehe UND Richtungsgleichheit mit der Fahrtrichtung
        bear = trace_bearing(x, y)
        ds, sidx = stree.query(np.column_stack([x, y]), k=4,
                               distance_upper_bound=STRUCT_M, workers=-1)
        ds = np.atleast_2d(ds); sidx = np.atleast_2d(sidx)
        on = np.zeros(len(s), bool)
        kind = np.zeros(len(s), np.int8)
        for c in range(ds.shape[1]):
            cand = np.isfinite(ds[:, c]) & (sidx[:, c] < len(sx)) & (~on)
            if not cand.any():
                continue
            j = sidx[cand, c]
            turn = np.abs((sb[j] - bear[cand] + 90.0) % 180.0 - 90.0)
            take = turn <= STRUCT_BEARING_DEG
            sel = np.flatnonzero(cand)[take]
            on[sel] = True
            kind[sel] = sk[j[take]]
        n_struct += int(on.sum())

        # Drift-Basislinie nur aus bauwerksfreien Punkten (inkl. Zufahrtszone)
        near = on.copy()
        if on.any():
            # Abstand zum naechsten Bauwerkspunkt ueber searchsorted (s ist sortiert),
            # keine NxM-Matrix
            si = s[on]
            pos = np.searchsorted(si, s)
            left = np.where(pos > 0, s - si[np.clip(pos - 1, 0, len(si) - 1)], np.inf)
            right = np.where(pos < len(si), si[np.clip(pos, 0, len(si) - 1)] - s, np.inf)
            near |= (np.minimum(left, right) <= APPROACH_M)
        # Gueltigkeit: genuegend bauwerksfreie Stuetzstellen fuer die Basislinie?
        share = float((~near).mean())
        if share < MIN_BASELINE_SHARE:
            n_skipped[0] += 1
            continue

        dz = z_ref - alt
        ser = pd.Series(np.where(near, np.nan, dz))
        base = ser.rolling(BASELINE_WIN_PTS, center=True, min_periods=5).median()
        base = base.interpolate(limit_direction="both").to_numpy()
        if not np.isfinite(base).any():
            continue
        dev = dz - base
        ctrl.append(dev[(~near) & np.isfinite(dev)])

        # Pass-Konsistenz-Rohdaten: mittlere Abweichung dieser Fahrt je
        # 200-m-Zelle (fuer den Abgleich Netzdefekt vs. Fahrzeugfehler)
        fin = np.isfinite(dev)
        cxs = (x[fin] // GROUP_GRID_M).astype(int)
        cys = (y[fin] // GROUP_GRID_M).astype(int)
        for cx_, cy_, dv_ in zip(cxs, cys, dev[fin]):
            acc = cell_trips.setdefault((cx_, cy_), {}).setdefault(tid, [0.0, 0])
            acc[0] += dv_; acc[1] += 1

        # --- Defekte unabhaengig von Bauwerken: zusammenhaengende Abschnitte
        # mit |dev| ueber DEFECT_THRESH_M und mindestens DEFECT_MIN_LEN_M Laenge
        big = np.isfinite(dev) & (np.abs(dev) > DEFECT_THRESH_M)
        k = 0
        while k < len(big):
            if not big[k]:
                k += 1
                continue
            m2 = k
            while m2 + 1 < len(big) and big[m2 + 1]:
                m2 += 1
            if s[m2] - s[k] >= DEFECT_MIN_LEN_M:
                sl = slice(k, m2 + 1)
                defects.append(dict(
                    trip_id=tid, s0_m=float(s[k]), s1_m=float(s[m2]),
                    len_m=float(s[m2] - s[k]),
                    on_struct_share=float(on[sl].mean()),
                    dev_mean_m=float(np.nanmean(dev[sl])),
                    dev_max_abs_m=float(np.nanmax(np.abs(dev[sl]))),
                    x=float(x[sl].mean()), y=float(y[sl].mean()),
                    lat=float(lat[sl].mean()), lon=float(lon[sl].mean()),
                    _cells=set(zip((x[sl] // GROUP_GRID_M).astype(int),
                                   (y[sl] // GROUP_GRID_M).astype(int)))))
            k = m2 + 1

        # Passes bilden
        k = 0
        while k < len(on):
            if not on[k]:
                k += 1
                continue
            m2 = k
            while m2 + 1 < len(on) and on[m2 + 1]:
                m2 += 1
            sl = slice(k, m2 + 1)
            if (m2 + 1 - k) >= MIN_PASS_POINTS and np.isfinite(dev[sl]).sum() >= 2:
                # lokale Basislinie: Versatz aus den Fenstern unmittelbar vor und
                # hinter dem Bauwerk, damit grossraeumige Profildefekte nicht dem
                # Bauwerk angelastet werden
                s0p, s1p = s[k], s[m2]
                w = (((s >= s0p - LOCAL_BASE_GAP_M - LOCAL_BASE_WIN_M)
                      & (s <= s0p - LOCAL_BASE_GAP_M))
                     | ((s >= s1p + LOCAL_BASE_GAP_M)
                        & (s <= s1p + LOCAL_BASE_GAP_M + LOCAL_BASE_WIN_M)))
                w &= (~on) & np.isfinite(dz)
                dev_p = dev[sl]
                local = False
                if int(w.sum()) >= LOCAL_BASE_MIN_PTS:
                    dev_p = dz[sl] - float(np.median(dz[w]))
                    local = True
                zn = z_ref[sl]
                zm = zn - dev_p
                dn, dm = np.diff(zn), np.diff(zm)
                passes.append(dict(
                    trip_id=tid, s0_m=float(s[k]), s1_m=float(s[m2]),
                    len_m=float(s[m2] - s[k]), n_points=int(m2 + 1 - k),
                    kind=("tunnel" if kind[k] == 1 else "bridge"),
                    lat=float(lat[sl].mean()), lon=float(lon[sl].mean()),
                    x=float(x[sl].mean()), y=float(y[sl].mean()),
                    local_base=bool(local),
                    dev_in_m=float(dev_p[0]), dev_out_m=float(dev_p[-1]),
                    dev_mean_m=float(np.nanmean(dev_p)),
                    dev_max_abs_m=float(np.nanmax(np.abs(dev_p))),
                    dev_global_max_abs_m=float(np.nanmax(np.abs(dev[sl]))),
                    excess_climb_m=float(np.nansum(np.maximum(dn, 0.0))
                                         - np.nansum(np.maximum(dm, 0.0)))))
            k = m2 + 1
        if gi % 200 == 0:
            print(f"    {gi}/{len(groups)} Fahrten, {len(passes):,} Passes",
                  flush=True)

    print(f"  zugeordnet {n_match:,} von {n_pts:,} Punkten "
          f"({100.0*n_match/max(n_pts,1):.1f} %), auf Bauwerk {n_struct:,}", flush=True)
    if n_skipped[0]:
        print(f"  verworfen: {n_skipped[0]} Fahrten mit unter "
              f"{100*MIN_BASELINE_SHARE:.0f} % bauwerksfreien Stuetzstellen "
              f"(Basislinie nicht bestimmbar)", flush=True)
    # Pass-Konsistenz je Defekt (Befund 2026-08-18: 7 von 9 gesichteten
    # Top-Korridoren waren fahrzeugseitig): Ein Netzdefekt muss sich in den
    # Zellen des Defekts auch bei ANDEREN Fahrten als mittlere Abweichung
    # gleichen Vorzeichens (>= 2 m) zeigen; Fahrzeugfehler tun das nicht.
    AGREE_MIN_M = 2.0
    for dd in defects:
        per_trip = {}
        for c in dd.pop("_cells"):
            for tid2, (sm, n) in cell_trips.get(c, {}).items():
                acc = per_trip.setdefault(tid2, [0.0, 0])
                acc[0] += sm; acc[1] += n
        sign = 1.0 if dd["dev_mean_m"] >= 0 else -1.0
        means = [sm / n for sm, n in per_trip.values() if n >= 3]
        agree = sum(1 for mv in means if sign * mv >= AGREE_MIN_M)
        dd["n_trips_here"] = len(per_trip)
        dd["n_trips_defect"] = agree   # inkl. der Fahrt des Defekts selbst
        dd["defect_share"] = (agree / len(per_trip) if per_trip else np.nan)
        # Konsens-Groesse fuers Ranking: Median ueber ALLE Fahrten an der
        # Stelle. Ein Fahrzeugspike einer Einzelfahrt (680 m, t000764)
        # verwaessert damit auf ~0, ein echter Netzdefekt bleibt in voller
        # Hoehe stehen.
        dd["dev_consens_m"] = float(np.median(means)) if means else np.nan
    return (pd.DataFrame(passes), (np.concatenate(ctrl) if ctrl else np.array([])),
            pd.DataFrame(defects))


def group_structures(P, min_passes):
    if P.empty:
        return P, pd.DataFrame()
    P = P.copy()
    P["group"] = ((P["x"] / GROUP_GRID_M).round().astype(int).astype(str) + "_"
                  + (P["y"] / GROUP_GRID_M).round().astype(int).astype(str))
    G = P.groupby("group").agg(
        n_passes=("trip_id", "nunique"), kind=("kind", "first"),
        len_m=("len_m", "median"), x=("x", "mean"), y=("y", "mean"),
        dev_mean_med=("dev_mean_m", "median"),
        dev_max_med=("dev_max_abs_m", "median"),
        dev_max_spread=("dev_max_abs_m", lambda v: float(np.std(v))),
        dev_in_med=("dev_in_m", "median"), dev_out_med=("dev_out_m", "median"),
        excess_med=("excess_climb_m", "median")).reset_index()
    inv = Transformer.from_crs(f"EPSG:{TARGET_EPSG}", "EPSG:4326", always_xy=True)
    lo, la = inv.transform(G["x"].to_numpy(), G["y"].to_numpy())
    G["lon"], G["lat"] = lo, la
    return P, G


def summarize(P, C, G, min_passes, ref, path):
    Gm = G[G["n_passes"] >= min_passes]
    t = P.assign(lenbin=pd.cut(P["len_m"], [0, 50, 100, 200, 400, 800, 5000])) \
         .groupby("lenbin", observed=True).agg(
             n=("len_m", "size"), p50=("dev_max_abs_m", "median"),
             p90=("dev_max_abs_m", lambda v: v.quantile(0.9)),
             mx=("dev_max_abs_m", "max"),
             excess=("excess_climb_m", "sum")).round(2)
    tk = P.groupby("kind").agg(n=("len_m", "size"),
                               p50=("dev_max_abs_m", "median"),
                               p90=("dev_max_abs_m", lambda v: v.quantile(0.9))).round(2)
    txt = [f"Referenz: {ref} ({path.name})",
           f"Bauwerksdurchfahrten: {len(P):,} auf {P['trip_id'].nunique()} Fahrten",
           f"unterscheidbare Bauwerksstellen: {len(G):,} "
           f"(mit >= {min_passes} Durchfahrten: {len(Gm):,})", ""]
    if C.size:
        txt.append(f"Rauschboden bauwerksfrei |dev|: p50 {np.median(np.abs(C)):.2f} m, "
                   f"p90 {np.percentile(np.abs(C), 90):.2f} m (n={C.size:,})")
    txt += [f"Bauwerk max|dev| je Pass: p50 {P['dev_max_abs_m'].median():.2f}, "
            f"p90 {P['dev_max_abs_m'].quantile(0.9):.2f}, "
            f"max {P['dev_max_abs_m'].max():.2f} m",
            f"Vorzeichen: {int((P['dev_mean_m']<0).sum()):,} Passes zu tief / "
            f"{int((P['dev_mean_m']>0).sum()):,} zu hoch",
            f"Anstiegsüberschuss aller Passes: {P['excess_climb_m'].sum():+.1f} m",
            "", "max|dev| nach Bauwerkslänge:", t.to_string(),
            "", "nach Typ:", tk.to_string()]
    if len(Gm):
        txt += ["", f"Wiederholstreuung derselben Stelle (std max|dev| über "
                    f"Durchfahrten): p50 {Gm['dev_max_spread'].median():.2f} m, "
                    f"p90 {Gm['dev_max_spread'].quantile(0.9):.2f} m "
                    f"= empirischer Rauschboden je Bauwerk",
                f"schlechteste Stellen (median max|dev|, >= {min_passes} Durchfahrten):",
                Gm.sort_values("dev_max_med", ascending=False)
                  .head(15)[["group", "kind", "n_passes", "len_m", "dev_max_med",
                             "dev_mean_med", "dev_max_spread", "excess_med",
                             "lon", "lat"]].round(2).to_string(index=False)]
    return "\n".join(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="dense_V2", choices=sorted(REFS))
    ap.add_argument("--min-passes", type=int, default=3)
    ap.add_argument("--limit-trips", type=int, default=0)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / f"{args.ref}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Ausgabe: {out_dir}", flush=True)

    rx, ry, rz = load_reference(args.ref)
    print(f"  {len(rx):,} Referenzpunkte", flush=True)
    sx, sy, sb, sk = load_structure_segments()

    corpus = pd.read_parquet(CORPUS)
    if args.limit_trips:
        keep = corpus["trip_id"].drop_duplicates().head(args.limit_trips)
        corpus = corpus[corpus["trip_id"].isin(keep)]
    print(f"  Korpus: {len(corpus):,} Punkte, "
          f"{corpus['trip_id'].nunique()} Fahrten", flush=True)

    P, C, DF = evaluate(corpus, rx, ry, rz, sx, sy, sb, sk)
    if not DF.empty:
        DF.to_csv(out_dir / "defects.csv", index=False, encoding="utf-8")
    if P.empty:
        print("keine Bauwerksdurchfahrten")
        return
    P, G = group_structures(P, args.min_passes)
    P.to_csv(out_dir / "passes.csv", index=False, encoding="utf-8")
    G.to_csv(out_dir / "structures.csv", index=False, encoding="utf-8")
    out = summarize(P, C, G, args.min_passes, args.ref, REFS[args.ref][1])
    if not DF.empty:
        # Fehleranteil, der NICHT an Bauwerken sitzt (User-Verdacht 2026-08-17)
        d = DF.copy()
        d["klasse"] = np.where(d["on_struct_share"] >= 0.30, "Bauwerk",
                               np.where(d["on_struct_share"] > 0.0,
                                        "gemischt", "ohne Bauwerk"))
        t = d.groupby("klasse").agg(
            n=("len_m", "size"), km=("len_m", lambda v: v.sum() / 1000.0),
            dev_p50=("dev_max_abs_m", "median"),
            dev_p90=("dev_max_abs_m", lambda v: v.quantile(0.9)),
            dev_max=("dev_max_abs_m", "max")).round(2)
        out += ("\n\n== Defekte (|dev| > "
                f"{DEFECT_THRESH_M:.0f} m ueber mindestens {DEFECT_MIN_LEN_M:.0f} m), "
                "nach Bauwerksbezug ==\n" + t.to_string()
                + f"\n  Anteil der Defektlaenge OHNE Bauwerksbezug: "
                  f"{100.0 * d.loc[d.klasse == 'ohne Bauwerk', 'len_m'].sum() / d.len_m.sum():.1f} %")
    (out_dir / "summary.txt").write_text(out, encoding="utf-8")
    print("\n" + out, flush=True)
    print(f"\nfertig: {out_dir}", flush=True)


if __name__ == "__main__":
    main()
