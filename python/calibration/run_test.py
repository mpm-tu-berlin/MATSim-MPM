"""
Testlauf: fuehrt einen einzelnen MATSim-Durchlauf mit Standardparametern durch.
Dient zur Verifikation der MATSim-Konfiguration und des Fahrzeugmodells,
ohne Optuna-Optimierung oder Fehlerberechnung.

Aufruf:
    .venv/Scripts/python run_test.py
"""

from src.matsim_runner import run_all_scenarios
from src.config import PARAM_BOUNDS

# Standardwerte (Mitte der jeweiligen Wertebereiche)
params = {name: (low + high) / 2 for name, (low, high) in PARAM_BOUNDS.items()}

print("=== MATSim-Testlauf ===")
print("Parameter:")
for name, value in params.items():
    print(f"  {name} = {value:.4f}")
print()

outputs = run_all_scenarios(params, run_id_prefix="test")

print()
print("=== Ergebnis ===")
for scenario, path in outputs.items():
    print(f"  {scenario}: {path}")
print()
print("Testlauf abgeschlossen.")
