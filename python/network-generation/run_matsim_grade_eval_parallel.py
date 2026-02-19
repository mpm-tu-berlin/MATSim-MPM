import os
import multiprocessing as mp
import tkinter as tk
from tkinter import filedialog
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd
from matsim_grade_eval import (
    EvalParams,
    summarize_results,
    print_overall_averages,
    load_measurements_parquet,
    GradeEvaluator,
)

def select_paths_gui():
    root = tk.Tk(); root.withdraw()
    parquet_path = filedialog.askopenfilename(
        title="Parquet-Datei mit Messdaten auswählen",
        filetypes=[("Parquet files", "*.parquet"), ("Alle Dateien", "*.*")]
    )
    if not parquet_path:
        raise SystemExit("Keine Parquet-Datei gewählt.")
    network_paths = filedialog.askopenfilenames(
        title="Eine oder mehrere MATSim-Netzwerke auswählen (.xml/.xml.gz)",
        filetypes=[("MATSim network", "*.xml *.xml.gz"), ("Alle Dateien", "*.*")]
    )
    if not network_paths:
        raise SystemExit("Keine Netzwerk-Dateien gewählt.")
    root.destroy()
    return parquet_path, list(network_paths)

def _eval_one(args):
    parquet_path, net_path, params_dict = args
    params = EvalParams(**params_dict)
    gdf_meas = load_measurements_parquet(
        parquet_path,
        work_crs=params.work_crs,
        raw_crs=params.raw_crs,
        filter_zero_velocity=params.filter_zero_velocity,
        max_km_from_start=params.max_km_from_start,
    )
    evaluator = GradeEvaluator(gdf_meas, params)
    return evaluator.evaluate_network_with_matches(net_path)  # -> metrics, matches_df, skipped_df

def print_grouped_averages(df, group_col="label"):
    """
    Druckt für jede Gruppe (z.B. Netzwerk-Auflösung in `label`) die
    längengewichteten MAE/RMSE. Erkannt werden u.a. `mae_pct` und `rmse_pct`.
    """
    def find_col(df, candidates):
        for cand in candidates:
            lcand = cand.lower()
            for c in df.columns:
                if lcand in c.lower():
                    return c
        return None

    if group_col not in df.columns:
        print(f"[WARN] Gruppenspalte `{group_col}` nicht in Ergebnissen")
        return

    mae_col = find_col(df, ["mae_pct", "mae", "mean_abs", "mean_absolute", "meanabsolute"])
    rmse_col = find_col(df, ["rmse_pct", "rmse", "root_mean_square", "root_mean_sq"])
    length_cols = ["total_length_m", "length_m", "matched_length_m", "total_length"]
    wcol = next((c for c in length_cols if c in df.columns), None)

    if mae_col is None or rmse_col is None:
        print(f"[WARN] Spalten für MAE/RMSE nicht gefunden. Verfügbare Spalten: {df.columns.tolist()}")
        if mae_col:
            print(f"[INFO] Gefundene MAE-Spalte: {mae_col}")
        if rmse_col:
            print(f"[INFO] Gefundene RMSE-Spalte: {rmse_col}")
        return

    print(f"\n=== Längengewichtete Durchschnittswerte gruppiert nach `{group_col}` ===")
    for name, g in df.groupby(group_col):
        if wcol and wcol in g.columns:
            w = g[wcol].fillna(0)
            wsum = w.sum()
            if wsum > 0:
                mae = (g[mae_col] * w).sum() / wsum
                rmse = (g[rmse_col] * w).sum() / wsum
            else:
                mae = g[mae_col].mean()
                rmse = g[rmse_col].mean()
        else:
            mae = g[mae_col].mean()
            rmse = g[rmse_col].mean()

        print(f"{name}: MAE: {mae:.2f} %-Pkt RMSE: {rmse:.2f} %-Pkt")

import matplotlib.pyplot as plt
import numpy as np

def _find_col(df, candidates):
    for cand in candidates:
        lcand = cand.lower()
        for c in df.columns:
            if lcand in c.lower():
                return c
    return None

def compute_grouped_errors(df, group_col=None):
    """
    Liefert DataFrame mit Spalten: group, mae, rmse
    Erkannt werden verschiedene MAE/RMSE-Namen und mögliche Längen-Spalten
    für längengewichtete Mittelwerte.
    """
    # mögliche Gruppen-Spalten (zuerst direkte Kandidaten, dann generisch)
    group_candidates = [group_col] if group_col else []
    group_candidates += ["max_link_len_m", "max_link_length_m", "max_allowed_link_length_m",
                         "max_link_len", "max_link_length", "max_length_m", "max_km_from_start",
                         "max_length"]
    group_col_found = None
    for g in group_candidates:
        if g and g in df.columns:
            group_col_found = g
            break
    if group_col_found is None:
        # fallback: Versuch Spalten mit "max" und ("link" oder "length")
        for c in df.columns:
            lc = c.lower()
            if "max" in lc and ("link" in lc or "length" in lc):
                group_col_found = c
                break
    if group_col_found is None:
        return None

    mae_col = _find_col(df, ["mae_pct", "mae", "mean_abs", "mean_absolute", "meanabsolute"])
    rmse_col = _find_col(df, ["rmse_pct", "rmse", "root_mean_square", "root_mean_sq"])
    if mae_col is None or rmse_col is None:
        return None

    length_cols = ["total_length_m", "length_m", "matched_length_m", "total_length", "length"]
    wcol = next((c for c in length_cols if c in df.columns), None)

    rows = []
    for name, g in df.groupby(group_col_found):
        if wcol:
            w = g[wcol].fillna(0)
            wsum = w.sum()
            if wsum > 0:
                mae = (g[mae_col] * w).sum() / wsum
                rmse = (g[rmse_col] * w).sum() / wsum
            else:
                mae = g[mae_col].mean()
                rmse = g[rmse_col].mean()
        else:
            mae = g[mae_col].mean()
            rmse = g[rmse_col].mean()
        rows.append({"group": name, "mae": float(mae), "rmse": float(rmse)})
    df_summary = pd.DataFrame(rows)
    # sortiere numerisch wenn möglich
    try:
        df_summary["group_num"] = pd.to_numeric(df_summary["group"], errors="coerce")
        if df_summary["group_num"].notna().any():
            df_summary = df_summary.sort_values("group_num")
            df_summary = df_summary.drop(columns=["group_num"])
    except Exception:
        df_summary = df_summary.sort_values("group")
    return df_summary

def plot_errors_vs_group(df, parquet_path, group_col=None, save_name="errors_by_max_link_length.png"):
    """
    Erstellt eine horizontale Balkgrafik: y = Gruppen (max link length), x = Fehler (MAE und RMSE).
    Speichert PNG im Verzeichnis der Parquet-Datei.
    """
    df_sum = compute_grouped_errors(df, group_col=group_col)
    if df_sum is None or df_sum.empty:
        print("[WARN] Konnte gruppierte Fehler nicht berechnen (fehlende Spalten oder leere Gruppen).")
        return

    groups = df_sum["group"].astype(str).tolist()
    mae = df_sum["mae"].values
    rmse = df_sum["rmse"].values

    y = np.arange(len(groups))
    height = 0.35

    fig, ax = plt.subplots(figsize=(8, max(4, len(groups) * 0.5)))
    ax.barh(y - height/2, mae, height=height, color="tab:blue", label="MAE")
    ax.barh(y + height/2, rmse, height=height, color="tab:orange", label="RMSE")

    ax.set_yticks(y)
    ax.set_yticklabels(groups)
    ax.set_xlabel("Fehler (%-Pkt)")
    ax.set_ylabel("Max. erlaubte Linklänge (Gruppe)")
    ax.set_title("MAE / RMSE vs. maximale erlaubte Linklänge")
    ax.legend()
    ax.invert_yaxis()  # größte oben
    plt.tight_layout()

    out_dir = os.path.dirname(parquet_path) or "."
    save_path = os.path.join(out_dir, save_name)
    try:
        fig.savefig(save_path, dpi=150)
        plt.close(fig)
        print(f"[SAVED] Fehler-Grafik: {save_path}")
    except Exception as e:
        print(f"[ERR] Speichern der Grafik fehlgeschlagen: {e}")


def main():
    parquet_path, network_paths = select_paths_gui()
    print("\n✅ Eingaben:")
    print("Messdaten:", parquet_path)
    print("Netzwerke:")
    for n in network_paths:
        print("  •", n)

    params = EvalParams(
        node_match_tol_m=20.0,
        abs_len_tol_m=30.0,
        rel_len_tol=0.08,
        raw_crs="EPSG:4326",
        work_crs="EPSG:25833",
        network_input_crs="EPSG:4839",
        filter_zero_velocity=True,
        rel_spatial_tol=0.02,
        spatial_crop_buffer_m=20000.0,
        max_km_from_start=None,  # None = keine Beschränkung
    )

    max_workers = min(len(network_paths), os.cpu_count() or 1)
    params_dict = vars(params)

    tasks = [(parquet_path, p, params_dict) for p in network_paths]
    results = []
    bundles = []

    print(f"[Info] Starte parallele Auswertung von {len(tasks)} Netzwerken mit {max_workers} Prozessen ...")
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        fut_to_np = {ex.submit(_eval_one, t): t[1] for t in tasks}
        for fut in as_completed(fut_to_np):
            net_path = fut_to_np[fut]
            try:
                metrics, matches_df, skipped_df = fut.result()
                results.append(metrics)
                bundles.append((metrics.label, net_path, matches_df, skipped_df))
                print(f"[OK] {os.path.basename(net_path)} fertig")
            except Exception as e:
                print(f"[ERR] {os.path.basename(net_path)} -> {e}")

    # --- Ausgabe ---
    df_res = pd.DataFrame([vars(m) for m in results])
    print_grouped_averages(df_res, group_col="label")
    plot_errors_vs_group(df_res, parquet_path, group_col=None)

    out_dir = os.path.join(os.path.dirname(parquet_path),
                           os.path.splitext(os.path.basename(parquet_path))[0] + "__matches")
    os.makedirs(out_dir, exist_ok=True)

    for label, net_path, mdf, sdf in bundles:
        safe = str(label).replace(" ", "")
        mdf.to_csv(os.path.join(out_dir, f"matches__{safe}.csv"), index=False)
        sdf.to_csv(os.path.join(out_dir, f"skipped__{safe}.csv"), index=False)
        print(f"[SAVED] {label}: {len(mdf)} matches, {len(sdf)} skipped")

    create_interactive_map(parquet_path, bundles, params, out_dir)

def create_interactive_map(parquet_path, bundles, params, out_dir, map_filename="matches_map.html"):
    import folium
    import geopandas as gpd
    from shapely.geometry import LineString
    import itertools
    import os

    colors = ["red", "green", "purple", "orange", "cadetblue", "darkred", "darkblue", "black"]

    gdf_meas = load_measurements_parquet(
        parquet_path,
        work_crs=params.work_crs,
        raw_crs=params.raw_crs,
        filter_zero_velocity=params.filter_zero_velocity,
        max_km_from_start=params.max_km_from_start,
    )
    gdf_meas_wgs = gdf_meas.to_crs(params.raw_crs)

    track_coords = [(pt.y, pt.x) for pt in gdf_meas_wgs.geometry]

    if len(gdf_meas_wgs):
        center_lat = gdf_meas_wgs.geometry.y.mean()
        center_lon = gdf_meas_wgs.geometry.x.mean()
    else:
        center_lat, center_lon = 51.1657, 10.4515

    m = folium.Map(location=(center_lat, center_lon), tiles="OpenStreetMap", zoom_start=12)
    folium.TileLayer("OpenStreetMap").add_to(m)
    folium.PolyLine(track_coords, color="blue", weight=3, opacity=0.8, tooltip="Messfahrt").add_to(m)

    for (label, net_path, mdf, sdf), color in zip(bundles, itertools.cycle(colors)):
        if mdf is None or mdf.empty:
            continue

        lines = []
        popups = []
        for _, r in mdf.iterrows():
            try:
                ls = LineString([(float(r["start_x"]), float(r["start_y"])), (float(r["end_x"]), float(r["end_y"]))])
            except Exception:
                continue
            lines.append(ls)
            popup = f'link_id: {r.get("link_id")}<br>length_m: {r.get("length_m")}<br>grade_pct: {r.get("grade_pct"):.2f}'
            popups.append(popup)

        gdf_lines = gpd.GeoDataFrame({"popup": popups}, geometry=lines, crs=params.work_crs)
        try:
            gdf_lines_wgs = gdf_lines.to_crs(params.raw_crs)
        except Exception:
            continue

        fg = folium.FeatureGroup(name=str(label), show=False)
        for geom, popup in zip(gdf_lines_wgs.geometry, gdf_lines_wgs["popup"]):
            if geom is None or geom.is_empty:
                continue
            coords = []
            for c in geom.coords:
                if hasattr(c, "y") and hasattr(c, "x"):
                    lat, lon = c.y, c.x
                else:
                    lon = c[0]
                    lat = c[1]
                coords.append((lat, lon))
            folium.PolyLine(coords, color=color, weight=4, opacity=0.9, popup=folium.Popup(popup, max_width=300)).add_to(fg)

        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    os.makedirs(out_dir, exist_ok=True)
    map_path = os.path.join(out_dir, map_filename)
    m.save(map_path)
    print(f"[SAVED] Interaktive Karte: {map_path}")


if __name__ == "__main__":
    mp.freeze_support()
    main()
