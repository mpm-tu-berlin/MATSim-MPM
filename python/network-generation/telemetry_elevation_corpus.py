# -*- coding: utf-8 -*-
"""Höhen-Korpus aus der gesamten HoLa-Telemetrie.

Zweck: Ground Truth für die Netzhöhen NICHT nur auf den elf Energie-Routen,
sondern auf allen verfügbaren Fahrten. Für einen Höhenvergleich sind die
Auswahlkriterien der Energievalidierung (massenkonstant, stichfahrtenfrei,
ladefrei) irrelevant; Mehrfachbefahrungen derselben Bauwerke sind sogar
erwünscht, weil sich GPS-Rauschen über die Wiederholungen wegmittelt.

Gelesen werden AUSSCHLIESSLICH Geo-/Höhen-/Weg-Spalten:
  vehicle_id, signal_ts, Latitude, Longitude, Altitude, Velocity, Mileage
Leistungs-, SOC-, Temperatur- und Massenspalten werden nie angefasst.

Verarbeitung je Wochendatei:
  1. Zeilen ohne Lat/Lon/Alt verwerfen
  2. nach Fahrzeug und Zeit sortieren, Fahrten an Zeitlücken > GAP_MIN trennen
  3. Bogenlänge aus projizierten Koordinaten (EPSG:4839) aufsummieren
  4. auf >= STEP_M Punktabstand ausdünnen (Standzeiten fallen dabei weg)
  5. Fahrten unter MIN_TRIP_KM verwerfen

Ausgabe (gitignored): data/telemetry_elev_corpus/corpus.parquet mit
trip_id, vehicle, ts, s_m, lat, lon, alt_m  plus trips.csv als Übersicht.
stdout nur Aggregate.

Aufruf:  python telemetry_elevation_corpus.py [--limit-files N]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pyproj import Transformer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TELEMETRY_DIR = Path((Path(__file__).resolve().parent / "data" /
                      "telemetry_dir.txt").read_text(encoding="utf-8").strip())
OUT_DIR = Path(__file__).parent / "data" / "telemetry_elev_corpus"

COLS = ["vehicle_id", "signal_ts", "Latitude", "Longitude", "Altitude",
        "Velocity", "Mileage"]
GAP_MIN = 15.0        # Zeitlücke, ab der eine neue Fahrt beginnt [min]
STEP_M = 20.0         # Ziel-Punktabstand entlang der Fahrt
MIN_TRIP_KM = 5.0     # kürzere Fahrten bringen für Bauwerke nichts
MAX_JUMP_M = 2000.0   # Positionssprung -> Fahrt trennen (GPS-Ausfall)


def thin_by_distance(x, y, step):
    """Indizes eines Teilzugs mit >= step Abstand (erster/letzter Punkt immer)."""
    keep = [0]
    lx, ly = x[0], y[0]
    for i in range(1, len(x)):
        if (x[i] - lx) ** 2 + (y[i] - ly) ** 2 >= step * step:
            keep.append(i)
            lx, ly = x[i], y[i]
    if keep[-1] != len(x) - 1:
        keep.append(len(x) - 1)
    return np.asarray(keep, int)


def process_file(path, tf, trip_counter):
    t = pq.read_table(path, columns=COLS).to_pandas()
    t = t.dropna(subset=["Latitude", "Longitude", "Altitude", "signal_ts"])
    if t.empty:
        return None, None, trip_counter
    out_rows, trip_rows = [], []
    for veh, g in t.groupby("vehicle_id", sort=True):
        g = g.sort_values("signal_ts")
        lon = g["Longitude"].to_numpy(float)
        lat = g["Latitude"].to_numpy(float)
        alt = g["Altitude"].to_numpy(float)
        ts = g["signal_ts"].to_numpy()
        x, y = tf.transform(lon, lat)
        x = np.asarray(x, float); y = np.asarray(y, float)

        dt_min = np.diff(ts).astype("timedelta64[s]").astype(float) / 60.0
        jump = np.hypot(np.diff(x), np.diff(y))
        cut = np.flatnonzero((dt_min > GAP_MIN) | (jump > MAX_JUMP_M)) + 1
        bounds = np.concatenate([[0], cut, [len(x)]])

        for b0, b1 in zip(bounds[:-1], bounds[1:]):
            if b1 - b0 < 20:
                continue
            xs, ys = x[b0:b1], y[b0:b1]
            idx = thin_by_distance(xs, ys, STEP_M)
            if len(idx) < 20:
                continue
            xk, yk = xs[idx], ys[idx]
            s = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xk), np.diff(yk)))])
            if s[-1] < MIN_TRIP_KM * 1000.0:
                continue
            trip_counter += 1
            tid = f"t{trip_counter:06d}"
            out_rows.append(pd.DataFrame({
                "trip_id": tid, "vehicle": str(veh),
                "ts": ts[b0:b1][idx], "s_m": s,
                "lat": lat[b0:b1][idx], "lon": lon[b0:b1][idx],
                "alt_m": alt[b0:b1][idx]}))
            trip_rows.append({"trip_id": tid, "vehicle": str(veh),
                              "n_points": len(idx), "km": s[-1] / 1000.0,
                              "t_start": pd.Timestamp(ts[b0]),
                              "t_end": pd.Timestamp(ts[b1 - 1])})
    if not out_rows:
        return None, None, trip_counter
    return pd.concat(out_rows, ignore_index=True), pd.DataFrame(trip_rows), trip_counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-files", type=int, default=0)
    args = ap.parse_args()

    files = sorted(TELEMETRY_DIR.glob("*.parquet"))
    if args.limit_files:
        files = files[:args.limit_files]
    print(f"{len(files)} Wochendateien", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tf = Transformer.from_crs("EPSG:4326", "EPSG:4839", always_xy=True)

    parts, trips, counter = [], [], 0
    for i, f in enumerate(files, 1):
        try:
            p, tr, counter = process_file(f, tf, counter)
        except Exception as e:
            print(f"  {f.name}: FEHLER {type(e).__name__}: {e}", flush=True)
            continue
        if p is not None:
            parts.append(p); trips.append(tr)
        if i % 20 == 0 or i == len(files):
            km = sum(t["km"].sum() for t in trips) if trips else 0.0
            print(f"  {i}/{len(files)} Dateien, {counter} Fahrten, {km:,.0f} km",
                  flush=True)

    if not parts:
        print("keine Daten")
        return
    corpus = pd.concat(parts, ignore_index=True)
    T = pd.concat(trips, ignore_index=True)
    corpus.to_parquet(OUT_DIR / "corpus.parquet", index=False)
    T.to_csv(OUT_DIR / "trips.csv", index=False, encoding="utf-8")

    print("\n== Korpus ==")
    print(f"  Fahrten:      {len(T):,}")
    print(f"  Punkte:       {len(corpus):,}")
    print(f"  Fahrstrecke:  {T['km'].sum():,.0f} km")
    print(f"  Zeitraum:     {T['t_start'].min().date()} bis {T['t_end'].max().date()}")
    print(f"  Fahrzeuge:    {T['vehicle'].nunique()}")
    print(f"  Fahrtlänge:   median {T['km'].median():.1f} km, "
          f"p90 {T['km'].quantile(0.9):.1f} km, max {T['km'].max():.1f} km")
    print(f"  Höhenbereich: {corpus['alt_m'].min():.0f} bis {corpus['alt_m'].max():.0f} m")
    print(f"\nAusgabe: {OUT_DIR} (gitignored)")


if __name__ == "__main__":
    main()
