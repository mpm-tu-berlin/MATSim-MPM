import subprocess
from pathlib import Path

import pandas as pd

from src.config import VECTO_EXECUTABLE, DATA_DIR, RESULTS_DIR


def run_vecto(params: dict) -> dict:
    """Führt VECTO mit den gegebenen Parametern aus.

    Args:
        params: Dictionary mit VECTO-Parametern
                (z.B. rolling_resistance, air_drag_coefficient, ...).

    Returns:
        Dictionary mit VECTO-Ergebnissen (Verbrauchswerte, CO2, etc.).
    """
    # TODO: Parameter in VECTO-Input-Dateien schreiben
    # TODO: VECTO-Simulation starten
    # TODO: Ergebnisse parsen und zurückgeben

    raise NotImplementedError("VECTO-Wrapper muss noch implementiert werden")


def compute_error(matsim_result: Path) -> float:
    """Berechnet den Fehler zwischen Simulations- und Referenzdaten.

    Args:
        matsim_result: Pfad zum MATSim-Output-Verzeichnis.

    Returns:
        Skalarer Fehlerwert (z.B. RMSE).
    """
    # TODO: MATSim-Output einlesen
    # TODO: Mit Referenzdaten vergleichen
    # TODO: Fehlermetrik berechnen (RMSE, MAE, etc.)

    raise NotImplementedError("Fehlerberechnung muss noch implementiert werden")
