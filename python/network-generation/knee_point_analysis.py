# -*- coding: utf-8 -*-
"""
Auswertung des 20-Sektionen-Diskretisierungs-Sweeps
(energy_results_summary.csv aus run_section_energy_analysis.py).

Redesign 2026-07-06 (User): ersetzt die alte 2/3-Sektionen-Studie. Kernpunkte:
  - KEIN Konvergenz-Framing. Feine Stufen konvergieren nicht (DTM-Restfehler
    erzeugt skalenabhaengigen Verbrauch); Referenz ist die 250-m-Stufe
    (VECTO-verankerte Kalibrier-Skala), NICHT ein Kontinuumslimit.
  - Darstellung RELATIV zur 250-m-Stufe je (Sektion, Beladung), nicht absolut
    und nicht relativ zur rauschbehafteten 50-m-Stufe.
  - Kniepunkt je (Sektion, Beladung) via Kneedle auf spline-geglaetteter Kurve.
  - HAUPTERGEBNIS: Knie-Linklaenge vs. Topografie-Kennzahl (Hm/km bzw. sigma_g).

Eingaben:
  - <results-dir>/energy_results_summary.csv  (Sweep-Ausgabe)
  - selected_sections_features.csv der ZUGEHOERIGEN Auswahl (Hm/km, sigma_g je
    Sektion); Default = kanonischer Run 182750 im Netzgen-Worktree.

Figuren werden versioniert (_V1, _V2, … — nie ueberschreiben) und bewusst OHNE
In-Grafik-Beschriftungen erzeugt (nur Achsen/Legende), damit sie leicht in
TikZ/LaTeX uebernommen werden koennen; Erlaeuterungen gehoeren in den Text.

Ausgaben (alle in results-dir, <N> = Laufversion):
  - sensitivity_and_knee_V<N>.png/pdf  Kernfigur (3 Zeilen ueber mittlere abs.
                                       Steigung): Netz-Steigungsverteilung /
                                       Verbrauch [kWh/km] mit Gitter-Bandbreite /
                                       Knie-Linklaenge
  - energy_relative_to_250m_V<N>.png/pdf  Relativdarstellung je Sektion
  - knee_analysis_V<N>.png/pdf          Rohdaten + Spline + Knie (Uebersicht)
  - knee_points.csv                    je Kurve: Knie + Topografie-Merge
  - relative_to_250m.csv               volle Relativ-Tabelle fuer paper_findings
  - sensitivity_vs_topography.csv      Spanne 50<->1000 m je Sektion
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
import pandas as pd
from kneed import KneeLocator
from scipy.interpolate import UnivariateSpline

_SCRIPT_DIR = Path(__file__).parent
_NETGEN_DIR = _SCRIPT_DIR.parents[2] / "MATSim-MPM-netgen" / "python" / "network-generation"
DEFAULT_FEATURES_CSV = (_NETGEN_DIR / "data" / "sections_quantile_run_20260706_182750"
                        / "selected_sections_features.csv")

REFERENCE_LINK_LENGTH_M = 250.0  # VECTO-verankerte Kalibrier-Skala
SPLINE_RESOLUTION = 500
LOADING_LINESTYLE = {"loaded": "-", "empty": "--"}
LOADING_MARKER = {"loaded": "o", "empty": "s"}


def _next_version(output_dir, base="sensitivity_and_knee"):
    """Naechste Versionsnummer: scannt <base>_V<N>.png und gibt max(N)+1 (min. 1).
    So bleibt jede Figuren-Variante erhalten (User-Regel: nie ueberschreiben)."""
    import re
    mx = 0
    for f in Path(output_dir).glob(f"{base}_V*.png"):
        m = re.search(r"_V(\d+)\.png$", f.name)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _savefig(fig, output_dir, name, version=None):
    """Speichert PNG + PDF (optional mit _V<version>-Suffix); gesperrte Dateien
    (im Viewer offen) werden mit Warnung uebersprungen statt abzubrechen."""
    stem = f"{name}_V{version}" if version is not None else name
    for ext in ("png", "pdf"):
        try:
            fig.savefig(output_dir / f"{stem}.{ext}", dpi=300, bbox_inches="tight")
        except PermissionError:
            print(f"  WARNING: {stem}.{ext} gesperrt (offen im Viewer?) — uebersprungen.")


def _quantile_number(section):
    """'q97' -> 97; 'flat' -> -1 (fuer Sortierung/Farbe)."""
    if section == "flat":
        return -1
    return int(section[1:])


def section_color(section, all_sections):
    """coolwarm nach Quantil-Rang (konsistent zu run_section_energy_analysis.py)."""
    if section == "flat":
        return "#757575"
    quant = sorted((s for s in all_sections if s != "flat"), key=_quantile_number)
    i = quant.index(section)
    return cm.get_cmap("coolwarm")(i / max(1, len(quant) - 1))


def fit_and_find_knee(x, y, smoothing_mult=1.0):
    """UnivariateSpline (k=3) + Kneedle (convex/decreasing) auf dichtem Gitter.

    Rueckgabe (x_dense, y_dense, knee_x, knee_y). knee_* = None, wenn Kneedle
    keinen Knick findet (erwartet fuer sehr flache Sektionen ohne
    Aufloesungs-Sensitivitaet).
    """
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    order = np.argsort(x_arr)
    x_sorted, y_sorted = x_arr[order], y_arr[order]

    s = smoothing_mult * 0.02
    spline = UnivariateSpline(x_sorted, y_sorted, k=3, s=s)
    x_dense = np.linspace(x_sorted.min(), x_sorted.max(), SPLINE_RESOLUTION)
    y_dense = spline(x_dense)

    try:
        kl = KneeLocator(x_dense, y_dense, curve="convex", direction="decreasing",
                         S=1.0, online=True)
        knee_x, knee_y = kl.knee, kl.knee_y
    except Exception:
        knee_x, knee_y = None, None
    return x_dense, y_dense, knee_x, knee_y


def compute_relative(df, sections, loadings):
    """rel(L) = kWh(L)/kWh(250) - 1 je (Sektion, Beladung). Long-Format-DataFrame."""
    rows = []
    for section in sections:
        for loading in loadings:
            sub = df[(df.section == section) & (df.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            ref = sub.loc[sub.max_link_length == REFERENCE_LINK_LENGTH_M, "kWh_per_km"]
            if ref.empty:
                print(f"  WARNING: keine 250-m-Referenz fuer {section}/{loading} — skip")
                continue
            ref_val = float(ref.iloc[0])
            for _, r in sub.iterrows():
                rows.append({
                    "section": section, "loading": loading,
                    "max_link_length": int(r["max_link_length"]),
                    "kWh_per_km": float(r["kWh_per_km"]),
                    "kWh_per_km_ref250": ref_val,
                    "rel_to_250_pct": 100.0 * (float(r["kWh_per_km"]) / ref_val - 1.0),
                })
    return pd.DataFrame(rows)


def plot_relative(rel_df, sections, output_dir, version=None):
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharey=True)
    for ax, loading in zip(axes, ("empty", "loaded")):
        for section in sections:
            sub = rel_df[(rel_df.section == section) & (rel_df.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            color = section_color(section, sections)
            ax.plot(sub.max_link_length, sub.rel_to_250_pct, marker="o", ms=4,
                    color=color, lw=1.4, label=section)
        ax.axvline(REFERENCE_LINK_LENGTH_M, color="black", ls=":", lw=1, alpha=0.6)
        ax.axhline(0, color="black", lw=0.8, alpha=0.5)
        ax.set_xscale("log")
        xs = sorted(rel_df.max_link_length.unique())
        ax.set_xticks(xs)
        ax.set_xticklabels([str(x) for x in xs], rotation=45, fontsize=8)
        ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
        ax.set_title(loading, fontsize=12)
        ax.grid(True, alpha=0.3, which="both")
    axes[0].set_ylabel("Energy relative to 250 m step [%]", fontsize=12)
    axes[1].legend(loc="upper right", fontsize=7, ncol=2, title="section")
    fig.tight_layout()
    _savefig(fig, output_dir, "energy_relative_to_250m", version)
    plt.close(fig)


def compute_sensitivity(rel_df, feats):
    """Spanne rel%(50 m) - rel%(1000 m) je (Sektion, Beladung) + Hm/km-Merge.

    Diese Spanne ist das robuste Sensitivitaetsmass (wie stark haengt der
    Verbrauch ueberhaupt von der Linklaenge ab). Im Gegensatz zur Knie-POSITION
    skaliert sie klar mit der Topografie.
    """
    rows = []
    for (section, loading), sub in rel_df.groupby(["section", "loading"]):
        if section == "flat":
            hm = 0.0
        elif section in feats.index:
            hm = float(feats.loc[section, "D_plus_per_km"])
        else:
            continue
        r50 = sub.loc[sub.max_link_length == 50, "rel_to_250_pct"]
        r1000 = sub.loc[sub.max_link_length == 1000, "rel_to_250_pct"]
        if r50.empty or r1000.empty:
            continue
        rows.append({"section": section, "loading": loading, "D_plus_per_km": hm,
                     "rel_50m_pct": float(r50.iloc[0]),
                     "rel_1000m_pct": float(r1000.iloc[0]),
                     "span_50_1000_pct": float(r50.iloc[0]) - float(r1000.iloc[0])})
    return pd.DataFrame(rows)


def plot_sensitivity_and_knee(sens_df, knee_df, feats, df, cand_grades, output_dir, version=None):
    """Zweizeilige Kernfigur ueber x = mittlere absolute Steigung [%].

    OBEN: Verteilung der Streckensteigung im dt. Fernstrassennetz (1707
    Kandidatenrouten) — die Masse ist flach, unser Q5–Q97-Sample daher dort
    dicht. UNTEN Dual-Achse: links Verbrauchsaenderung 50→1000 m (steigt klar
    mit der Steigung), rechts Knie-Linklaenge (Marker nach Verlaesslichkeit =
    Kurven-Spread skaliert; mild-steigende gewichtete Trendlinie). Ehrliche
    Aussage statt „konstant": der Grossteil des Netzes ist flach und dort fast
    aufloesungsunabhaengig; Sensitivitaet UND optimale Linklaenge steigen erst
    im seltenen steilen Schwanz.
    """
    def grade_pct(section):
        return 100.0 * float(feats.loc[section, "g_abs_mean"])

    COL_SENS = "#C0392B"   # crimson — Verbrauch
    COL_KNEE = "#21618C"   # blau    — Knie

    def cons(section, L, loading="loaded"):
        sub = df[(df.section == section) & (df.loading == loading)]
        v = sub.loc[sub.max_link_length == L, "kWh_per_km"]
        return float(v.iloc[0]) if not v.empty else np.nan

    x_max = max(grade_pct(s) for s in feats.index) + 0.3
    secs = sorted((s for s in feats.index), key=_quantile_number)
    gx = np.array([grade_pct(s) for s in secs])

    fig = plt.figure(figsize=(10.5, 9.5))
    gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 3.0, 2.2], hspace=0.10)
    ax_top = fig.add_subplot(gs[0])
    ax_mid = fig.add_subplot(gs[1], sharex=ax_top)
    ax_bot = fig.add_subplot(gs[2], sharex=ax_top)

    # --- ZEILE 1: Netz-Steigungsverteilung (Rug = 20 Sektionen) ---
    ax_top.hist(cand_grades, bins=np.arange(0, cand_grades.max() + 0.2, 0.2),
                color="#7f8c8d", alpha=0.6, edgecolor="white", linewidth=0.4)
    ax_top.axvline(1.0, color="black", ls=":", lw=1)
    for s in feats.index:
        ax_top.axvline(grade_pct(s), ymin=0, ymax=0.14, color=COL_KNEE, lw=0.8, alpha=0.7)
    ax_top.set_ylabel("Route count", fontsize=10)
    plt.setp(ax_top.get_xticklabels(), visible=False)

    # --- ZEILE 2: Verbrauch [kWh/km], Punkt=250 m, Balken=Gitter-Bandbreite 50-1000 m ---
    c250 = np.array([cons(s, 250) for s in secs])
    c50 = np.array([cons(s, 50) for s in secs])
    c1000 = np.array([cons(s, 1000) for s in secs])
    yerr = np.vstack([c250 - c1000, c50 - c250])  # unten bis 1000 m, oben bis 50 m
    ax_mid.errorbar(gx, c250, yerr=yerr, fmt="o", ms=6, color=COL_SENS, ecolor=COL_SENS,
                    elinewidth=1.3, capsize=3, zorder=4,
                    label="loaded, 250 m grid (bar: 50–1000 m)")
    cf = df[(df.section == "flat") & (df.loading == "loaded")].set_index("max_link_length").kWh_per_km
    if not cf.empty:
        ax_mid.errorbar([0.0], [cf.loc[250]],
                        yerr=[[cf.loc[250] - cf.loc[1000]], [cf.loc[50] - cf.loc[250]]],
                        fmt="^", ms=8, color="grey", ecolor="grey", capsize=3, zorder=4,
                        label="flat control")
    ax_mid.set_ylabel("Loaded consumption\n[kWh/km]", fontsize=11)
    ax_mid.grid(True, alpha=0.25)
    ax_mid.legend(loc="upper left", fontsize=9, framealpha=0.9)
    plt.setp(ax_mid.get_xticklabels(), visible=False)

    # --- ZEILE 3: Knie (einheitliche Markergroesse), empty als graue Referenz ---
    subE = knee_df[knee_df.loading == "empty"].dropna(subset=["knee_link_length_m"]).copy()
    subE["grade_pct"] = subE.section.map(grade_pct)
    ax_bot.scatter(subE.grade_pct, subE.knee_link_length_m, marker="x", s=36, zorder=3,
                   color="#9e9e9e", linewidths=1.2, label="empty knee")

    subK = knee_df[knee_df.loading == "loaded"].dropna(subset=["knee_link_length_m"]).copy()
    subK["grade_pct"] = subK.section.map(grade_pct)
    ax_bot.scatter(subK.grade_pct, subK.knee_link_length_m, marker="D", s=55, zorder=4,
                   facecolors=COL_KNEE, edgecolors="black", linewidths=0.6, alpha=0.9,
                   label="loaded knee")
    ax_bot.set_ylabel("Knee link\nlength [m]", fontsize=11, color=COL_KNEE)
    ax_bot.tick_params(axis="y", colors=COL_KNEE)
    ax_bot.set_ylim(0, 500)
    ax_bot.grid(True, alpha=0.25)
    ax_bot.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax_bot.set_xlabel("Mean absolute grade [%]", fontsize=12)

    ax_top.set_xlim(-0.1, x_max)
    _savefig(fig, output_dir, "sensitivity_and_knee", version)
    plt.close(fig)


def plot_knee_overview(curves, sections, output_dir, version=None):
    link_lengths = sorted({int(x) for c in curves for x in c["x"]})
    fig, ax = plt.subplots(1, 1, figsize=(13, 7.5))
    for c in curves:
        color = section_color(c["section"], sections)
        ls = LOADING_LINESTYLE[c["loading"]]
        ax.scatter(c["x"], c["y"], color=color, s=18, zorder=3,
                   marker=LOADING_MARKER[c["loading"]], edgecolors="white", linewidths=0.4)
        ax.plot(c["x_dense"], c["y_dense"], color=color, linestyle=ls, lw=1.3, alpha=0.8,
                label=f"{c['section']} ({c['loading']})")
        if c["knee_x"] is not None:
            ax.scatter([c["knee_x"]], [c["knee_y"]], color=color, marker="*",
                       s=150, edgecolors="black", linewidths=0.6, zorder=5)
    ax.set_xscale("log")
    ax.set_xticks(link_lengths)
    ax.set_xticklabels([str(x) for x in link_lengths], rotation=45, fontsize=8)
    ax.set_xlabel("Max. allowed link length [m]", fontsize=12)
    ax.set_ylabel("Energy consumption [kWh/km]", fontsize=12)
    ax.grid(True, alpha=0.3, which="both")
    ax.legend(loc="upper right", fontsize=6, ncol=3)
    fig.tight_layout()
    _savefig(fig, output_dir, "knee_analysis", version)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="20-Sektionen-Sweep-Auswertung (relativ + Knie).")
    parser.add_argument("--results-dir", type=str, required=True,
                        help="Ordner mit energy_results_summary.csv (= variants-dir des Sweeps).")
    parser.add_argument("--features-csv", type=str, default=str(DEFAULT_FEATURES_CSV),
                        help="selected_sections_features.csv der zugehoerigen Auswahl (Hm/km, sigma_g).")
    parser.add_argument("--candidates-csv", type=str, default=None,
                        help="candidate_paths_features.csv fuer die Netz-Steigungsverteilung "
                             "(Default: neben features-csv).")
    parser.add_argument("--smoothing", type=float, default=1.0,
                        help="Spline-Glaettungsmultiplikator (>1 glatter).")
    parser.add_argument("--max-link-length", type=float, default=700.0,
                        help="Linklaengen-Achse/Knee auf diesen Wert kappen [m]; Default 700.")
    parser.add_argument("--include-flat", action="store_true",
                        help="Flat-Referenz in Relativ-Plot einbeziehen (hat keinen Knie).")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    df = pd.read_csv(results_dir / "energy_results_summary.csv")
    print(f"Loaded {len(df)} rows from {results_dir / 'energy_results_summary.csv'}")

    feats = pd.read_csv(args.features_csv).set_index("section")

    # Netz-Steigungsverteilung (mittlere abs. Steigung [%] ueber alle Kandidatenrouten)
    cand_csv = Path(args.candidates_csv) if args.candidates_csv \
        else Path(args.features_csv).parent / "candidate_paths_features.csv"
    cand_grades = None
    if cand_csv.exists():
        cand_grades = (pd.read_csv(cand_csv)["g_abs_mean"] * 100.0).values
        print(f"Netz-Steigungsverteilung aus {cand_csv.name}: n={len(cand_grades)}, "
              f"Median {np.median(cand_grades):.2f} %, <1 %: {(cand_grades<1).mean()*100:.0f} %")
    else:
        print(f"WARNING: {cand_csv} fehlt — Histogramm-Panel wird uebersprungen.")

    # Sektionen dynamisch aus der CSV (ausser flat, sofern nicht gewuenscht)
    sections = sorted((s for s in df.section.unique() if s != "flat"), key=_quantile_number)
    sections_for_rel = (["flat"] + sections) if args.include_flat and "flat" in df.section.unique() else sections
    loadings = ["empty", "loaded"]

    # --- Relative Darstellung (volle Leiter, ungekappt) ---
    # Flat immer mitrechnen (Kontrolle fuer Sensitivitaet), aber nur bei
    # --include-flat mitplotten (sonst ueberladen).
    rel_sections = sections_for_rel if "flat" in sections_for_rel else (sections_for_rel + ["flat"])
    # Eine Versionsnummer pro Lauf (nie ueberschreiben, User-Regel): alle Figuren
    # dieses Laufs bekommen dasselbe _V<N>.
    version = _next_version(results_dir)
    print(f"Figuren-Version dieses Laufs: V{version}")

    rel_df = compute_relative(df, rel_sections, loadings)
    rel_df.to_csv(results_dir / "relative_to_250m.csv", index=False)
    plot_relative(rel_df, sections_for_rel, results_dir, version)

    # --- Knie je (Sektion, Beladung) auf gekappter Leiter ---
    df_knee = df[df.max_link_length <= args.max_link_length].copy()
    curves, knee_rows = [], []
    for section in sections:
        for loading in loadings:
            sub = df_knee[(df_knee.section == section) & (df_knee.loading == loading)]
            sub = sub.sort_values("max_link_length")
            if sub.empty:
                continue
            x, y = sub.max_link_length.values, sub.kWh_per_km.values
            x_dense, y_dense, knee_x, knee_y = fit_and_find_knee(x, y, args.smoothing)
            curves.append(dict(section=section, loading=loading, x=x, y=y,
                               x_dense=x_dense, y_dense=y_dense, knee_x=knee_x, knee_y=knee_y))
            knee_rows.append({
                "section": section, "loading": loading,
                "knee_link_length_m": float(knee_x) if knee_x is not None else None,
                "knee_kWh_per_km": float(knee_y) if knee_y is not None else None,
                "D_plus_per_km": float(feats.loc[section, "D_plus_per_km"]) if section in feats.index else None,
                "sigma_g": float(feats.loc[section, "sigma_g"]) if section in feats.index else None,
                "g_abs_mean": float(feats.loc[section, "g_abs_mean"]) if section in feats.index else None,
            })

    knee_df = pd.DataFrame(knee_rows)
    knee_df.to_csv(results_dir / "knee_points.csv", index=False)

    sens_df = compute_sensitivity(rel_df, feats)
    sens_df.to_csv(results_dir / "sensitivity_vs_topography.csv", index=False)

    if cand_grades is not None:
        plot_sensitivity_and_knee(sens_df, knee_df, feats, df, cand_grades, results_dir, version)
    plot_knee_overview(curves, sections, results_dir, version)

    # --- Konsolen-Zusammenfassung fuer paper_findings.md ---
    print("\n=== Relative Abweichung zur 250-m-Stufe [%] (Auszug: 50 m, 100 m, 1000 m) ===")
    pivot = rel_df.pivot_table(index=["section", "loading"], columns="max_link_length",
                               values="rel_to_250_pct")
    cols = [c for c in (50, 100, 1000) if c in pivot.columns]
    print(pivot[cols].round(2).to_string())

    print("\n=== Kniepunkte + Topografie ===")
    print(knee_df.round({"knee_link_length_m": 0, "knee_kWh_per_km": 3,
                         "D_plus_per_km": 2, "sigma_g": 4, "g_abs_mean": 4}).to_string(index=False))

    n_knee = knee_df.knee_link_length_m.notna().sum()
    print(f"\n{n_knee}/{len(knee_df)} Kurven mit detektiertem Knie.")
    print(f"\nOutputs in: {results_dir}")


if __name__ == "__main__":
    main()
