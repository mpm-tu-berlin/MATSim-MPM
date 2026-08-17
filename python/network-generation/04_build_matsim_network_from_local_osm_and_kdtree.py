# -*- coding: utf-8 -*-
"""
MATSim Edge Shortener & Network Exporter

This script builds a MATSim network from local (simplified + detailed) OSM data and a height
KDTree (lon/lat EPSG:4326), while enforcing a maximum link length constraint by replacing long
simplified edges with the corresponding directed sequence of detailed segments and recursively
splitting at the nearest detailed segment boundary around the simplified half-length.

Key features
------------
- Only simplified edges with length > `max_allowed_link_length` are replaced.
- Replacement follows the matched *directed* detailed sequence between (u, v).
- The split point is chosen by summing from both ends and taking the first detailed boundary
  that exceeds half of the simplified edge length, minimizing imbalance.
- Recursive halving continues until all parts are <= `max_allowed_link_length`.
- Robust attribute handling and geometry length fallbacks (EPSG:3857) for safety.
- Outputs a MATSim network_v2 DTD XML (gzipped) with optional node Z (height).

Dependencies
------------
- numpy, pandas, geopandas, shapely, scipy (KDTree), tqdm

Inputs (example from __main__)
-----------------------------
- data/{area}_kdtree_from_roads3d_epsg4326.npz  (npz with 'coords' [lon, lat] & 'heights')
- data/{area}_simplified.gpkg                    (layers: 'nodes', 'edges')
- data/{area}_detailed_sorted.gpkg               (layers: 'nodes', 'edges')

Author
------
Refactored and documented in English.
"""

import math
from collections import defaultdict
import gzip
import xml.etree.ElementTree as ET
import xml.dom.minidom as md

import os
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from tqdm import tqdm
import rasterio
from pyproj import Transformer

# --------------------------- Hoehen aus LiDAR-DTM ---------------------------
# Frueher kamen die Knotenhoehen aus einer npz-Punktwolke per KD-Tree-Nearest-
# Neighbor im GRAD-Raum (lon/lat). Das ist anisotrop (1 deg lon != 1 deg lat) und
# erzeugte ~2 m Hoehenrauschen mit vereinzelten Mehrmeter-Ausreissern an Bruecken/
# Parallelfahrbahnen, zusaetzlich float32-Quantisierung der Koordinaten.
# Ersetzt durch direktes, CRS-korrektes bilineares Sampling des LiDAR-DTM an der
# Knotenposition. Damit haengt die Pipeline nur noch an OSM (Topologie) + DTM (Hoehe).

DTM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "DTM Germany 20m v3b by Sonny.tif")
_DTM_QUERY_CRS = "EPSG:4326"        # Knotenkoordinaten beim Hoehenabruf (lon/lat)
_DTM_MAX_BLOCK_PX = 150_000_000     # max. Fenster fuer Block-Sampling (~600 MB), sonst knotenweise

# OSM 'none' (unlimitierte Autobahn) -> Auslegungs-/Richtgeschwindigkeit. Der LKW wird
# im Sim ohnehin durch seine eigene maximumVelocity begrenzt; entscheidend ist nur, dass
# diese Links NICHT faelschlich auf 50 km/h Default fallen.
NONE_MAXSPEED_KMH = 130.0


def load_dtm(dtm_path: str = DTM_PATH) -> str:
    """Prueft die DTM-Datei und gibt einen leichtgewichtigen Handle (den Pfad) zurueck.
    Das Raster wird in sample_heights() PRO AUFRUF geoeffnet -> thread-sicher fuer die
    parallele Varianten-Generierung."""
    if not os.path.exists(dtm_path):
        raise FileNotFoundError(f"LiDAR-DTM nicht gefunden: {dtm_path}")
    return dtm_path


def sample_heights(dtm_path: str, lons, lats) -> np.ndarray:
    """Bilineare DTM-Hoehe an (lon, lat) in EPSG:4326. NaN ausserhalb/nodata.

    Oeffnet das Raster selbst (thread-sicher), transformiert ins DTM-CRS und
    interpoliert bilinear. Regionale Netze: umschliessendes Fenster einmal laden
    (vektorisiert, schnell). Sehr grosse Bounding-Box: knotenweise 2x2-Fenster."""
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    out = np.full(lons.shape, np.nan, dtype=float)

    with rasterio.open(dtm_path) as ds:
        transformer = Transformer.from_crs(_DTM_QUERY_CRS, ds.crs, always_xy=True)
        xs, ys = transformer.transform(lons, lats)
        cols, rows = (~ds.transform) * (np.asarray(xs, float), np.asarray(ys, float))
        # HALBPIXEL-KORREKTUR (Befund 2026-08-17): (~transform) zaehlt Pixel ab der
        # ECKE, das Zentrum von Pixel 0 liegt bei 0,5. Die bilineare Interpolation
        # unten mischt aber PIXELWERTE, die am Zentrum gemessen sind (das Raster
        # traegt zudem AREA_OR_POINT=Point). Ohne diese Verschiebung wird die
        # Hoehe systematisch ein halbes Pixel (10 m bei 20 m Raster) daneben
        # abgetastet. Empirisch gegen 115.754 km Telemetrie bestaetigt: das
        # Fehleroptimum lag bei dx -10 m / dy +5..10 m, genau der Halbpixel-Versatz;
        # MAE 2,841 -> 2,399 m (-15,6 %), Median 1,361 -> 1,048 m (-23 %).
        cols = cols - 0.5
        rows = rows - 0.5
        cols = np.where(np.isfinite(cols), cols, -1e9)
        rows = np.where(np.isfinite(rows), rows, -1e9)
        c0 = np.floor(cols).astype("int64")
        r0 = np.floor(rows).astype("int64")
        inb = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < ds.width) & (r0 + 1 < ds.height)
        if not inb.any():
            return out

        nodata = ds.nodata
        ci, ri = c0[inb], r0[inb]
        fx, fy = cols[inb] - ci, rows[inb] - ri
        cmin, cmax = int(ci.min()), int(ci.max())
        rmin, rmax = int(ri.min()), int(ri.max())
        win_w, win_h = cmax - cmin + 2, rmax - rmin + 2

        if win_w * win_h <= _DTM_MAX_BLOCK_PX:
            block = ds.read(1, window=rasterio.windows.Window(cmin, rmin, win_w, win_h)).astype(float)
            if nodata is not None:
                block = np.where(block == nodata, np.nan, block)
            rr, cc = ri - rmin, ci - cmin
            top = block[rr, cc] * (1 - fx) + block[rr, cc + 1] * fx
            bot = block[rr + 1, cc] * (1 - fx) + block[rr + 1, cc + 1] * fx
            vals = top * (1 - fy) + bot * fy
        else:
            # Sehr grosse Bounding-Box (z.B. deutschlandweites Feinnetz): in Kacheln
            # <= _DTM_MAX_BLOCK_PX aufteilen, pro belegter Kachel EIN Fenster lesen und
            # vektorisiert bilinear interpolieren (memory-bounded, schnell; nur Kacheln
            # mit Knoten werden gelesen -> bei linienhaften Strassennetzen wenig Daten).
            vals = np.full(ci.shape, np.nan, dtype=float)
            tile = int(max(256, math.floor(math.sqrt(_DTM_MAX_BLOCK_PX))))  # Kachelkante [px]
            n_tcol = ((cmax - cmin) // tile) + 1
            tile_id = ((ri - rmin) // tile) * n_tcol + ((ci - cmin) // tile)
            for tid in np.unique(tile_id):
                m = tile_id == tid
                cm0, cm1 = int(ci[m].min()), int(ci[m].max())
                rm0, rm1 = int(ri[m].min()), int(ri[m].max())
                w, h = cm1 - cm0 + 2, rm1 - rm0 + 2
                blk = ds.read(1, window=rasterio.windows.Window(cm0, rm0, w, h)).astype(float)
                if nodata is not None:
                    blk = np.where(blk == nodata, np.nan, blk)
                rr, cc, fxm, fym = ri[m] - rm0, ci[m] - cm0, fx[m], fy[m]
                top = blk[rr, cc] * (1 - fxm) + blk[rr, cc + 1] * fxm
                bot = blk[rr + 1, cc] * (1 - fxm) + blk[rr + 1, cc + 1] * fxm
                vals[m] = top * (1 - fym) + bot * fym

        out[inb] = vals
    return out


def _is_structure(row):
    """True, wenn die Kante als Bruecke/Tunnel/Viadukt getaggt ist (OSM bridge/tunnel).
    Auf solchen Spans gibt das DTM (Bare-Earth) Talboden/Berg statt der Fahrbahn ->
    dort wird die Hoehe linear zwischen den An-Grade-Enden interpoliert.

    ACHTUNG (Befund 2026-07-05): osmnx aggregiert Tags beim Simplifizieren mit
    any-Semantik — eine 44-km-Kette mit EINEM Brueckenstueck bekommt bridge='yes'.
    Ganz-Kanten-Flagging linearisiert dann kilometerweise echtes Terrain (q97/A72:
    81 % der Korridorlaenge). Deshalb gilt dieses Flag nur noch als FALLBACK; wo
    eine Detail-Sequenz existiert, kommen praezise Intervalle aus _struct_fractions
    (Spalte '_struct_ivals')."""
    for key in ("bridge", "tunnel"):
        if key in row.index:
            val = row[key]
            # osmnx aggregiert abweichende Tags zu Listen (z.B. [nan, 'yes'] bei
            # teilweiser Bruecke) -> ALLE Elemente pruefen, nicht nur das erste.
            vals = val if isinstance(val, (list, tuple)) else [val]
            for v in vals:
                if str(v).strip().lower() in ("yes", "true", "1", "viaduct", "bridge", "tunnel"):
                    return True
    return False


def _struct_fractions(seq_det):
    """Bauwerks-Intervalle als Anteile [0..1] der Kantenlaenge, aus den DETAIL-
    Segmenten (dort sind bridge/tunnel in Original-OSM-Granularitaet getaggt).
    Rueckgabe: Liste von (f0, f1) — leere Liste = sicher KEIN Bauwerk;
    None = nicht bestimmbar (Aufrufer faellt auf das Ganz-Kanten-Tag zurueck)."""
    lens = pd.to_numeric(seq_det['length'], errors='coerce').fillna(0.0).to_numpy(dtype=float)
    total = float(lens.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    cum = np.concatenate([[0.0], np.cumsum(lens)]) / total
    ivals = []
    for i, (_, s) in enumerate(seq_det.iterrows()):
        if _is_structure(s):
            if ivals and abs(ivals[-1][1] - cum[i]) < 1e-12:
                ivals[-1] = (ivals[-1][0], float(cum[i + 1]))
            else:
                ivals.append((float(cum[i]), float(cum[i + 1])))
    return ivals


def _edge_struct_ivals(row):
    """'_struct_ivals' einer Kante lesen (Liste von Anteils-Intervallen) oder None."""
    iv = row.get('_struct_ivals') if hasattr(row, 'get') else None
    return iv if isinstance(iv, list) else None


# --- Korridoruebergreifende Bauwerksbehandlung -------------------------------
# Befund 2026-08-17 (Telemetrie-Ground-Truth, 115.754 km): Die alte, rein
# korridorlokale Linearisierung scheitert dort, wo das Bauwerk an einer Kreuzung
# liegt. Bruecken sitzen typischerweise an Anschlussstellen; der Korridor
# zwischen zwei Rampenknoten ist dann median nur 165 m lang und endet GENAU am
# Bauwerk (59 % der Faelle: Span beginnt bei s=0, 58 %: endet am Korridorende).
# Damit greift `z_a = zdense[i-1]` nicht mehr, der Fallback nimmt einen Punkt
# AUF dem Bauwerk, und die Linearisierung wird zum Nulloperator (gemessen:
# Spline-Abweichung 0,00 m an den schlimmsten Stellen, Netz median 6,3 m zu tief,
# 91 % der Durchfahrten zu tief; Tunnel spiegelbildlich bis +38 m zu hoch).
# Gilt fuer Bruecken UND Tunnel gleichermassen.

STRUCT_GUARD_M = 25.0     # Zone neben dem Bauwerk, die als kontaminiert gilt
                          # (Widerlager, Damm, Voreinschnitt) -> nicht anfitten
STRUCT_FIT_M = 60.0       # Laenge der Zufahrt, ueber die der Anker robust
                          # geschaetzt wird (Median-Steigung, extrapoliert)
STRUCT_MIN_FIT_PTS = 4
# Schutzmechanismen (Diagnose 2026-08-17): ohne sie erzeugt die Extrapolation auf
# Kleinstbauwerken Unsinn (4,9-m-Region mit -142 % Deckenneigung) und blaeht den
# Anstiegsueberschuss auf.
STRUCT_MIN_LEN_M = 20.0        # darunter: lokal zwischen den Nachbarpunkten
                               # interpolieren statt zwei Anker zu extrapolieren
STRUCT_MAX_LIFT_M = 3.0        # max. Abweichung des extrapolierten Ankers vom
                               # naechsten echten Nicht-Bauwerkspunkt
STRUCT_MAX_GRADE_PCT = 8.0     # darueber gilt die Deckenlinie als unplausibel


def _theil_sen(s, z):
    """Robuste Ausgleichsgerade (Median der Paarsteigungen). Rueckgabe (m, b)."""
    s = np.asarray(s, float); z = np.asarray(z, float)
    ok = np.isfinite(s) & np.isfinite(z)
    s, z = s[ok], z[ok]
    n = s.size
    if n < 2:
        return (0.0, float(z[0])) if n else (0.0, float('nan'))
    if n > 60:                      # Rechenzeit deckeln, Genauigkeit reicht
        sel = np.linspace(0, n - 1, 60).astype(int)
        s, z = s[sel], z[sel]
        n = s.size
    i, j = np.triu_indices(n, k=1)
    ds = s[j] - s[i]
    good = np.abs(ds) > 1e-9
    if not good.any():
        return 0.0, float(np.median(z))
    slope = float(np.median((z[j] - z[i])[good] / ds[good]))
    return slope, float(np.median(z - slope * s))


class _CorridorIndex:
    """Nachbarschaft der Korridore an ihren Endknoten (fuer Spruenge ueber
    Kreuzungen hinweg)."""

    def __init__(self, slices, all_lon, all_lat):
        self.slices = slices
        self.lon = np.asarray(all_lon, float)
        self.lat = np.asarray(all_lat, float)
        self.at_node = {}
        for ci, sl in enumerate(slices):
            path_nodes = sl[0]
            self.at_node.setdefault(path_nodes[0], []).append((ci, 0))
            self.at_node.setdefault(path_nodes[-1], []).append((ci, 1))

    def n(self, ci):
        return self.slices[ci][3]

    def z_index(self, ci, k):
        """globaler Index des k-ten Samples von Korridor ci."""
        return self.slices[ci][2] + k

    def bearing(self, ci, k0, k1):
        i0, i1 = self.z_index(ci, k0), self.z_index(ci, k1)
        dx = (self.lon[i1] - self.lon[i0]) * math.cos(
            math.radians(0.5 * (self.lat[i0] + self.lat[i1])))
        dy = self.lat[i1] - self.lat[i0]
        return math.degrees(math.atan2(dy, dx))

    def neighbours(self, ci, side):
        """(cj, side_j) aller anderen Korridore am Endknoten `side` von ci."""
        node = self.slices[ci][0][0 if side == 0 else -1]
        return [(cj, sj) for (cj, sj) in self.at_node.get(node, [])
                if not (cj == ci and sj == side)]


def _structure_decks(slices, all_lon, all_lat, zall, junctions,
                     guard_m=STRUCT_GUARD_M, fit_m=STRUCT_FIT_M,
                     debug_rows=None):
    """Bauwerke ueber Korridorgrenzen hinweg aufloesen und Deckenlinien setzen.

    Rueckgabe:
      forced   Liste je Korridor: (bool-Maske, z-Array) mit den Deckenhoehen
      jz       dict Knoten-ID -> Hoehe fuer Kreuzungsknoten IM Bauwerk
      stats    dict mit Zaehlern fuer die Log-Ausgabe
    """
    idx = _CorridorIndex(slices, all_lon, all_lat)
    nC = len(slices)
    is_st, runs = [], []
    for ci, (path_nodes, node_s, start, n_samp, ss, spans) in enumerate(slices):
        m = np.zeros(n_samp, bool)
        for (a, b) in [(sp[0], sp[1]) for sp in spans]:
            m[(ss >= a) & (ss <= b)] = True
        is_st.append(m)
        rr, k = [], 0
        while k < n_samp:
            if m[k]:
                j = k
                while j < n_samp and m[j]:
                    j += 1
                rr.append((k, j))
                k = j
            else:
                k += 1
        runs.append(rr)

    forced = [(np.zeros(idx.n(ci), bool), np.full(idx.n(ci), np.nan))
              for ci in range(nC)]
    jz = {}
    stats = dict(regions=0, multi_corridor=0, anchors_crossed=0,
                 anchor_failed=0, junction_nodes=0, samples=0,
                 short_local=0, lift_capped=0, grade_rejected=0,
                 local_fallback=0)

    def struct_at_end(ci, side):
        """Run, der am Ende `side` von Korridor ci anliegt (oder None)."""
        n = idx.n(ci)
        for (a, b) in runs[ci]:
            if side == 0 and a == 0:
                return (a, b)
            if side == 1 and b == n:
                return (a, b)
        return None

    last_turn = [float('nan')]      # Richtungsaenderung der letzten Auswahl

    def pick_continuation(ci, side, want_struct=None):
        """Nachbarkorridor mit der kleinsten Richtungsaenderung. want_struct=True
        verlangt, dass sein angrenzender Abschnitt Bauwerk ist, False das
        Gegenteil, None laesst beides zu (fuer den Anker-Walk)."""
        last_turn[0] = float('nan')
        n = idx.n(ci)
        if n < 2:
            return None
        b_ref = (idx.bearing(ci, min(3, n - 1), 0) if side == 0
                 else idx.bearing(ci, max(0, n - 4), n - 1))
        best, best_turn = None, 1e9
        for (cj, sj) in idx.neighbours(ci, side):
            nj = idx.n(cj)
            if nj < 2:
                continue
            if want_struct is not None:
                has = struct_at_end(cj, sj) is not None
                if has != want_struct:
                    continue
            b_new = (idx.bearing(cj, 0, min(3, nj - 1)) if sj == 0
                     else idx.bearing(cj, nj - 1, max(0, nj - 4)))
            turn = abs((b_new - b_ref + 180.0) % 360.0 - 180.0)
            if turn < best_turn:
                best, best_turn = (cj, sj), turn
        if best is not None and best_turn <= 70.0:
            last_turn[0] = best_turn
            return best
        return None

    def walk_outward(ci, side, start_k, max_dist, max_hops=4):
        """Laeuft vom Bauwerksrand nach aussen und liefert (Abstand, Hoehe,
        ist_Bauwerk, Kreuzungen_ueberschritten). Laeuft ueber Kreuzungen hinweg
        UND ueber zwischenliegende Bauwerksabschnitte (die werden nur als
        ist_Bauwerk=True markiert, nicht abgebrochen). Genau das fehlte:
        bei Bauwerken direkt an der Kreuzung gibt es im eigenen Korridor gar
        keinen bauwerksfreien Punkt."""
        cur_c, cur_side, cur_k = ci, side, start_k
        base, hops = 0.0, 0
        while True:
            ss = slices[cur_c][4]
            s_ref = ss[cur_k]
            ks = (range(cur_k - 1, -1, -1) if cur_side == 0
                  else range(cur_k + 1, idx.n(cur_c)))
            last_d = base
            for k in ks:
                d = base + abs(ss[k] - s_ref)
                if d > max_dist:
                    return
                last_d = d
                yield d, float(zall[idx.z_index(cur_c, k)]), bool(is_st[cur_c][k]), hops
            hops += 1
            if hops > max_hops:
                return
            nb = pick_continuation(cur_c, cur_side, want_struct=None)
            if nb is None:
                return
            cj, sj = nb
            base = last_d
            cur_c = cj
            # den Nachbarkorridor VOM gemeinsamen Knoten weg durchlaufen
            cur_side = 1 if sj == 0 else 0
            cur_k = 0 if sj == 0 else idx.n(cj) - 1

    def collect_outward(ci, side, start_k):
        """Zufahrt fuer den Ankerfit: bauwerksfreie Punkte zwischen guard_m und
        guard_m + fit_m Abstand vom Bauwerksrand, ueber Kreuzungen hinweg."""
        out_d, out_z, crossed = [], [], False
        for d, z, on, hops in walk_outward(ci, side, start_k,
                                           guard_m + fit_m + 200.0):
            if on:
                continue
            if d < guard_m:
                continue
            if d > guard_m + fit_m and len(out_d) >= STRUCT_MIN_FIT_PTS:
                break
            if d > guard_m + fit_m + 200.0:
                break
            out_d.append(d); out_z.append(z)
            if hops > 0:
                crossed = True
        return np.asarray(out_d, float), np.asarray(out_z, float), crossed

    def nearest_free(ci, side, k_edge):
        """Naechster bauwerksfreier Punkt nach aussen, notfalls ueber Kreuzungen
        hinweg (NaN nur, wenn im Umkreis von 400 m keiner existiert)."""
        for d, z, on, hops in walk_outward(ci, side, k_edge, 400.0):
            if (not on) and np.isfinite(z):
                return float(z)
        return float('nan')

    def anchor(ci, side, k_edge, allow_fit=True):
        """Ankerhoehe am Bauwerksrand: robuste Gerade der Zufahrt, auf den Rand
        extrapoliert. Die Extrapolation wird verworfen, wenn sie mehr als
        STRUCT_MAX_LIFT_M vom naechsten echten Nicht-Bauwerkspunkt abweicht
        (Schutz gegen instabile Extrapolation ueber die Schutzzone hinweg).
        Rueckgabe: (z, ok, info-dict fuer die Diagnose)."""
        z_near = nearest_free(ci, side, k_edge)
        if allow_fit:
            d, z, crossed = collect_outward(ci, side, k_edge)
            ok = np.isfinite(z)
            if ok.sum() >= STRUCT_MIN_FIT_PTS:
                m, b = _theil_sen(d[ok], z[ok])
                if (not np.isfinite(z_near)) or abs(b - z_near) <= STRUCT_MAX_LIFT_M:
                    if crossed:
                        stats['anchors_crossed'] += 1
                    return float(b), True, dict(mode='fit', n=int(ok.sum()),
                                                crossed=bool(crossed),
                                                slope_pct=100.0 * m)
                stats['lift_capped'] += 1
        if np.isfinite(z_near):
            return float(z_near), True, dict(mode='nearest', n=0,
                                             crossed=False,
                                             slope_pct=float('nan'))
        stats['anchor_failed'] += 1
        v = zall[idx.z_index(ci, k_edge)]
        return ((float(v) if np.isfinite(v) else float('nan')), False,
                dict(mode='on_structure', n=0, crossed=False,
                     slope_pct=float('nan')))

    visited = set()
    for ci in range(nC):
        for ri, (a, b) in enumerate(runs[ci]):
            if (ci, ri) in visited:
                continue
            # --- Region ueber Korridorgrenzen hinweg aufsammeln ---
            chain = [(ci, a, b)]
            turns = []
            visited.add((ci, ri))
            # nach links
            cur_c, cur_a = ci, a
            while cur_a == 0:
                nb = pick_continuation(cur_c, 0, want_struct=True)
                if nb is None:
                    break
                turns.append(last_turn[0])
                cj, sj = nb
                run_j = struct_at_end(cj, sj)
                if run_j is None:
                    break
                rj = runs[cj].index(run_j)
                if (cj, rj) in visited:
                    break
                visited.add((cj, rj))
                chain.insert(0, (cj, run_j[0], run_j[1]))
                cur_c, cur_a = cj, (run_j[0] if sj == 0 else 0)
                if sj == 1 and run_j[1] != idx.n(cj):
                    break
                if sj == 0 and run_j[0] != 0:
                    break
            # nach rechts
            cur_c, cur_b = ci, b
            while cur_b == idx.n(cur_c):
                nb = pick_continuation(cur_c, 1, want_struct=True)
                if nb is None:
                    break
                turns.append(last_turn[0])
                cj, sj = nb
                run_j = struct_at_end(cj, sj)
                if run_j is None:
                    break
                rj = runs[cj].index(run_j)
                if (cj, rj) in visited:
                    break
                visited.add((cj, rj))
                chain.append((cj, run_j[0], run_j[1]))
                cur_c = cj
                cur_b = idx.n(cj) if (sj == 0 and run_j[1] == idx.n(cj)) else -1
                if cur_b == -1:
                    break

            stats['regions'] += 1
            if len(chain) > 1:
                stats['multi_corridor'] += 1

            # --- Ausdehnung der Region (bestimmt, wie geankert wird) ---
            seg_len = []
            for (cc, aa, bb) in chain:
                sc = slices[cc][4]
                seg_len.append(float(sc[bb - 1] - sc[aa]) if bb - aa > 1 else 0.0)
            total = float(sum(seg_len))

            # --- Anker an den beiden Aussenenden ---
            c0, a0, _b0 = chain[0]
            cN, _aN, bN = chain[-1]
            # Kleinstbauwerke: keine Extrapolation ueber die Schutzzone hinweg,
            # sondern lokal zwischen den Nachbarpunkten interpolieren. Bei 5-20 m
            # Spannweite ist der Bare-Earth-Einbruch ohnehin vernachlaessigbar,
            # zwei unabhaengige Extrapolationen erzeugen dagegen Stufen.
            short = total < STRUCT_MIN_LEN_M
            if short:
                stats['short_local'] += 1
            z_a, ok_a, info_a = anchor(c0, 0, a0, allow_fit=not short)
            z_b, ok_b, info_b = anchor(cN, 1, bN - 1, allow_fit=not short)
            # Plausibilitaet der Deckenlinie; sonst auf die Nachbarpunkte zurueck
            def _grade(za, zb):
                return (abs(100.0 * (zb - za) / total) if total > 0
                        else float('inf'))
            if (total > 0 and np.isfinite(z_a) and np.isfinite(z_b)
                    and _grade(z_a, z_b) > STRUCT_MAX_GRADE_PCT):
                za2, ok2, ia2 = anchor(c0, 0, a0, allow_fit=False)
                zb2, ok3, ib2 = anchor(cN, 1, bN - 1, allow_fit=False)
                if (np.isfinite(za2) and np.isfinite(zb2)
                        and _grade(za2, zb2) < _grade(z_a, z_b)):
                    stats['grade_rejected'] += 1
                    z_a, ok_a, info_a = za2, ok2, ia2
                    z_b, ok_b, info_b = zb2, ok3, ib2

            # Letzter Rueckfall: bleibt die Deckenlinie unplausibel, wird die
            # Region NICHT global behandelt, sondern wie frueher lokal zwischen
            # den unmittelbaren Nachbarpunkten interpoliert. Ohne das ueberlebten
            # Faelle wie 7.09/51.23 (39,9 m Spannweite, -36,6 % Deckenneigung).
            if (not (np.isfinite(z_a) and np.isfinite(z_b))
                    or _grade(z_a, z_b) > STRUCT_MAX_GRADE_PCT):
                stats['local_fallback'] += 1
                for (cc, aa, bb) in chain:
                    n_cc = idx.n(cc)
                    raw = zall[slices[cc][2]:slices[cc][2] + n_cc]
                    la = raw[aa - 1] if (aa > 0 and np.isfinite(raw[aa - 1])) else raw[aa]
                    lb = raw[bb] if (bb < n_cc and np.isfinite(raw[bb])) else raw[bb - 1]
                    if not (np.isfinite(la) and np.isfinite(lb)):
                        continue
                    forced[cc][0][aa:bb] = True
                    forced[cc][1][aa:bb] = np.linspace(float(la), float(lb), bb - aa)
                continue
            if debug_rows is not None:
                ss0, ssN = slices[c0][4], slices[cN][4]
                L_reg = total
                raw_a = float(zall[idx.z_index(c0, a0)])
                raw_b = float(zall[idx.z_index(cN, bN - 1)])
                debug_rows.append(dict(
                    parts=len(chain), corridors=[c[0] for c in chain],
                    len_m=L_reg, max_turn_deg=(max(turns) if turns else 0.0),
                    z_a=z_a, z_b=z_b, ok_a=ok_a, ok_b=ok_b,
                    mode_a=info_a['mode'], mode_b=info_b['mode'],
                    nfit_a=info_a['n'], nfit_b=info_b['n'],
                    crossed_a=info_a['crossed'], crossed_b=info_b['crossed'],
                    lift_a=z_a - raw_a, lift_b=z_b - raw_b,
                    deck_grade_pct=(100.0 * (z_b - z_a) / L_reg
                                    if L_reg > 0 else float('nan')),
                    lon=float(all_lon[idx.z_index(c0, a0)]),
                    lat=float(all_lat[idx.z_index(c0, a0)])))
            if not (np.isfinite(z_a) and np.isfinite(z_b)):
                continue

            # --- Deckenlinie linear ueber die GESAMTE Region ---
            run_off = 0.0
            for (cc, aa, bb), L in zip(chain, seg_len):
                ss = slices[cc][4]
                if bb - aa > 1 and total > 0:
                    t = (run_off + (ss[aa:bb] - ss[aa])) / total
                elif total > 0:
                    t = np.array([run_off / total])
                else:
                    t = np.zeros(bb - aa)
                zline = z_a + (z_b - z_a) * np.clip(t, 0.0, 1.0)
                forced[cc][0][aa:bb] = True
                forced[cc][1][aa:bb] = zline
                stats['samples'] += bb - aa
                # Kreuzungsknoten IM Bauwerk aus der Deckenlinie bedienen
                path_nodes, node_s = slices[cc][0], slices[cc][1]
                s_lo, s_hi = ss[aa], ss[bb - 1]
                for nd in path_nodes:
                    if nd in junctions and s_lo <= node_s.get(nd, -1e18) <= s_hi:
                        jz[nd] = float(np.interp(node_s[nd], ss[aa:bb], zline))
                        stats['junction_nodes'] += 1
                run_off += L
    return forced, jz, stats


def assign_heights_along_corridors(gdf_edges, node_lonlat, dtm_path,
                                   target_epsg=4839, sample_step_m=5.0,
                                   smooth_rms_m=1.0, debug=False,
                                   collect_dense=False,
                                   global_structures=False,
                                   struct_debug=None):
    """Aufloesungsunabhaengige, sanfte Hoehenzuweisung ueber ein Master-Profil.

    Statt das DTM nur an den (Post-Split-)Knoten zu sampeln, wird pro Korridor
    (Kette von Grad-2-Knoten zwischen Kreuzungen) das DTM DICHT entlang der
    Kantengeometrie gesampelt (~sample_step_m; Default 5 m = 4x Oversampling der
    20-m-DTM -> Nyquist-sicher gegen Aliasing der bilinearen Rasterflaeche, ohne
    echte Info < 20 m vorzutaeuschen), das Profil EINMAL sanft geglaettet
    (Spline, Ziel-RMS = smooth_rms_m) und jede Knotenhoehe am zugehoerigen
    Bogenlaengen-Punkt gelesen. Damit sind die Hoehen ueber alle Linklaengen
    konsistent (kein Auflösungs-Confound), Bare-Earth-Artefakte (Damm/Bruecke)
    werden ueber viele dichte Punkte geglaettet, ohne echtes Terrain zu loeschen.
    Kreuzungsknoten (Grad != 2) werden direkt (ungeglaettet) gesampelt.
    smooth_rms_m <= 0 -> keine Glaettung (nur dichtes Profil, ~Punktwert am Knoten).

    Es faellt genau EIN DTM-Zugriff an (alle Punkte gebuendelt). Ersetzt Skript 05
    fuer die DTM-Pipeline. Rueckgabe: dict node_id(str) -> Hoehe [m] (NaN wo DTM fehlt).

    global_structures: NOCH NICHT DEFAULT (Stand 2026-08-17). Der Ansatz senkt den
    gemessenen Bauwerksfehler in der Pfalz-Testregion deutlich (max|dev| p50
    10,05 -> 5,41 m), liefert in Koeln-Sued und im Ruhrgebiet aber nur beim Median
    einen Gewinn und verschlechtert dort einzelne Faelle (Ruhr paarweise: 30
    Durchfahrten besser, 37 schlechter, netto +31,8 m; die Verschlechterung haengt
    fast vollstaendig an EINER Stelle, 7.09/51.23). Bis das geklaert ist, bleibt
    der Default auf False, damit sich die Produktion nicht unbemerkt aendert.
    Auf True gesetzt gilt: Bruecken UND Tunnel werden
    korridoruebergreifend aufgeloest. Anker der Deckenlinie kommen aus der
    Zufahrt jenseits der Kreuzung (robuste Gerade ueber STRUCT_FIT_M, mit
    STRUCT_GUARD_M Schutzzone gegen Widerlager/Damm/Voreinschnitt), das Bauwerk
    wird von der Spline-Glaettung ausgenommen, und Kreuzungsknoten IM Bauwerk
    bekommen die Deckenhoehe statt eines Bare-Earth-Direktsamples.
    global_structures=False reproduziert exakt das alte, korridorlokale
    Verhalten (fuer A/B-Vergleiche gegen die Telemetrie-Referenz).

    collect_dense=True: zusaetzlich wird das DICHTE, geglaettete Fahrbahnprofil
    (alle Sample-Punkte, nicht nur Knoten) als Nx3-Array [lon, lat, z] geliefert
    -> Rueckgabe (z, dense) bzw. (z, dbg, dense). Ideal fuer eine reine
    Koordinate->Hoehe-Lookup-Tabelle (Autobahn/Bundesstrasse), unabhaengig von der
    MATSim-Netztopologie/Linklaenge.
    """
    from collections import defaultdict
    import warnings as _warn
    try:
        from scipy.interpolate import UnivariateSpline
    except Exception:
        UnivariateSpline = None

    # --- Topologie ---
    adj = defaultdict(list)      # node -> [(edge_key, other_node)]
    egeom = {}                   # edge_key -> (LineString, u, v)
    estruct = {}                 # edge_key -> bool (Bruecke/Tunnel, Ganz-Kanten-Tag)
    eivals = {}                  # edge_key -> Anteils-Intervalle aus Detail-Segmenten
    for k, row in gdf_edges.iterrows():
        u, v = str(row['u']), str(row['v'])
        g = row.geometry
        if g is None or u == v:
            continue
        egeom[k] = (g, u, v)
        estruct[k] = _is_structure(row)
        eivals[k] = _edge_struct_ivals(row)
        adj[u].append((k, v))
        adj[v].append((k, u))

    deg = {n: len(lst) for n, lst in adj.items()}
    junctions = {n for n, d in deg.items() if d != 2}
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True)

    # --- Korridore (Kanten in Pfadreihenfolge) aufbauen ---
    visited = set()
    corridors = []

    def walk(start, first_edge, first_other):
        path_nodes = [start, first_other]
        path_edges = [first_edge]
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
            if o == start:   # Schleife geschlossen
                break
        return path_nodes, path_edges

    for j in junctions:
        for (ek, o) in adj[j]:
            if ek not in visited:
                corridors.append(walk(j, ek, o))
    for k in list(egeom.keys()):       # reine Schleifen ohne Kreuzung
        if k not in visited:
            g, u, v = egeom[k]
            corridors.append(walk(u, k, v))

    # --- alle Sample-Punkte buendeln (dicht je Korridor + direkte Knoten) ---
    all_lon, all_lat = [], []
    slices = []          # (path_nodes, node_s, start, n_samp, ss)
    covered = set()
    direct_nodes = []

    for path_nodes, path_edges in corridors:
        try:
            fx, fy = [], []
            node_s = {path_nodes[0]: 0.0}
            struct_spans = []            # (s0, s1) Bauwerks-Spans in Korridor-Bogenlaenge
            cum = 0.0
            for i, ek in enumerate(path_edges):
                g, eu, ev = egeom[ek]
                lon, lat = zip(*list(g.coords))
                mx, my = tf.transform(np.asarray(lon, float), np.asarray(lat, float))
                mx = np.asarray(mx, float); my = np.asarray(my, float)
                rev = (eu != path_nodes[i])
                if rev:                           # Geometrie an path_nodes[i] ausrichten
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
                    # praezise Detail-Intervalle (Anteile -> Korridor-Bogenlaenge)
                    for (f0, f1) in iv:
                        a, b = ((1.0 - f1, 1.0 - f0) if rev else (f0, f1))
                        struct_spans.append((s0e + a * Le, s0e + b * Le))
                elif estruct.get(ek, False):
                    # Fallback ohne Detail-Info: ganze Kante linearisieren
                    # (User-Entscheidung 2026-07-05: besser als Bare-Earth-Artefakte
                    # an echten Tunneln/Talbruecken)
                    struct_spans.append((s0e, cum))
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

    for n in junctions:                # Kreuzungen immer direkt (konsistent ueber Korridore)
        direct_nodes.append(n)
    for n in adj:                      # nicht abgedeckte Knoten
        if n not in covered and n not in junctions:
            direct_nodes.append(n)
    direct_nodes = list(dict.fromkeys(direct_nodes))
    direct_start = len(all_lon)
    for n in direct_nodes:
        lo, la = node_lonlat[n]
        all_lon.append(lo); all_lat.append(la)

    # --- EIN DTM-Zugriff fuer alle Punkte ---
    zall = sample_heights(dtm_path, np.asarray(all_lon), np.asarray(all_lat))

    # --- Bauwerke korridoruebergreifend aufloesen (Bruecken UND Tunnel) ---
    forced, jz = None, {}
    if global_structures:
        forced, jz, _st = _structure_decks(slices, all_lon, all_lat, zall,
                                           junctions, debug_rows=struct_debug)
        if _st['regions']:
            print(f"  Bauwerke: {_st['regions']} Regionen "
                  f"({_st['multi_corridor']} korridoruebergreifend), "
                  f"{_st['anchors_crossed']} Anker jenseits einer Kreuzung, "
                  f"{_st['junction_nodes']} Kreuzungsknoten im Bauwerk korrigiert; "
                  f"{_st['short_local']} kurz (lokal), {_st['lift_capped']} Anker "
                  f"verworfen (Hub), {_st['grade_rejected']} Deckenlinie verworfen "
                  f"(Neigung)"
                  + (f", {_st['anchor_failed']} ohne Anker" if _st['anchor_failed'] else ""))

    z = {}
    dbg = {}
    dense_rows = []      # Nx3 [lon, lat, z] fuer collect_dense
    for j, n in enumerate(direct_nodes):
        z[n] = float(zall[direct_start + j])
        if n in jz and np.isfinite(jz[n]):
            z[n] = float(jz[n])          # Kreuzung IM Bauwerk -> Deckenhoehe
        if debug:
            dbg[n] = (float("nan"), float("nan"), False)
        if collect_dense:
            lo, la = node_lonlat[n]
            dense_rows.append((float(lo), float(la), float(z[n])))

    for ci, (path_nodes, node_s, start, n_samp, ss, struct_spans) in enumerate(slices):
        zdense = zall[start:start + n_samp].astype(float).copy()
        st_mask = None
        if forced is not None:
            st_mask, zdeck = forced[ci]
            if st_mask.any():
                # Deckenlinie aus der korridoruebergreifenden Aufloesung setzen;
                # diese Punkte gehen NICHT in den Spline-Fit ein und werden auch
                # nicht mehr von ihm verbogen.
                good = st_mask & np.isfinite(zdeck)
                zdense[good] = zdeck[good]
        # Bruecken/Tunnel: Bare-Earth-DTM durch lineare Interpolation zwischen den
        # An-Grade-Hoehen an den Bauwerksenden ersetzen (wie Skript 02).
        if struct_spans and forced is None:
            is_st = np.zeros(len(ss), dtype=bool)
            for (a, b) in struct_spans:
                is_st[(ss >= a) & (ss <= b)] = True
            i, N = 0, len(ss)
            while i < N:
                if is_st[i]:
                    j = i
                    while j < N and is_st[j]:
                        j += 1
                    z_a = zdense[i - 1] if (i > 0 and np.isfinite(zdense[i - 1])) else zdense[i]
                    z_b = zdense[j] if (j < N and np.isfinite(zdense[j])) else zdense[j - 1]
                    if np.isfinite(z_a) and np.isfinite(z_b):
                        zdense[i:j] = np.linspace(z_a, z_b, j - i)
                    i = j
                else:
                    i += 1
        interior = [n for n in path_nodes if n not in junctions]
        fin = np.isfinite(zdense)
        if fin.sum() < 2:
            for n in interior:
                z.setdefault(n, float("nan"))
            continue
        # Bauwerkspunkte NICHT mitfitten: sonst verbiegt der Spline die exakte
        # Deckenlinie wieder (gemessen bis 4,5 m, Median 0,74 m).
        fit_sel = fin
        if st_mask is not None and st_mask.any():
            alt = fin & ~st_mask
            if alt.sum() >= max(4, int(0.2 * fin.sum())):
                fit_sel = alt
        s_fit, z_fit = ss[fit_sel], zdense[fit_sel]
        if smooth_rms_m and smooth_rms_m > 0 and UnivariateSpline is not None and s_fit.size >= 4:
            w = np.clip(np.abs(np.gradient(s_fit)), 1e-6, None)
            k = min(3, s_fit.size - 1)
            try:
                with _warn.catch_warnings():
                    _warn.simplefilter("ignore")
                    spl = UnivariateSpline(s_fit, z_fit, w=w, s=(smooth_rms_m ** 2) * float(w.sum()), k=k)
                zfun = lambda q, _spl=spl: float(_spl(q))
            except Exception:
                zfun = lambda q, a=s_fit, b=z_fit: float(np.interp(q, a, b))
        else:
            zfun = lambda q, a=s_fit, b=z_fit: float(np.interp(q, a, b))
        if st_mask is not None and st_mask.any():
            # ausserhalb der Bauwerke der geglaettete Spline, im Bauwerk exakt
            # die Deckenlinie (auch fuer collect_dense und alle Knoten)
            z_grid = np.fromiter((zfun(q) for q in ss), float, count=n_samp)
            put = st_mask & np.isfinite(zdense)
            z_grid[put] = zdense[put]
            zfun = lambda q, _a=ss, _b=z_grid: float(np.interp(q, _a, _b))
        st = bool(struct_spans); Lc = float(ss[-1])
        for n in interior:
            z[n] = zfun(node_s[n])
            if debug:
                dbg[n] = (Lc, float(node_s[n]), st)
        if collect_dense:
            lon_c = np.asarray(all_lon[start:start + n_samp], float)
            lat_c = np.asarray(all_lat[start:start + n_samp], float)
            z_c = np.fromiter((zfun(q) for q in ss), float, count=n_samp)
            good = np.isfinite(z_c)
            if good.any():
                dense_rows.extend(zip(lon_c[good].tolist(),
                                      lat_c[good].tolist(),
                                      z_c[good].tolist()))

    if collect_dense:
        dense = (np.asarray(dense_rows, float).reshape(-1, 3)
                 if dense_rows else np.empty((0, 3), float))
        return (z, dbg, dense) if debug else (z, dense)
    return (z, dbg) if debug else z


def _normalize_maxspeed(v):
    """OSM-maxspeed -> km/h float. 'none' (unlimitierte Autobahn) -> NONE_MAXSPEED_KMH,
    'walk' -> 7. Unbekannt/leer -> np.nan (Aufrufer setzt dann seinen Default)."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return np.nan
    s = str(v).strip().lower()
    if s in ("none", "de:motorway", "signals", "variable"):
        return float(NONE_MAXSPEED_KMH)
    if s in ("walk", "de:walk", "de:living_street"):
        return 7.0
    try:
        return float(s.replace(",", "."))
    except Exception:
        return np.nan


def _default_maxspeed_for(highway):
    """Typ-abhaengiger maxspeed-Default [km/h], falls KEIN maxspeed-Tag vorhanden ist.
    Greift nur bei fehlendem/unbekanntem Wert und ueberschreibt keine getaggten Werte.
      motorway      -> 130 (Richtgeschwindigkeit; getaggtes 'none' -> ebenfalls 130)
      motorway_link ->  60 (Rampen/Auffahrten)
      trunk/primary -> 100 (Bundesstrasse ausserorts)
      sonst         ->  50 (innerorts/Default)"""
    h = highway
    if isinstance(h, (list, tuple)) and h:
        h = h[0]
    h = str(h).strip().lower()
    if h == "motorway":
        return 130.0
    if h == "motorway_link":
        return 60.0
    if h in ("trunk", "primary"):
        return 100.0
    return 50.0


# --- Rueckwaerts-kompatible Shims fuer bestehende Aufrufer -------------------
# Die alte npz/KD-Tree-Schnittstelle bleibt aufrufbar, liefert aber jetzt DTM-Hoehen.
# load_kdtree(pfad) ignoriert den (nicht mehr noetigen) npz-Pfad.

def load_kdtree(input_path=None):
    """DEPRECATED: Hoehen kommen jetzt direkt aus dem LiDAR-DTM. Gibt
    (dtm_handle, None, None) zurueck, damit altes 3-Tupel-Entpacken weiter funktioniert."""
    return load_dtm(), None, None


def kdtree_heights_vectorized(tree, heights, xs, ys):
    """DEPRECATED: ignoriert die alten KD-Tree-Argumente und sampelt das DTM direkt.
    `tree` ist der dtm_handle aus load_kdtree(); `heights` wird ignoriert."""
    return sample_heights(tree, xs, ys)

def load_local_osm_file(local_osm_input_path):
    """Load nodes and edges from a local GeoPackage with layers 'nodes' and 'edges' (EPSG:4326)."""
    gdf_nodes = gpd.read_file(local_osm_input_path, layer="nodes").set_crs("EPSG:4326", allow_override=True)
    gdf_edges = gpd.read_file(local_osm_input_path, layer="edges").set_crs("EPSG:4326", allow_override=True)
    return gdf_nodes, gdf_edges

def _truthy_flag(val) -> bool:
    """Interpret a variety of truthy/falsy inputs as boolean."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip().lower()
    return s not in ("", "no", "false", "0")

def _num(x, default=None, as_int=False):
    """Parse robust numeric value: return float/int or default. Removes NaN/inf."""
    try:
        v = float(x)
        if not math.isfinite(v):
            return default
        return int(round(v)) if as_int else v
    except Exception:
        return default

def _clean_text(val, default="unknown"):
    """Return a clean string value without empty/'nan'."""
    if val is None:
        return default
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return default
    return s

def _maybe_bool(x):
    """Tri-state bool parser: True/False or None if unknown."""
    s = str(x).strip().lower()
    if s in ("1", "true", "yes"):  return True
    if s in ("0", "false", "no"):  return False
    return None

def _flatten_osmids_from_block(block: gpd.GeoDataFrame) -> list[int]:
    """Extract ordered, de-duplicated OSMIDs from block['osmid']."""
    out, seen = [], set()
    for val in block.get('osmid', []):
        for osm in _osmid_list(val):
            if osm not in seen:
                seen.add(osm)
                out.append(osm)
    return out


# --------------------------- Detailed sequence (directed) ---------------------------
def _osmid_list(val):
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [int(x) for x in s.replace(" ", "").split(",") if x]

def build_osmid_index_for_detailed(gdf_edges_detailed: gpd.GeoDataFrame,
                                   reversed_col: str = "reversed"):
    """
    Build indices for quick candidate lookup by OSMID and optional 'reversed' flag.
    Returns a dict with:
      - 'any': osmid -> [row_idx]
      - 'rev': (osmid, reversed_bool) -> [row_idx]
      - 'has_rev': whether the column exists
    """
    idx_any = defaultdict(list)              # osmid -> [row_idx]
    idx_rev = defaultdict(list)              # (osmid, rev_bool) -> [row_idx]
    has_rev = reversed_col in gdf_edges_detailed.columns

    for i, row in gdf_edges_detailed.iterrows():
        # collect OSMIDs for this detailed edge row
        for osmid in str(row.get('osmid', '')).strip('[]').replace(' ', '').split(','):
            if not osmid:
                continue
            try:
                osm = int(osmid)
            except Exception:
                continue

            idx_any[osm].append(i)

            if has_rev:
                rev = _maybe_bool(row.get(reversed_col))
                if rev is not None:
                    idx_rev[(osm, rev)].append(i)

    return {"any": idx_any, "rev": idx_rev, "has_rev": has_rev}

def _empty_like(df, crs="EPSG:4326"):
    cols = list(df.columns) if isinstance(df, (pd.DataFrame, gpd.GeoDataFrame)) else []
    return gpd.GeoDataFrame(columns=cols).set_crs(crs)

def get_directed_detailed_sequence(edge, gdf_edges_detailed, osmid_index, DEBUG=False) -> gpd.GeoDataFrame:
    """
    Build a *directed* sequence of detailed segments from u0 -> v0.

    Priority when extending from the *current* set (primary/alt):
      1) same OSMID,   u == current (forward within OSMID block)
      2) same OSMID,   v == current (flip within OSMID block)
      3) other OSMID,  u == current (forward at OSMID boundary)
      4) other OSMID,  v == current (flip at OSMID boundary)

    Switching between primary/alt is kept as fallback. When reconstructing:
      - Segments from 'alt' toggle the 'reversed' flag (if present).
      - Steps taken via 'v' (reverse direction) flip geometry and swap u/v.
    """
    try:
        # --- Simplified edge meta ---
        u0, v0 = int(edge["u"]), int(edge["v"])
        osmid_str = str(edge.get("osmid", "")).strip("[]").replace(" ", "")
        osmids = [int(x) for x in osmid_str.split(",") if x]

        edge_rev = _maybe_bool(edge.get("reversed")) if ("reversed" in edge) else None
        has_rev = bool(osmid_index.get("has_rev"))

        # --- Candidate selection via OSMIDs (+ optional reversed) ---
        cand_idx_set = set()
        if has_rev and (edge_rev is not None):
            for osm in osmids:
                cand_idx_set.update(osmid_index["rev"].get((osm, edge_rev), []))
            if DEBUG and not cand_idx_set:
                print(f"[osmid+rev] no candidates for {u0}->{v0} (osmids={osmids}, reversed={edge_rev})")
        else:
            for osm in osmids:
                cand_idx_set.update(osmid_index["any"].get(osm, []))
            if DEBUG and not cand_idx_set:
                print(f"[osmid] no candidates for {u0}->{v0} (osmids={osmids})")

        if not cand_idx_set:
            return _empty_like(gdf_edges_detailed)

        cand = gdf_edges_detailed.loc[sorted(cand_idx_set)].copy()
        if cand.empty:
            if DEBUG:
                print("[cand] empty after OSMID(+reversed) filter")
            return _empty_like(gdf_edges_detailed)

        # --- Cleanup u/v ---
        def _cleanup_uv(df):
            df = df.copy()
            df["u"] = pd.to_numeric(df["u"], errors="coerce")
            df["v"] = pd.to_numeric(df["v"], errors="coerce")
            df = df.dropna(subset=["u", "v"]).copy()
            df["u"] = df["u"].astype(int)
            df["v"] = df["v"].astype(int)
            return df

        cand = _cleanup_uv(cand)

        # --- Build lookups ---
        def build_by_start(df):
            m = {}
            for i, r in df.iterrows():
                m.setdefault(int(r["u"]), []).append(i)
            return m

        def build_by_end(df):
            m = {}
            for i, r in df.iterrows():
                m.setdefault(int(r["v"]), []).append(i)
            return m

        by_u_primary   = build_by_start(cand)
        by_end_primary = build_by_end(cand)

        # --- Alt set: reversed == not edge_rev ---
        cand_alt = _empty_like(gdf_edges_detailed)
        by_u_alt, by_end_alt = {}, {}
        if has_rev and (edge_rev is not None):
            alt_idx_set = set()
            for osm in osmids:
                alt_idx_set.update(osmid_index["rev"].get((osm, not edge_rev), []))
            if alt_idx_set:
                cand_alt = _cleanup_uv(gdf_edges_detailed.loc[sorted(alt_idx_set)].copy())
                by_u_alt   = build_by_start(cand_alt)
                by_end_alt = build_by_end(cand_alt)

        # --- Stable first-OSMID accessor per row ---
        def row_osmid_first(r):
            try:
                ids = _osmid_list(r.get('osmid', ''))
                return ids[0] if ids else None
            except Exception:
                return None

        # --- Select start: prefer primary u==u0, else alt u==u0 ---
        used_primary, used_alt = set(), set()
        seq = []  # list of (src, idx, how) with how in {'u','v','start'}

        original_source = "primary"
        current_source = "primary"
        cand_cur, by_u_cur, by_end_cur = cand, by_u_primary, by_end_primary
        cand_other, by_u_other, by_end_other, other_label = cand_alt, by_u_alt, by_end_alt, "alt"

        start_list = by_u_cur.get(u0, [])
        if not start_list:
            alt_list = by_u_alt.get(u0, [])
            if alt_list:
                current_source = "alt"
                cand_cur, by_u_cur, by_end_cur = cand_alt, by_u_alt, by_end_alt
                cand_other, by_u_other, by_end_other, other_label = cand, by_u_primary, by_end_primary, "primary"
                start_list = alt_list
            else:
                if DEBUG:
                    print(f"[start] no start segment via u==u0 in primary/alt | u0={u0}")
                return _empty_like(gdf_edges_detailed)

        start_idx = start_list[0]
        (used_primary if current_source == "primary" else used_alt).add(start_idx)
        seq.append((current_source, start_idx, "start"))

        cur = int(cand_cur.loc[start_idx]["v"])
        last_osm = row_osmid_first(cand_cur.loc[start_idx])  # active OSMID block

        if DEBUG:
            ru = int(cand_cur.loc[start_idx]["u"]); rv = int(cand_cur.loc[start_idx]["v"])
            print(f"[start] idx={start_idx} seg=({ru}->{rv}) source={current_source} cur={cur} target v0={v0} osmid={last_osm}")

        # For legacy fallback (once) when switching back to original_source
        allow_v_once = False

        # --- Try to extend within current source with OSMID-aware priority ---
        def try_current(cur_node, last_osm):
            """
            Priorities:
              1) same OSMID, u==cur
              2) same OSMID, v==cur (flip)
              3) other OSMID, u==cur
              4) other OSMID, v==cur (flip)
            If all fail:
              - optional legacy once-flip only in primary (allow_v_once)
            """
            nonlocal allow_v_once
            df = cand_cur
            used_set = used_primary if current_source == "primary" else used_alt

            lst_u = by_u_cur.get(cur_node, [])
            lst_v = by_end_cur.get(cur_node, [])

            def pick(candidates, prefer_same_osm: bool, via: str):
                # via: 'u' (forward) or 'v' (flip)
                for i in candidates:
                    if i in used_set:
                        continue
                    osm_i = row_osmid_first(df.loc[i])
                    same = (osm_i == last_osm) if (last_osm is not None and osm_i is not None) else False
                    if (prefer_same_osm and same) or (not prefer_same_osm and not same):
                        new_cur = int(df.loc[i]["v"] if via == 'u' else df.loc[i]["u"])
                        used_set.add(i)
                        seq.append((current_source, i, via))
                        if DEBUG:
                            u_i = int(df.loc[i]["u"]); v_i = int(df.loc[i]["v"])
                            how = "via u==cur" if via == 'u' else "via v==cur (flip)"
                            print(f"[step/{current_source}] idx={i} seg=({u_i}->{v_i}) {how} cur={cur_node} -> {new_cur} osm_same={same} osm={osm_i}")
                        if via == 'v':
                            allow_v_once = False  # flip consumed
                        return new_cur, osm_i, True
                return cur_node, last_osm, False

            # 1) same OSMID, u==cur
            new_cur, new_osm, ok = pick(lst_u, prefer_same_osm=True, via='u')
            if ok: return new_cur, new_osm, True

            # 2) same OSMID, v==cur (flip)
            new_cur, new_osm, ok = pick(lst_v, prefer_same_osm=True, via='v')
            if ok: return new_cur, new_osm, True

            # 3) other OSMID, u==cur
            new_cur, new_osm, ok = pick(lst_u, prefer_same_osm=False, via='u')
            if ok: return new_cur, new_osm, True

            # 4) other OSMID, v==cur (flip)
            new_cur, new_osm, ok = pick(lst_v, prefer_same_osm=False, via='v')
            if ok: return new_cur, new_osm, True

            # last resort: legacy once-flip only in primary
            if current_source == "primary" and allow_v_once:
                for i in lst_v:
                    if i not in used_set:
                        new_cur = int(df.loc[i]["u"])
                        used_set.add(i)
                        seq.append((current_source, i, "v"))
                        if DEBUG:
                            u_i = int(df.loc[i]["u"]); v_i = int(df.loc[i]["v"])
                            print(f"[step/{current_source}] idx={i} seg=({u_i}->{v_i}) via v==cur (legacy once) cur={cur_node} -> {new_cur}")
                        allow_v_once = False
                        return new_cur, row_osmid_first(df.loc[i]), True

            return cur_node, last_osm, False

        # --- Main chaining loop ---
        guard = 0
        while guard < (len(cand) + len(cand_alt) + 10):
            guard += 1

            new_cur, last_osm_new, ok = try_current(cur, last_osm)
            if ok:
                cur, last_osm = new_cur, last_osm_new
                if cur == v0:
                    break
                continue

            # switch primary <-> alt
            current_source, other_label = other_label, current_source
            cand_cur,  cand_other  = cand_other,  cand_cur
            by_u_cur,  by_u_other  = by_u_other,  by_u_cur
            by_end_cur, by_end_other = by_end_other, by_end_cur

            # When switching back to the original source, allow one legacy flip
            allow_v_once = (current_source == original_source)

            if DEBUG:
                print(f"[switch] now={current_source} allow_v_once={allow_v_once}  cur={cur} last_osm={last_osm}")

            new_cur, last_osm_new, ok = try_current(cur, last_osm)
            if ok:
                cur, last_osm = new_cur, last_osm_new
                if cur == v0:
                    break
                continue

            if DEBUG:
                print(f"[chain] no continuation at cur={cur} (now={current_source})")
            break

        if not seq:
            if DEBUG:
                print("[end] empty sequence")
            return _empty_like(gdf_edges_detailed)

        # --- Reconstruct output rows with harmonized orientation ---
        rows = []
        for src, idx, how in seq:
            # choose source row
            if src == "primary":
                if idx not in cand.index:
                    continue
                row = cand.loc[idx].copy()
            else:  # 'alt'
                if idx not in cand_alt.index:
                    continue
                row = cand_alt.loc[idx].copy()

            # 1) toggle 'reversed' for segments from 'alt'
            if src == "alt" and "reversed" in row:
                r = _maybe_bool(row["reversed"])
                if r is not None:
                    row["reversed"] = (not r)

            # 2) flip geometry + swap u/v if chosen via v==cur (reverse)
            if how == "v":
                u_old, v_old = row["u"], row["v"]
                row["u"], row["v"] = v_old, u_old
                try:
                    row["geometry"] = LineString(list(row.geometry.coords)[::-1])
                except Exception:
                    pass

            rows.append(row)

        if not rows:
            return _empty_like(gdf_edges_detailed)

        out = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        return out.set_crs("EPSG:4326", allow_override=True)

    except Exception as e:
        if DEBUG:
            print("[get_directed_detailed_sequence:osmid-aware] EXCEPTION:", repr(e))
        return _empty_like(gdf_edges_detailed)

def split_once_at_half(edge, seq_det: gpd.GeoDataFrame):
    """
    Split 'edge' at its half simplified length *on a detailed segment boundary*.

    Returns:
      (left_edge_df, right_edge_df, split_meta, left_seq, right_seq)
    or (None, None, None, None, None).
    """
    L = float(edge['length'])
    if L <= 0 or seq_det.empty:
        return None, None, None, None, None

    n = len(seq_det)
    if n < 2:
        return None, None, None, None, None

    seq = seq_det.copy()
    seq['len_det'] = pd.to_numeric(seq['length'], errors='coerce').fillna(0.0).astype(float)

    half = 0.5 * L
    cum_fwd = seq['len_det'].cumsum().to_numpy()
    cum_bwd = seq['len_det'][::-1].cumsum().to_numpy()

    k_fwd = int(np.searchsorted(cum_fwd, half, side='left'))
    k_bwd = int(np.searchsorted(cum_bwd, half, side='left'))

    over_fwd = cum_fwd[k_fwd] - half if k_fwd < n else float('inf')
    over_bwd = cum_bwd[k_bwd] - half if k_bwd < n else float('inf')

    use_front = (over_fwd <= over_bwd)
    cut_idx = (k_fwd if use_front else (n - k_bwd))

    if cut_idx <= 0:
        cut_idx = 1
    elif cut_idx >= n:
        cut_idx = n - 1

    left_seg  = seq.iloc[:cut_idx].copy()
    right_seg = seq.iloc[cut_idx:].copy()
    if left_seg.empty or right_seg.empty:
        return None, None, None, None, None

    # Split node + XY
    if use_front:
        split_nid = str(int(left_seg.iloc[-1]['v']))
        split_xy  = left_seg.iloc[-1].geometry.coords[-1]
    else:
        split_nid = str(int(right_seg.iloc[0]['u']))
        split_xy  = right_seg.iloc[0].geometry.coords[0]
    split_meta = {"nid": split_nid, "xy": (float(split_xy[0]), float(split_xy[1]))}

    # Helper: build a simplified sub-edge from a detailed block
    def make_block(block):
        row = edge.copy()
        row['u'] = str(int(block.iloc[0]['u']))
        row['v'] = str(int(block.iloc[-1]['v']))

        coords = []
        prev_end = None
        for _, s in block.iterrows():
            g = s.geometry
            if prev_end is not None and g.coords[0] != prev_end:
                g = LineString(list(g.coords)[::-1])
            if not coords:
                coords.extend(list(g.coords))
            else:
                coords.extend(list(g.coords)[1:])
            prev_end = g.coords[-1]
        row['geometry'] = LineString(coords)

        # length
        L_sum = pd.to_numeric(block['len_det'], errors='coerce').sum()
        if not np.isfinite(L_sum) or L_sum <= 0:
            try:
                L_sum = float(gpd.GeoSeries([row['geometry']], crs="EPSG:4326").to_crs(3857).length.iloc[0])
            except Exception:
                L_sum = 1.0
        row['length'] = float(max(1.0, L_sum))

        # attributes (conservative)
        row['highway']  = block.iloc[0].get('highway', row.get('highway', 'unknown'))
        if 'maxspeed' in block:
            # _normalize_maxspeed statt rohem to_numeric: 'none' -> 130 statt NaN
            # (sonst gewinnt in Mischbloecken [100, 'none'] faelschlich die 100).
            try:
                vals = []
                for x in block['maxspeed']:
                    xs = x if isinstance(x, (list, tuple, np.ndarray)) else [x]
                    vals.extend(_normalize_maxspeed(v) for v in xs)
                vals = [v for v in vals if v is not None and math.isfinite(v)]
                row['maxspeed'] = float(max(vals)) if vals else np.nan
            except Exception:
                row['maxspeed'] = row.get('maxspeed', 50.0)
        else:
            row['maxspeed'] = row.get('maxspeed', 50.0)
        if 'oneway' in block:
            try:
                flags = block['oneway'].astype(str).str.lower().isin(['1', 'true', 'yes'])
                row['oneway'] = bool(flags.any())
            except Exception:
                row['oneway'] = False
        else:
            row['oneway'] = False

        # OSMIDs taken exactly from the block, filtered by original set (if present)
        orig_ids = set(_osmid_list(edge.get('osmid', '')))
        block_ids = _flatten_osmids_from_block(block)
        filtered_ids = [osm for osm in block_ids if (not orig_ids) or (osm in orig_ids)]
        row['osmid'] = "[" + ",".join(str(x) for x in filtered_ids) + "]"

        row['_struct_ivals'] = _struct_fractions(block)
        row['origin'] = 'split'
        return gpd.GeoDataFrame([row]).set_crs("EPSG:4326", allow_override=True)

    left_edge  = make_block(left_seg)
    right_edge = make_block(right_seg)

    # Provide sequence halves as well
    left_seq = left_seg.drop(columns=[c for c in left_seg.columns if c not in seq_det.columns], errors='ignore').copy()
    right_seq = right_seg.drop(columns=[c for c in right_seg.columns if c not in seq_det.columns], errors='ignore').copy()
    left_seq  = left_seq.set_crs("EPSG:4326", allow_override=True)
    right_seq = right_seq.set_crs("EPSG:4326", allow_override=True)

    return left_edge, right_edge, split_meta, left_seq, right_seq

import sys


def _canonicalize_to_detailed(edge_row, seq_det):
    """Ersetzt Geometrie + Laenge einer (nicht gesplitteten) Kante durch ihre
    konkatenierte DETAILGEOMETRIE. Damit nutzen kept- und split-Kanten dieselbe
    Repraesentation -> Laenge und Hoehenprofil sind unabhaengig von der erlaubten
    Linklaenge. u/v und alle Tags (inkl. bridge/tunnel) bleiben erhalten.
    Rueckgabe: Series (Kante) oder None, wenn keine brauchbare Sequenz."""
    if seq_det is None or seq_det.empty:
        return None
    # Guard: get_directed_detailed_sequence kann bei Matching-Abbruch TEILKETTEN
    # liefern. Eine unvollstaendige Sequenz wuerde hier stillschweigend zu kurze
    # Geometrie/Laenge einsetzen (und darueber die Korridor-Bogenlaengen der
    # Hoehenzuweisung verschieben). Daher: Sequenz muss die Kante exakt von u
    # nach v ueberspannen, sonst simplified-Fallback (Rueckgabe None).
    try:
        if (int(seq_det.iloc[0]['u']) != int(edge_row['u'])
                or int(seq_det.iloc[-1]['v']) != int(edge_row['v'])):
            return None
    except (TypeError, ValueError):
        return None
    row = edge_row.copy()
    coords = []
    prev_end = None
    for _, s in seq_det.iterrows():
        g = s.geometry
        if g is None:
            continue
        if prev_end is not None and g.coords[0] != prev_end:
            g = LineString(list(g.coords)[::-1])
        if not coords:
            coords.extend(list(g.coords))
        else:
            coords.extend(list(g.coords)[1:])
        prev_end = g.coords[-1]
    if len(coords) < 2:
        return None
    row['geometry'] = LineString(coords)
    L = pd.to_numeric(seq_det['length'], errors='coerce').fillna(0.0).sum()
    if np.isfinite(L) and L > 0:
        row['length'] = float(L)
    # Bauwerks-Intervalle aus den Detail-Segmenten (ersetzt das durch osmnx-
    # any-Aggregation unbrauchbare Ganz-Kanten-bridge/tunnel-Tag).
    row['_struct_ivals'] = _struct_fractions(seq_det)
    row['origin'] = 'keep'
    return row


def _geo_seg_lengths_m(coords):
    """Haversine-Laengen [m] der Segmente einer lon/lat-Koordinatenliste."""
    c = np.asarray(coords, dtype=float)
    lon = np.radians(c[:, 0]); lat = np.radians(c[:, 1])
    dlon = np.diff(lon); dlat = np.diff(lat)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    return 2.0 * 6371000.0 * np.arcsin(np.sqrt(a))


_FBS_SEQ = [0]  # Zaehler fuer synthetische Fallback-Split-Knoten-IDs


def _split_edge_by_own_geometry(edge, max_allowed_length, split_nodes_xy):
    """Fallback-Splitting OHNE Detail-Sequenz: teilt die Kante an ihren EIGENEN
    Geometrie-Stuetzpunkten in ~gleich lange Teile <= max_allowed_length.
    Noetig, weil Kanten mit leerer/unbrauchbarer Detail-Sequenz sonst UNGETEILT
    uebernommen werden (Germany 2026-07: 14 Links >10 km, max 26,4 km -> ein
    gemittelter Grade ueber die volle Laenge). Neue Knoten bekommen synthetische
    String-IDs ('fbs<n>'), XY wird in split_nodes_xy registriert.
    Rueckgabe: Liste von GeoDataFrames oder None (Geometrie nicht teilbar)."""
    try:
        coords = list(edge.geometry.coords)
    except Exception:
        return None
    if len(coords) < 3:
        return None
    L = float(pd.to_numeric(edge['length'], errors='coerce'))
    if not np.isfinite(L) or L <= 0:
        return None
    seg = _geo_seg_lengths_m(coords)
    total = float(seg.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    n_parts = int(np.ceil(L / float(max_allowed_length)))
    if n_parts < 2:
        return None

    # Ziel-Schnittpositionen; je der naechstliegende Stuetzpunkt, strikt steigend.
    cut_idx, last = [], 0
    for j in range(1, n_parts):
        k = int(np.argmin(np.abs(cum - total * j / n_parts)))
        k = max(k, last + 1)
        if k >= len(coords) - 1:
            break
        cut_idx.append(k)
        last = k
    if not cut_idx:
        return None

    bounds = [0] + cut_idx + [len(coords) - 1]
    scale = L / total  # OSM-Laenge proportional auf die Teile verteilen
    parent_iv = _edge_struct_ivals(edge)  # Bauwerks-Intervalle des Elternteils (o. None)
    out, prev_nid = [], str(edge['u'])
    for bi in range(len(bounds) - 1):
        a, b = bounds[bi], bounds[bi + 1]
        row = edge.copy()
        # Eltern-Intervalle auf den Teilabschnitt umrechnen; ohne Detail-Info bleibt
        # das geerbte Ganz-Kanten-Tag wirksam (-> Linearisierung, Entscheidung
        # 2026-07-05: besser als Bare-Earth-Artefakte an echten Bauwerken).
        if parent_iv is not None:
            ga, gb = cum[a] / total, cum[b] / total
            row['_struct_ivals'] = [((max(f0, ga) - ga) / (gb - ga),
                                     (min(f1, gb) - ga) / (gb - ga))
                                    for (f0, f1) in parent_iv
                                    if min(f1, gb) > max(f0, ga)]
        row['u'] = prev_nid
        if bi == len(bounds) - 2:
            nid = str(edge['v'])
        else:
            _FBS_SEQ[0] += 1
            nid = f"fbs{_FBS_SEQ[0]}"
            split_nodes_xy[nid] = (float(coords[b][0]), float(coords[b][1]))
        row['v'] = nid
        row['geometry'] = LineString(coords[a:b + 1])
        row['length'] = float(max(1.0, (cum[b] - cum[a]) * scale))
        row['origin'] = 'split'
        out.append(gpd.GeoDataFrame([row]).set_crs("EPSG:4326", allow_override=True))
        prev_nid = nid
    return out


def short_edges(gdf_edges_simplified: gpd.GeoDataFrame,
                gdf_edges_detailed: gpd.GeoDataFrame,
                max_allowed_length: float):
    """
    Replace overly long simplified edges with directed sequences of detailed segments and
    recursively split until each part is <= max_allowed_length.
    Returns a tuple (edges_out_gdf, split_nodes_xy_dict).
    """

    # Ensure CRS
    if not gdf_edges_simplified.crs:
        gdf_edges_simplified = gdf_edges_simplified.set_crs("EPSG:4326", allow_override=True)
    if not gdf_edges_detailed.crs:
        gdf_edges_detailed = gdf_edges_detailed.set_crs("EPSG:4326", allow_override=True)

    osmid_index = build_osmid_index_for_detailed(gdf_edges_detailed)

    is_long = pd.to_numeric(gdf_edges_simplified['length'], errors='coerce').fillna(0.0) > float(max_allowed_length)
    long_edges = gdf_edges_simplified[is_long]
    keep_edges = gdf_edges_simplified[~is_long].copy()
    keep_edges['origin'] = 'keep'
    # Kanonisierung: auch NICHT gesplittete Kanten auf ihre Detailgeometrie umstellen,
    # damit Laenge/Hoehe unabhaengig von max_allowed_length sind (sonst nutzen kept-
    # Kanten die simplified-, split-Kanten die detailed-Repraesentation -> ~0.6 %
    # Laengen- und ~0.7 m Hoehen-Drift ueber Auflösungen). Bei fehlender Sequenz:
    # simplified-Kante als Fallback behalten.
    if len(keep_edges):
        canon = []
        for _, e in keep_edges.iterrows():
            try:
                seq = get_directed_detailed_sequence(e, gdf_edges_detailed, osmid_index)
                r = _canonicalize_to_detailed(e, seq)
            except Exception:
                r = None
            canon.append(r if r is not None else e)
        keep_edges = gpd.GeoDataFrame(canon, crs="EPSG:4326")

    result_parts = [keep_edges]
    split_nodes_xy = {}
    to_process = []

    # -------- Progress target estimation --------
    def _needed_segments(L, maxlen):
        try:
            Lf = float(L)
        except Exception:
            return 0
        if not np.isfinite(Lf) or Lf <= 0:
            return 0
        return int(np.ceil(Lf / float(maxlen)))

    needed_total = (
            sum(_needed_segments(e['length'], max_allowed_length) for _, e in long_edges.iterrows()) +
            len(keep_edges)
    )

    # -------- Progress bars --------
    pbar_tasks = tqdm(total=0, desc="Splitting work", position=0,
                      unit="task", mininterval=0.3,
                      dynamic_ncols=True, leave=False, file=sys.stdout)

    pbar_final = tqdm(total=needed_total, desc="Final edges", position=1,
                      unit="edge", mininterval=0.3,
                      dynamic_ncols=True, leave=False, file=sys.stdout)

    n_fb_edges = n_fb_parts = n_fb_unsplittable = 0

    # -------- Initial sequences --------
    for _, e in long_edges.iterrows():
        seq = get_directed_detailed_sequence(e, gdf_edges_detailed, osmid_index)
        if seq.empty:
            # Keine Detail-Sequenz -> Fallback: an eigener Geometrie splitten
            # (sonst bleibt die Kante ungeteilt, s. _split_edge_by_own_geometry).
            parts = _split_edge_by_own_geometry(e, max_allowed_length, split_nodes_xy)
            if parts is not None:
                n_fb_edges += 1; n_fb_parts += len(parts)
                result_parts.extend(parts)
                pbar_final.update(len(parts))
            else:
                n_fb_unsplittable += 1
                ee = gpd.GeoDataFrame([e], crs="EPSG:4326"); ee['origin'] = 'keep'
                result_parts.append(ee)
                pbar_final.update(1)
            if pbar_final.n > pbar_final.total:
                pbar_final.total = pbar_final.n
                pbar_final.refresh()
        else:
            to_process.append({'edge_row': e, 'seq_det': seq})
            pbar_tasks.total += 1
            pbar_tasks.refresh()

    # guard against infinite loops
    def _part_key(edge_row, seq_det):
        return (str(edge_row['u']), str(edge_row['v']),
                int(round(float(edge_row['length']))),
                str(edge_row.get('osmid')), int(len(seq_det)))

    seen_parts = set()

    # -------- Main processing loop --------
    while to_process:
        item = to_process.pop(0)
        edge = item['edge_row']
        seq  = item['seq_det']
        L = float(edge['length'])

        if L <= max_allowed_length:
            out_df = gpd.GeoDataFrame([edge], crs="EPSG:4326")
            out_df['origin'] = 'split'
            result_parts.append(out_df)
            pbar_final.update(1)
            pbar_tasks.update(1)
            continue

        left, right, split_meta, left_seq, right_seq = split_once_at_half(edge, seq)
        if left is None or right is None:
            # Detail-Sequenz nicht weiter teilbar (z.B. n<2) -> Fallback an
            # eigener Geometrie, sonst Kante ungeteilt uebernehmen wie bisher.
            parts = _split_edge_by_own_geometry(edge, max_allowed_length, split_nodes_xy)
            if parts is not None:
                n_fb_edges += 1; n_fb_parts += len(parts)
                result_parts.extend(parts)
                pbar_final.update(len(parts))
            else:
                n_fb_unsplittable += 1
                ee = gpd.GeoDataFrame([edge], crs="EPSG:4326"); ee['origin'] = 'keep'
                result_parts.append(ee)
                pbar_final.update(1)
            pbar_tasks.update(1)
            continue

        split_nodes_xy[split_meta['nid']] = split_meta['xy']

        new_tasks = 0

        # --- left ---
        L_edge = left.iloc[0]
        if float(L_edge['length']) > max_allowed_length:
            key = _part_key(L_edge, left_seq)
            if key not in seen_parts:
                seen_parts.add(key)
                to_process.append({'edge_row': L_edge, 'seq_det': left_seq})
                new_tasks += 1
        else:
            df_out = gpd.GeoDataFrame([L_edge], crs="EPSG:4326"); df_out['origin'] = 'split'
            result_parts.append(df_out)
            pbar_final.update(1)

        # --- right ---
        R_edge = right.iloc[0]
        if float(R_edge['length']) > max_allowed_length:
            key = _part_key(R_edge, right_seq)
            if key not in seen_parts:
                seen_parts.add(key)
                to_process.append({'edge_row': R_edge, 'seq_det': right_seq})
                new_tasks += 1
        else:
            df_out = gpd.GeoDataFrame([R_edge], crs="EPSG:4326"); df_out['origin'] = 'split'
            result_parts.append(df_out)
            pbar_final.update(1)

        # finished one task
        pbar_tasks.update(1)

        if new_tasks:
            pbar_tasks.total += new_tasks
            pbar_tasks.refresh()

        if pbar_final.n > pbar_final.total:
            pbar_final.total = pbar_final.n
            pbar_final.refresh()

    pbar_tasks.close()
    pbar_final.close()

    if n_fb_edges or n_fb_unsplittable:
        print(f"Fallback-Splitting (eigene Geometrie): {n_fb_edges} Kante(n) -> "
              f"{n_fb_parts} Teile; ungeteilt geblieben: {n_fb_unsplittable}")

    out = pd.concat(result_parts, ignore_index=True)
    if not out.crs:
        out = out.set_crs("EPSG:4326", allow_override=True)

    return out, split_nodes_xy

def write_matsim_network(gdf_nodes, gdf_edges, epsg_code, output_path, nodes_without_z: set = None):
    """
    Write a MATSim network (network_v2 DTD). For non-oneway links, produce two directed links.

    Parameters
    ----------
    gdf_nodes : GeoDataFrame (EPSG:4326)
        Must contain column 'height' for node Z (optional) and 'geometry' points.
    gdf_edges : GeoDataFrame (EPSG:4326)
        Must contain columns 'u', 'v', 'length', 'maxspeed' (km/h), 'capacity', 'lanes',
        'highway', 'oneway', and LineString geometry.
    epsg_code : int
        Target planar CRS for x/y coordinates in the MATSim file.
    output_path : str
        Path to write gzipped MATSim network XML.
    nodes_without_z : set[str], optional
        Node IDs for which 'z' should be omitted even if height is present.
    """
    print("Writing MATSim network...")
    # unify CRS (x/y in target EPSG)
    gdf_nodes = gdf_nodes.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)
    gdf_edges = gdf_edges.set_crs(epsg=4326, allow_override=True).to_crs(epsg=epsg_code)

    # Node-ID series (robust, ensure single scalar value)
    if "osmid" in gdf_nodes.columns:
        osmid_series = gdf_nodes["osmid"].copy()
        osmid_series = osmid_series.apply(lambda v: (v[0] if isinstance(v, (list, tuple, np.ndarray)) else v))
    else:
        osmid_series = gdf_nodes.index.to_series()
    osmid_series = osmid_series.astype(str)

    # Node lookup: id -> (x, y, z)
    has_height = "height" in gdf_nodes.columns
    node_lookup = {}
    for idx, row in gdf_nodes.iterrows():
        osm_id = osmid_series.loc[idx]
        x = float(row.geometry.x)
        y = float(row.geometry.y)
        z = None
        if has_height:
            try:
                z_val = row["height"]
                if pd.notna(z_val):
                    z = float(z_val)
            except Exception:
                z = None
        node_lookup[osm_id] = (x, y, z)

    # Build links (possibly bidirectional)
    links_data = []

    def parse_maxspeed(val, default=130.0):
        if isinstance(val, (list, tuple, np.ndarray)):
            cand = [_normalize_maxspeed(x) for x in val]
            cand = [c for c in cand if c is not None and math.isfinite(c)]
            return max(cand) if cand else default
        r = _normalize_maxspeed(val)
        return r if (r is not None and math.isfinite(r)) else default

    for _, row in gdf_edges.iterrows():
        u = _clean_text(row.get("u"), None)
        v = _clean_text(row.get("v"), None)
        # Skip if u/v missing
        if (u is None) or (v is None):
            continue

        length_m = _num(row.get("length"), default=None)
        if length_m is None or length_m <= 0:
            # Fallback: Geometrielaenge im (metrischen) Ziel-CRS des Netzes.
            # gdf_edges ist hier bereits nach epsg_code (z.B. 4839) reprojiziert,
            # daher ist geometry.length direkt in Metern. (Frueher: to_crs(3857),
            # was in DE die Laenge um ~50-75 % ueberschaetzte.)
            try:
                length_m = float(row.geometry.length)
            except Exception:
                length_m = 1.0
        length = str(int(round(max(1.0, length_m))))

        # Default nur wenn maxspeed fehlt/unbekannt -> typ-abhaengig (Rampe/Bundesstrasse/...)
        maxspeed = parse_maxspeed(row.get("maxspeed"),
                                  default=_default_maxspeed_for(row.get("highway")))
        freespeed = round((maxspeed or 50.0) / 3.6, 2)

        cap = _num(row.get("capacity"), default=3000, as_int=True) or 3000
        permlanes = _num(row.get("lanes"), default=1, as_int=True) or 1

        highway_type = _clean_text(row.get("highway"), "unknown")
        one_way_flag = _truthy_flag(row.get("oneway", None))
        if (not one_way_flag) and highway_type in ("motorway", "motorway_link"):
            one_way_flag = True

        def add_link(u_, v_):
            lid = f"{u_}-{v_}"
            links_data.append({
                "id": lid, "from": u_, "to": v_,
                "length": length,
                "freespeed": float(freespeed),
                "capacity": str(int(cap)),
                "permlanes": str(int(max(1, permlanes))),
                "highway_type": highway_type,
                "oneway_attr": "1" if one_way_flag else "0",
            })

        add_link(u, v)
        if not one_way_flag:
            add_link(v, u)


    # Deduplicate by ID (keep the faster freespeed if duplicates exist)
    unique_links = {}
    for link in links_data:
        lid = link["id"]
        if (lid not in unique_links) or (link["freespeed"] > unique_links[lid]["freespeed"]):
            unique_links[lid] = link

    # XML structure
    network = ET.Element("network")
    network.insert(1, ET.Comment("======================================================================"))
    nodes_element = ET.SubElement(network, "nodes")
    network.append(ET.Comment("======================================================================"))
    links_element = ET.SubElement(
        network, "links",
        capperiod="01:00:00", effectivecellsize="7.5", effectivelanewidth="3.75"
    )
    network.append(ET.Comment("======================================================================"))

    # Write links & collect used nodes
    used_node_ids = set()
    for link in unique_links.values():
        if link["from"] not in node_lookup or link["to"] not in node_lookup:
            continue
        le = ET.SubElement(
            links_element, "link",
            id=link["id"],
            **{
                "from": link["from"],
                "to": link["to"],
                "length": link["length"],
                "freespeed": str(link["freespeed"]),
                "capacity": link["capacity"],
                "permlanes": link["permlanes"],
                "modes": "car",
            }
        )
        attrs = ET.SubElement(le, "attributes")
        a_speed = ET.SubElement(attrs, "attribute", name="allowed_speed", **{"class": "java.lang.Double"})
        a_speed.text = str(link["freespeed"])
        a_type = ET.SubElement(attrs, "attribute", name="type", **{"class": "java.lang.String"})
        a_type.text = _clean_text(link["highway_type"], "unknown")
        a_oneway = ET.SubElement(attrs, "attribute", name="oneway_source", **{"class": "java.lang.String"})
        a_oneway.text = "1" if link.get("oneway_attr") == "1" else "0"

        used_node_ids.add(link["from"]); used_node_ids.add(link["to"])

    # Write only used nodes; optionally omit z
    nodes_without_z = nodes_without_z or set()
    for osm_id in used_node_ids:
        x, y, z = node_lookup[osm_id]
        node_attrs = {"id": str(osm_id), "x": f"{x}", "y": f"{y}"}
        # only write z if it is a finite number and not in the omit-list
        if (z is not None) and math.isfinite(z) and (str(osm_id) not in nodes_without_z):
            node_attrs["z"] = f"{z}"
        ET.SubElement(nodes_element, "node", **node_attrs)

    xml_string = ET.tostring(network, encoding="utf-8")
    pretty_xml = md.parseString(xml_string).toprettyxml()
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" ?>\n')
        f.write('<!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd">\n')
        f.write("\n".join(pretty_xml.splitlines()[1:]))
    print("Done:", output_path)

def sanitize_edges_for_export(gdf_edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Clean edges before export:
    - Length: fix invalid/missing values from geometry (EPSG:3857), min=1 m
    - lanes/capacity/maxspeed/highway/oneway with robust defaults without NaN
    - keep u/v as strings (as required by MATSim)
    """
    df = gdf_edges.copy()

    # ensure CRS
    if not df.crs:
        df = df.set_crs("EPSG:4326", allow_override=True)

    # length
    if 'length' not in df.columns:
        df['length'] = np.nan
    len_num = pd.to_numeric(df['length'], errors='coerce')
    bad_len = len_num.isna() | (len_num <= 0)
    if bad_len.any():
        # Geometrielaenge im metrischen Netz-CRS (frueher EPSG:3857, was in DE
        # die Laenge um ~50-75 % ueberschaetzt; analog zum Fix in write_matsim_network).
        df_m = df.to_crs(4839)
        df.loc[bad_len, 'length'] = df_m.loc[bad_len, 'geometry'].length.values
    df['length'] = pd.to_numeric(df['length'], errors='coerce').fillna(1.0).clip(lower=1.0)

    # lanes
    if 'lanes' not in df.columns:
        df['lanes'] = 1
    df['lanes'] = pd.to_numeric(df['lanes'], errors='coerce').fillna(1).clip(lower=1).round().astype(int)

    # capacity
    if 'capacity' not in df.columns:
        df['capacity'] = 3000
    df['capacity'] = pd.to_numeric(df['capacity'], errors='coerce').fillna(3000).clip(lower=100).round().astype(int)

    # maxspeed – fehlende/unbekannte Werte typ-abhaengig defaulten (nur wenn kein Tag)
    _hw_default = (df['highway'].apply(_default_maxspeed_for)
                   if 'highway' in df.columns else pd.Series(50.0, index=df.index))
    if 'maxspeed' not in df.columns:
        df['maxspeed'] = _hw_default
    else:
        def _mx(v):
            if isinstance(v, (list, tuple, np.ndarray, pd.Series)):
                try:
                    vals = pd.to_numeric(
                        pd.Series([_normalize_maxspeed(x) for x in v]), errors='coerce')
                    vals = vals[vals.notna()]
                    return float(vals.max()) if not vals.empty else np.nan
                except Exception:
                    return np.nan
            return _normalize_maxspeed(v)
        ms = pd.to_numeric(df['maxspeed'].apply(_mx), errors='coerce')
        df['maxspeed'] = ms.where(ms.notna(), _hw_default)
    df['maxspeed'] = pd.to_numeric(df['maxspeed'], errors='coerce').fillna(50.0).clip(lower=1.0)

    # highway
    if 'highway' not in df.columns:
        df['highway'] = 'unknown'
    df['highway'] = df['highway'].astype(str)
    df.loc[df['highway'].str.strip().isin(['', 'nan', 'None']), 'highway'] = 'unknown'

    # oneway -> bool
    if 'oneway' not in df.columns:
        df['oneway'] = False
    df['oneway'] = df['oneway'].apply(lambda x: str(x).strip().lower() in ('1', 'true', 'yes'))

    # u/v as strings
    df['u'] = df['u'].astype(str)
    df['v'] = df['v'].astype(str)

    return df

def _save_dense_heights_csv(dense, out_csv, coord_decimals=6, z_decimals=2):
    """Speichert das dichte Fahrbahnprofil (Nx3 [lon, lat, z]) als lon,lat,z-CSV.
    Rundet Koordinaten/Hoehe und mittelt z an mehrfach abgedeckten (lon,lat)-Punkten
    (geteilte Knoten benachbarter Korridore) fuer eine konsistente Lookup-Tabelle."""
    import os as _os
    dense = np.asarray(dense, float).reshape(-1, 3)
    df = pd.DataFrame({
        "lon": np.round(dense[:, 0], coord_decimals),
        "lat": np.round(dense[:, 1], coord_decimals),
        "z":   np.round(dense[:, 2], z_decimals),
    }).dropna()
    df = df.groupby(["lon", "lat"], as_index=False)["z"].mean()
    df["z"] = df["z"].round(z_decimals)
    df = df.sort_values(["lat", "lon"], kind="mergesort")
    d = _os.path.dirname(_os.path.abspath(out_csv))
    if d:
        _os.makedirs(d, exist_ok=True)
    df.to_csv(out_csv, index=False)
    print(f"Dichtes Fahrbahn-Hoehenprofil: {len(df)} Punkte -> {out_csv}")
    return out_csv


def generate_network(
        area: str,
        max_allowed_link_length: float,
        target_epsg: int = 4839,
        version: str = "V0",
        simplified_gpkg: str = None,
        detailed_gpkg: str = None,
        dtm_path: str = None,
        output_path: str = None,
        smooth_rms_m: float = 1.0,
        sample_step_m: float = 5.0,
        dense_heights_csv: str = None,
):
    """Baut das MATSim-Netz. Wird `dense_heights_csv` gesetzt, wird zusaetzlich das
    im Hoehen-Schritt ohnehin berechnete DICHTE Fahrbahnprofil (lon, lat, z) als CSV
    gespeichert -- eine reine Koordinate->Hoehe-Lookup-Tabelle fuer Autobahn/Bundes-
    strasse, unabhaengig von der gewaehlten Linklaenge. Kein zweiter OSM-/DTM-Zugriff."""
    # --- Pfade & Namen ---
    # Hoehen kommen jetzt direkt aus dem LiDAR-DTM (siehe load_dtm/sample_heights),
    # nicht mehr aus einer npz/KD-Tree-Punktwolke. GPKG-/DTM-/Output-Pfade sind
    # parametrisierbar, damit die Pipeline regional wiederverwendbar ist.
    local_osm_input_path_simplified = simplified_gpkg or "data/germany_simplified_DF.gpkg"
    local_osm_input_path_detailed   = detailed_gpkg   or "data/germany_detailed_sorted_DF.gpkg"
    if output_path is None:
        output_path = f"data/{area}_max{int(max_allowed_link_length)}m_{version}.xml.gz"

    # --- Daten laden ---
    dtm = load_dtm(dtm_path) if dtm_path else load_dtm()
    gdf_nodes_simplified, gdf_edges_simplified = load_local_osm_file(local_osm_input_path_simplified)
    gdf_nodes_detailed,   gdf_edges_detailed   = load_local_osm_file(local_osm_input_path_detailed)

    # --- Kanten kürzen ---
    print(f"\nShortening edges for area={area} with max_allowed_link_length={max_allowed_link_length} m ...")
    gdf_edges_shortened, split_nodes_xy = short_edges(
        gdf_edges_simplified=gdf_edges_simplified,
        gdf_edges_detailed=gdf_edges_detailed,
        max_allowed_length=max_allowed_link_length
    )

    # --- genutzte Nodes bestimmen ---
    used_nodes = set(map(str, gdf_edges_shortened['u'])) | set(map(str, gdf_edges_shortened['v']))

    from shapely.geometry import Point
    def _first_scalar(v):
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) > 0:
            return v[0]
        return v

    gdf_nodes_detailed = gdf_nodes_detailed.copy()
    gdf_nodes_detailed["osmid_norm"] = pd.to_numeric(
        gdf_nodes_detailed["osmid"].apply(_first_scalar), errors='coerce'
    )
    gdf_nodes_detailed = gdf_nodes_detailed.dropna(subset=["osmid_norm"]).copy()
    gdf_nodes_detailed["osmid_norm"] = gdf_nodes_detailed["osmid_norm"].astype(int)

    osm_xy = {
        str(int(r["osmid_norm"])): (float(r.geometry.x), float(r.geometry.y))
        for _, r in gdf_nodes_detailed.iterrows()
    }

    def _xy_from_edge(nid: str):
        rows = gdf_edges_shortened[(gdf_edges_shortened['u'].astype(str) == nid) |
                                   (gdf_edges_shortened['v'].astype(str) == nid)]
        if rows.empty:
            return None
        r = rows.iloc[0]
        line: LineString = r.geometry
        if str(r['u']) == nid:
            return (float(line.coords[0][0]), float(line.coords[0][1]))
        else:
            return (float(line.coords[-1][0]), float(line.coords[-1][1]))

    node_rows, seen = [], set()
    for nid in used_nodes:
        if nid in seen:
            continue
        if nid in osm_xy:
            x, y = osm_xy[nid]
        elif nid in split_nodes_xy:
            x, y = split_nodes_xy[nid]
        else:
            fb = _xy_from_edge(nid)
            if fb is None:
                continue
            x, y = fb
        node_rows.append({'osmid': nid, 'geometry': Point(x, y)})
        seen.add(nid)

    gdf_nodes_export = gpd.GeoDataFrame(node_rows, crs="EPSG:4326")
    # Aufloesungsunabhaengige, sanft geglaettete Hoehen ueber Master-Korridor-Profile
    # (ersetzt Punkt-Sampling + Skript 05). smooth_rms_m=0 -> ungeglaettet.
    node_lonlat = {str(r['osmid']): (float(r['geometry'].x), float(r['geometry'].y))
                   for r in node_rows}
    if dense_heights_csv:
        z_by_node, dense = assign_heights_along_corridors(
            gdf_edges_shortened, node_lonlat, dtm,
            target_epsg=target_epsg, sample_step_m=sample_step_m,
            smooth_rms_m=smooth_rms_m, collect_dense=True)
        _save_dense_heights_csv(dense, dense_heights_csv)
    else:
        z_by_node = assign_heights_along_corridors(
            gdf_edges_shortened, node_lonlat, dtm,
            target_epsg=target_epsg, sample_step_m=sample_step_m, smooth_rms_m=smooth_rms_m)
    gdf_nodes_export['height'] = [z_by_node.get(str(nid), np.nan)
                                  for nid in gdf_nodes_export['osmid']]

    # --- Export ---
    write_matsim_network(
        gdf_nodes=gdf_nodes_export,
        gdf_edges=gdf_edges_shortened,
        epsg_code=target_epsg,
        output_path=output_path,
        nodes_without_z=set()
    )
    return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build MATSim networks with different max link lengths.")
    parser.add_argument("--area", type=str, required=True, help="z.B. 'Germany'")
    parser.add_argument("--max-length", type=float, required=True, help="Max. Linklänge in Metern")
    parser.add_argument("--version", type=str, default="V0")
    parser.add_argument("--epsg", type=int, default=4839)
    parser.add_argument("--dtm", type=str, default=None, help="Pfad zur DTM-.tif")
    parser.add_argument("--simplified-gpkg", type=str, default=None)
    parser.add_argument("--detailed-gpkg", type=str, default=None)
    parser.add_argument("--output", type=str, default=None, help="Netz-Ausgabepfad (.xml.gz)")
    parser.add_argument("--sample-step", type=float, default=5.0)
    parser.add_argument("--smooth-rms", type=float, default=1.0)
    parser.add_argument("--dense-heights-csv", type=str, default=None,
                        help="Wenn gesetzt: dichtes Fahrbahnprofil (lon,lat,z) zusaetzlich hierhin speichern")
    args = parser.parse_args()

    generate_network(
        area=args.area,
        max_allowed_link_length=args.max_length,
        target_epsg=args.epsg,
        version=args.version,
        dtm_path=args.dtm,
        simplified_gpkg=args.simplified_gpkg,
        detailed_gpkg=args.detailed_gpkg,
        output_path=args.output,
        sample_step_m=args.sample_step,
        smooth_rms_m=args.smooth_rms,
        dense_heights_csv=args.dense_heights_csv,
    )

