#!/usr/bin/env bash
# Run replanning scenarios in groups of 5 parallel processes each.
# Each group waits for all 5 to finish before the next group starts.

set -euo pipefail

JAVA="/c/Users/diego/.jdks/openjdk-25.0.1/bin/java"
JAR="matsim-example-project-0.0.1-SNAPSHOT.jar"
MAIN="org.matsim.mpm.run.RunBetScenario"

# Group 1: 1pct operational
#GROUP1=(
 #   scenarios/BETs/replanning/1pct_BETs_operational/chargers_249_038/config.xml
  #  scenarios/BETs/replanning/1pct_BETs_operational/chargers_280_037/config.xml
   # scenarios/BETs/replanning/1pct_BETs_operational/chargers_299_045/config.xml
   # scenarios/BETs/replanning/1pct_BETs_operational/chargers_344_012/config.xml
   # scenarios/BETs/replanning/1pct_BETs_operational/chargers_361_060/config.xml
#)

# Group 2: 10pct operational
GROUP2=(

    scenarios/BETs/replanning/10pct_BETs_operational/chargers_0290/config.xml
#    scenarios/BETs/replanning/10pct_BETs_operational/chargers_267_005/config.xml
    scenarios/BETs/replanning/10pct_BETs_operational/chargers_311_004/config.xml
#    scenarios/BETs/replanning/10pct_BETs_operational/chargers_323_021/config.xml
#    scenarios/BETs/replanning/10pct_BETs_operational/chargers_392_054/config.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_628_027/config.xml
    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_640_041/config.xml
)

# Group 3: 10pct deutschlandnetz
#GROUP3=(
#    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_0217/config.xml
#    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_581_051/config.xml
#    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_628_027/config.xml
#   scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_640_041/config.xml
#    scenarios/BETs/replanning/10pct_BETs_deutschlandnetz/chargers_646_030/config.xml
#)

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
        # Use parent dir name + grandparent dir name for unique log names
        local parent=$(basename "$(dirname "$config")")
        local grandparent=$(basename "$(dirname "$(dirname "$config")")")
        logfile="logs/replanning_${grandparent}_${parent}.log"
        echo "  Launching: $config -> $logfile"
        "$JAVA" -Djava.awt.headless=true -Xmx30g -XX:MaxMetaspaceSize=512m -XX:+UseG1GC -cp "$JAR" "$MAIN" "$config" > "$logfile" 2>&1 &
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

#run_group "1pct operational"       "${GROUP1[@]}"
run_group "10pct operational"      "${GROUP2[@]}"
#run_group "10pct deutschlandnetz"  "${GROUP3[@]}"

echo ""
echo "All 3 groups (15 scenarios) completed."
