# -*- coding: utf-8 -*-
"""Audit der Bauwerks-Ankerlogik (Brücken/Tunnel) in der Höhenzuweisung.

Prüft NICHT die Routen der Realfahrten, sondern flächendeckend jedes Bauwerk in
einer Region: greift die Linearisierung, ist der Anker gültig, bleibt nach
Linearisierung + Spline ein Rest-Artefakt übrig.

Der Kern ist ein instrumentierter SPIEGEL von
`assign_heights_along_corridors` (Skript 04, Zeilen 224-416). Damit der Spiegel
nicht von der Produktion abdriftet, wird am Ende der echte Produktionsaufruf
gegengerechnet: max|Δz| über alle Knoten muss ~0 sein (Selbstvalidierung).

Fehlermodi, die je Linearisierungs-Run gezählt werden:
  F1 anchor_single        Anker ist genau EIN Sample-Punkt (immer wahr, Kennzahl
                          ist die Abweichung gegen einen robusten Fit, s. F1b)
  F1b anchor_dev_*        |Anker - Theil-Sen-Extrapolation der letzten k Punkte|
  F2 anchor_at_edge       Anker liegt am Korridorrand -> Punkt AUF dem Bauwerk
  F3 junction_in_span     Kreuzungsknoten im Bauwerk (wird direkt/ungeglättet
                          gesampelt, Strukturkorrektur greift dort nicht)
  F4 spline_dev           Spline verbiegt die linearisierte Fläche wieder
  F5 too_short            Run kürzer als 2 Sample-Punkte
  F6 fallback_whole_edge  Ganz-Kanten-Fallback statt Detail-Intervall
  F7 grade_implausible    implizite Bauwerkslängsneigung unplausibel

Aufruf:
  python structure_anchor_audit.py --region TH
  python structure_anchor_audit.py --bbox 10.0,50.6,11.2,51.2 --plot-top 20
  python structure_anchor_audit.py --region TH --plot-id TH_00042

Ausgaben (alle unter data/structure_audit/<tag>/, gitignored):
  spans.csv        eine Zeile je Linearisierungs-Run mit allen Flags/Kennzahlen
  summary.txt      Häufigkeitstabelle
  plots/*.png|svg  Höhenprofil ±5 km um die stärksten Kandidaten
"""
import argparse
import sys
from datetime import datetime
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import pyogrio
from pyproj import Transformer
from shapely.geometry import Point

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).parent
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"

DTM_PATH = _SCRIPT_DIR / "data" / "DTM Germany 20m v3b by Sonny.tif"
SIMPLIFIED_GPKG = _NETGEN_DIR / "data" / "germany_simplified_DF.gpkg"
DETAILED_GPKG = _NETGEN_DIR / "data" / "germany_detailed_sorted_DF.gpkg"
OUT_ROOT = _SCRIPT_DIR / "data" / "structure_audit"

# Höhenzuweisung: identisch zu generate_section_link_length_variants.py
TARGET_EPSG = 4839
SAMPLE_STEP_M = 5.0
SMOOTH_RMS_M = 1.0
MAX_LINK_LENGTH_M = 250.0

ANCHOR_FIT_K = 10        # Punkte für den robusten Anker-Vergleichsfit (10 x 5 m = 50 m)
GRADE_LIMIT_PCT = 6.0    # oberhalb: Bauwerkslängsneigung unplausibel
PLOT_CONTEXT_M = 5000.0  # Kontext vor/nach der Struktur im Plot

# Bundesland-Bboxen (lon0, lat0, lon1, lat1), grob, nur als Lesefilter.
REGIONS = {
    "TH": (9.87, 50.20, 12.66, 51.65),    # Thüringen: viele Talbrücken + Tunnel
    "SL": (6.35, 49.11, 7.41, 49.64),     # Saarland: klein, schneller Testlauf
    "BW": (7.51, 47.53, 10.50, 49.79),
    "BY": (8.98, 47.27, 13.84, 50.56),
    "HE": (7.77, 49.39, 10.24, 51.66),
    "SN": (11.87, 50.17, 15.04, 51.69),
    "RP": (6.11, 48.97, 8.51, 50.94),
    "NW": (5.86, 50.32, 9.46, 52.53),
    "NI": (6.65, 51.29, 11.60, 53.89),
    "ST": (10.56, 50.94, 13.19, 53.04),
    "BB": (11.27, 51.36, 14.77, 53.56),
    "MV": (10.59, 53.11, 14.41, 54.68),
    "SH": (7.86, 53.36, 11.31, 55.06),
}
BORDER_PAD_DEG = 0.10    # Lese-Puffer; Spans im Puffer werden als Randfall markiert


def _import_script04():
    """Skript 04 als Modul laden (lokal, sonst aus dem Netzgen-Worktree)."""
    p = _SCRIPT_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    if not p.exists():
        p = _NETGEN_DIR / "04_build_matsim_network_from_local_osm_and_kdtree.py"
    spec = spec_from_file_location("script04", str(p))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- Eingangsdaten

def load_region(bbox):
    """Kanten/Knoten der Region aus den Germany-GPKGs (bbox in EPSG:4326)."""
    lon0, lat0, lon1, lat1 = bbox
    read_bbox = (lon0 - BORDER_PAD_DEG, lat0 - BORDER_PAD_DEG,
                 lon1 + BORDER_PAD_DEG, lat1 + BORDER_PAD_DEG)
    print(f"  lese GPKGs mit bbox {read_bbox} ...", flush=True)
    e_simp = pyogrio.read_dataframe(SIMPLIFIED_GPKG, layer="edges", bbox=read_bbox)
    e_det = pyogrio.read_dataframe(DETAILED_GPKG, layer="edges", bbox=read_bbox)
    n_det = pyogrio.read_dataframe(DETAILED_GPKG, layer="nodes", bbox=read_bbox)
    print(f"  simplified {len(e_simp)} Kanten, detailed {len(e_det)} Kanten, "
          f"{len(n_det)} Knoten", flush=True)
    return e_simp, e_det, n_det


def build_shortened_network(s04, e_simp, e_det, n_det, max_len):
    """Produktionspfad: short_edges + Knoten-Lookup (wie der Sektions-Generator)."""
    edges_short, split_xy = s04.short_edges(
        gdf_edges_simplified=e_simp.copy(),
        gdf_edges_detailed=e_det.copy(),
        max_allowed_length=float(max_len))

    def _first(v):
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) > 0:
            return v[0]
        return v

    nd = n_det.copy()
    nd["osmid_norm"] = pd.to_numeric(nd["osmid"].apply(_first), errors="coerce")
    nd = nd.dropna(subset=["osmid_norm"])
    osm_xy = {str(int(r["osmid_norm"])): (float(r.geometry.x), float(r.geometry.y))
              for _, r in nd.iterrows()}

    # Endpunkt-Koordinaten in EINEM Durchlauf statt per-Knoten-Suche
    edge_xy = {}
    for _, r in edges_short.iterrows():
        g = r.geometry
        if g is None:
            continue
        c = list(g.coords)
        edge_xy.setdefault(str(r["u"]), (float(c[0][0]), float(c[0][1])))
        edge_xy.setdefault(str(r["v"]), (float(c[-1][0]), float(c[-1][1])))

    used = set(map(str, edges_short["u"])) | set(map(str, edges_short["v"]))
    node_lonlat = {}
    for nid in used:
        if nid in osm_xy:
            node_lonlat[nid] = osm_xy[nid]
        elif nid in split_xy:
            node_lonlat[nid] = split_xy[nid]
        elif nid in edge_xy:
            node_lonlat[nid] = edge_xy[nid]
    return edges_short, node_lonlat


# ------------------------------------------------- Spiegel der Produktionslogik

def build_corridors(s04, gdf_edges):
    """Korridor-Topologie exakt wie assign_heights_along_corridors (04:224-273)."""
    from collections import defaultdict
    adj = defaultdict(list)
    egeom, estruct, eivals, etags = {}, {}, {}, {}
    for k, row in gdf_edges.iterrows():
        u, v = str(row["u"]), str(row["v"])
        g = row.geometry
        if g is None or u == v:
            continue
        egeom[k] = (g, u, v)
        estruct[k] = s04._is_structure(row)
        eivals[k] = s04._edge_struct_ivals(row)
        etags[k] = _tag_kind(row)
        adj[u].append((k, v))
        adj[v].append((k, u))

    deg = {n: len(lst) for n, lst in adj.items()}
    junctions = {n for n, d in deg.items() if d != 2}

    visited, corridors = set(), []

    def walk(start, first_edge, first_other):
        path_nodes, path_edges = [start, first_other], [first_edge]
        visited.add(first_edge)
        cur = first_other
        while deg.get(cur, 0) == 2 and cur not in junctions:
            nxts = [(ek, o) for (ek, o) in adj[cur] if ek not in visited]
            if not nxts:
                break
            ek, o = nxts[0]
            visited.add(ek)
            path_nodes.append(o)
            path_edges.append(ek)
            cur = o
            if o == start:
                break
        return path_nodes, path_edges

    for j in junctions:
        for (ek, o) in adj[j]:
            if ek not in visited:
                corridors.append(walk(j, ek, o))
    for k in list(egeom.keys()):
        if k not in visited:
            g, u, v = egeom[k]
            corridors.append(walk(u, k, v))
    return adj, deg, junctions, egeom, estruct, eivals, etags, corridors


def _tag_kind(row):
    """'bridge' / 'tunnel' / 'both' / '' aus den Rohtags der Kante."""
    def has(key):
        if key not in row.index:
            return False
        val = row[key]
        vals = val if isinstance(val, (list, tuple)) else [val]
        return any(str(v).strip().lower() in
                   ("yes", "true", "1", "viaduct", "bridge", "tunnel") for v in vals)
    b, t = has("bridge"), has("tunnel")
    return "both" if (b and t) else ("bridge" if b else ("tunnel" if t else ""))


def sample_corridor_profiles(s04, gdf_edges, node_lonlat, dtm, target_epsg,
                             sample_step_m):
    """Dichte Sample-Punkte je Korridor + Bauwerks-Spans MIT Herkunft.

    Spiegelt 04:275-344, ergänzt je Span (s0, s1, source, kind).
    """
    adj, deg, junctions, egeom, estruct, eivals, etags, corridors = \
        build_corridors(s04, gdf_edges)
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True)

    all_lon, all_lat, slices, covered, direct_nodes = [], [], [], set(), []

    for path_nodes, path_edges in corridors:
        try:
            fx, fy = [], []
            node_s = {path_nodes[0]: 0.0}
            struct_spans = []
            cum = 0.0
            for i, ek in enumerate(path_edges):
                g, eu, ev = egeom[ek]
                lon, lat = zip(*list(g.coords))
                mx, my = tf.transform(np.asarray(lon, float), np.asarray(lat, float))
                mx = np.asarray(mx, float); my = np.asarray(my, float)
                rev = (eu != path_nodes[i])
                if rev:
                    mx, my = mx[::-1], my[::-1]
                if i == 0:
                    fx.extend(mx.tolist()); fy.extend(my.tolist())
                else:
                    fx.extend(mx[1:].tolist()); fy.extend(my[1:].tolist())
                s0e = cum
                cum += float(np.hypot(np.diff(mx), np.diff(my)).sum())
                node_s[path_nodes[i + 1]] = cum
                Le = cum - s0e
                iv = eivals.get(ek)
                if iv is not None:
                    for (f0, f1) in iv:
                        a, b = ((1.0 - f1, 1.0 - f0) if rev else (f0, f1))
                        struct_spans.append((s0e + a * Le, s0e + b * Le,
                                             "detail", etags.get(ek, "")))
                elif estruct.get(ek, False):
                    struct_spans.append((s0e, cum, "fallback_whole_edge",
                                         etags.get(ek, "")))
            fx = np.asarray(fx); fy = np.asarray(fy)
            seg = np.hypot(np.diff(fx), np.diff(fy))
            s_vtx = np.concatenate([[0.0], np.cumsum(seg)])
            L = float(s_vtx[-1])
            if fx.size < 2 or L <= 0:
                raise ValueError("degenerate")
            n_samp = max(2, int(np.ceil(L / max(1.0, sample_step_m))) + 1)
            ss = np.linspace(0.0, L, n_samp)
            sx = np.interp(ss, s_vtx, fx); sy = np.interp(ss, s_vtx, fy)
            lon_s, lat_s = tf.transform(sx, sy, direction="INVERSE")
            start = len(all_lon)
            all_lon.extend(np.asarray(lon_s).tolist())
            all_lat.extend(np.asarray(lat_s).tolist())
            slices.append((path_nodes, node_s, start, n_samp, ss, struct_spans))
            covered.update(path_nodes)
        except Exception:
            direct_nodes.extend(path_nodes)

    for n in junctions:
        direct_nodes.append(n)
    for n in adj:
        if n not in covered and n not in junctions:
            direct_nodes.append(n)
    direct_nodes = list(dict.fromkeys(direct_nodes))
    direct_start = len(all_lon)
    for n in direct_nodes:
        if n in node_lonlat:
            lo, la = node_lonlat[n]
        else:
            lo, la = float("nan"), float("nan")
        all_lon.append(lo); all_lat.append(la)

    zall = s04.sample_heights(dtm, np.asarray(all_lon), np.asarray(all_lat))
    return dict(slices=slices, direct_nodes=direct_nodes, direct_start=direct_start,
                zall=np.asarray(zall, float), all_lon=np.asarray(all_lon, float),
                all_lat=np.asarray(all_lat, float), junctions=junctions,
                node_lonlat=node_lonlat)


def _fit_spline(ss, zdense, smooth_rms_m):
    """Spline-Fit exakt wie 04:379-396; gibt (zfun, ok) zurück."""
    try:
        from scipy.interpolate import UnivariateSpline
    except Exception:
        UnivariateSpline = None
    import warnings as _warn
    fin = np.isfinite(zdense)
    if fin.sum() < 2:
        return None, False
    s_fit, z_fit = ss[fin], zdense[fin]
    if smooth_rms_m and smooth_rms_m > 0 and UnivariateSpline is not None and s_fit.size >= 4:
        w = np.clip(np.abs(np.gradient(s_fit)), 1e-6, None)
        k = min(3, s_fit.size - 1)
        try:
            with _warn.catch_warnings():
                _warn.simplefilter("ignore")
                spl = UnivariateSpline(s_fit, z_fit, w=w,
                                       s=(smooth_rms_m ** 2) * float(w.sum()), k=k)
            return (lambda q, _s=spl: np.asarray(_s(q), float)), True
        except Exception:
            pass
    return (lambda q, a=s_fit, b=z_fit: np.interp(q, a, b)), True


def _linearize(ss, zdense_raw, struct_spans):
    """Bauwerks-Linearisierung exakt wie 04:361-377, gibt zusätzlich die Runs."""
    zlin = zdense_raw.astype(float).copy()
    runs = []
    if not struct_spans:
        return zlin, runs
    is_st = np.zeros(len(ss), dtype=bool)
    for (a, b, _src, _kind) in struct_spans:
        is_st[(ss >= a) & (ss <= b)] = True
    i, N = 0, len(ss)
    while i < N:
        if is_st[i]:
            j = i
            while j < N and is_st[j]:
                j += 1
            z_a = zlin[i - 1] if (i > 0 and np.isfinite(zlin[i - 1])) else zlin[i]
            z_b = zlin[j] if (j < N and np.isfinite(zlin[j])) else zlin[j - 1]
            applied = False
            if np.isfinite(z_a) and np.isfinite(z_b):
                zlin[i:j] = np.linspace(z_a, z_b, j - i)
                applied = True
            runs.append(dict(i=i, j=j, z_a=float(z_a), z_b=float(z_b),
                             anchor_left_edge=(i == 0), anchor_right_edge=(j >= N),
                             applied=applied))
            i = j
        else:
            i += 1
    return zlin, runs


def _robust_anchor(ss, z, idx_anchor, direction, k):
    """Theil-Sen-Extrapolation aus k Punkten AUSSERHALB des Runs auf die Spankante.

    direction=-1: Punkte links davon, +1: rechts. Gibt (z_hat, slope) oder (nan, nan).
    """
    try:
        from scipy.stats import theilslopes
    except Exception:
        return float("nan"), float("nan")
    if direction < 0:
        lo, hi = max(0, idx_anchor - k + 1), idx_anchor + 1
    else:
        lo, hi = idx_anchor, min(len(ss), idx_anchor + k)
    s, y = ss[lo:hi], z[lo:hi]
    m = np.isfinite(y)
    if m.sum() < 3:
        return float("nan"), float("nan")
    s, y = s[m], y[m]
    try:
        slope, inter, _, _ = theilslopes(y, s)
    except Exception:
        return float("nan"), float("nan")
    return float(inter + slope * ss[idx_anchor]), float(slope)


# --------------------------------------------------------------------- Audit

def audit_region(s04, edges_short, node_lonlat, dtm, bbox, tag):
    prof = sample_corridor_profiles(s04, edges_short, node_lonlat, dtm,
                                    TARGET_EPSG, SAMPLE_STEP_M)
    zall, junctions = prof["zall"], prof["junctions"]
    lon0, lat0, lon1, lat1 = bbox
    rows, z_mirror, corridor_cache = [], {}, {}

    for j_dn, n in enumerate(prof["direct_nodes"]):
        z_mirror[n] = float(zall[prof["direct_start"] + j_dn])

    for ci, (path_nodes, node_s, start, n_samp, ss, struct_spans) in enumerate(prof["slices"]):
        zraw = zall[start:start + n_samp].astype(float).copy()
        zlin, runs = _linearize(ss, zraw, struct_spans)
        zfun, ok = _fit_spline(ss, zlin, SMOOTH_RMS_M)
        if not ok:
            for n in path_nodes:
                if n not in junctions:
                    z_mirror.setdefault(n, float("nan"))
            continue
        zfin = np.asarray(zfun(ss), float)
        for n in [x for x in path_nodes if x not in junctions]:
            z_mirror[n] = float(zfun(np.asarray([node_s[n]], float))[0])
        corridor_cache[ci] = dict(start=start, n_samp=n_samp, ss=ss, zraw=zraw,
                                  zlin=zlin, zfin=zfin, path_nodes=path_nodes,
                                  node_s=node_s, struct_spans=struct_spans)

        for ri, r in enumerate(runs):
            i, j = r["i"], r["j"]
            s0, s1 = float(ss[i]), float(ss[min(j, n_samp - 1)])
            span_len = s1 - s0
            mid = 0.5 * (s0 + s1)
            kmid = int(np.clip(np.searchsorted(ss, mid), 0, n_samp - 1))
            lon_c = float(prof["all_lon"][start + kmid])
            lat_c = float(prof["all_lat"][start + kmid])

            # Herkunft/Art der beitragenden Spans
            srcs = {sp[2] for sp in struct_spans if not (sp[1] < s0 or sp[0] > s1)}
            kinds = {sp[3] for sp in struct_spans if not (sp[1] < s0 or sp[0] > s1)}
            kinds.discard("")

            # F1b robuster Anker-Vergleich
            zl_hat, sl_l = _robust_anchor(ss, zraw, max(i - 1, 0), -1, ANCHOR_FIT_K)
            zr_hat, sl_r = _robust_anchor(ss, zraw, min(j, n_samp - 1), +1, ANCHOR_FIT_K)
            dev_l = abs(r["z_a"] - zl_hat) if np.isfinite(zl_hat) else float("nan")
            dev_r = abs(r["z_b"] - zr_hat) if np.isfinite(zr_hat) else float("nan")

            # F3 Kreuzungen im Span
            jn = [n for n in path_nodes
                  if n in junctions and s0 <= node_s.get(n, -1e18) <= s1]

            # F4 Spline-Abweichung von der Linearisierung im Span
            sl = slice(i, max(j, i + 1))
            spline_dev = (float(np.nanmax(np.abs(zfin[sl] - zlin[sl])))
                          if j > i else float("nan"))

            # Rest-Welligkeit des FINALEN Profils im Span (sollte ~linear sein)
            if j - i >= 3:
                lin_ref = np.linspace(zfin[i], zfin[min(j, n_samp) - 1], j - i)
                resid = zfin[i:j] - lin_ref
                d = np.diff(resid)
                residual_climb = float(np.nansum(d[d > 0]))
            else:
                residual_climb = float("nan")

            # Wie tief das DTM unter/über der linearisierten Fahrbahn lag
            dive = (float(np.nanmax(zlin[sl] - zraw[sl])) if j > i else float("nan"))
            rise = (float(np.nanmax(zraw[sl] - zlin[sl])) if j > i else float("nan"))

            grade_pct = (100.0 * (r["z_b"] - r["z_a"]) / span_len
                         if span_len > 0 else float("nan"))

            in_core = (lon0 <= lon_c <= lon1) and (lat0 <= lat_c <= lat1)
            near_border = not in_core

            sev = np.nansum([np.nan_to_num(max(dev_l, dev_r) if np.isfinite(dev_l) or np.isfinite(dev_r) else 0.0),
                             np.nan_to_num(spline_dev), np.nan_to_num(residual_climb)])

            rows.append(dict(
                span_id=f"{tag}_{ci:06d}_{ri:02d}", corridor=ci, run=ri,
                i_idx=int(i), j_idx=int(j), s0_m=s0, s1_m=s1,
                s_anchor_l_m=float(ss[max(i - 1, 0)]),
                s_anchor_r_m=float(ss[min(j, n_samp - 1)]),
                lon=lon_c, lat=lat_c, span_len_m=span_len, n_samples=j - i,
                kind=("+".join(sorted(kinds)) if kinds else ""),
                source=("+".join(sorted(srcs)) if srcs else ""),
                corridor_len_m=float(ss[-1]),
                z_a=r["z_a"], z_b=r["z_b"], anchor_jump_m=abs(r["z_b"] - r["z_a"]),
                grade_pct=grade_pct,
                F1b_anchor_dev_left_m=dev_l, F1b_anchor_dev_right_m=dev_r,
                F2_anchor_at_edge=bool(r["anchor_left_edge"] or r["anchor_right_edge"]),
                F3_junctions_in_span=len(jn),
                F4_spline_dev_m=spline_dev,
                F5_too_short=bool((j - i) < 2),
                F6_fallback=bool("fallback_whole_edge" in srcs),
                F7_grade_implausible=bool(np.isfinite(grade_pct)
                                          and abs(grade_pct) > GRADE_LIMIT_PCT),
                lin_applied=bool(r["applied"]),
                dtm_dive_m=dive, dtm_rise_m=rise,
                residual_climb_m=residual_climb,
                severity_m=float(sev), near_border=near_border))

    df = pd.DataFrame(rows)
    return df, prof, corridor_cache, z_mirror


def cross_check(s04, edges_short, node_lonlat, dtm, z_mirror):
    """Selbstvalidierung: Spiegel gegen echten Produktionsaufruf."""
    z_prod = s04.assign_heights_along_corridors(
        edges_short, node_lonlat, dtm, target_epsg=TARGET_EPSG,
        sample_step_m=SAMPLE_STEP_M, smooth_rms_m=SMOOTH_RMS_M)
    common = [n for n in z_prod if n in z_mirror]
    d = np.array([abs(z_prod[n] - z_mirror[n]) for n in common
                  if np.isfinite(z_prod[n]) and np.isfinite(z_mirror[n])], float)
    return (float(d.max()) if d.size else float("nan")), len(common), d.size


# ---------------------------------------------------------------------- Plots

def _corridor_endpoint_index(cache):
    """node -> [(corridor_id, 'start'|'end')] für das Stitchen über Kreuzungen."""
    idx = {}
    for ci, c in cache.items():
        pn = c["path_nodes"]
        idx.setdefault(pn[0], []).append((ci, "start"))
        idx.setdefault(pn[-1], []).append((ci, "end"))
    return idx


def _corridor_arrays(prof, c, flip):
    """(lon, lat, zraw, zlin, zfin, ss, spans, node_s) eines Korridors, ggf. gedreht."""
    st, n = c["start"], c["n_samp"]
    lon = prof["all_lon"][st:st + n].copy()
    lat = prof["all_lat"][st:st + n].copy()
    ss, zraw, zlin, zfin = c["ss"].copy(), c["zraw"].copy(), c["zlin"].copy(), c["zfin"].copy()
    L = float(ss[-1])
    spans = [(a, b, s, k) for (a, b, s, k) in c["struct_spans"]]
    nodes = [(nid, s) for nid, s in c["node_s"].items()]
    if flip:
        lon, lat = lon[::-1], lat[::-1]
        zraw, zlin, zfin = zraw[::-1], zlin[::-1], zfin[::-1]
        ss = L - ss[::-1]
        spans = [(L - b, L - a, s, k) for (a, b, s, k) in spans]
        nodes = [(nid, L - s) for nid, s in nodes]
    return lon, lat, zraw, zlin, zfin, ss, spans, nodes, L


def _bearing(lon, lat, i0, i1):
    dx = (lon[i1] - lon[i0]) * np.cos(np.radians(0.5 * (lat[i0] + lat[i1])))
    dy = lat[i1] - lat[i0]
    return np.degrees(np.arctan2(dy, dx))


def stitch_profile(prof, cache, ci, context_m):
    """Korridor ci über Kreuzungen hinweg verlängern, bis beidseitig context_m
    Kontext vorliegt. Fortsetzung = Nachbarkorridor mit der kleinsten Richtungs-
    änderung an der Kreuzung. Gibt ein zusammenhängendes Profil zurück."""
    idx = _corridor_endpoint_index(cache)
    c0 = cache[ci]
    lon, lat, zraw, zlin, zfin, ss, spans, nodes, L = _corridor_arrays(prof, c0, False)
    jmarks = []                      # Bogenlängen der Stitch-Kreuzungen
    ends = {"left": (c0["path_nodes"][0], 0.0), "right": (c0["path_nodes"][-1], L)}
    used = {ci}

    for side in ("left", "right"):
        need = context_m
        guard = 0
        while need > 0 and guard < 40:
            guard += 1
            node, _ = ends[side]
            cands = [(cj, pos) for (cj, pos) in idx.get(node, [])
                     if cj not in used]
            if not cands:
                break
            # Richtung der bisherigen Kette an dieser Kreuzung
            if side == "left":
                b_ref = _bearing(lon, lat, min(3, len(lon) - 1), 0)
            else:
                b_ref = _bearing(lon, lat, max(0, len(lon) - 4), len(lon) - 1)
            best, best_turn = None, 1e9
            for (cj, pos) in cands:
                cj_c = cache[cj]
                flip = (pos == "end")     # Nachbar so drehen, dass er am Knoten beginnt
                l2, a2, *_ = _corridor_arrays(prof, cj_c, flip)
                b_new = _bearing(l2, a2, 0, min(3, len(l2) - 1))
                turn = abs((b_new - b_ref + 180.0) % 360.0 - 180.0)
                if turn < best_turn:
                    best, best_turn = (cj, flip), turn
            if best is None or best_turn > 60.0:
                break
            cj, flip = best
            used.add(cj)
            l2, a2, r2, n2, f2, s2, sp2, nd2, L2 = _corridor_arrays(prof, cache[cj], flip)
            if side == "left":
                # Nachbar läuft VOM Knoten weg -> umdrehen, damit er auf den Knoten zuläuft
                l2, a2 = l2[::-1], a2[::-1]
                r2, n2, f2 = r2[::-1], n2[::-1], f2[::-1]
                s2r = L2 - s2[::-1]
                off = ss[0] - L2
                jmarks.append(ss[0])
                lon = np.concatenate([l2[:-1], lon]); lat = np.concatenate([a2[:-1], lat])
                zraw = np.concatenate([r2[:-1], zraw]); zlin = np.concatenate([n2[:-1], zlin])
                zfin = np.concatenate([f2[:-1], zfin])
                ss = np.concatenate([off + s2r[:-1], ss])
                spans = [(off + (L2 - b), off + (L2 - a), s, k) for (a, b, s, k) in sp2] + spans
                nodes = [(nid, off + (L2 - s)) for nid, s in nd2] + nodes
                ends["left"] = (cache[cj]["path_nodes"][-1 if not flip else 0], ss[0])
                need -= L2
            else:
                off = ss[-1]
                jmarks.append(off)
                lon = np.concatenate([lon, l2[1:]]); lat = np.concatenate([lat, a2[1:]])
                zraw = np.concatenate([zraw, r2[1:]]); zlin = np.concatenate([zlin, n2[1:]])
                zfin = np.concatenate([zfin, f2[1:]])
                ss = np.concatenate([ss, off + s2[1:]])
                spans = spans + [(off + a, off + b, s, k) for (a, b, s, k) in sp2]
                nodes = nodes + [(nid, off + s) for nid, s in nd2]
                ends["right"] = (cache[cj]["path_nodes"][-1 if not flip else 0], ss[-1])
                need -= L2
    return ss, zraw, zlin, zfin, spans, nodes, jmarks


def plot_span(df_row, prof, corridor_cache, out_dir, context_m=PLOT_CONTEXT_M):
    """Höhenprofil um ein Bauwerk: DTM roh, linearisiert, final, Struktur schraffiert."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ci = int(df_row["corridor"])
    c = corridor_cache.get(ci)
    if c is None:
        return None
    # Kontext über Kreuzungen hinweg zusammensetzen (Korridore enden an Kreuzungen)
    ss, zraw, zlin, zfin, spans, nodes, jmarks = stitch_profile(
        prof, corridor_cache, ci, context_m)
    # exakte Grenzen DIESES Linearisierungs-Runs (Bogenlänge des Basiskorridors)
    s_lo, s_hi = float(df_row["s0_m"]), float(df_row["s1_m"])
    w0, w1 = max(ss[0], s_lo - context_m), min(ss[-1], s_hi + context_m)
    m = (ss >= w0) & (ss <= w1)
    if m.sum() < 5:
        return None

    fig, ax = plt.subplots(figsize=(11, 4.2))
    x = (ss[m] - s_lo) / 1000.0
    ax.plot(x, zraw[m], lw=0.9, color="0.55", label="DTM roh (bare earth)")
    ax.plot(x, zlin[m], lw=1.3, color="tab:orange", label="nach Bauwerks-Linearisierung")
    ax.plot(x, zfin[m], lw=1.6, color="tab:blue", label="final (nach Spline)")
    for (a, b, src, kind) in spans:
        if b < w0 or a > w1:
            continue
        ax.axvspan((a - s_lo) / 1000.0, (b - s_lo) / 1000.0, color="tab:red",
                   alpha=0.12, lw=0)
    # Ankerpunkte der Linearisierung (je EIN Sample-Punkt)
    ax.plot([(float(df_row["s_anchor_l_m"]) - s_lo) / 1000.0,
             (float(df_row["s_anchor_r_m"]) - s_lo) / 1000.0],
            [float(df_row["z_a"]), float(df_row["z_b"])], "x", ms=9, mew=2,
            color="tab:red", label="Anker der Linearisierung")
    # Stitch-Kreuzungen (dort wird direkt und ungeglättet gesampelt)
    for jm in jmarks:
        if w0 <= jm <= w1:
            ax.axvline((jm - s_lo) / 1000.0, color="0.3", ls=":", lw=0.8)
    # Knoten des Netzes im Fenster
    ns = np.array([s for _nid, s in nodes], float)
    inw = (ns >= w0) & (ns <= w1)
    if inw.any():
        ax.plot((ns[inw] - s_lo) / 1000.0, np.interp(ns[inw], ss, zfin), ".",
                ms=4, color="k", label="Netzknoten")
    ax.set_xlabel("Bogenlänge relativ zum Bauwerksbeginn (km)")
    ax.set_ylabel("Höhe (m)")
    flags = [k for k in ("F2_anchor_at_edge", "F5_too_short", "F6_fallback",
                         "F7_grade_implausible") if bool(df_row.get(k))]
    ax.set_title(f"{df_row['span_id']}  {df_row['kind']}  "
                 f"L={df_row['span_len_m']:.0f} m  "
                 f"Ankerabw. l/r {df_row['F1b_anchor_dev_left_m']:.2f}/"
                 f"{df_row['F1b_anchor_dev_right_m']:.2f} m  "
                 f"Kreuzungen im Span: {int(df_row['F3_junctions_in_span'])}"
                 + (f"  [{', '.join(flags)}]" if flags else ""), fontsize=9)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{df_row['span_id']}.png"
    fig.savefig(png, dpi=150)
    fig.savefig(out_dir / f"{df_row['span_id']}.svg")
    plt.close(fig)
    return png


# ----------------------------------------------------------------------- Main

def summarize(df):
    d = df[~df["near_border"]]
    n = len(d)
    if n == 0:
        return "keine Spans im Kerngebiet"
    L = d["span_len_m"].sum() / 1000.0
    lines = [f"Bauwerks-Runs im Kerngebiet: {n} ({L:.1f} km)",
             f"  davon Brücke/Tunnel/both: "
             f"{(d['kind']=='bridge').sum()}/{(d['kind']=='tunnel').sum()}/"
             f"{(d['kind']=='both').sum()}", ""]
    for col, txt in [("F2_anchor_at_edge", "F2 Anker am Korridorrand (Anker AUF dem Bauwerk)"),
                     ("F5_too_short", "F5 Run < 2 Sample-Punkte"),
                     ("F6_fallback", "F6 Ganz-Kanten-Fallback"),
                     ("F7_grade_implausible", f"F7 |Längsneigung| > {GRADE_LIMIT_PCT} %")]:
        k = int(d[col].sum())
        lines.append(f"  {txt}: {k} ({100.0*k/n:.1f} %)")
    k3 = int((d["F3_junctions_in_span"] > 0).sum())
    lines.append(f"  F3 Kreuzungsknoten im Bauwerk: {k3} ({100.0*k3/n:.1f} %), "
                 f"insgesamt {int(d['F3_junctions_in_span'].sum())} Knoten")
    k_na = int((~d["lin_applied"]).sum())
    lines.append(f"  Linearisierung NICHT angewendet (nicht-finite Anker): {k_na}")
    lines.append("")
    for col, txt in [("F1b_anchor_dev_left_m", "F1b Ankerabweichung links [m]"),
                     ("F1b_anchor_dev_right_m", "F1b Ankerabweichung rechts [m]"),
                     ("F4_spline_dev_m", "F4 Spline-Abweichung im Span [m]"),
                     ("residual_climb_m", "Rest-Welligkeit im Span [m]"),
                     ("dtm_dive_m", "DTM-Einbruch unter Fahrbahn [m]"),
                     ("anchor_jump_m", "Ankersprung |z_b - z_a| [m]")]:
        v = pd.to_numeric(d[col], errors="coerce").dropna()
        if len(v):
            lines.append(f"  {txt}: median {v.median():.2f}, p90 {v.quantile(0.9):.2f}, "
                         f"max {v.max():.2f}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=sorted(REGIONS), default="TH")
    ap.add_argument("--bbox", help="lon0,lat0,lon1,lat1 (überschreibt --region)")
    ap.add_argument("--max-link-length", type=float, default=MAX_LINK_LENGTH_M)
    ap.add_argument("--plot-top", type=int, default=0,
                    help="die N schlimmsten Kandidaten plotten")
    ap.add_argument("--plot-id", default=None, help="einzelne span_id plotten")
    ap.add_argument("--plot-filter", default=None,
                    help="pandas-query auf spans.csv vor der Top-N-Auswahl, "
                         "z.B. \"F3_junctions_in_span>0 and span_len_m>200\"")
    ap.add_argument("--replot", default=None,
                    help="Ordner eines frueheren Laufs: nur neu plotten (kein Audit)")
    ap.add_argument("--context-km", type=float, default=PLOT_CONTEXT_M / 1000.0)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    if args.replot:
        import pickle
        d = Path(args.replot)
        with open(d / "state.pkl", "rb") as fh:
            st = pickle.load(fh)
        df, prof, cache = st["df"], st["prof"], st["cache"]
        sel = df[~df["near_border"]]
        if args.plot_id:
            sel = df[df["span_id"] == args.plot_id]
        else:
            if args.plot_filter:
                sel = sel.query(args.plot_filter)
            sel = sel.sort_values("severity_m", ascending=False).head(max(args.plot_top, 1))
        print(f"replot: {len(sel)} Kandidaten aus {d}", flush=True)
        for _, r in sel.iterrows():
            p = plot_span(r, prof, cache, d / "plots",
                          context_m=args.context_km * 1000.0)
            if p:
                print(f"  Plot: {p}", flush=True)
        return

    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        tag = args.tag or "BBOX"
    else:
        bbox = REGIONS[args.region]
        tag = args.tag or args.region

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / f"{tag}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Region {tag} bbox={bbox}\nAusgabe: {out_dir}", flush=True)

    s04 = _import_script04()
    e_simp, e_det, n_det = load_region(bbox)
    print("  short_edges ...", flush=True)
    edges_short, node_lonlat = build_shortened_network(
        s04, e_simp, e_det, n_det, args.max_link_length)
    print(f"  {len(edges_short)} Kanten, {len(node_lonlat)} Knoten", flush=True)

    dtm = s04.load_dtm(str(DTM_PATH))
    print("  Audit-Durchlauf ...", flush=True)
    df, prof, cache, z_mirror = audit_region(s04, edges_short, node_lonlat, dtm,
                                             bbox, tag)
    print("  Selbstvalidierung gegen Produktionsaufruf ...", flush=True)
    maxdz, n_common, n_cmp = cross_check(s04, edges_short, node_lonlat, dtm, z_mirror)
    ok = np.isfinite(maxdz) and maxdz < 1e-6
    print(f"  max|dz| Spiegel vs. Produktion: {maxdz:.3e} m über {n_cmp} Knoten "
          f"-> {'OK' if ok else 'ABWEICHUNG! Audit-Logik prüfen'}", flush=True)

    df.to_csv(out_dir / "spans.csv", index=False, encoding="utf-8")
    summary = (f"Region {tag}  bbox={bbox}  max_link_length={args.max_link_length} m\n"
               f"Selbstvalidierung max|dz| = {maxdz:.3e} m ({'OK' if ok else 'FEHLER'})\n\n"
               + summarize(df))
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary, flush=True)

    core = df[~df["near_border"]].sort_values("severity_m", ascending=False)
    core.head(200).to_csv(out_dir / "top200.csv", index=False, encoding="utf-8")

    import pickle
    with open(out_dir / "state.pkl", "wb") as fh:
        pickle.dump(dict(df=df, prof=prof, cache=cache), fh,
                    protocol=pickle.HIGHEST_PROTOCOL)

    todo = []
    if args.plot_id:
        todo = [r for _, r in df[df["span_id"] == args.plot_id].iterrows()]
    elif args.plot_top > 0:
        sel = core.query(args.plot_filter) if args.plot_filter else core
        todo = [r for _, r in sel.head(args.plot_top).iterrows()]
    for r in todo:
        p = plot_span(r, prof, cache, out_dir / "plots",
                      context_m=args.context_km * 1000.0)
        if p:
            print(f"  Plot: {p}", flush=True)
    print(f"\nfertig: {out_dir}", flush=True)


def _dtm_takes_path(s04):
    import inspect
    try:
        return len(inspect.signature(s04.load_dtm).parameters) > 0
    except Exception:
        return False


if __name__ == "__main__":
    main()
