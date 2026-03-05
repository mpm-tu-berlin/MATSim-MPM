# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```sh
# Build (creates executable JAR in top directory)
./mvnw clean package            # Linux/Mac
mvnw.cmd clean package          # Windows

# Run tests
./mvnw -B verify                # Full test suite (same as CI)
./mvnw -pl . -Dtest=RunMatsimTest test   # Single test class

# Run the BET scenario
java -jar matsim-example-project-0.0.1-SNAPSHOT.jar
# Or directly via main class:
# RunBetScenario.main() defaults to scenarios/BETs/10_BETs_Test/config.xml
```

Java 17 required. Maven 3.8+ (wrapper included).

## Architecture

This is **MATSim-MPM**: a MATSim extension for simulating **Battery Electric Trucks (BETs)** with physics-based energy consumption and charging-aware routing. Built on MATSim 2024.0 and its EV contrib.

### Module Hierarchy (Guice DI)

`RunBetScenario` installs two key overriding modules into the MATSim Controler:

1. **`MpmEvModule`** → `MpmEvBaseModule` → installs:
   - `ElectricFleetModule` — electric vehicle fleet from XML
   - `ChargingInfrastructureModule` — charger locations/specs
   - `ChargingModule` — charging logic
   - `MpmDischargingModule` — custom energy consumption (see below)
   - `MpmEvStatsModule` — SOC profiles, charger occupancy stats
   - `VehicleChargingHandler` — QSim event handler for charging interactions

2. **`MpmEvNetworkRoutingProvider`** — replaces default car routing with EV-aware multi-stop routing

### Key Packages (`org.matsim.mpm`)

- **`discharging/`** — Energy consumption models implementing `DriveEnergyConsumption`:
  - `MpmDynamicBetDriveEnergyConsumption` — full physics model (rolling resistance, aerodynamic drag, grade from z-coords, recuperation, mass=12t default). Precomputes power lookup tables.
  - `BetDriveEnergyConsumption` — simple fixed-rate model (1200 Wh/km)
  - `CalibrationParams` — loads `.properties` files for Optuna parameter optimization

- **`routing/`** — EV-aware routing with charging stop insertion:
  - `MpmEvNetworkRoutingModule` — calculates energy per link, enforces regulatory driving-time limits (4.5h continuous, 6h trip, 9h daily), finds nearest chargers via k-NN, inserts charging activities
  - `MpmEvNetworkRoutingProvider` — factory/provider for the routing module

- **`stats/`** — `ChargerQueuingCollector` tracks queue times at chargers via MATSim events

- **`run/`** — Entry point `RunBetScenario`; `archive/` has legacy runners

### Scenario Structure

Scenarios live under `scenarios/BETs/` with MATSim XML configs referencing:
- Network file (with z-coordinates for elevation)
- Plans file (agent activity chains)
- Vehicle definitions (battery capacity, initial SOC)
- Charger infrastructure XML (locations, power ratings)
- Coordinate system: EPSG:4839 (German)

### Testing

JUnit 5 with MATSim's `MatsimTestUtils`. The main test (`RunMatsimTest`) runs the equil example scenario through `RunBetScenario`, then compares output plans (per-person scores) and events files against reference data in `test/input/`.
