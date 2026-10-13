"""
Testlauf: fuehrt einen einzelnen MATSim-Durchlauf mit Standardparametern durch.
Dient zur Verifikation der MATSim-Konfiguration und des Fahrzeugmodells,
ohne Optuna-Optimierung oder Fehlerberechnung.

Aufruf:
    .venv/Scripts/python run_test.py                       # Default-Auflösung
    .venv/Scripts/python run_test.py --resolution 1        # 1m
    .venv/Scripts/python run_test.py --resolution 100      # 100m
"""

import argparse

import src.config as _cfg
from src.matsim_runner import run_all_scenarios
from src.config import PARAM_BOUNDS

parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument(
    "--resolution",
    type=int,
    default=_cfg.ACTIVE_RESOLUTION_M,
    help=f"Netzauflösung in Metern. Default: {_cfg.ACTIVE_RESOLUTION_M}.",
)
args = parser.parse_args()

# Resolution + zugehoeriges RAM/N_JOBS-Profil setzen
_cfg.ACTIVE_RESOLUTION_M = args.resolution
_cfg.MATSIM_MEMORY, _cfg.N_JOBS = _cfg.resource_profile_for(args.resolution)

# Standardwerte (Mitte der jeweiligen Wertebereiche)
params = {name: (low + high) / 2 for name, (low, high) in PARAM_BOUNDS.items()}

print("=== MATSim-Testlauf ===")
print(f"Auflösung:           {_cfg.ACTIVE_RESOLUTION_M} m")
print(f"MATSim-Heap:         {_cfg.MATSIM_MEMORY}")
print("Parameter:")
for name, value in params.items():
    print(f"  {name} = {value:.4f}")
print()

outputs = run_all_scenarios(params, run_id_prefix=f"test_{args.resolution}m")

print()
print("=== Ergebnis ===")
for scenario, path in outputs.items():
    print(f"  {scenario}: {path}")
print()
print("Testlauf abgeschlossen.")
