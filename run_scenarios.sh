#!/usr/bin/env bash
# Run all BET scenarios in 3 groups of 5 parallel processes each.
# Each group waits for all 5 to finish before the next group starts.

set -euo pipefail

JAVA="/c/Users/diego/.jdks/openjdk-25.0.1/bin/java"
JAR="matsim-example-project-0.0.1-SNAPSHOT.jar"
MAIN="org.matsim.mpm.run.RunBetScenario"

# Group 1: operational
GROUP1=(
    scenarios/BETs/1pct_BETs_unlimited_operational/config.xml
    scenarios/BETs/5pct_BETs_unlimited_operational/config.xml
    scenarios/BETs/10pct_BETs_unlimited_operational/config.xml
    scenarios/BETs/15pct_BETs_unlimited_operational/config.xml
    scenarios/BETs/20pct_BETs_unlimited_operational/config.xml
)

# Group 2: deutschlandnetz
GROUP2=(
    scenarios/BETs/1pct_BETs_unlimited_deutschlandnetz/config.xml
    scenarios/BETs/5pct_BETs_unlimited_deutschlandnetz/config.xml
    scenarios/BETs/10pct_BETs_unlimited_deutschlandnetz/config.xml
    scenarios/BETs/15pct_BETs_unlimited_deutschlandnetz/config.xml
    scenarios/BETs/20pct_BETs_unlimited_deutschlandnetz/config.xml
)

# Group 3: diesel reference
GROUP3=(
    scenarios/BETs/1pct_diesel_reference/config.xml
    scenarios/BETs/5pct_diesel_reference/config.xml
    scenarios/BETs/10pct_diesel_reference/config.xml
    scenarios/BETs/15pct_diesel_reference/config.xml
    scenarios/BETs/20pct_diesel_reference/config.xml
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
        logfile="logs/$(basename "$(dirname "$config")").log"
        echo "  Launching: $config -> $logfile"
        "$JAVA" -cp "$JAR" "$MAIN" "$config" > "$logfile" 2>&1 &
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

run_group "operational"     "${GROUP1[@]}"
run_group "deutschlandnetz" "${GROUP2[@]}"
run_group "diesel reference" "${GROUP3[@]}"

echo ""
echo "All 3 groups (15 scenarios) completed."