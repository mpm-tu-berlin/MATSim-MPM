import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.config import (
    MATSIM_JAR, MATSIM_ITERATIONS,
)
from src import config as _cfg  # Laufzeit-Zugriff, damit Monkey-Patch aus run_optimization greift


# Mission -> (Subdir-Name unter scenarios/, Netz-Stem ohne Suffix).
# Wird benutzt, um aus _cfg.ACTIVE_RESOLUTION_M den passenden Netzpfad
# fuer das jeweilige Szenario zu konstruieren.
_NETWORK_LOCATION: dict[str, tuple[str, str]] = {
    "LongHaul":         ("VECTO_Longhaul",         "longhaul_network"),
    "RegionalDelivery": ("VECTO_RegionalDelivery", "regional_delivery_network"),
}


def _resolution_overrides(scenario_name: str, config_path: Path,
                          resolution_m: int) -> list[str]:
    """Baut die --config-Overrides fuer Netz, Plans und qsim.timeStepSize.

    Netzwerk liegt unter `scenarios/VECTO_<Mission>/<stem>_<N>m.xml.gz`,
    Plans neben dem Szenario-Config als `plans_<N>m.xml`.
    Bei N < 100 wird timeStepSize auf 0.04 gesetzt (sonst rundet die QSim
    Linkdurchfahrtszeiten so stark, dass 100km-Trips in 24h nicht durchkommen).
    """
    if scenario_name not in _NETWORK_LOCATION:
        return []
    subdir, stem = _NETWORK_LOCATION[scenario_name]
    network_path = _cfg.MATSIM_MPM_DIR / "scenarios" / subdir / f"{stem}_{resolution_m}m.xml.gz"
    plans_path = config_path.parent / f"plans_{resolution_m}m.xml"
    timestep = "0.04" if resolution_m < 100 else "1.0"
    return [
        "--config:network.inputNetworkFile", str(network_path),
        "--config:plans.inputPlansFile",     str(plans_path),
        "--config:qsim.timeStepSize",        timestep,
    ]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Beendet den Subprozess inklusive aller Kindprozesse.

    Auf Windows ist das `java`, das wir via PATH starten, nur ein Stub, der die
    eigentliche JVM als Kindprozess re-exect. proc.kill() wuerde nur den Stub
    treffen und die JVM verwaisen lassen -> daher taskkill /T (ganzer Baum).
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


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


def run_matsim(run_id: str, config_path: Path, params_file: Path,
               scenario_name: str | None = None) -> Path:
    """Startet einen einzelnen MATSim-Lauf.

    Wird typischerweise nicht direkt aufgerufen, sondern ueber run_all_scenarios.

    Args:
        run_id: Bezeichner fuer das Output-Verzeichnis (z.B. "trial_3_LongHaul").
        config_path: Pfad zur MATSim-Konfigurationsdatei des Szenarios.
        params_file: Trial-spezifische Kalibrierungsparameter-Datei.
        scenario_name: Optionaler Szenario-Name (z.B. "LongHaul"); wenn gesetzt,
            werden Netzwerk/Plans/timeStepSize entsprechend `_cfg.ACTIVE_RESOLUTION_M`
            via CLI ueberschrieben.

    Returns:
        Pfad zum MATSim-Output-Verzeichnis.
    """
    output_dir = _cfg.RESULTS_DIR / "matsim_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "java",
        f"-Xmx{_cfg.MATSIM_MEMORY}",
        f"-Dcalibration.params.file={params_file}",
        "-jar", str(MATSIM_JAR),
        str(config_path),
        "--config:controler.outputDirectory", str(output_dir),
        f"--config:controler.lastIteration={MATSIM_ITERATIONS - 1}",
        # Einzel-Fahrzeug-Szenario -> paralleles QSim bringt keinen Speedup, aber sein
        # Thread-Pool (QNetsimEngine_PooledThread_*) wird bei Shutdown nicht immer
        # sauber beendet. Diese Nicht-Daemon-Threads verhindern dann das JVM-Exit, die
        # JVM haengt nach return von main() als Zombie, und subprocess.run() blockiert
        # ewig. Mit 1 Thread gibt es keinen Pool -> JVM beendet zuverlaessig.
        "--config:qsim.numberOfThreads", "1",
    ]
    if scenario_name is not None:
        cmd.extend(_resolution_overrides(scenario_name, config_path,
                                         _cfg.ACTIVE_RESOLUTION_M))

    # MATSims QSim-/Events-Threadpools (QNetsimEngine_PooledThread_*) werden beim
    # Shutdown nicht zuverlaessig beendet. Diese Nicht-Daemon-Threads halten die
    # JVM nach return von main() am Leben -> der Prozess haengt als Zombie und ein
    # blockierendes wait() wuerde ewig warten (beobachtet: >75 min). Daher NICHT
    # auf das JVM-Exit verlassen, sondern auf den fachlichen Erfolgs-Marker:
    # sobald output_events.xml.gz fertig geschrieben ist (Groesse stabil), ist
    # alles da, was die Auswertung braucht (parse_events_consumption) -> JVM killen.
    events_file = output_dir / "output_events.xml.gz"
    POLL_S = 2.0
    STABLE_S = 15.0       # output_events so lange unveraendert -> fertig geschrieben
    BACKSTOP_S = 1200.0   # absolute Obergrenze (echter Haenger ganz ohne Output)

    # stdout/stderr in TEMP-Datei (nicht PIPE: ungeleerter Puffer wuerde den
    # Subprozess blockieren; nicht in output_dir: MATSim loescht den Ordner beim Start).
    log_fd, log_tmp = tempfile.mkstemp(prefix=f"{run_id}_", suffix=".log")
    rc: int | None = None
    killed_zombie = False
    t0 = time.monotonic()
    try:
        with os.fdopen(log_fd, "w", encoding="utf-8", errors="replace") as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    text=True)
            last_size = -1
            stable_since: float | None = None
            while True:
                rc = proc.poll()
                if rc is not None:
                    break  # JVM hat sich regulaer beendet (Normalfall)

                if events_file.exists():
                    size = events_file.stat().st_size
                    if size > 0 and size == last_size:
                        if stable_since is None:
                            stable_since = time.monotonic()
                        elif time.monotonic() - stable_since >= STABLE_S:
                            _kill_process_tree(proc)  # MATSim fertig, nur Zombie uebrig
                            rc = 0
                            killed_zombie = True
                            break
                    else:
                        last_size = size
                        stable_since = None

                if time.monotonic() - t0 > BACKSTOP_S:
                    _kill_process_tree(proc)
                    rc = proc.returncode if proc.returncode not in (None, 0) else -1
                    break

                time.sleep(POLL_S)

        # Log in output_dir uebernehmen (existiert jetzt; MATSim hat es angelegt)
        try:
            (output_dir / "logfile.log").write_text(
                Path(log_tmp).read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        except OSError:
            pass

        # Erfolg = Events-Datei vorhanden (egal ob regulaeres Exit oder Zombie-Kill).
        if not events_file.exists() or rc != 0:
            tail = "\n".join(
                Path(log_tmp).read_text(encoding="utf-8", errors="replace")
                .splitlines()[-50:]
            )
            raise RuntimeError(
                f"MATSim-Lauf fehlgeschlagen [{run_id}] "
                f"(exit code {rc}, zombie_kill={killed_zombie}):\n{tail}"
            )
    finally:
        Path(log_tmp).unlink(missing_ok=True)

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

    with ThreadPoolExecutor(max_workers=len(_cfg.SCENARIOS)) as executor:
        futures = {
            executor.submit(run_matsim, f"{prefix}_{name}", scenario["config"],
                            params_file, scenario_name=name): name
            for name, scenario in _cfg.SCENARIOS.items()
        }

        outputs = {}
        for future in as_completed(futures):
            name = futures[future]
            outputs[name] = future.result()  # wirft RuntimeError bei MATSim-Fehler

    return outputs
