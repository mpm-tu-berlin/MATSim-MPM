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
N_TRIALS = 150
STORAGE = f"sqlite:///{RESULTS_DIR / 'optuna_study.db'}"

# === MATSim ===
# 5G pro JVM: LH- und RD-Szenario laufen parallel (2x5G=10G < 12G freier RAM)
MATSIM_MEMORY = "5G"
MATSIM_ITERATIONS = 1

# Pfad zur Kalibrierungsparameter-Datei (wird pro Trial geschrieben)
CALIBRATION_PARAMS_FILE = RESULTS_DIR / "calibration_params.properties"

# === Kalibrierungsparameter-Bereiche ===
# Wertebereiche (low, high) fuer die 4 Optuna-Parameter
# a1/a2 entfallen: kinetische Energie wird jetzt exakt pro Link berechnet
# RatedPower entfaellt: wird fahrzeugspezifisch in der MATSim-Vehicles-Datei definiert
PARAM_BOUNDS = {
    "drivetrainEfficiency":  (0.7, 0.8),  # Antriebsstrang-Effizienz [-]
    "inertiaC":              (1.01,  1.05),   # Traegheitsbeiwert [-] (rotierende Massen: Raeder, Antrieb)
    "recupEfficiency":       (0.75,  0.95),   # Rekuperations-Wirkungsgrad [-]
    "maxRecupPowerFraction": (0.8,  1),   # Max. Rekuperationsleistung als Anteil der fahrzeugspezifischen RatedPower [-]
}