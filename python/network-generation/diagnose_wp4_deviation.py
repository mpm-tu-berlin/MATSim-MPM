# -*- coding: utf-8 -*-
"""
WP4-Diagnose der Validierungs-Abweichungen (Stand 2026-07-07, User-Hinweise):

(A) SIGNAL-LATENZ: Die ~sekuendlichen Truck-Signale sind verzoegert/verzerrt
    (Batterie- und MotorPower zeitlich inkonsistent). Gemessen wird:
      A1 interner Lag Batterie vs. (Motor+HVAC+Aux) via Kreuzkorrelation
         ueber Zeilen-Shifts (25-m-Raster),
      A2 raeumlicher Lag Messleistung vs. Sim-Leistung (100-m-Bins),
      A3 Steigungsklassen-Bias VOR/NACH Shift-Korrektur — blaeht der Lag den
         Bergauf-Bias auf?
(B) LAG-ROBUSTE VERGLEICHE:
      B1 signierter Steigungsbias bergauf (Netzsteigung vs. Mess-Steigung aus
         dem Hoehenprofil, je Bin),
      B2 Anstiegs-Segmente (zusammenhaengend Netz-grade > 1 % ueber >= 1 km):
         SEGMENT-ENERGIEN Sim vs. Messung (Integrale sind latenz-unempfindlich).

Nur lh_high-Sims (besseres Set). Ausgaben NUR Aggregate; alle Zwischendaten
verbleiben im gitignorten networks-dir. Rohdaten werden nicht angezeigt.
"""

import argparse
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent
BIN_M = 100.0
CANONICAL_L = 250
CSET = "lh_high"
TRIPS = {"19t": "19t", "24t": "25t", "43t": "43t"}
POWER_SUFFIX = {"19t": "", "24t": ".1", "43t": ".2"}
CLIMB_MIN_GRADE = 0.01
CLIMB_MIN_LEN_M = 1000.0


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def xcorr_best_shift(a, b, max_shift):
    """Shift k (b um k nach rechts) mit maximaler Korrelation; (k, corr)."""
    best = (0, -2.0)
    for k in range(-max_shift, max_shift + 1):
        if k >= 0:
            x, y = a[k:], b[:len(b) - k if k else None]
        else:
            x, y = a[:k], b[-k:]
        m = min(len(x), len(y))
        x, y = x[:m], y[:m]
        v = np.isfinite(x) & np.isfinite(y)
        if v.sum() < 50:
            continue
        xx, yy = x[v] - x[v].mean(), y[v] - y[v].mean()
        den = xx.std() * yy.std()
        c = float((xx * yy).mean() / den) if den > 0 else -2
        if c > best[1]:
            best = (k, c)
    return best


def class_bias(p_sim, p_meas, g, shift_bins=0):
    """Bias Sim-Mess je Steigungsklasse, Messung optional um shift_bins verschoben."""
    pm = np.roll(p_meas, -shift_bins)  # Messung nach vorn ziehen (Lag rueckgaengig)
    if shift_bins > 0:
        pm[-shift_bins:] = np.nan
    elif shift_bins < 0:
        pm[:-shift_bins] = np.nan
    out = {}
    v = np.isfinite(p_sim) & np.isfinite(pm) & np.isfinite(g)
    for name, lo, hi in (("uphill", CLIMB_MIN_GRADE, np.inf),
                         ("flat", -CLIMB_MIN_GRADE, CLIMB_MIN_GRADE),
                         ("downhill", -np.inf, -CLIMB_MIN_GRADE)):
        sel = v & (g > lo) & (g <= hi)
        out[name] = float(np.mean(p_sim[sel] - pm[sel])) if sel.sum() > 5 else np.nan
    out["all"] = float(np.mean(p_sim[v] - pm[v]))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--networks-dir", type=str, required=True)
    parser.add_argument("--measurements", type=str,
                        default=str(_SCRIPT_DIR.parent / "calibration" / "data"
                                    / "Geschwindigkeitsprofile.xlsx"))
    args = parser.parse_args()
    net_dir = Path(args.networks_dir)

    rsea = _import_module("rsea", "run_section_energy_analysis.py")
    rpc = _import_module("rpc", "realtrip_power_comparison.py")
    meas = pd.read_excel(args.measurements)
    align_df = pd.read_csv(net_dir / "realtrip_profile_validation.csv")

    for trip, col in TRIPS.items():
        print(f"\n{'='*72}\n{trip}\n{'='*72}")
        al = align_df[(align_df.trip == trip)
                      & (align_df.max_link_length == CANONICAL_L)].iloc[0]
        sfx = POWER_SUFFIX[trip]

        m = meas[[f"Mileage {col}", f"Altitude {col}", f"Velocity {col}"]].copy()
        for c in ("BatteryPower", "HVACpower", "MotorPower", "PowerOtherAuxiliary"):
            m[c] = meas[f"{c}{sfx}"]
        m = m.dropna()
        s_meas = (m[f"Mileage {col}"].values - m[f"Mileage {col}"].values[0]) * 1000.0
        step_m = float(np.median(np.diff(s_meas)))

        # --- A1: interner Lag Batterie vs. Motor+HVAC+Aux (Zeilen-Shifts) ---
        batt = m["BatteryPower"].values
        sign = -1.0 if np.nansum(batt) < 0 else 1.0
        p_batt = sign * batt
        p_sum = m["MotorPower"].values + m["HVACpower"].values \
            + m["PowerOtherAuxiliary"].values
        k, c = xcorr_best_shift(p_batt, p_sum, max_shift=40)
        v_med = float(np.nanmedian(m[f"Velocity {col}"].values)) / 3.6
        print(f"A1 interner Lag Batterie vs. Motor+Aux: {k} Zeilen "
              f"= {k*step_m:.0f} m = {k*step_m/max(v_med,1):.1f} s "
              f"(corr {c:.3f}; corr@0: "
              f"{xcorr_best_shift(p_batt, p_sum, 0)[1]:.3f})")

        # --- Bins wie im Leistungsvergleich (Messung inkl. z fuer B1) ---
        p_drive = p_batt - m["HVACpower"].values - m["PowerOtherAuxiliary"].values
        z_meas = m[f"Altitude {col}"].values
        v_meas = m[f"Velocity {col}"].values / 3.6
        if al.direction == -1:
            s_mm = s_meas[-1] - s_meas[::-1]
            p_drive, z_meas, v_meas = p_drive[::-1], z_meas[::-1], v_meas[::-1]
        else:
            s_mm = s_meas
        s_mm = s_mm * al.scale + al.offset_m

        net_path = net_dir / f"section_{trip}_{CANONICAL_L}m_realspeed.xml.gz"
        arc_by_link, total_len = rpc.load_chain(net_path)
        run_dir = net_dir / "validation_sims" / f"{trip}_{CANONICAL_L}m_{CSET}"
        fwd = rsea.get_forward_links_per_vehicle(net_path, run_dir / "resistance_debug.csv")
        ddf = pd.read_csv(run_dir / "resistance_debug.csv")
        ddf["linkId"] = ddf["linkId"].astype(str)
        vid, fwd_ids = next(iter(fwd.items()))
        vdf = (ddf[ddf.vehicleId == vid].set_index("linkId")
               .reindex(fwd_ids).dropna(how="all"))

        bins = np.arange(0.0, total_len + BIN_M, BIN_M)
        centers = 0.5 * (bins[:-1] + bins[1:])
        p_sim = np.full(len(centers), np.nan)
        g_net = np.full(len(centers), np.nan)
        for lid, r in vdf.iterrows():
            cinfo = arc_by_link.get(str(lid))
            if cinfo is None:
                continue
            i0 = int(cinfo["s0"] // BIN_M)
            i1 = min(int(np.ceil(cinfo["s1"] / BIN_M)), len(centers))
            p_sim[i0:i1] = float(r["pBattery_W"]) / 1000.0
            g_net[i0:i1] = cinfo["grade"]

        idx = np.clip((s_mm // BIN_M).astype(int), 0, len(centers) - 1)
        p_meas_bin = np.full(len(centers), np.nan)
        v_bin = np.full(len(centers), np.nan)
        z_bin = np.full(len(centers), np.nan)
        for i in np.unique(idx):
            sel = idx == i
            p_meas_bin[i] = float(np.nanmean(p_drive[sel]))
            v_bin[i] = float(np.nanmean(v_meas[sel]))
            z_bin[i] = float(np.nanmean(z_meas[sel]))

        # --- A2: raeumlicher Lag Mess vs. Sim ---
        k2, c2 = xcorr_best_shift(p_sim, p_meas_bin, max_shift=20)
        c0 = xcorr_best_shift(p_sim, p_meas_bin, 0)[1]
        print(f"A2 raeumlicher Lag Messung vs. Sim: {k2} Bins = {k2*BIN_M:.0f} m "
              f"(corr {c2:.3f} vs. {c0:.3f} @0)")

        # --- A3: Klassen-Bias vor/nach Shift-Korrektur ---
        b0 = class_bias(p_sim, p_meas_bin, g_net, 0)
        b1 = class_bias(p_sim, p_meas_bin, g_net, k2)
        print(f"A3 Bias [kW] vor Korrektur : uphill {b0['uphill']:+.1f}  "
              f"flat {b0['flat']:+.1f}  downhill {b0['downhill']:+.1f}  all {b0['all']:+.1f}")
        print(f"   Bias [kW] nach Korrektur: uphill {b1['uphill']:+.1f}  "
              f"flat {b1['flat']:+.1f}  downhill {b1['downhill']:+.1f}  all {b1['all']:+.1f}")

        # --- B1: signierter Steigungsbias (Netz - Messung) je Klasse ---
        g_meas = np.full(len(centers), np.nan)
        g_meas[1:] = np.diff(z_bin) / BIN_M
        v = np.isfinite(g_net) & np.isfinite(g_meas)
        for name, lo, hi in (("uphill", CLIMB_MIN_GRADE, np.inf),
                             ("flat", -CLIMB_MIN_GRADE, CLIMB_MIN_GRADE),
                             ("downhill", -np.inf, -CLIMB_MIN_GRADE)):
            sel = v & (g_net > lo) & (g_net <= hi)
            if sel.sum() > 5:
                d = (g_net[sel] - g_meas[sel]) * 100
                print(f"B1 Steigungsbias {name:9s}: {np.mean(d):+.3f} %-Pkt "
                      f"(MAE {np.mean(np.abs(d)):.3f}, n={sel.sum()})")

        # --- B2: Anstiegs-Segment-Energien (lag-robust) ---
        up = np.isfinite(g_net) & (g_net > CLIMB_MIN_GRADE)
        segs = []
        i = 0
        while i < len(up):
            if up[i]:
                j = i
                while j < len(up) and up[j]:
                    j += 1
                if (j - i) * BIN_M >= CLIMB_MIN_LEN_M:
                    segs.append((i, j))
                i = j
            else:
                i += 1
        ratios, e_sims, e_meass = [], [], []
        for i0, i1 in segs:
            sl = slice(i0, i1)
            dt_h = BIN_M / np.maximum(v_bin[sl], 0.5) / 3600.0
            ok = np.isfinite(p_sim[sl]) & np.isfinite(p_meas_bin[sl]) & np.isfinite(dt_h)
            if ok.sum() < 5:
                continue
            es = float(np.nansum((p_sim[sl] * dt_h)[ok]))
            em = float(np.nansum((p_meas_bin[sl] * dt_h)[ok]))
            if em > 0.5:
                ratios.append(es / em)
                e_sims.append(es)
                e_meass.append(em)
        if ratios:
            print(f"B2 Anstiegs-Segmente (>1 %, >=1 km): n={len(ratios)}, "
                  f"E_sim/E_meas gesamt = {sum(e_sims)/sum(e_meass):.3f}, "
                  f"Median {np.median(ratios):.3f}, "
                  f"Bereich {min(ratios):.3f}..{max(ratios):.3f}")
        else:
            print("B2 keine Anstiegs-Segmente >= 1 km (flache Route)")


if __name__ == "__main__":
    main()
