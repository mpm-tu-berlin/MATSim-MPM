import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.config import (
    MATSIM_JAR, MATSIM_MEMORY, MATSIM_ITERATIONS, SCENARIOS,
)
from src import config as _cfg  # Laufzeit-Zugriff, damit Monkey-Patch aus run_optimization greift


def write_calibration_params(params: dict[str, float]) -> Path:
    """Schreibt die Kalibrierungsparameter als .properties-Datei.

    Args:
        params: Dictionary mit den Kalibrierungsparametern.

    Returns:
        Pfad zur geschriebenen Datei.
    """
    _cfg.CALIBRATION_PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_cfg.CALIBRATION_PARAMS_FILE, "w", encoding="utf-8") as f:
        for key, value in params.items():
            f.write(f"{key}={value}\n")
    return _cfg.CALIBRATION_PARAMS_FILE


def run_matsim(run_id: str, config_path: Path) -> Path:
    """Startet einen einzelnen MATSim-Lauf.

    Setzt voraus, dass CALIBRATION_PARAMS_FILE bereits geschrieben wurde.
    Wird typischerweise nicht direkt aufgerufen, sondern ueber run_all_scenarios.

    Args:
        run_id: Bezeichner fuer das Output-Verzeichnis (z.B. "trial_3_LongHaul").
        config_path: Pfad zur MATSim-Konfigurationsdatei des Szenarios.

    Returns:
        Pfad zum MATSim-Output-Verzeichnis.
    """
    output_dir = _cfg.RESULTS_DIR / "matsim_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "java",
        f"-Xmx{MATSIM_MEMORY}",
        f"-Dcalibration.params.file={_cfg.CALIBRATION_PARAMS_FILE}",
        "-jar", str(MATSIM_JAR),
        str(config_path),
        "--config:controler.outputDirectory", str(output_dir),
        f"--config:controler.lastIteration={MATSIM_ITERATIONS - 1}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"MATSim-Lauf fehlgeschlagen [{run_id}] (exit code {result.returncode}):\n"
            f"{result.stderr}"
        )

    return output_dir


def run_all_scenarios(params: dict[str, float],
                      run_id_prefix: str | None = None) -> dict[str, Path]:
    """Schreibt Kalibrierungsparameter und fuehrt alle Szenarien parallel aus.

    LongHaul- und RegionalDelivery-Lauf starten gleichzeitig in separaten JVMs
    (je -Xmx{MATSIM_MEMORY}), um die Gesamtlaufzeit pro Trial zu halbieren.
    Die Parameterdatei wird einmalig vor dem parallelen Start geschrieben, damit
    kein Race Condition zwischen den Threads entsteht.

    Args:
        params: Kalibrierungsparameter fuer diesen Trial.
        run_id_prefix: Praefix fuer Output-Verzeichnisse (z.B. "trial_3").

    Returns:
        Dict {scenario_name: output_dir}
    """
    # Parameterdatei einmalig schreiben (vor dem parallelen Start)
    write_calibration_params(params)

    prefix = run_id_prefix or "latest"

    with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as executor:
        futures = {
            executor.submit(run_matsim, f"{prefix}_{name}", scenario["config"]): name
            for name, scenario in SCENARIOS.items()
        }

        outputs = {}
        for future in as_completed(futures):
            name = futures[future]
            outputs[name] = future.result()  # wirft RuntimeError bei MATSim-Fehler

    return outputs
