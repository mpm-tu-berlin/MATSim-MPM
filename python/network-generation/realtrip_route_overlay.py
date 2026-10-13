# -*- coding: utf-8 -*-
"""Interaktiver Routen- und Hoehenvergleich der drei klassischen Realfahrten.

Erzeugt zwei HTML-Dateien (beide offline lesbar bis auf die Kartenkacheln):
  1. karte_realfahrten.html   Leaflet-Karte auf OpenStreetMap: reale
     Referenzstrecke gegen die im Deutschlandnetz gefundene Route, die Route
     nach seitlichem Abstand eingefaerbt. Fahrten einzeln zuschaltbar.
  2. hoehenprofil_realfahrten.html   Plotly: gemessenes gegen simuliertes
     Hoehenprofil ueber der Bogenlaenge plus Differenzspur, zoom- und
     verschiebbar, Fahrt ueber ein Auswahlmenue.

VERTRAULICH: liest aus dem Messexport ausschliesslich Mileage/Altitude, keine
Leistungsspalten; alle Ausgaben liegen unter dem gitignorten data/-Pfad und
duerfen nicht veroeffentlicht werden.

Aufruf:
  python realtrip_route_overlay.py --networks-dir data/realtrip_networks_v2_20260817_V3b
"""
import argparse
import gzip
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from importlib.util import spec_from_file_location, module_from_spec

_SCRIPT_DIR = Path(__file__).parent
_ROOT = _SCRIPT_DIR.parent.parent
EXCEL_PATH = _ROOT / "python" / "calibration" / "data" / "Geschwindigkeitsprofile.xlsx"
EXCEL_SUFFIX = {"19t": "19t", "24t": "25t", "43t": "43t"}
NETWORK_CRS = "EPSG:4839"

# Abstandsklassen fuer die Einfaerbung [m] und zugehoerige Farben
DEV_STEPS = [10.0, 30.0, 60.0, 150.0]
DEV_COLORS = ["#1a9850", "#a6d96a", "#fdae61", "#f46d43", "#d73027"]
DEV_LABELS = ["bis 10 m", "10 bis 30 m", "30 bis 60 m", "60 bis 150 m",
              "ueber 150 m (ausserhalb des Suchschlauchs)"]


def _eval_mod():
    spec = spec_from_file_location("rme", str(_SCRIPT_DIR / "realtrip_measured_eval.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chain_xyz(path):
    """Geordnete Knotenkette (xy, z, s) eines Routen-Subnetzes."""
    with gzip.open(path, "rb") as f:
        root = ET.parse(f).getroot()
    nodes = {n.get("id"): (float(n.get("x")), float(n.get("y")),
                           float(n.get("z")) if n.get("z") else np.nan)
             for n in root.find("nodes").findall("node")}
    adj = {}
    for l in root.find("links").findall("link"):
        u, v = l.get("from"), l.get("to")
        if u == v:
            continue
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    ends = [n for n, nb in adj.items() if len(nb) == 1]
    start = ends[0] if ends else next(iter(adj))
    order, prev = [start], None
    while True:
        nxt = [n for n in adj[order[-1]] if n != prev and n not in order]
        if not nxt:
            break
        prev = order[-1]
        order.append(sorted(nxt)[0])
    xy = np.array([[nodes[n][0], nodes[n][1]] for n in order], float)
    z = np.array([nodes[n][2] for n in order], float)
    s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    return xy, z, s


def densify(xy, step=25.0):
    out = [xy[0]]
    for a, b in zip(xy[:-1], xy[1:]):
        L = float(np.hypot(*(b - a)))
        if L <= step:
            out.append(b)
            continue
        k = int(np.ceil(L / step))
        for i in range(1, k + 1):
            out.append(a + (b - a) * (i / k))
    return np.array(out)


def lateral_deviation(route_xy, ref_xy):
    from scipy.spatial import cKDTree
    return cKDTree(densify(ref_xy)).query(route_xy)[0]


def to_wgs84(xy):
    tr = Transformer.from_crs(NETWORK_CRS, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(xy[:, 0], xy[:, 1])
    return np.column_stack([lat, lon])


def measured_profile(label):
    col = EXCEL_SUFFIX[label]
    m = pd.read_excel(EXCEL_PATH, usecols=["Mileage " + col, "Altitude " + col]).dropna()
    s = (m["Mileage " + col].values - m["Mileage " + col].values[0]) * 1000.0
    return s, m["Altitude " + col].values


def dev_class(d):
    for i, thr in enumerate(DEV_STEPS):
        if d <= thr:
            return i
    return len(DEV_STEPS)


def build_map_payload(label, route_xy, ref_xy, dev, s_route):
    """Referenzlinie, nach Abstandsklasse zerlegte Routensegmente, Kennzahlen."""
    ref_ll = to_wgs84(ref_xy)
    route_ll = to_wgs84(route_xy)

    # Zusammenhaengende Laeufe gleicher Abstandsklasse (ein Polyline-Objekt je Lauf)
    cls = [dev_class(d) for d in dev]
    runs, start = [], 0
    for i in range(1, len(cls) + 1):
        if i == len(cls) or cls[i] != cls[start]:
            a, b = start, min(i, len(cls) - 1)
            if b > a:
                runs.append({"c": cls[start],
                             "pts": [[round(p[0], 6), round(p[1], 6)]
                                     for p in route_ll[a:b + 1]],
                             "km0": round(s_route[a] / 1000.0, 2),
                             "km1": round(s_route[b] / 1000.0, 2)})
            start = i
    k = int(np.argmax(dev))
    return {
        "label": label,
        "ref": [[round(p[0], 6), round(p[1], 6)] for p in ref_ll],
        "runs": runs,
        "worst": {"lat": round(float(route_ll[k][0]), 6),
                  "lon": round(float(route_ll[k][1]), 6),
                  "dev": round(float(dev[k]), 1),
                  "km": round(float(s_route[k]) / 1000.0, 2)},
        "stats": {"median": round(float(np.median(dev)), 1),
                  "p90": round(float(np.percentile(dev, 90)), 1),
                  "max": round(float(dev.max()), 1),
                  "km": round(float(s_route[-1]) / 1000.0, 1),
                  "share_gt150": round(float((dev > 150).mean() * 100), 2)},
    }


MAP_TEMPLATE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Realfahrten: reale Strecke gegen gefundene Netzroute</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { height:100%; }
  .panel { position:absolute; top:10px; right:10px; z-index:1000; background:#fff;
           padding:10px 12px; border-radius:6px; box-shadow:0 1px 6px rgba(0,0,0,.35);
           font-size:13px; max-width:290px; }
  .panel h3 { margin:0 0 6px; font-size:14px; }
  .panel table { border-collapse:collapse; margin-top:6px; }
  .panel td { padding:1px 6px 1px 0; }
  .swatch { display:inline-block; width:22px; height:4px; vertical-align:middle;
            margin-right:6px; }
  label { display:block; margin:2px 0; cursor:pointer; }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h3>Realfahrten</h3>
  <div id="trips"></div>
  <div id="stats"></div>
  <hr>
  <div><b>Seitlicher Abstand</b><br>__LEGEND__</div>
  <div style="margin-top:6px"><span class="swatch" style="background:#555;height:6px"></span>reale Strecke</div>
</div>
<script>
const DATA = __DATA__;
const COLORS = __COLORS__;
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap-Mitwirkende'
}).addTo(map);

const layers = {}, bounds = {};
DATA.forEach(t => {
  const g = L.layerGroup();
  L.polyline(t.ref, {color:'#444', weight:7, opacity:0.45}).addTo(g)
    .bindPopup('reale Strecke (Referenz), ' + t.label);
  t.runs.forEach(r => {
    L.polyline(r.pts, {color: COLORS[r.c], weight:3.5, opacity:0.95}).addTo(g)
      .bindPopup(t.label + ': km ' + r.km0 + ' bis ' + r.km1);
  });
  L.circleMarker([t.worst.lat, t.worst.lon], {radius:7, color:'#d73027',
     weight:3, fillOpacity:0.2}).addTo(g)
    .bindPopup(t.label + ': groesster Abstand ' + t.worst.dev + ' m bei km ' + t.worst.km);
  layers[t.label] = g;
  bounds[t.label] = L.polyline(t.ref).getBounds();
});

const tripsDiv = document.getElementById('trips');
DATA.forEach((t, i) => {
  const id = 'cb_' + t.label;
  tripsDiv.insertAdjacentHTML('beforeend',
    '<label><input type="checkbox" id="' + id + '"' + (i === 0 ? ' checked' : '') +
    '> ' + t.label + ' (' + t.stats.km + ' km)</label>');
});
function refresh() {
  let b = null;
  DATA.forEach(t => {
    const on = document.getElementById('cb_' + t.label).checked;
    if (on) { layers[t.label].addTo(map); b = b ? b.extend(bounds[t.label]) : L.latLngBounds(bounds[t.label]); }
    else map.removeLayer(layers[t.label]);
  });
  const rows = DATA.filter(t => document.getElementById('cb_' + t.label).checked)
    .map(t => '<tr><td><b>' + t.label + '</b></td><td>Median ' + t.stats.median +
              ' m</td><td>p90 ' + t.stats.p90 + ' m</td><td>max ' + t.stats.max +
              ' m</td></tr>').join('');
  document.getElementById('stats').innerHTML = rows ? '<table>' + rows + '</table>' : '';
  if (b) map.fitBounds(b.pad(0.05));
}
DATA.forEach(t => document.getElementById('cb_' + t.label)
  .addEventListener('change', refresh));
refresh();
</script>
</body>
</html>
"""


def write_map(payloads, out_path):
    legend = "".join(
        '<div><span class="swatch" style="background:%s"></span>%s</div>' % (c, l)
        for c, l in zip(DEV_COLORS, DEV_LABELS))
    html = (MAP_TEMPLATE
            .replace("__DATA__", json.dumps(payloads))
            .replace("__COLORS__", json.dumps(DEV_COLORS))
            .replace("__LEGEND__", legend))
    out_path.write_text(html, encoding="utf-8")


def elevation_series(label, s_net, z_net, s_meas, z_meas, align):
    direction, offset, scale, corr = align
    if direction == -1:
        s_m, z_m = s_meas[-1] - s_meas[::-1], z_meas[::-1]
    else:
        s_m, z_m = s_meas, z_meas
    s_m = s_m * scale + offset
    grid = np.arange(0, s_net[-1], 25.0)
    zn = np.interp(grid, s_net, z_net)
    zm = np.interp(grid, s_m, z_m)
    dz = zn - zm
    bias = float(np.median(dz))
    return {"km": grid / 1000.0, "z_net": zn, "z_meas": zm + bias, "d": dz - bias,
            "bias": bias, "corr": float(corr), "direction": int(direction),
            "scale": float(scale),
            "mae": float(np.mean(np.abs(dz - bias))),
            "rmse": float(np.sqrt(np.mean((dz - bias) ** 2)))}


def write_elevation(series, out_path):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    labels = list(series)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        row_heights=[0.68, 0.32],
                        subplot_titles=("Hoehe ueber Bogenlaenge",
                                        "Differenz Netz minus Messung"))
    for i, lab in enumerate(labels):
        s, vis = series[lab], (i == 0)
        fig.add_trace(go.Scatter(x=s["km"], y=s["z_meas"], name="gemessen",
                                 line=dict(color="#555", width=1.6), visible=vis,
                                 hovertemplate="km %{x:.2f}<br>%{y:.1f} m<extra>gemessen</extra>"),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=s["km"], y=s["z_net"], name="Netz (V3)",
                                 line=dict(color="#1f77b4", width=1.2), visible=vis,
                                 hovertemplate="km %{x:.2f}<br>%{y:.1f} m<extra>Netz</extra>"),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=s["km"], y=s["d"], name="Differenz",
                                 line=dict(color="#d62728", width=1.0), visible=vis,
                                 hovertemplate="km %{x:.2f}<br>%{y:+.2f} m<extra>Differenz</extra>"),
                      row=2, col=1)

    def title(lab):
        s = series[lab]
        return ("Realfahrt " + lab + ": Hoehenprofil, MAE " + format(s["mae"], ".2f") +
                " m, RMSE " + format(s["rmse"], ".2f") + " m, Hoehenversatz " +
                format(s["bias"], "+.1f") + " m entfernt, Korrelation " +
                format(s["corr"], ".3f"))

    buttons = []
    for i, lab in enumerate(labels):
        vis = [False] * (3 * len(labels))
        vis[3 * i:3 * i + 3] = [True] * 3
        buttons.append(dict(label=lab, method="update",
                            args=[{"visible": vis}, {"title": title(lab)}]))

    fig.update_layout(
        title=title(labels[0]),
        updatemenus=[dict(active=0, buttons=buttons, x=1.0, xanchor="right",
                          y=1.14, yanchor="top", direction="right")],
        hovermode="x unified", height=760, template="plotly_white",
        legend=dict(orientation="h", y=1.06, x=0))
    fig.update_xaxes(title_text="Bogenlaenge der Netzroute [km]", row=2, col=1,
                     rangeslider=dict(visible=True, thickness=0.05))
    fig.update_yaxes(title_text="Hoehe [m]", row=1, col=1)
    fig.update_yaxes(title_text="Netz - Messung [m]", row=2, col=1)
    fig.write_html(str(out_path), include_plotlyjs=True, full_html=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--networks-dir", default="data/realtrip_networks_v2_20260817_V3b")
    ap.add_argument("--refs-dir", default="data/realtrip_refs_v2")
    ap.add_argument("--resolution", type=int, default=250)
    ap.add_argument("--trips", default="19t,24t,43t")
    ap.add_argument("--outdir", default="data/realtrip_elevation")
    a = ap.parse_args()

    net_dir, ref_dir = _SCRIPT_DIR / a.networks_dir, _SCRIPT_DIR / a.refs_dir
    outdir = _SCRIPT_DIR / a.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    rme = _eval_mod()

    payloads, series, rows = [], {}, []
    for label in [t.strip() for t in a.trips.split(",")]:
        net = net_dir / ("section_" + label + "_" + str(a.resolution) + "m.xml.gz")
        ref = ref_dir / ("section_" + label + "_100km.xml.gz")
        if not net.exists() or not ref.exists():
            print("  WARNING: " + label + ": Netz oder Referenz fehlt, uebersprungen.")
            continue
        route_xy, z_net, s_net = chain_xyz(net)
        ref_xy, _, _ = chain_xyz(ref)
        dev = lateral_deviation(route_xy, ref_xy)
        payloads.append(build_map_payload(label, route_xy, ref_xy, dev, s_net))

        s_meas, z_meas = measured_profile(label)
        series[label] = elevation_series(
            label, s_net, z_net, s_meas, z_meas,
            rme.align_profiles(s_net, z_net, s_meas, z_meas))
        st = payloads[-1]["stats"]
        rows.append({"trip": label, "laenge_km": st["km"], "abstand_median_m": st["median"],
                     "abstand_p90_m": st["p90"], "abstand_max_m": st["max"],
                     "anteil_ueber_150m_pct": st["share_gt150"],
                     "hoehen_mae_m": round(series[label]["mae"], 3),
                     "hoehen_rmse_m": round(series[label]["rmse"], 3)})

    if not payloads:
        raise SystemExit("Keine Route gefunden.")

    map_path = outdir / "karte_realfahrten.html"
    elev_path = outdir / "hoehenprofil_realfahrten.html"
    write_map(payloads, map_path)
    write_elevation(series, elev_path)
    df = pd.DataFrame(rows)
    df.to_csv(outdir / "route_overlay_metrics.csv", index=False)
    print(df.to_string(index=False))
    print("\n" + str(map_path) + "\n" + str(elev_path))


if __name__ == "__main__":
    main()
