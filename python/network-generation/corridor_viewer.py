# -*- coding: utf-8 -*-
"""Korridor-Viewer: Original-Telemetrie-Hoehenprofile gegen das Hoehennetz.

Fuer die schlechtesten Defekt-Korridore einer network_elevation_vs_telemetry-
Auswertung (defects.csv) wird je Korridor ein zoombares Hoehenprofil gebaut:

  - gemessene Fahrt (Traeger-Trip des Defekts), GPS-Offset je Fenster entfernt
    (Median gegen die V3-Referenz ausserhalb von Bauwerken)
  - weitere Fahrten desselben Korridors (Wiederholungs-Passagen, duenn grau)
  - dichtes Fahrbahnprofil V3 (Halbpixel-Fix) und V2 (alt) zum Vergleich
  - Bauwerks-Spannen (Bruecke/Tunnel aus den Detail-Kanten) schattiert

Dazu eine Leaflet-Karte mit den Korridor-Positionen (Rang = Dropdown-Name).

VERTRAULICH: Roh-GPS bleibt im gitignorten data/-Ordner; die HTMLs zeigen
Hoehenprofile realer Fahrten und duerfen nicht veroeffentlicht werden.

Aufruf:
  python corridor_viewer.py --eval-dir data/network_elev_vs_telemetry/net_V3_<stamp> \
      [--top 30] [--window-km 5]
"""
import argparse
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree
from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent
_NETGEN = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
CORPUS = _SCRIPT_DIR / "data" / "telemetry_elev_corpus" / "corpus.parquet"
DENSE_V3 = _NETGEN / "data" / "germany_dense_heights_V3.csv"
DENSE_V2 = _NETGEN / "data" / "germany_dense_heights_V2.zip"
MATCH_M = 15.0
STRUCT_M = 12.0
TR = Transformer.from_crs("EPSG:4326", "EPSG:4839", always_xy=True)


def _nev():
    spec = spec_from_file_location("nev", str(_SCRIPT_DIR / "network_elevation_vs_telemetry.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dense(path):
    """(x, y, z) in EPSG:4839 aus einem dichten Fahrbahnprofil (lon,lat,z)."""
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = [n for n in z.namelist() if n.endswith(".csv")][0]
            with z.open(name) as f:
                d = pd.read_csv(f)
    else:
        d = pd.read_csv(path)
    cl = {c.lower(): c for c in d.columns}
    lon, lat, z = d[cl["lon"]].values, d[cl["lat"]].values, d[cl["z"]].values
    x, y = TR.transform(lon, lat)
    return np.asarray(x), np.asarray(y), np.asarray(z, float)


def offset_free(alt, ref_z, on_struct):
    """GPS-/Antennen-Offset: Median(alt - ref) ausserhalb von Bauwerken."""
    ok = np.isfinite(ref_z) & ~on_struct
    if ok.sum() < 10:
        ok = np.isfinite(ref_z)
    if ok.sum() == 0:
        return 0.0
    return float(np.median(alt[ok] - ref_z[ok]))


def spans_from_mask(s, mask, min_len=15.0):
    """Zusammenhaengende s-Spannen, in denen mask gilt."""
    out, start = [], None
    for i in range(len(s)):
        if mask[i] and start is None:
            start = s[i]
        elif not mask[i] and start is not None:
            if s[i - 1] - start >= min_len:
                out.append((start, s[i - 1]))
            start = None
    if start is not None and s[-1] - start >= min_len:
        out.append((start, s[-1]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-dir", required=True,
                    help="Ausgabeordner einer network_elevation_vs_telemetry-Auswertung")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--window-km", type=float, default=5.0)
    ap.add_argument("--fix-overlay", type=str, default=None,
                    help="fix_overlay.parquet aus viewer_fix_overlay.py — haengt "
                         "das Profil MIT Bauwerks-Fix als weitere Linie ein")
    ap.add_argument("--dedup-km", type=float, default=2.0,
                    help="Defekte innerhalb dieses Radius zaehlen als derselbe Korridor")
    ap.add_argument("--min-defect-trips", type=int, default=2,
                    help="Pass-Konsistenz: mindestens so viele Fahrten muessen den "
                         "Defekt gleichsinnig zeigen (0 = aus; filtert "
                         "Fahrzeugfehler heraus, Befund 2026-08-18)")
    a = ap.parse_args()
    ev = Path(a.eval_dir) if Path(a.eval_dir).is_absolute() else _SCRIPT_DIR / a.eval_dir

    df = pd.read_csv(ev / "defects.csv")
    if "dev_consens_m" in df.columns:
        # Ranggroesse = Konsens ueber alle Fahrten, nicht Einzelfahrt-Maximum
        df = df.sort_values("dev_consens_m", key=lambda v: v.abs(),
                            ascending=False)
    else:
        df = df.sort_values("dev_max_abs_m", ascending=False)
    if a.min_defect_trips > 0 and "n_trips_defect" in df.columns:
        n0 = len(df)
        df = df[df.n_trips_defect >= a.min_defect_trips]
        print(f"Pass-Konsistenz: {len(df)}/{n0} Defekte mit >= "
              f"{a.min_defect_trips} gleichsinnigen Fahrten")
    # Dedup ueber Ortsraster
    grid = a.dedup_km * 1000.0
    df["cell"] = (df.x // grid).astype(int).astype(str) + "_" + (df.y // grid).astype(int).astype(str)
    df = df.drop_duplicates("cell").head(a.top).reset_index(drop=True)
    print(f"{len(df)} Korridore (Top nach |Abweichung|, dedupliziert {a.dedup_km} km)")

    print("Lade Referenzen ...", flush=True)
    x3, y3, z3 = load_dense(DENSE_V3)
    t3 = cKDTree(np.column_stack([x3, y3]))
    x2, y2, z2 = load_dense(DENSE_V2)
    t2 = cKDTree(np.column_stack([x2, y2]))

    nev = _nev()
    sx, sy, sb, sk = nev.load_structure_segments()
    ts = cKDTree(np.column_stack([sx, sy]))

    print("Lade Korpus ...", flush=True)
    corpus = pd.read_parquet(CORPUS)
    cx, cy = TR.transform(corpus.lon.values, corpus.lat.values)
    corpus["x"], corpus["y"] = cx, cy
    tcorp = cKDTree(np.column_stack([cx, cy]))

    win = a.window_km * 1000.0
    figs = []
    for rank, row in df.iterrows():
        trip = corpus[corpus.trip_id == row.trip_id].sort_values("s_m")
        lo, hi = row.s0_m - win, row.s1_m + win
        w = trip[(trip.s_m >= lo) & (trip.s_m <= hi)].reset_index(drop=True)
        if len(w) < 30:
            print(f"  K{rank+1:02d}: zu wenige Punkte — uebersprungen")
            continue
        pxy = np.column_stack([w.x.values, w.y.values])
        d3, i3 = t3.query(pxy); d2, i2 = t2.query(pxy); ds, is_ = ts.query(pxy)
        zt3 = np.where(d3 <= MATCH_M, z3[i3], np.nan)
        zt2 = np.where(d2 <= MATCH_M, z2[i2], np.nan)
        on_s = ds <= STRUCT_M
        kind = np.where(on_s, sk[is_], "")
        off = offset_free(w.alt_m.values, zt3, on_s)
        s_km = (w.s_m.values - row.s0_m) / 1000.0

        # Wiederholungs-Passagen anderer Fahrten im Korridor
        others = {}
        near = set()
        for idx in tcorp.query_ball_point(pxy[:: max(1, len(pxy) // 200)], MATCH_M):
            near.update(idx)
        near = corpus.iloc[sorted(near)]
        near = near[near.trip_id != row.trip_id]
        # Projektion: naechster Traeger-Punkt bestimmt die s-Position
        ttrip = cKDTree(pxy)
        for tid, g in near.groupby("trip_id"):
            if len(g) < 30:
                continue
            dg, ig = ttrip.query(np.column_stack([g.x.values, g.y.values]))
            keep = dg <= MATCH_M
            if keep.sum() < 30:
                continue
            gs = s_km[ig[keep]]
            gz3 = zt3[ig[keep]]
            galt = g.alt_m.values[keep]
            o = offset_free(galt, gz3, on_s[ig[keep]])
            order = np.argsort(gs)
            others[tid] = (gs[order], (galt - o)[order])
            if len(others) >= 6:
                break

        figs.append({
            "name": f"K{rank+1:02d} {row.lat:.3f}/{row.lon:.3f} (max {row.dev_max_abs_m:.1f} m)",
            "lat": float(row.lat), "lon": float(row.lon),
            "dev": float(row.dev_max_abs_m),
            "s": s_km, "meas": w.alt_m.values - off,
            "v3": zt3, "v2": zt2,
            "bridge": spans_from_mask(w.s_m.values, on_s & (kind == "bridge")),
            "tunnel": spans_from_mask(w.s_m.values, on_s & (kind == "tunnel")),
            "s0": row.s0_m, "others": others, "trip_s": w.s_m.values,
        })
        print(f"  {figs[-1]['name']}: {len(w)} Punkte, {len(others)} weitere Passagen")

    overlay = None
    if a.fix_overlay:
        overlay = pd.read_parquet(a.fix_overlay)
        print(f"Fix-Overlay: {len(overlay)} Punktzeilen aus {a.fix_overlay}")

    # --- Plotly-HTML mit Dropdown ---
    import plotly.graph_objects as go
    fig = go.Figure()
    ntr = []
    for k, F in enumerate(figs):
        vis = (k == 0)
        tr0 = len(fig.data)
        for tid, (gs, gz) in F["others"].items():
            fig.add_trace(go.Scatter(x=gs, y=gz, mode="lines", visible=vis,
                                     line=dict(color="rgba(120,120,120,0.5)", width=1),
                                     name="weitere Passage", showlegend=False,
                                     hovertemplate="km %{x:.2f}<br>%{y:.1f} m<extra>" + str(tid) + "</extra>"))
        fig.add_trace(go.Scatter(x=F["s"], y=F["meas"], mode="lines", visible=vis,
                                 line=dict(color="#333", width=2), name="gemessen (offsetfrei)"))
        fig.add_trace(go.Scatter(x=F["s"], y=F["v2"], mode="lines", visible=vis,
                                 line=dict(color="#e08214", width=1.4), name="Netzprofil V2"))
        fig.add_trace(go.Scatter(x=F["s"], y=F["v3"], mode="lines", visible=vis,
                                 line=dict(color="#1f77b4", width=1.6), name="Netzprofil V3"))
        if overlay is not None:
            name_k = F["name"].split(" ")[0]
            ov = overlay[overlay.corridor == name_k].sort_values("s_m")
            if len(ov):
                zf = pd.Series(ov.z_fix.values, index=ov.s_m.values)
                zfit = zf.reindex(F["trip_s"]).values.astype(float)
                fig.add_trace(go.Scatter(x=F["s"], y=zfit, mode="lines", visible=vis,
                                         line=dict(color="#2ca02c", width=1.6, dash="dot"),
                                         name="V3 + Bauwerks-Fix (adaptiv)"))
        ntr.append((tr0, len(fig.data)))

    def shapes_for(F):
        sh = []
        for b0, b1 in F["bridge"]:
            sh.append(dict(type="rect", xref="x", yref="paper",
                           x0=(b0 - F["s0"]) / 1000.0, x1=(b1 - F["s0"]) / 1000.0,
                           y0=0, y1=1, fillcolor="rgba(31,119,180,0.10)", line_width=0))
        for b0, b1 in F["tunnel"]:
            sh.append(dict(type="rect", xref="x", yref="paper",
                           x0=(b0 - F["s0"]) / 1000.0, x1=(b1 - F["s0"]) / 1000.0,
                           y0=0, y1=1, fillcolor="rgba(140,86,75,0.15)", line_width=0))
        return sh

    buttons = []
    total = len(fig.data)
    for k, F in enumerate(figs):
        vis = [False] * total
        for i in range(*ntr[k]):
            vis[i] = True
        buttons.append(dict(label=F["name"], method="update",
                            args=[{"visible": vis},
                                  {"title": F["name"] + "  (blau schattiert = Bruecke, braun = Tunnel)",
                                   "shapes": shapes_for(F)}]))
    fig.update_layout(
        title=figs[0]["name"] + "  (blau schattiert = Bruecke, braun = Tunnel)",
        shapes=shapes_for(figs[0]),
        updatemenus=[dict(active=0, buttons=buttons, x=0, xanchor="left", y=1.15, yanchor="top")],
        xaxis_title="Bogenlaenge um den Defekt [km]", yaxis_title="Hoehe [m]",
        template="plotly_white", height=680, hovermode="x unified",
        legend=dict(orientation="h", y=1.06))
    out_html = ev / "corridor_viewer.html"
    fig.write_html(str(out_html), include_plotlyjs=True)

    # --- Leaflet-Uebersichtskarte ---
    markers = [{"name": F["name"], "lat": F["lat"], "lon": F["lon"], "dev": F["dev"]} for F in figs]
    map_html = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>Defekt-Korridore</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{margin:0;height:100%}</style></head><body><div id="m"></div>
<script>
const M = __MARKERS__;
const map = L.map('m');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; OpenStreetMap'}).addTo(map);
const g = L.featureGroup(M.map(d => L.circleMarker([d.lat, d.lon], {
  radius: 6 + Math.min(8, d.dev / 4), color: d.dev > 15 ? '#d73027' : d.dev > 8 ? '#fc8d59' : '#fee08b',
  fillOpacity: 0.7}).bindPopup(d.name + '<br>max ' + d.dev.toFixed(1) + ' m')));
g.addTo(map); map.fitBounds(g.getBounds().pad(0.1));
</script></body></html>"""
    (ev / "corridor_karte.html").write_text(
        map_html.replace("__MARKERS__", json.dumps(markers)), encoding="utf-8")
    print(f"\n{out_html}\n{ev / 'corridor_karte.html'}")


if __name__ == "__main__":
    main()
