from pathlib import Path

# === Pfade ===
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "results"

# === MATSim-MPM Projekt ===
MATSIM_MPM_DIR = Path(r"C:\Users\Tobias\IdeaProjects\MATSim-MPM")
MATSIM_JAR = MATSIM_MPM_DIR / "matsim-example-project-0.0.1-SNAPSHOT.jar"

# === Fahrzeuggruppen (je eine Kalibrierungsstudie pro Gruppe) ===
# Zum Wechseln: ACTIVE_VEHICLE_GROUP aendern und Optimierung neu starten.
VEHICLE_GROUPS = {
    "Volvo_FH42TE": {
        "LongHaul": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_Longhaul" / "config.xml",
            "route_km": 100.185,
        },
        "RegionalDelivery": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_RegionalDelivery" / "config.xml",
            "route_km": 100.000,
        },
    },
    "IVECO_SeWay": {
        "LongHaul": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_Longhaul_IVECO_SeWay" / "config.xml",
            "route_km": 100.185,
        },
        "RegionalDelivery": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_RegionalDelivery_IVECO_SeWay" / "config.xml",
            "route_km": 100.000,
        },
    },
}

# Aktives Fahrzeug — hier aendern fuer andere Kalibrierung:
ACTIVE_VEHICLE_GROUP = "IVECO_SeWay"

SCENARIOS = VEHICLE_GROUPS[ACTIVE_VEHICLE_GROUP]
# Abwaertskompatibel: MATSIM_CONFIG zeigt auf Long Haul
MATSIM_CONFIG = SCENARIOS["LongHaul"]["config"]

# === Referenzdaten (VECTO EEA 2023) ===
REFERENCE_CONSUMPTION_FILE = DATA_DIR / "reference_consumption.csv"
# === Optuna ===
STUDY_NAME = "matsim-vecto-calibration"
N_TRIALS = 100
STORAGE = f"sqlite:///{RESULTS_DIR / 'optuna_study.db'}"

# === MATSim ===
# 3G pro JVM: N_JOBS parallele Trials x 2 Szenarien x 3G = N_JOBS*6G RAM
MATSIM_MEMORY = "3G"
MATSIM_ITERATIONS = 1

# === Parallelisierung ===
# N_JOBS parallele Optuna-Trials gleichzeitig.
# RAM-Bedarf: N_JOBS * 2 Szenarien * MATSIM_MEMORY
# Beispiel: 2 * 2 * 3G = 12G (passt bei 13G freiem RAM)
N_JOBS = 2

# Pfad zur Kalibrierungsparameter-Datei (wird pro Trial geschrieben)
CALIBRATION_PARAMS_FILE = RESULTS_DIR / "calibration_params.properties"

# === Beladungsklassen ===
# Jede Klasse wird als separate Optuna-Studie optimiert.
# "low"  = Leerfahrt-Szenario (geringe Zuladung)
# "high" = Vollladungs-Szenario (hohe Zuladung)
# Fahrzeug-IDs enden jeweils auf "_low" bzw. "_high".
PAYLOAD_CLASSES: list[str] = ["low", "high"]

# Wird zur Laufzeit von run_optimization.py gesetzt (Monkey-Patch).
# Steuert, welche Fahrzeuge in die Fehlerberechnung einfliessen.
ACTIVE_PAYLOAD_CLASS: str = "all"

# === Kalibrierungsparameter-Bereiche
# Wertebereiche (low, high) fuer die 4 Optuna-Parameter
# a1/a2 entfallen: kinetische Energie wird jetzt exakt pro Link berechnet
# RatedPower entfaellt: wird fahrzeugspezifisch in der MATSim-Vehicles-Datei definiert
PARAM_BOUNDS = {
    "tractionEfficiency":    (0.70,   0.90),       # Gesamteffizienz Batterie→Rad bei Traktion [-]
    "inertiaC":              (1.03,  1.03),       # Traegheitsbeiwert [-] (rotierende Massen: Raeder, Antrieb)
    "recupEfficiency":       (0.50,   0.80),        # Rekuperations-Wirkungsgrad [-]
    "maxRecupPowerFraction": (0.70,   0.90),        # Max. Rekuperationsleistung als Anteil der fahrzeugspezifischen RatedPower [-]
    "auxPowerW":             (2_500,   7_500.0),    # Konstante Nebenverbrauchsleistung [W]
}