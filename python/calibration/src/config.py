from pathlib import Path

# === Pfade ===
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "results"

# === MATSim-MPM Projekt ===
# Relativ zur Kalibrierungsordnerstruktur: python/calibration/src/ -> ../../.. = MATSim-MPM/
MATSIM_MPM_DIR = PROJECT_ROOT.parent.parent
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

# === Netzauflösung (für Optuna + Sweep) ===
# Wird zur Laufzeit von run_optimization.py / run_convergence_sweep.py per
# Monkey-Patch ueberschrieben (zusammen mit MATSIM_MEMORY und N_JOBS).
ACTIVE_RESOLUTION_M: int = 250


def resource_profile_for(resolution_m: int) -> tuple[str, int]:
    """Liefert (MATSim-Heap, N_JOBS) passend zur Auflösung.

    N_JOBS = maximale Zahl gleichzeitiger MATSim-JVMs des gesamten Laufs.
    run_optimization.py teilt dieses Budget pro Studie auf
    (n_jobs_study = N_JOBS // n_scenarios), sodass total_jvms <= N_JOBS bleibt.

    Auslegung fuer den Kalibrierungs-Host (64 Kerne / 96 GB RAM):
    Ein 1m-Run belegt real ~3 GB (beobachtet), daher Heap 4G und 16 parallele
    JVMs (~48 GB real, max. 64 GB committed -> sicher unter 96 GB). Nicht hoeher,
    da Optunas TPE-Sampler bei zu vielen gleichzeitig "blind" gesampelten Trials
    Richtung Zufallssuche degeneriert (Faustregel n_jobs <= ~8-10% von N_TRIALS).
    Grobe Netze brauchen kaum Heap, daher gleiche Parallelitaet bei kleinem Heap.
    """
    if resolution_m < 50:
        return "4G", 16
    return "2G", 16


# === MATSim ===
MATSIM_MEMORY, N_JOBS = resource_profile_for(ACTIVE_RESOLUTION_M)
MATSIM_ITERATIONS = 1

# Obergrenze paralleler Trials pro Studie. Die Studien laufen sequenziell, daher
# wuerde sonst das ganze JVM-Budget in eine Studie fliessen (N_JOBS // n_scenarios
# = 16 bei Einzel-Szenarien). Dann sampelt Optunas TPE-Sampler zu viele Trials
# "blind" aus demselben Prior, bevor Feedback aus fertigen Trials eintrifft, und
# degeneriert Richtung Zufallssuche. 8 haelt die Parallelitaet bei ~4 % von
# N_TRIALS (Faustregel <= 8-10 %) -> deutlich gesuendere Konvergenz.
MAX_PARALLEL_TRIALS_PER_STUDY = 8

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

# === Trip-End-KE-Korrektur (nur Vergleichsseite!) ===
# Das Java-Modell endet jedes Leg bei vEnd > 0 (letzter Link-Freespeed) und
# bremst nie. Die Korrektur haengt von der RANDBEDINGUNG der Referenz ab:
#
#   "stop"    - Referenzzyklus bremst am Ende auf 0 (VECTO .vdri: letzter
#               Punkt v=0 + Stop-Flag). Die Referenz enthaelt also bereits
#               die Rekuperation der End-Bremsung, die dem Modell fehlt:
#               E_korr = 0.5 * mInertia * vEnd^2 * recupEfficiency
#   "rolling" - Referenzfenster endet ROLLEND (Realfahrt-Batteriedelta).
#               Kein Bremsvorgang; Start- und End-KE heben sich im Fenster
#               auf, das Modell hat aber die Anfahr-KE von 0 gebucht:
#               E_korr = 0.5 * mInertia * vEnd^2 / tractionEfficiency
#
# FALSCH herum angewendet ueberkorrigiert das um Faktor 1/(eta_t*eta_r)~1.5
# (Befund 2026-07-02: B2-Lauf mit "rolling" auf VECTO drueckte
# tractionEfficiency unphysisch auf ~0.80). VECTO-Kalibrierung => "stop".
# BEWUSST NICHT im Java-Modell verankert (Entscheidung 2026-07-02): im
# deutschlandweiten Flottenszenario gibt es dieses Fenster-Artefakt nicht,
# das Modell bleibt dort physikalisch unveraendert.
TRIP_END_KE_CORRECTION = True
TRIP_END_KE_BOUNDARY = "stop"   # VECTO-Zyklen enden im Stillstand

# === Kalibrierungsparameter-Bereiche
# Wertebereiche (low, high) fuer die 6 Optuna-Parameter.
# a1/a2 entfallen: kinetische Energie wird jetzt exakt pro Link berechnet.
# RatedPower entfaellt: wird fahrzeugspezifisch in der MATSim-Vehicles-Datei definiert.
# auxPowerW entfaellt: fix bei 4000 W (Java-Default DEF_AUX_POWER_W), da nur sehr
# geringe Auswirkung auf den Gesamtverbrauch.
# cdXA/rollingC ueberschreiben zur Laufzeit die Werte aus vehicles.xml (Override in
# MpmDischargingModule + PowerLimitedLinkSpeedCalculator, sobald in der Params-Datei gesetzt).
PARAM_BOUNDS = {
    "tractionEfficiency":     (0.75,   0.95),     # Gesamteffizienz Batterie→Rad bei Traktion [-]
    "inertiaC":               (1.01,   1.05),     # Traegheitsbeiwert [-] (rotierende Massen: Raeder, Antrieb)
    "recupEfficiency":        (0.45,   0.85),     # Rekuperations-Wirkungsgrad [-]
    "maxRecupPowerFraction":  (0.5,    1.0),      # Anteil maxMotorPowerW, der fuer Rekuperation nutzbar ist [-]
    "cdXA":                   (5.65,      5.93),       # Luftwiderstand CdxA [m²] – Grenzen EU-Klasse A15
    "rollingC":               (0.0045225, 0.0055275),  # Rollwiderstand [-] – Mittel(RRC48,RRC53)=0.005025 ±10%
}