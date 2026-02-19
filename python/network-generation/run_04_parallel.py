import os, sys, subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from rich.live import Live
from rich.table import Table

AREA = "Germany"
LENGTHS = [999999]
MAX_WORKERS = min(30, len(LENGTHS))

# Oversubscription vermeiden
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# tqdm der Worker standardmäßig ausschalten (sauberere Anzeige)
os.environ.setdefault("TQDM_DISABLE", "0")

# Optional: Logs pro Job schreiben (True/False)
WRITE_LOGS = False
LOG_DIR = "logs"

def run_one(L: int) -> int:
    cmd = [
        sys.executable,
        "04_build_matsim_network_from_local_osm_and_kdtree.py",
        "--area", AREA,
        "--max-length", str(L),
        "--version", "V0",
    ]
    if WRITE_LOGS:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"{AREA}_{L}.out"), "w") as out, \
                open(os.path.join(LOG_DIR, f"{AREA}_{L}.err"), "w") as err:
            return subprocess.call(cmd, stdout=out, stderr=err)
    else:
        return subprocess.call(cmd)

def render(status: dict) -> Table:
    t = Table(title="Parallel-Run (30 Kerne)")
    t.add_column("max_length [m]", justify="right")
    t.add_column("Status")
    for L in sorted(status):
        t.add_row(str(L), status[L])
    return t

if __name__ == "__main__":
    status = {L: "⏳ läuft…" for L in LENGTHS}

    with Live(render(status), refresh_per_second=6) as live:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(run_one, L): L for L in LENGTHS}
            for fut in as_completed(futs):
                L = futs[fut]
                rc = fut.result()
                status[L] = "✅ fertig" if rc == 0 else f"❌ Fehler (rc={rc})"
                # WICHTIG: Tabelle nach jeder Änderung neu rendern
                live.update(render(status))

        # finaler Refresh bevor Live-Kontext endet
        live.update(render(status))

    # Non-Zero Rückgaben zusammenfassen (optional)
    failures = [L for L, s in status.items() if not s.startswith("✅")]
    if failures:
        print(f"Fehlgeschlagene Läufe: {failures}")
        sys.exit(1)
    print("Alle Läufe erfolgreich.")
