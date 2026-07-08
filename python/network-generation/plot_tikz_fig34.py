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
    """Dreizeilige Kernfigur als groupplot (Histogramm/Verbrauch/Knie)."""
    kl = knee[knee.loading == "loaded"].dropna(
        subset=["knee_link_length_m"]).sort_values("g_abs_mean")
    ke = knee[knee.loading == "empty"].dropna(
        subset=["knee_link_length_m"]).sort_values("g_abs_mean")
    gx = kl.g_abs_mean.values * 100.0
    secs = kl.section.values
    x_max = gx.max() + 0.3

    def cons(sec, L):
        v = df[(df.section == sec) & (df.loading == "loaded") &
               (df.max_link_length == L)].kWh_per_km
        return float(v.iloc[0])

    c250 = np.array([cons(s, 250) for s in secs])
    c50 = np.array([cons(s, 50) for s in secs])
    c1000 = np.array([cons(s, 1000) for s in secs])
    f250, f50, f1000 = (cons("flat", L) for L in (250, 50, 1000))
    y2min = min(c1000.min(), f1000) - 0.06
    y2max = max(c50.max(), f50) + 0.06

    # Histogramm wie matplotlib: Binbreite 0,2 %
    kanten = np.arange(0, cand.max() + 0.2, 0.2)
    zaehl, _ = np.histogram(cand, bins=kanten)
    hmax = float(zaehl.max()) * 1.08
    hist = paare(kanten[:-1], zaehl, dx=1, dy=0, je_zeile=6)
    hist += f"\n({kanten[-1]:.1f},{zaehl[-1]:.0f})"  # ybar interval: Endkante
    rug = "\n".join(
        f"\\draw[figknee, line width=0.5pt, opacity=0.7] "
        f"(axis cs:{g:.2f},0) -- (axis cs:{g:.2f},{0.14 * hmax:.1f});"
        for g in gx)

    tab = "\n".join(
        f"{g:.2f} {c:.3f} {cp:.3f} {cm:.3f}"
        for g, c, cp, cm in zip(gx, c250, c50 - c250, c250 - c1000))

    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% energy_results_summary.csv / knee_points.csv /
% candidate_paths_features.csv (Run 182750). Nicht von Hand editieren.
\\begin{{tikzpicture}}
\\definecolor{{figsens}}{{HTML}}{{{COL_SENS}}}
\\definecolor{{figknee}}{{HTML}}{{{COL_KNEE}}}
\\begin{{groupplot}}[group style={{group size=1 by 3,
    x descriptions at=edge bottom, vertical sep=5pt}},
  width=0.84\\columnwidth, scale only axis,
  xmin=-0.15, xmax={x_max:.2f},
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{font=\\scriptsize, draw=none, fill=white,
    fill opacity=0.85, text opacity=1}},
  legend cell align=left, grid=major, grid style={{black!12}},
]
% --- Zeile 1: Netz-Steigungsverteilung (1707 Kandidaten) + Sektions-Rug
\\nextgroupplot[height=1.5cm, ylabel={{Route count}},
  ymin=0, ymax={hmax:.0f}, ytick={{0,400,800}}, grid=none]
\\addplot[ybar interval, fill=black!30, draw=white, line width=0.2pt]
  coordinates {{
{hist}
}};
\\draw[densely dotted, black] (axis cs:1,0) -- (axis cs:1,{hmax:.0f});
{rug}
% --- Zeile 2: Verbrauch, Punkt = 250-m-Gitter, Balken = 50/1000 m
\\nextgroupplot[height=3.4cm, ylabel={{Loaded consumption [kWh/km]}},
  ymin={y2min:.2f}, ymax={y2max:.2f},
  legend style={{at={{(0.02,0.97)}}, anchor=north west}}]
\\addplot[only marks, mark=*, mark size=1.8pt, color=figsens,
  error bars/.cd, y dir=both, y explicit,
  error bar style={{line width=0.8pt}}, error mark options={{
    rotate=90, mark size=1.6pt, line width=0.8pt}}]
  table[x=x, y=y, y error plus=eyp, y error minus=eym, row sep=newline]{{
x y eyp eym
{tab}
}};
\\addlegendentry{{loaded, 250\\,m grid (bar top: 50\\,m, bottom: 1000\\,m)}}
\\addplot[only marks, mark=triangle*, mark size=2.4pt, color=black!45,
  error bars/.cd, y dir=both, y explicit,
  error bar style={{line width=0.8pt}}, error mark options={{
    rotate=90, mark size=1.6pt, line width=0.8pt}}]
  table[x=x, y=y, y error plus=eyp, y error minus=eym, row sep=newline]{{
x y eyp eym
0.00 {f250:.3f} {f50 - f250:.3f} {f250 - f1000:.3f}
}};
\\addlegendentry{{flat control}}
% --- Zeile 3: Knie-Linklaenge
\\nextgroupplot[height=2.6cm, ylabel={{Knee link length [m]}},
  ymin=0, ymax=500, xlabel={{Mean absolute grade [\\%]}},
  ylabel style={{color=figknee, font=\\footnotesize}},
  yticklabel style={{color=figknee}},
  legend columns=3,
  legend style={{at={{(0.98,0.06)}}, anchor=south east}}]
\\addplot[densely dotted, black, no marks, line width=0.7pt]
  coordinates {{(-0.15,250) ({x_max:.2f},250)}};
\\addlegendentry{{250\\,m calibration scale}}
\\addplot[only marks, mark=x, mark size=2.2pt, color=black!45,
  line width=0.8pt] coordinates {{
{paare(ke.g_abs_mean.values * 100.0, ke.knee_link_length_m.values, dy=0)}
}};
\\addlegendentry{{empty knee}}
\\addplot[only marks, mark=diamond*, mark size=2.4pt, color=black,
  fill=figknee] coordinates {{
{paare(gx, kl.knee_link_length_m.values, dy=0)}
}};
\\addlegendentry{{loaded knee}}
\\end{{groupplot}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def schreibe_fig4(dec, pfad):
    """Validitaets-Scatter dE_sim vs. dE_stat mit 45-Grad-Linie."""
    ne = dec[dec.max_link_length != 250]
    lim = max(ne.dE_sim_rel250_pct.abs().max(),
              ne.dE_stat_rel250_pct.abs().max()) * 1.08
    ld = ne[ne.loading == "loaded"]
    em = ne[ne.loading == "empty"]
    tex = f"""% Auto-generiert von plot_tikz_fig34.py aus
% grade_decomposition.csv. Nicht von Hand editieren.
\\begin{{tikzpicture}}
\\definecolor{{figsens}}{{HTML}}{{{COL_SENS}}}
\\definecolor{{figknee}}{{HTML}}{{{COL_KNEE}}}
\\begin{{axis}}[
  width=0.86\\columnwidth, scale only axis, axis equal image,
  xmin={-lim:.1f}, xmax={lim:.1f}, ymin={-lim:.1f}, ymax={lim:.1f},
  xlabel={{Simulated energy change vs.\\ 250\\,m grid [\\%]}},
  ylabel={{Static model change vs.\\ 250\\,m grid [\\%]}},
  tick label style={{font=\\scriptsize}},
  label style={{font=\\footnotesize}},
  legend style={{at={{(0.02,0.98)}}, anchor=north west,
    font=\\scriptsize, draw=none, fill=white, fill opacity=0.85,
    text opacity=1}}, legend cell align=left,
  grid=major, grid style={{black!12}},
]
\\addplot[dotted, black, no marks, line width=0.4pt, forget plot]
  coordinates {{({-lim:.1f},{-lim:.1f}) ({lim:.1f},{lim:.1f})}};
\\addplot[only marks, mark=o, mark size=1.1pt, color=figsens,
  line width=0.5pt] coordinates {{
{paare(ld.dE_sim_rel250_pct.values, ld.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{loaded}}
\\addplot[only marks, mark=square, mark size=1.0pt, color=figknee,
  line width=0.5pt] coordinates {{
{paare(em.dE_sim_rel250_pct.values, em.dE_stat_rel250_pct.values)}
}};
\\addlegendentry{{empty}}
\\end{{axis}}
\\end{{tikzpicture}}
"""
    pfad.write_text(tex, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection-dir", default=str(SELEKTION))
    args = ap.parse_args()
    sel = Path(args.selection_dir)

    df = pd.read_csv(DATA / "energy_results_summary.csv")
    knee = pd.read_csv(DATA / "knee_points.csv")
    dec = pd.read_csv(DATA / "grade_decomposition.csv")
    cand = (pd.read_csv(sel / "candidate_paths_features.csv")
            ["g_abs_mean"] * 100.0).values

    v3 = naechste_version("sensitivity_knee_tikz")
    out3 = DATA / f"sensitivity_knee_tikz_V{v3}.tex"
    schreibe_fig3(df, knee, cand, out3)
    v4 = naechste_version("decomposition_validity_tikz")
    out4 = DATA / f"decomposition_validity_tikz_V{v4}.tex"
    schreibe_fig4(dec, out4)
    print(f"TikZ: {out3.name}, {out4.name}")


if __name__ == "__main__":
    main()
