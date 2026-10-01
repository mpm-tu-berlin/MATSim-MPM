#!/usr/bin/env bash
# Run replanning phase 2 scenarios (iterations 51-100, ReRoute 0.1).
# Two groups of 5, each group runs in parallel.

set -euo pipefail

JAVA="/c/Users/diego/.jdks/openjdk-25.0.1/bin/java"
JAR="matsim-example-project-0.0.1-SNAPSHOT.jar"
MAIN="org.matsim.mpm.run.RunBetScenario"

# All 10pct scenarios in one group
SCENARIOS=(
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_0290/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_267_005/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_311_004/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_323_021/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_392_054/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_0217/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_581_051/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_628_027/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_640_041/config_phase2.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_646_030/config_phase2.xml
)

mkdir -p logs

run_group() {
    local group_name="$1"
    shift
    local configs=("$@")

    echo ""
    echo "========================================"
    echo "Starting group: $group_name (${#configs[@]} scenarios in parallel)"
    echo "========================================"

    local pids=()
    for config in "${configs[@]}"; do
        local parent=$(basename "$(dirname "$config")")
        local grandparent=$(basename "$(dirname "$(dirname "$config")")")
        logfile="logs/replanning_phase2_${grandparent}_${parent}.log"
        echo "  Launching: $config -> $logfile"
        "$JAVA" -Djava.awt.headless=true -Xmx23g -XX:MaxMetaspaceSize=512m -XX:+UseG1GC -cp "$JAR" "$MAIN" "$config" > "$logfile" 2>&1 &
        pids+=($!)
    done

    local failed=0
    for pid in "${pids[@]}"; do
        if ! wait "$pid"; then
            failed=$((failed + 1))
        fi
    done

    if [ "$failed" -gt 0 ]; then
        echo "  WARNING: $failed scenario(s) failed in group $group_name"
    else
        echo "  Group $group_name completed successfully."
    fi
}

run_group "10pct phase2"  "${SCENARIOS[@]}"

echo ""
echo "All phase 2 scenarios (10 total) completed."