import os
import math
import numpy as np
import pandas as pd
import geopandas as gpd
from dataclasses import dataclass
from typing import Optional, Tuple
from scipy.spatial import cKDTree

# ---------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------
WORK_CRS = "EPSG:25833"
RAW_GPS_CRS = "EPSG:4326"
DEFAULT_NODE_MATCH_TOL_M = 20.0
DEFAULT_ABS_LEN_TOL_M = 30.0
DEFAULT_REL_LEN_TOL = 0.08
DEFAULT_FILTER_ZERO_VELOCITY = True
DEFAULT_REL_SPATIAL_TOL = 0.02
DEFAULT_MAX_SPATIAL_TOL_M = 300.0
DEFAULT_SPATIAL_CROP_BUFFER_M = 20000.0
MIN_SEGMENT_LENGTH_M = 2.0

# ---------------------------------------------------------------------
# Parameterdataclass
# ---------------------------------------------------------------------
@dataclass
class EvalParams:
    node_match_tol_m: float = DEFAULT_NODE_MATCH_TOL_M
    abs_len_tol_m: float = DEFAULT_ABS_LEN_TOL_M
    rel_len_tol: float = DEFAULT_REL_LEN_TOL
    filter_zero_velocity: bool = DEFAULT_FILTER_ZERO_VELOCITY
    work_crs: str = WORK_CRS
    raw_crs: str = RAW_GPS_CRS
    network_input_crs: str = "EPSG:4839"
    rel_spatial_tol: float = DEFAULT_REL_SPATIAL_TOL
    max_spatial_tol_m: float = DEFAULT_MAX_SPATIAL_TOL_M
    spatial_crop_buffer_m: float = DEFAULT_SPATIAL_CROP_BUFFER_M
    max_km_from_start: Optional[float] = None  # Beschränkung auf X km ab Start

# ---------------------------------------------------------------------
# Dummy-Funktionen – ersetze diese durch deine echten Implementierungen
# ---------------------------------------------------------------------
import xml.etree.ElementTree as ET
import gzip

def load_matsim_network(net_path: str, input_crs: str, work_crs: str):
    """
    Liest ein MATSim-Netzwerk (.xml oder .xml.gz) ein und gibt:
      nodes_gdf (GeoDataFrame), links_df (DataFrame)
    zurück.
    """
    # XML öffnen (unterstützt gzip)
    if net_path.endswith(".gz"):
        with gzip.open(net_path, "rt", encoding="utf-8") as f:
            tree = ET.parse(f)
    else:
        tree = ET.parse(net_path)
    root = tree.getroot()

    # --- Nodes extrahieren ---
    nodes = []
    for node in root.find("nodes"):
        nid = node.attrib["id"]
        x = float(node.attrib["x"])
        y = float(node.attrib["y"])
        z = float(node.attrib.get("z", 0.0))
        nodes.append({"id": nid, "x": x, "y": y, "z": z})
    nodes_df = pd.DataFrame(nodes)

    # GeoDataFrame mit CRS
    nodes_gdf = gpd.GeoDataFrame(
        nodes_df,
        geometry=gpd.points_from_xy(nodes_df["x"], nodes_df["y"]),
        crs=input_crs,
    ).to_crs(work_crs)

    # --- Links extrahieren ---
    links = []
    for link in root.find("links"):
        lid = link.attrib["id"]
        from_id = link.attrib["from"]
        to_id = link.attrib["to"]
        length = float(link.attrib.get("length", 0.0))
        links.append({
            "id": lid,
            "from": from_id,
            "to": to_id,
            "length": length
        })
    links_df = pd.DataFrame(links)

    return nodes_gdf, links_df


def _meas_buffer_polygon(gdf_meas: gpd.GeoDataFrame, buf_m: float):
    return gdf_meas.buffer(buf_m).unary_union

def _crop_network(nodes: gpd.GeoDataFrame, links: pd.DataFrame, geom):
    """Beschränkt das Netzwerk auf Knoten innerhalb von geom und behält nur
    Links, bei denen beide Endknoten (from/to) im gefilterten Bereich liegen."""
    nodes_in = nodes[nodes.geometry.within(geom)].copy()
    node_ids = set(nodes_in["id"])
    links_in = links[links["from"].isin(node_ids) & links["to"].isin(node_ids)].copy()
    return nodes_in, links_in

def _length_tolerance_m(link_len_m: float, abs_tol_m: float, rel_tol: float) -> float:
    return abs_tol_m + rel_tol * link_len_m

def _extract_max_link_len_from_filename(path: str) -> Optional[float]:
    import re
    m = re.search(r"max(\d+)m", os.path.basename(path))
    return float(m.group(1)) if m else None

# ---------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------
def load_measurements_parquet(
        parquet_path: str,
        work_crs: str = WORK_CRS,
        raw_crs: str = RAW_GPS_CRS,
        filter_zero_velocity: bool = True,
        max_km_from_start: Optional[float] = None,
) -> gpd.GeoDataFrame:
    """Lade und beschneide Messdaten."""
    gdf = pd.read_parquet(parquet_path)
    if "Latitude" in gdf.columns and "Longitude" in gdf.columns:
        gdf = gpd.GeoDataFrame(gdf, geometry=gpd.points_from_xy(gdf["Longitude"], gdf["Latitude"]), crs=raw_crs)
    else:
        raise ValueError("Keine Spalten 'Latitude'/'Longitude' gefunden.")

    if filter_zero_velocity and "Velocity" in gdf.columns:
        gdf = gdf[gdf["Velocity"] > 0].copy()

    # Kilometer-Spalte erstellen (wenn nicht vorhanden)
    if "Mileage" in gdf.columns and "mileage_km" not in gdf.columns:
        gdf["mileage_km"] = gdf["Mileage"]  # hier ggf. /1000.0 anpassen, falls Mileage in m vorliegt

    # sortieren
    gdf = gdf.sort_values("mileage_km").reset_index(drop=True)

    # Beschränkung auf erste X km
    if max_km_from_start is not None and np.isfinite(max_km_from_start):
        start_km = float(gdf["mileage_km"].iloc[0])
        gdf = gdf.loc[gdf["mileage_km"] <= start_km + float(max_km_from_start)].copy().reset_index(drop=True)

    gdf = gdf.to_crs(work_crs)
    return gdf

# ---------------------------------------------------------------------
# Hauptklasse
# ---------------------------------------------------------------------
class NetworkMetrics:
    def __init__(self, network_path, label, total_length_m, mae_pct, rmse_pct, n_links_used, n_links_total):
        self.network_path = network_path
        self.label = label
        self.total_length_m = total_length_m
        self.mae_pct = mae_pct
        self.rmse_pct = rmse_pct
        self.n_links_used = n_links_used
        self.n_links_total = n_links_total

class GradeEvaluator:
    def __init__(self, gdf_meas: gpd.GeoDataFrame, params: EvalParams = EvalParams()):
        self.params = params
        self.meas = gdf_meas.to_crs(params.work_crs).copy()
        coords = np.column_stack([self.meas.geometry.x.values, self.meas.geometry.y.values])
        self._tree = cKDTree(coords)
        self._mil_km = self.meas["mileage_km"].to_numpy(dtype=float)
        self._alt_m = self.meas["Altitude"].to_numpy(dtype=float, copy=False) if "Altitude" in self.meas.columns else np.zeros_like(self._mil_km)
        # Strecke in Metern für Segmentierung
        self._mil_m = self._mil_km * 1000.0

    def _nearest_meas_idx_with_dist(self, x: float, y: float, tol_m: float) -> Tuple[Optional[int], float]:
        d, i = self._tree.query([x, y], k=1)
        if np.isfinite(d) and d <= tol_m:
            return int(i), float(d)
        return None, float(d) if np.isfinite(d) else float("inf")

    def _segment_errors_along_link(
            self,
            i_start: int,
            i_end: int,
            link_avg_grade_pct: float,
            segment_len_m: float = 5.0,
    ) -> pd.DataFrame:
        """
        Zerlegt den Messfahrts-Abschnitt i_start..i_end in ~segment_len_m lange Intervalle
        und berechnet pro Intervall:
            - lokale Steigung der Messfahrt (grade_local_pct)
            - mittlere Steigung des Links (link_avg_grade_pct)
            - Steigungsfehler = lokal - Linkmittel (grade_error_pct)

        Rückgabe: DataFrame mit einer Zeile pro Segment.
        """
        if i_start is None or i_end is None:
            return pd.DataFrame()

        if i_end <= i_start:
            return pd.DataFrame()

        s_start = float(self._mil_m[i_start])
        s_end = float(self._mil_m[i_end])

        if not np.isfinite(s_start) or not np.isfinite(s_end):
            return pd.DataFrame()

        if s_end - s_start < MIN_SEGMENT_LENGTH_M:
            # zu kurz, um sinnvoll zu segmentieren
            return pd.DataFrame()

        seg_rows = []
        mil_m = self._mil_m
        alt_m = self._alt_m

        s = s_start
        while s < s_end:
            s0 = s
            s1 = min(s0 + segment_len_m, s_end)
            ds = s1 - s0
            if ds < MIN_SEGMENT_LENGTH_M:
                break

            z0 = float(np.interp(s0, mil_m, alt_m))
            z1 = float(np.interp(s1, mil_m, alt_m))
            dz = z1 - z0
            grade_local_pct = (dz / ds) * 100.0 if ds > 0 else np.nan
            grade_error_pct = grade_local_pct - link_avg_grade_pct

            seg_rows.append({
                "i_start_seg": i_start,
                "i_end_seg": i_end,
                "s0_m": s0,
                "s1_m": s1,
                "segment_len_m": ds,
                "grade_local_pct": grade_local_pct,
                "grade_link_pct": link_avg_grade_pct,
                "grade_error_pct": grade_error_pct,
            })

            s = s1

        return pd.DataFrame(seg_rows)

    def evaluate_network_with_matches(self, network_path: str):
        """
        Evaluiert ein MATSim-Netzwerk gegen die Messdaten.
        Gibt (metrics, matches_df, skipped_df) zurück.

        Die Steigungs-Metriken (MAE/RMSE) werden auf Basis von ~5m-Segmenten
        entlang der Messfahrt berechnet, um Auslöschungseffekte innerhalb
        langer Links zu vermeiden.
        """
        from shapely.geometry import Point, LineString
        import numpy as np
        import pandas as pd
        import os

        params = self.params
        nodes_gdf, links_df = load_matsim_network(
            network_path, params.network_input_crs, params.work_crs
        )

        # Netzwerk auf den räumlichen Bereich der Messdaten beschränken
        meas_buffer = _meas_buffer_polygon(self.meas, params.spatial_crop_buffer_m)
        nodes_gdf, links_df = _crop_network(nodes_gdf, links_df, meas_buffer)

        matches = []
        skipped = []

        for _, link in links_df.iterrows():
            link_len = float(link["length"])
            from_node = nodes_gdf.loc[nodes_gdf["id"] == link["from"]].iloc[0]
            to_node = nodes_gdf.loc[nodes_gdf["id"] == link["to"]].iloc[0]
            x1, y1 = from_node.geometry.x, from_node.geometry.y
            x2, y2 = to_node.geometry.x, to_node.geometry.y

            # Start-/Endmatch prüfen
            i_start, d_start = self._nearest_meas_idx_with_dist(x1, y1, params.node_match_tol_m)
            i_end, d_end = self._nearest_meas_idx_with_dist(x2, y2, params.node_match_tol_m)
            if i_start is None or i_end is None:
                skipped.append({"link_id": link["id"], "reason": "no_match"})
                continue
            if i_end <= i_start:
                skipped.append({"link_id": link["id"], "reason": "reverse_or_zero"})
                continue

            # Längenvergleich (Messfahrt vs. Linklänge)
            dS = abs(self._mil_km[i_end] - self._mil_km[i_start]) * 1000.0
            tol_m = _length_tolerance_m(link_len, params.abs_len_tol_m, params.rel_len_tol)
            if abs(dS - link_len) > tol_m:
                skipped.append({
                    "link_id": link["id"],
                    "reason": "len_tol_fail",
                    "link_len_m": link_len,
                    "dS_m": dS,
                    "tol_m": tol_m,
                    "i_start": i_start,
                    "i_end": i_end
                })
                continue

            # Höhen- und Steigungsberechnung (mittlere Steigung aus Messdaten über den Link)
            z1 = float(self._alt_m[i_start])
            z2 = float(self._alt_m[i_end])
            dz = z2 - z1
            grade_pct = (dz / link_len) * 100.0 if link_len > 0 else np.nan

            matches.append({
                "link_id": link["id"],
                "from": link["from"],
                "to": link["to"],
                "length_m": link_len,
                "dS_m": dS,
                "tol_m": tol_m,
                "dz_m": dz,
                "grade_pct": grade_pct,
                "i_start": i_start,
                "i_end": i_end,
                "start_x": x1, "start_y": y1,
                "end_x": x2, "end_y": y2,
            })

        matches_df = pd.DataFrame(matches)
        skipped_df = pd.DataFrame(skipped)

        if matches_df.empty:
            return NetworkMetrics(
                network_path=network_path,
                label=os.path.basename(network_path),
                total_length_m=0,
                mae_pct=np.nan,
                rmse_pct=np.nan,
                n_links_used=0,
                n_links_total=len(links_df)
            ), matches_df, skipped_df

        # --- 5-m-Segmentfehler berechnen ---
        segment_dfs = []
        for _, row in matches_df.iterrows():
            i_start = int(row["i_start"])
            i_end = int(row["i_end"])
            link_avg_grade_pct = float(row["grade_pct"])

            seg_df = self._segment_errors_along_link(
                i_start=i_start,
                i_end=i_end,
                link_avg_grade_pct=link_avg_grade_pct,
                segment_len_m=5.0,  # Delta = 5 m
            )
            if not seg_df.empty:
                seg_df["link_id"] = row["link_id"]
                seg_df["length_m_link"] = row["length_m"]
                segment_dfs.append(seg_df)

        if segment_dfs:
            segment_errors_df = pd.concat(segment_dfs, ignore_index=True)
        else:
            segment_errors_df = pd.DataFrame()

        # --- MAE / RMSE auf Basis der 5-m-Segmente ---
        if segment_errors_df.empty:
            # KEIN Fallback auf Link-Metrik -> explizit NaN und Hinweis
            print(f"[INFO] Keine gültigen 5m-Segmente für Netzwerk {os.path.basename(network_path)}. "
                  f"MAE/RMSE werden auf NaN gesetzt.")
            mae = float("nan")
            rmse = float("nan")
            total_length = 0.0
        else:
            w = segment_errors_df["segment_len_m"].to_numpy()
            errors = segment_errors_df["grade_error_pct"].to_numpy()
            if np.any(w):
                mae = np.average(np.abs(errors), weights=w)
                rmse = np.sqrt(np.average(errors ** 2, weights=w))
                total_length = float(w.sum())
            else:
                mae = float("nan")
                rmse = float("nan")
                total_length = 0.0

        metrics = NetworkMetrics(
            network_path=network_path,
            label=os.path.basename(network_path),
            total_length_m=total_length,
            mae_pct=mae,
            rmse_pct=rmse,
            n_links_used=len(matches_df),
            n_links_total=len(links_df)
        )
        return metrics, matches_df, skipped_df

# ---------------------------------------------------------------------
# Ergebnis-Zusammenfassung & Statistikdruck
# ---------------------------------------------------------------------

def summarize_results(metrics_list):
    """Fasst eine Liste von NetworkMetrics-Objekten zu einem DataFrame zusammen."""
    if not metrics_list:
        return pd.DataFrame(columns=[
            "label", "network_path", "total_length_m",
            "mae_pct", "rmse_pct", "n_links_used", "n_links_total"
        ])
    df = pd.DataFrame([{
        "label": m.label,
        "network_path": m.network_path,
        "total_length_m": m.total_length_m,
        "mae_pct": m.mae_pct,
        "rmse_pct": m.rmse_pct,
        "n_links_used": m.n_links_used,
        "n_links_total": m.n_links_total,
        "max_link_len_m": _extract_max_link_len_from_filename(m.network_path)
    } for m in metrics_list])
    return df


def print_overall_averages(df: pd.DataFrame):
    """Berechne und drucke den längengewichteten Gesamtmittelwert."""
    if df.empty:
        print("[WARN] Keine Ergebnisse für Gesamtmittel.")
        return
    w = df["total_length_m"].to_numpy()
    mae = np.average(df["mae_pct"].fillna(0), weights=w) if np.any(w) else float("nan")
    rmse = np.average(df["rmse_pct"].fillna(0), weights=w) if np.any(w) else float("nan")
    print("\n=== Längengewichtete Durchschnittswerte über alle Netzwerke ===")
    print(f"MAE:  {mae:.2f} %-Pkt")
    print(f"RMSE: {rmse:.2f} %-Pkt")
