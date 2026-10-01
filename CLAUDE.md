# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
./mvnw clean package                                        # Build fat JAR
./mvnw clean test                                           # Run all tests
./mvnw test -Dtest=ChargingQueueWaitingScoringHandlerTest   # Run single test
```

Java 17 required. Maven wrapper is bundled — no system Maven needed.
JAVA_HOME must be set explicitly: `JAVA_HOME="/c/Users/diego/.jdks/openjdk-25.0.1"` (prefix all `./mvnw` calls).

**Known:** `RunMatsimTest` always fails because the equil test scenario lacks a `chargersFile` in EV config. This is a pre-existing config issue, not a code bug. The only passing test is `ChargingQueueWaitingScoringHandlerTest`.

## Architecture

This is a MATSim extension for simulating **Battery Electric Trucks (BETs)** on German highway networks. It builds on MATSim's EV contrib to add mandatory driving-time regulation stops, energy-aware routing, charger queuing, and rest area infrastructure.

### Guice Module Hierarchy

`RunBetScenario` (entry point) installs:
- **`MpmEvModule`** → `MpmEvBaseModule` → `MpmDischargingModule`, `MpmEvStatsModule`, charging/fleet/infrastructure modules
- Custom bindings: `MpmEvNetworkRoutingProvider` (car routing), `ChargingWaitingScoringFunctionFactory`, charger-type-specific `ChargingLogic` factories

### Core Routing (`org.matsim.mpm.routing`)

**`MpmEvNetworkRoutingModule`** (~820 lines) is the central class. It implements iterative sub-leg routing:

1. **`calcRoute()`**: Two-pass penalty system (penalize congested chargers), then a while-loop that repeatedly calls `computeStops()` on the remaining route, takes the first stop, routes to it, updates state (energy, driving time, rest flag), and continues.

2. **`computeStops()`**: Timing-first loop over 4.5h driving segments. Each segment produces timing/rest stops (alternating) and energy stops when SoC would drop below 20%. Stop types: `"Energy"`, `"Timing"` (45-min break), `"Rest"` (11-h overnight).

Key constants: `MAX_DRIVE_TIME_WITHOUT_BREAK=4.5h`, `MIN_SOC=0.2`, `MAX_FAST_SOC=0.9`, `CHARGER_SEARCH_BUFFER=10min`, `REST_AREA_GRACE=15min`, `PLANNING_CHARGER_POWER=720kW`.

Agents starting at 100% SoC only charge at timing/rest stops if they can't reach the next stop; otherwise they just rest without occupying a charger.

### Charging (`org.matsim.mpm.charging`)

- **`MpmVehicleChargingHandler`**: Deterministic charger selection using activity type encoding (`"car DC_fast charging interaction"` vs `"car DC_slow charging interaction"`)
- **`HoldUntilLeaveChargingLogic`**: Queue-based charging for normal chargers
- **`RejectIfFullChargingLogic`**: No queuing for rest area (DC_slow) chargers

### Scoring (`org.matsim.mpm.scoring`)

`ChargingQueueWaitingScoringHandler` tracks per-agent waiting time in charger queues via event pairs (`QueuedAtChargerEvent` → `ChargingStartEvent`) and applies a score penalty using the `waiting` utils/hour parameter.

### Rest Areas (`RestAreaSpecification`, `RestAreaReader`)

Highway Rastplätze loaded from XML via `MpmRoutingConfigGroup` (`restAreasFile` param). Used for non-charging rest stops via `StraightLineKnnFinder`.

### Stats (`org.matsim.mpm.stats`)

- `ChargerWaitingTimeTracker`: Feeds congestion data back to routing penalties
- `MpmChargingProceduresCSVWriter`: Per-stop CSV with stop type classification

## Scenario Structure

Default: `scenarios/BETs/1pct_BETs_unlimited_operational/`. Variants by penetration rate (1–20%), charger network (operational/deutschlandnetz), and diesel reference. Config uses custom MATSim modules: `ev`, `mpmRouting`.

## Conventions

- Package root: `org.matsim.mpm`
- Entry point: `org.matsim.mpm.run.RunBetScenario` (accepts optional config path as arg)
- Event-driven architecture with MATSim event handlers
- Charging activity types encode charger type in the string (parsed by handler)
- Stop reasons stored as strings in `StopPlan.reason` field