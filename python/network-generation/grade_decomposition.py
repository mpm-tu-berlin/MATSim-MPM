# -*- coding: utf-8 -*-
"""
Jensen/Grade-Fehlerzerlegung des 20-Sektionen-Diskretisierungs-Sweeps (Sec 5).

Frage: WARUM haengt der Verbrauch von der Linklaenge ab? Antwort quantitativ:

Statisches Konstantgeschwindigkeits-Modell je Link (repliziert
MpmDynamicBetDriveEnergyConsumption ohne Beschleunigungsdynamik):
    F_i = mSum*G*(c_rr*cos(g_i) + g_i) + fa*v^2      (fa = 0.5*1.225*cdXA)
    E_i = L_i * F_i * (1/eta_t  falls F_i >= 0,  sonst * eta_r)   [+ Recup-Cap]
Batterieenergie exakt zerlegbar:
    E = T1 + Loss
    T1   = [mSum*G*(c_rr*Sum L*cos + dz_net) + fa*v^2*Sum L] / eta_t
           -> aufloesungsUNabhaengig (dz_net = Endpunkt-Differenz, fix)
    Loss = (1/eta_t - eta_r) * Sum_{F<0} L*|F|   ("Bergab-Ueberschussenergie")
           -> waechst mit feinerer Aufloesung: Koarsierung mittelt Bergab-
           Steigungen unter den Kipp-Punkt g_kink = -(c_rr*cos + fa*v^2/(mSum*G))
           weg (Jensen-Argument, F(g) stueckweise linear-konvex).

Der Vergleich Delta_E_statisch vs. Delta_E_sim (beides relativ zur 250-m-Stufe)
zeigt, welcher Anteil der Aufloesungsabhaengigkeit durch diesen statischen
Grade/Effizienz-Asymmetrie-Mechanismus erklaert wird; der Rest ist Fahrdynamik
(Beschleunigungs-KE zwischen Links, Power-Cap-Verlangsamung bergauf).

Grades kommen aus den tatsaechlich GEFAHRENEN Links (resistance_debug.csv,
forward-gefiltert wie in der Sweep-Auswertung) mit voller z-Praezision aus dem
Netz. Selbstcheck: statische Link-Energie vs. Sim-Link-Energie auf
Steady-State-Links (vEntry ~ vExit ~ 80 km/h).

Ausgaben (results-dir, versioniert _V<N> wie knee_point_analysis):
  - grade_decomposition.csv       je (Sektion, Beladung, Stufe): E_sim, E_stat,
                                  T1, Loss, Cap-Anteil, rel-250-Deltas
  - decomposition_validity_V<N>.pdf/png  Scatter dE_stat vs dE_sim (45-Grad-Linie)
"""

import sys
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

_SCRIPT_DIR = Path(__file__).parent
G = 9.81
RHO = 1.225
V_TARGET = 22.222  # 80 km/h


def _import_module(name, filename):
    spec = spec_from_file_location(name, str(_SCRIPT_DIR / filename))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def solve_vmax(fa, lin_coeff, p_mech):
    """Loest fa*v^3 + lin_coeff*v = p_mech (positives lin_coeff) per Newton."""
    v = V_TARGET
    for _ in range(60):
        f = fa * v ** 3 + lin_coeff * v - p_mech
        df = 3.0 * fa * v ** 2 + lin_coeff
        step = f / df
        v -= step
        if abs(step) < 1e-9:
            break
    return max(v, 0.1)


def static_link_energy(lengths, grades, calib, mass, payload, max_motor_w,
                       power_limited_speed=True):
    """Vektorisierte statische Batterieenergie je Link [J] + Terme.

    Rueckgabe dict mit: e_batt [J-Array], T1 [J], loss [J], cap_extra [J],
    v [m/s-Array].
    """
    m_sum = mass + payload
    eta_t = calib["tractionEfficiency"]
    eta_r = calib["recupEfficiency"]
    fa = 0.5 * RHO * calib["cdXA"]
    c_rr = calib["rollingC"]
    max_recup_w = calib["maxRecupPowerFraction"] * max_motor_w

    g = np.asarray(grades, dtype=float)
    L = np.asarray(lengths, dtype=float)
    cosg = np.sqrt(1.0 - g * g)

    # Zielgeschwindigkeit je Link: bergauf ggf. leistungsbegrenzt (wie Java)
    v = np.full_like(g, V_TARGET)
    if power_limited_speed:
        total_c = c_rr * cosg + g
        p_mech_max = max_motor_w * eta_t
        # nur Links pruefen, die bei 80 km/h mehr als p_mech_max braeuchten
        need = fa * V_TARGET ** 3 + m_sum * G * total_c * V_TARGET
        idx = np.where((total_c > 0) & (need > p_mech_max))[0]
        for i in idx:
            v[i] = solve_vmax(fa, m_sum * G * total_c[i], p_mech_max)

    F = m_sum * G * (c_rr * cosg + g) + fa * v * v          # [N]
    e_mech = L * F                                           # [J]
    pos = e_mech >= 0.0
    e_batt = np.where(pos, e_mech / eta_t, e_mech * eta_r)

    # Rekup-Leistungscap (zeitbasiert wie im Java-Widerstandsanteil)
    p_batt = np.where(pos, F * v / eta_t, F * v * eta_r)
    t_link = L / v
    cap_mask = p_batt < -max_recup_w
    cap_extra = float(np.sum(np.where(cap_mask, (-max_recup_w * t_link) - e_batt, 0.0)))
    e_batt = np.where(cap_mask, -max_recup_w * t_link, e_batt)

    # Exakte Zerlegung (auf Basis der ungecappten stueckweisen Linearitaet):
    # T1 = Sum(e_mech)/eta_t ; Loss = (1/eta_t - eta_r)*Sum_{e<0}|e_mech| ; + cap_extra
    T1 = float(np.sum(e_mech)) / eta_t
    loss = (1.0 / eta_t - eta_r) * float(np.sum(np.abs(e_mech[~pos])))
    return {"e_batt": e_batt, "T1": T1, "loss": loss, "cap_extra": cap_extra, "v": v}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Jensen/Grade-Zerlegung des Sweeps.")
    parser.add_argument("--results-dir", type=str, required=True,
                        help="variants-dir des Sweeps (mit sim_results/ + energy_results_summary.csv)")
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = _SCRIPT_DIR / results_dir

    rsea = _import_module("rsea", "run_section_energy_analysis.py")
    kpa = _import_module("kpa", "knee_point_analysis.py")

    sim_df = pd.read_csv(results_dir / "energy_results_summary.csv")
    sections = [s for s in rsea.SECTIONS]
    link_lengths = list(rsea.LINK_LENGTHS)

    rows = []
    check_ratios = []
    for section in sections:
        for max_len in link_lengths:
            network_path = results_dir / f"section_{section}_{max_len}m.xml.gz"
            if not network_path.exists():
                continue
            nodes, links = rsea.load_matsim_network(str(network_path))
            for loading in ("empty", "loaded"):
                run_dir = results_dir / "sim_results" / f"{section}_{max_len}m_{loading}"
                debug_csv = run_dir / "resistance_debug.csv"
                if not debug_csv.exists():
                    print(f"  WARNING: {debug_csv} fehlt — skip")
                    continue
                fwd = rsea.get_forward_links_per_vehicle(network_path, debug_csv)
                ddf = pd.read_csv(debug_csv)
                ddf["linkId"] = ddf["linkId"].astype(str)
                vehicle_id, fwd_ids = next(iter(fwd.items()))
                vdf = (ddf[ddf.vehicleId == vehicle_id].set_index("linkId")
                       .reindex(fwd_ids).dropna(how="all"))

                # Grades mit voller Praezision aus dem Netz (Fallback: grade_pct)
                grades, lengths = [], []
                for lid, r in vdf.iterrows():
                    lk = links.get(str(lid))
                    if lk is not None:
                        zf = nodes[lk["from"]].get("z")
                        zt = nodes[lk["to"]].get("z")
                        if zf is not None and zt is not None and lk["length"] > 0:
                            grades.append((zt - zf) / lk["length"])
                            lengths.append(lk["length"])
                            continue
                    grades.append(float(r["grade_pct"]) / 100.0)
                    lengths.append(float(r["length_m"]))

                calib = rsea.CALIBRATION_PER_LOADING[loading]
                vp = rsea.VEHICLE_PARAMS[loading]
                res = static_link_energy(lengths, grades, calib,
                                         vp["mass"], vp["payload"], vp["maxMotorPower"])

                # Selbstcheck auf Steady-State-Links (Ein-/Ausfahrt ~80 km/h, kein Cap)
                steady = ((vdf["vEntry_kmh"] > 79.5) & (vdf["vExit_kmh"] > 79.5)).values
                sim_e_link = (vdf["energy_Wh"].values * 3600.0)
                with np.errstate(divide="ignore", invalid="ignore"):
                    ratio = np.where(np.abs(sim_e_link) > 1e3,
                                     res["e_batt"] / sim_e_link, np.nan)
                check_ratios.append(np.nanmedian(ratio[steady]))

                total_L = float(np.sum(lengths))
                dz_net = float(np.sum(np.asarray(grades) * np.asarray(lengths)))
                rows.append({
                    "section": section, "loading": loading, "max_link_length": max_len,
                    "total_length_m": total_L,
                    "dz_net_m": dz_net,
                    "E_sim_kWh": float(vdf["energy_Wh"].sum()) / 1000.0,
                    "E_stat_kWh": float(np.sum(res["e_batt"])) / 3.6e6,
                    "T1_kWh": res["T1"] / 3.6e6,
                    "loss_kWh": res["loss"] / 3.6e6,
                    "cap_extra_kWh": res["cap_extra"] / 3.6e6,
                    "n_powerlimited_links": int(np.sum(res["v"] < V_TARGET - 1e-6)),
                })
        print(f"{section}: fertig")

    df = pd.DataFrame(rows)

    # Relativ zur 250-m-Stufe je (Sektion, Beladung)
    out = []
    for (section, loading), sub in df.groupby(["section", "loading"]):
        sub = sub.sort_values("max_link_length").set_index("max_link_length")
        if 250 not in sub.index:
            continue
        ref = sub.loc[250]
        for L, r in sub.iterrows():
            out.append({
                **{k: r[k] for k in df.columns if k != "max_link_length"},
                "max_link_length": L,
                "dE_sim_rel250_pct": 100.0 * (r.E_sim_kWh / ref.E_sim_kWh - 1.0),
                "dE_stat_rel250_pct": 100.0 * (r.E_stat_kWh / ref.E_stat_kWh - 1.0),
                "dLoss_rel250_kWh": r.loss_kWh - ref.loss_kWh,
                "dE_stat_rel250_kWh": r.E_stat_kWh - ref.E_stat_kWh,
                "dE_sim_rel250_kWh": r.E_sim_kWh - ref.E_sim_kWh,
            })
    out_df = pd.DataFrame(out)
    out_df.to_csv(results_dir / "grade_decomposition.csv", index=False)

    # --- Konsolen-Zusammenfassung ---
    print(f"\nSelbstcheck statisches Modell vs. Sim (Steady-State-Links): "
          f"Median-Ratio ueber alle Laeufe = {np.nanmedian(check_ratios):.4f} "
          f"(P5 {np.nanpercentile(check_ratios,5):.4f}, P95 {np.nanpercentile(check_ratios,95):.4f})")

    ne = out_df[out_df.max_link_length != 250]
    for loading in ("empty", "loaded"):
        sub = ne[ne.loading == loading]
        expl = sub.dE_stat_rel250_kWh / sub.dE_sim_rel250_kWh
        loss_share = sub.dLoss_rel250_kWh / sub.dE_stat_rel250_kWh
        resid = (sub.dE_sim_rel250_kWh - sub.dE_stat_rel250_kWh).abs()
        print(f"\n{loading}: erklaerte Delta-Fraktion dE_stat/dE_sim: "
              f"Median {expl.median():.2f} (IQR {expl.quantile(.25):.2f}-{expl.quantile(.75):.2f})")
        print(f"  Loss-Anteil an dE_stat: Median {loss_share.median():.2f}")
        print(f"  |Residuum| Median {resid.median():.2f} kWh "
              f"(vs |dE_sim| Median {sub.dE_sim_rel250_kWh.abs().median():.2f} kWh)")
        cap = out_df[(out_df.loading == loading)].cap_extra_kWh
        pl = out_df[(out_df.loading == loading)].n_powerlimited_links
        print(f"  Recup-Cap-Zusatzverlust: max {cap.max():.2f} kWh; "
              f"power-limitierte Links: max {pl.max()} je Lauf")

    # --- Validitaets-Scatter (versioniert, TikZ-clean) ---
    version = kpa._next_version(results_dir, base="decomposition_validity")
    fig, ax = plt.subplots(figsize=(7.2, 6.6))
    for loading, marker, color in (("loaded", "o", "#C0392B"), ("empty", "s", "#21618C")):
        sub = ne[ne.loading == loading]
        ax.scatter(sub.dE_sim_rel250_pct, sub.dE_stat_rel250_pct, marker=marker, s=26,
                   facecolors="none", edgecolors=color, linewidths=1.0, label=loading)
    lim = max(ne.dE_sim_rel250_pct.abs().max(), ne.dE_stat_rel250_pct.abs().max()) * 1.08
    ax.plot([-lim, lim], [-lim, lim], color="black", lw=0.9, ls=":")
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_xlabel("Simulated energy change vs. 250 m grid [%]", fontsize=12)
    ax.set_ylabel("Static grade/asymmetry model change vs. 250 m grid [%]", fontsize=12)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=10)
    ax.set_aspect("equal")
    fig.tight_layout()
    kpa._savefig(fig, results_dir, "decomposition_validity", version)
    plt.close(fig)
    print(f"\nFigur: decomposition_validity_V{version}.pdf/png")
    print(f"CSV:   {results_dir / 'grade_decomposition.csv'}")


if __name__ == "__main__":
    main()
