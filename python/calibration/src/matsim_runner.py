import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.config import (
    MATSIM_JAR, MATSIM_MEMORY, MATSIM_ITERATIONS, SCENARIOS,
)
from src import config as _cfg  # Laufzeit-Zugriff, damit Monkey-Patch aus run_optimization greift


def write_calibration_params(params: dict[str, float], path: Path) -> None:
    """Schreibt die Kalibrierungsparameter als .properties-Datei.

    Args:
        params: Dictionary mit den Kalibrierungsparametern.
        path: Zieldatei (trial-spezifisch, damit parallele Trials sich nicht ueberschreiben).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for key, value in params.items():
            f.write(f"{key}={value}\n")


def run_matsim(run_id: str, config_path: Path, params_file: Path) -> Path:
    """Startet einen einzelnen MATSim-Lauf.

    Wird typischerweise nicht direkt aufgerufen, sondern ueber run_all_scenarios.

    Args:
        run_id: Bezeichner fuer das Output-Verzeichnis (z.B. "trial_3_LongHaul").
        config_path: Pfad zur MATSim-Konfigurationsdatei des Szenarios.
        params_file: Trial-spezifische Kalibrierungsparameter-Datei.

    Returns:
        Pfad zum MATSim-Output-Verzeichnis.
    """
    output_dir = _cfg.RESULTS_DIR / "matsim_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "java",
        f"-Xmx{MATSIM_MEMORY}",
        f"-Dcalibration.params.file={params_file}",
        "-jar", str(MATSIM_JAR),
        str(config_path),
        "--config:controler.outputDirectory", str(output_dir),
        f"--config:controler.lastIteration={MATSIM_ITERATIONS - 1}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Log-Output nach Prozessende in Datei schreiben (nicht vorher oeffnen,
    # da MATSim's OutputDirectoryHierarchy das Verzeichnis beim Start loescht)
    log_path = output_dir / "logfile.log"
    log_path.write_text(result.stdout or "", encoding="utf-8")

    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").splitlines()
        tail = "\n".join(lines[-50:])
        raise RuntimeError(
            f"MATSim-Lauf fehlgeschlagen [{run_id}] (exit code {result.returncode}):\n"
            f"{tail}"
        )

    return output_dir


def run_all_scenarios(params: dict[str, float],
                      run_id_prefix: str | None = None) -> dict[str, Path]:
    """Schreibt trial-spezifische Kalibrierungsparameter und fuehrt alle Szenarien parallel aus.

    LongHaul- und RegionalDelivery-Lauf starten gleichzeitig in separaten JVMs.
    Jeder Trial schreibt seine eigene Parameterdatei, sodass parallele Trials
    sich nicht gegenseitig ueberschreiben (kein Race Condition).

    Args:
        params: Kalibrierungsparameter fuer diesen Trial.
        run_id_prefix: Praefix fuer Output-Verzeichnisse (z.B. "trial_3").

    Returns:
        Dict {scenario_name: output_dir}
    """
    prefix = run_id_prefix or "latest"

    # Trial-spezifische Parameterdatei (verhindert Race Condition bei n_jobs > 1)
    params_file = _cfg.RESULTS_DIR / "matsim_runs" / f"{prefix}_params.properties"
    write_calibration_params(params, params_file)

    with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as executor:
        futures = {
            executor.submit(run_matsim, f"{prefix}_{name}", scenario["config"], params_file): name
            for name, scenario in SCENARIOS.items()
        }

        outputs = {}
        for future in as_completed(futures):
            name = futures[future]
            outputs[name] = future.result()  # wirft RuntimeError bei MATSim-Fehler

    return outputs
