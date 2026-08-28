"""Erzeugt Fig. 3 (sensitivity_and_knee) und Fig. 4 (decomposition_
validity) des IEEE-TTE-Papers als native TikZ/pgfplots-Snippets.

Repliziert die matplotlib-V3-Figuren (knee_point_analysis.py bzw.
grade_decomposition.py) 1:1 aus denselben CSVs; Ausgabe versioniert
(_V<N>, nie ueberschreiben). Benoetigt im Paper-Preamble:
\\usepgfplotslibrary{groupplots}.

Aufruf:  ../../.venv/Scripts/python plot_tikz_fig34.py
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data" / "section_variants_20260706_20q"
# kanonischer Auswahl-Run (182750, s. paper_findings Sec 13.2)
SELEKTION = (Path(__file__).parents[2].parent / "MATSim-MPM-netgen" /
             "python" / "network-generation" / "data" /
             "sections_quantile_run_20260706_182750")
COL_SENS, COL_KNEE = "C0392B", "21618C"


def naechste_version(muster, endung=".tex"):
    v = 1
    while (DATA / f"{muster}_V{v}{endung}").exists():
        v += 1
    return v


def paare(xs, ys, dx=2, dy=2, je_zeile=5):
    p = [f"({x:.{dx}f},{y:.{dy}f})" for x, y in zip(xs, ys)]
    return "\n".join(" ".join(p[i:i + je_zeile])
                     for i in range(0, len(p), je_zeile))


def schreibe_fig3(df, knee, cand, pfad):
    """Einzeilige Kernfigur (User-Entscheid 2026-08-27): relative
    Verbrauchs-Aufloesungs-Kurven (3 Sektionen, log-x, horizontaler
    Knie-Boxplot auf der Linklaengen-Achse). Spannen-Panel entfernt
    (Zahlen stehen im Text); Empty-Knees (Kneedle auf fast flacher
    Kurve = Rauschen) bewusst nicht enthalten. cand wird nicht mehr
    genutzt (Signatur wegen main() beibehalten)."""
    kl = knee[knee.loading == "loaded"].dropna(
        subset=["knee_link_length_m"]).sort_values("g_abs_mean")
    gx = kl.g_abs_mean.values * 100.0
    secs = list(kl.section.values)
    x_max = gx.max() + 0.3
    laengen = sorted(df.max_link_length.unique())

    def cons(sec, loading, L):
        v = df[(df.section == sec) & (df.loading == loading) &
               (df.max_link_length == L)].kWh_per_km
        return float(v.iloc[0])

    def rel_kurve(sec):
        e250 = cons(sec, "loaded", 250)
        return [(L, (cons(sec, "loaded", L) / e250 - 1.0) * 100.0)
                for L in laengen]

    # Nur drei repraesentative Kurven (User-Entscheid 2026-08-04):
    # flachste/mediane/steilste Sektion; kein Grau-Spaghetti, kein
    # Flat-Control, kein IQR-Band (250-m-Linie genuegt).
    # Monochrom + Marker-Formen statt Farben (User-Entscheid 2026-08-05,
    # IEEE-Stil): steepest schwarz/Quadrat, median grau/Dreieck,
    # flattest hellgrau/Kreis.
    hi = {secs[0]: ("black!35, mark=*, mark size=0.9pt", "flattest"),
          secs[len(secs) // 2]:
              ("black!60, mark=triangle*, mark size=1.3pt", "median"),
          secs[-1]: ("black, mark=square*, mark size=1.0pt", "steepest")}
    alle_rel = [v for s in hi for _, v in rel_kurve(s)]
    y2min, y2max = min(alle_rel) - 1.5, max(alle_rel) + 1.5

    def kurve_tex(sec, stil, forget=False):
        pts = paare(*zip(*rel_kurve(sec)), dx=0, dy=2, je_zeile=6)
        f = ", forget plot" if forget else ""
        return f"\\addplot[{stil}{f}] coordinates {{\n{pts}\n}};"

    # Marker zeigen die zwoelf diskreten Aufloesungsstufen
    bunt = "\n".join(
        kurve_tex(s, f"{hi[s][0]}, line width=1.1pt") +
        f"\n\\addlegendentry{{{hi[s][1]} route ({g:.1f}\\,\\%)}}"
        for s, g in zip(secs, gx) if s in hi)

    # Horizontaler Boxplot der 20 Loaded-Knees auf der Linklaengen-Achse
    # (ersetzt das fruehere Knie-Scatter-Panel; User-Entscheid 2026-08-04)
    kn = kl.knee_link_length_m
    kq1, kmed, kq3 = kn.quantile([0.25, 0.5, 0.75])
    kmin, kmax = kn.min(), kn.max()
    ym = y2max - 3.0   # Box-Mitte im kurvenfreien oberen Bereich
    bh, wh = 1.2, 0.6  # halbe Boxhoehe / halbe Whisker-Kappenhoehe
    fb = "black!75"    # Boxplot monochrom, leicht abgesetzt von Kurven
    box = "\n".join([
        f"\\draw[{fb}, fill=black!8, line width=0.6pt] "
        f"(axis cs:{kq1:.0f},{ym - bh:.1f}) rectangle "
        f"(axis cs:{kq3:.0f},{ym + bh:.1f});",
        f"\\draw[{fb}, line width=0.9pt] "
        f"(axis cs:{kmed:.0f},{ym - bh:.1f}) -- (axis cs:{kmed:.0f},{ym + bh:.1f});",
        f"\\draw[{fb}, line width=0.6pt] "
        f"(axis cs:{kmin:.0f},{ym:.1f}) -- (axis cs:{kq1:.0f},{ym:.1f});",
        f"\\draw[{fb}, line width=0.6pt] "
        f"(axis cs:{kq3:.0f},{ym:.1f}) -- (axis cs:{kmax:.0f},{ym:.1f});",
        f"\\draw[{fb}, line width=0.6pt] "
        f"(axis cs:{kmin:.0f},{ym - wh:.1f}) -- (axis cs:{kmin:.0f},{ym + wh:.1f});",
        f"\\draw[{fb}, line width=0.6pt] "
        f"(axis cs:{kmax:.0f},{ym - wh:.1f}) -- (axis cs:{kmax:.0f},{ym + wh:.1f});",
        f"\\node[{fb}, font=\\scriptsize, anchor=west, align=left] "
        f"at (axis cs:{kmax + 10:.0f},{ym:.1f}) "
        f"{{knees from\\\\loaded trucks}};",
    ])

    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% energy_results_summary.csv / knee_points.csv. Nicht von Hand editieren.
% Einpaneilig seit 2026-08-27 (User-Entscheid): Spannen-Panel entfernt,
% Zahlen stehen im Text. Monochrom seit V11 (Graustufen + Marker-Formen,
% User-Entscheid 2026-08-05).
\\begin{{tikzpicture}}
\\begin{{axis}}[
  width=0.78\\columnwidth, scale only axis,
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{font=\\scriptsize, draw=none, fill=white,
    fill opacity=0.85, text opacity=1}},
  legend cell align=left, grid=major, grid style={{black!12}},
  height=3.3cm, xmode=log, xmin=47, xmax=1060,
  xtick={{50,100,250,500,1000}}, xticklabels={{50,100,250,500,1000}},
  minor xtick={{}}, xlabel={{Maximum link length (m)}},
  ylabel={{Consumption deviation\\\\from 250\\,m grid (\\%)}},
  ylabel style={{align=center}}, ylabel shift=-3pt,
  ymin={y2min:.1f}, ymax={y2max:.1f},
  legend style={{at={{(0.02,0.03)}}, anchor=south west}}]
\\addplot[densely dotted, black, no marks, line width=0.7pt]
  coordinates {{(250,{y2min:.1f}) (250,{y2max:.1f})}};
\\addlegendentry{{250\\,m calibration scale}}
{bunt}
{box}
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def schreibe_fig4(dec, pfad):
    """Validitaets-Scatter dE_sim vs. dE_stat mit 45-Grad-Linie.
    Achsen fest bei +-15 % (User-Entscheid 2026-08-05); die wenigen
    Punkte ausserhalb entfallen mit Caption-Hinweis im Paper."""
    ne = dec[dec.max_link_length != 250]
    lim = 15.0
    innen = ne[(ne.dE_sim_rel250_pct.abs() <= lim) &
               (ne.dE_stat_rel250_pct.abs() <= lim)]
    n_aussen = len(ne) - len(innen)
    ld = innen[innen.loading == "loaded"]
    em = innen[innen.loading == "empty"]
    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% grade_decomposition.csv. Nicht von Hand editieren.
% Monochrom (User-Entscheid 2026-08-05, konsistent zu Fig. 3).
% Achsen fest +-15 %; {n_aussen} von {len(ne)} Punkten ausserhalb
% weggelassen (Caption-Hinweis im Paper).
\\begin{{tikzpicture}}
\\begin{{axis}}[
  width=0.86\\columnwidth, scale only axis, axis equal image,
  xmin={-lim:.1f}, xmax={lim:.1f}, ymin={-lim:.1f}, ymax={lim:.1f},
  xlabel={{Simulated energy change vs.\\ 250\\,m grid (\\%)}},
  ylabel={{Static model change vs.\\ 250\\,m grid (\\%)}},
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{at={{(0.02,0.98)}}, anchor=north west,
    font=\\scriptsize, draw=none, fill=white, fill opacity=0.85,
    text opacity=1}}, legend cell align=left,
  grid=major, grid style={{black!12}},
]
\\addplot[dotted, black, no marks, line width=0.4pt, forget plot]
  coordinates {{({-lim:.1f},{-lim:.1f}) ({lim:.1f},{lim:.1f})}};
\\addplot[only marks, mark=triangle, mark size=1.4pt, color=black,
  line width=0.5pt] coordinates {{
{paare(ld.dE_sim_rel250_pct.values, ld.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{loaded}}
\\addplot[only marks, mark=o, mark size=1.0pt, color=black!50,
  line width=0.5pt] coordinates {{
{paare(em.dE_sim_rel250_pct.values, em.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{empty}}
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def main():
    global DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection-dir", default=str(SELEKTION))
    ap.add_argument("--data-dir", default=str(DATA),
                    help="Sweep-Ordner mit energy_results_summary.csv, "
                         "knee_points.csv und grade_decomposition.csv")
    args = ap.parse_args()
    sel = Path(args.selection_dir)
    DATA = Path(args.data_dir)

    df = pd.read_csv(DATA / "energy_results_summary.csv")
    knee = pd.read_csv(DATA / "knee_points.csv")
    cand = (pd.read_csv(sel / "candidate_paths_features.csv")
            ["g_abs_mean"] * 100.0).values

    v3 = naechste_version("sensitivity_knee_tikz")
    out3 = DATA / f"sensitivity_knee_tikz_V{v3}.tex"
    schreibe_fig3(df, knee, cand, out3)
    namen = [out3.name]
    # Fig. 4 (Zerlegungs-Validitaet) gehoert zum gestrichenen Kap. V-C;
    # nur erzeugen, wenn der Sweep die Zerlegung mitgeliefert hat.
    dec_csv = DATA / "grade_decomposition.csv"
    if dec_csv.exists():
        dec = pd.read_csv(dec_csv)
        v4 = naechste_version("decomposition_validity_tikz")
        out4 = DATA / f"decomposition_validity_tikz_V{v4}.tex"
        schreibe_fig4(dec, out4)
        namen.append(out4.name)
    print("TikZ: " + ", ".join(namen))


if __name__ == "__main__":
    main()
