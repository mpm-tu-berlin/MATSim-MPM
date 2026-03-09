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
    "BET_G5": {
        "LongHaul": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_Longhaul_BET_G5" / "config.xml",
            "route_km": 100.185,
        },
        "RegionalDelivery": {
            "config": MATSIM_MPM_DIR / "scenarios" / "VECTO_RegionalDelivery_BET_G5" / "config.xml",
            "route_km": 100.000,
        },
    },
}

# Aktives Fahrzeug — hier aendern fuer andere Kalibrierung:
ACTIVE_VEHICLE_GROUP = "BET_G5"

SCENARIOS = VEHICLE_GROUPS[ACTIVE_VEHICLE_GROUP]
# Abwaertskompatibel: MATSIM_CONFIG zeigt auf Long Haul
MATSIM_CONFIG = SCENARIOS["LongHaul"]["config"]

# === Referenzdaten (VECTO EEA 2023) ===
REFERENCE_CONSUMPTION_FILE = DATA_DIR / "reference_consumption.csv"
# === Optuna ===
STUDY_NAME = "matsim-vecto-calibration"
N_TRIALS = 200
STORAGE = f"sqlite:///{RESULTS_DIR / 'optuna_study.db'}"

# === MATSim ===
# 3G pro JVM: N_JOBS parallele Trials x 2 Szenarien x 3G = N_JOBS*6G RAM
MATSIM_MEMORY = "1G"
MATSIM_ITERATIONS = 1

# === Parallelisierung ===
# N_JOBS parallele Optuna-Trials gleichzeitig.
# RAM-Bedarf: N_JOBS * 2 Szenarien * MATSIM_MEMORY
# Beispiel: 2 * 2 * 3G = 12G (passt bei 13G freiem RAM)
N_JOBS = 4

# Pfad zur Kalibrierungsparameter-Datei (wird pro Trial geschrieben)
CALIBRATION_PARAMS_FILE = RESULTS_DIR / "calibration_params.properties"

# === Studien-Konfiguration ===
# 5 Optuna-Studien: 4 Einzel-Szenarien + 1 Gesamtstudie
STUDIES = [
    {"name": "lh_low",  "scenarios": ["LongHaul"],                     "payload_class": "low"},
    {"name": "lh_high", "scenarios": ["LongHaul"],                     "payload_class": "high"},
    {"name": "rd_low",  "scenarios": ["RegionalDelivery"],             "payload_class": "low"},
    {"name": "rd_high", "scenarios": ["RegionalDelivery"],             "payload_class": "high"},
    {"name": "all",     "scenarios": ["LongHaul", "RegionalDelivery"], "payload_class": "all"},
]

# Wird zur Laufzeit von run_optimization.py gesetzt (Monkey-Patch).
ACTIVE_PAYLOAD_CLASS: str = "all"

# === Kalibrierungsparameter-Bereiche
# Wertebereiche (low, high) fuer die 4 Optuna-Parameter
# a1/a2 entfallen: kinetische Energie wird jetzt exakt pro Link berechnet
# RatedPower entfaellt: wird fahrzeugspezifisch in der MATSim-Vehicles-Datei definiert
PARAM_BOUNDS = {
    "tractionEfficiency":    (0.8,   0.9),       # Gesamteffizienz Batterie→Rad bei Traktion [-]
    "inertiaC":              (1.01,   1.05),       # Traegheitsbeiwert [-] (rotierende Massen: Raeder, Antrieb)
    "recupEfficiency":       (0.45,   0.85),       # Rekuperations-Wirkungsgrad [-]
    "auxPowerW":             (4_000,  5_000.0),   # Konstante Nebenverbrauchsleistung [W]
}