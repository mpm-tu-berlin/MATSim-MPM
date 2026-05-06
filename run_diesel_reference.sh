#!/usr/bin/env bash
# Run all diesel reference scenarios (5 in parallel).

set -euo pipefail

JAVA="/c/Users/diego/.jdks/openjdk-25.0.1/bin/java"
JAR="matsim-example-project-0.0.1-SNAPSHOT.jar"
MAIN="org.matsim.mpm.run.RunBetScenario"

CONFIGS=(
    scenarios/BETs/1pct_diesel_reference/config.xml
    scenarios/BETs/5pct_diesel_reference/config.xml
    scenarios/BETs/10pct_diesel_reference/config.xml
    scenarios/BETs/15pct_diesel_reference/config.xml
    scenarios/BETs/20pct_diesel_reference/config.xml
)

mkdir -p logs

echo "Starting ${#CONFIGS[@]} diesel reference scenarios in parallel..."

pids=()
for config in "${CONFIGS[@]}"; do
    logfile="logs/$(basename "$(dirname "$config")").log"
    echo "  Launching: $config -> $logfile"
    "$JAVA" -cp "$JAR" "$MAIN" "$config" > "$logfile" 2>&1 &
    pids+=($!)
done

failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        failed=$((failed + 1))
    fi
done

if [ "$failed" -gt 0 ]; then
    echo "WARNING: $failed scenario(s) failed."
else
    echo "All diesel reference scenarios completed successfully."
fi
