# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```sh
# Build (creates executable JAR in top directory)
./mvnw clean package            # Linux/Mac
mvnw.cmd clean package          # Windows

# Run tests
./mvnw -B verify                # Full test suite (same as CI)
./mvnw test -Dtest=ChargingQueueWaitingScoringHandlerTest   # Single test class

# Run the BET scenario
java -jar matsim-example-project-0.0.1-SNAPSHOT.jar [path/to/config.xml]
# RunBetScenario.main() defaults to scenarios/BETs/tests/10_BETs_Test/config.xml
```

Java 17 required. Maven wrapper is bundled, no system Maven needed. On Windows, JAVA_HOME must point to a JDK 17+.

**Known:** `RunMatsimTest` fails because the equil test scenario lacks a `chargersFile` in the EV config. This is a pre-existing config issue, not a code bug.

## Architecture

This is **MATSim-MPM**: a MATSim extension for simulating **Battery Electric Trucks (BETs)** on German highway networks with physics-based energy consumption, charging-aware routing, mandatory driving-time regulation stops, charger queuing, and rest area infrastructure. Built on MATSim 2024.0 and its EV contrib.

### Module Hierarchy (Guice DI)

`RunBetScenario` (entry point) installs:

1. **`MpmEvModule`** → `MpmEvBaseModule` → installs:
   - `ElectricFleetModule` — electric vehicle fleet from XML
   - `ChargingInfrastructureModule` — charger locations/specs
   - `ChargingModule` — charging logic
   - `MpmDischargingModule` — custom energy consumption (see below)
   - `MpmEvStatsModule` — SOC profiles, charger occupancy stats
   - `VehicleChargingHandler` — QSim event handler for charging interactions

2. **`MpmEvNetworkRoutingProvider`** — replaces default car routing with EV-aware multi-stop routing

3. Custom bindings: `ChargingWaitingScoringFunctionFactory` (scoring), charger-type-specific `ChargingLogic` factories (`HoldUntilLeaveChargingLogic` for normal chargers, `RejectIfFullChargingLogic` for `DC_slow` rest-area chargers)

### Key Packages (`org.matsim.mpm`)

- **`discharging/`** — Energy consumption models implementing `DriveEnergyConsumption`:
  - `MpmDynamicBetDriveEnergyConsumption` — full physics model (rolling resistance, aerodynamic drag, grade from z-coords, recuperation, mass=12t default). Precomputes power lookup tables.
  - `BetDriveEnergyConsumption` — simple fixed-rate model (1200 Wh/km)
  - `CalibrationParams` — loads `.properties` files for Optuna parameter optimization

- **`routing/`** — EV-aware routing with charging stop insertion. `MpmEvNetworkRoutingModule` is the central class and implements iterative sub-leg routing:
  1. **`calcRoute()`**: Two-pass penalty system (penalize congested chargers), then a loop that repeatedly calls `computeStops()` on the remaining route, takes the first stop, routes to it, updates state (energy, driving time, rest flag), and continues.
  2. **`computeStops()`**: Timing-first loop over 4.5h driving segments. Each segment produces timing/rest stops (alternating) and energy stops when SoC would drop below 20%. Stop types: `"Energy"`, `"Timing"` (45-min break), `"Rest"` (11-h overnight).
  - Key constants: `MAX_DRIVE_TIME_WITHOUT_BREAK=4.5h`, `MIN_SOC=0.2`, `MAX_FAST_SOC=0.9`, `CHARGER_SEARCH_BUFFER=10min`, `REST_AREA_GRACE=15min`, `PLANNING_CHARGER_POWER=720kW`.
  - Agents starting at 100% SoC only charge at timing/rest stops if they can't reach the next stop; otherwise they just rest without occupying a charger.
  - `MpmEvNetworkRoutingProvider` — factory/provider for the routing module
  - `RestAreaSpecification`, `RestAreaReader` — highway Rastplätze loaded from XML via `MpmRoutingConfigGroup` (`restAreasFile` param), used for non-charging rest stops via `StraightLineKnnFinder`

- **`charging/`** — `MpmVehicleChargingHandler` (deterministic charger selection using activity type encoding, e.g. `"car DC_fast charging interaction"` vs `"car DC_slow charging interaction"`), `HoldUntilLeaveChargingLogic` (queue-based), `RejectIfFullChargingLogic` (no queuing for rest area chargers)

- **`scoring/`** — `ChargingQueueWaitingScoringHandler` tracks per-agent waiting time in charger queues via event pairs (`QueuedAtChargerEvent` → `ChargingStartEvent`) and applies a score penalty using the `waiting` utils/hour parameter.

- **`stats/`** — `ChargerQueuingCollector` (queue times via events), `ChargerWaitingTimeTracker` (feeds congestion data back to routing penalties), `MpmChargingProceduresCSVWriter` (per-stop CSV with stop type classification), `RouteDetourTracker`

- **`run/`** — Entry points `RunBetScenario` (accepts optional config path as arg) and `RunSectionScenario`; `archive/` has legacy runners

### Scenario Structure

Scenarios live under `scenarios/` with MATSim XML configs referencing:
- Network file (with z-coordinates for elevation)
- Plans file (agent activity chains)
- Vehicle definitions (battery capacity, initial SOC)
- Charger infrastructure XML (locations, power ratings)
- Coordinate system: EPSG:4839 (German)

Config uses custom MATSim modules: `ev`, `mpmRouting`. Test scenarios: `scenarios/BETs/tests/` (default: `10_BETs_Test`). Production variants by penetration rate (1–20%), charger network (operational/deutschlandnetz), and diesel reference. VECTO calibration scenarios: `scenarios/VECTO_*`.

### Conventions

- Package root: `org.matsim.mpm`
- Event-driven architecture with MATSim event handlers
- Charging activity types encode charger type in the string (parsed by handler)
- Stop reasons stored as strings in `StopPlan.reason` field

### Testing

JUnit 5 with MATSim's `MatsimTestUtils`. `RunMatsimTest` runs the equil example scenario through `RunBetScenario` and compares output plans and events against `test/input/` (currently failing, see above). Unit tests: `ChargingQueueWaitingScoringHandlerTest`, `LoadDependentRollingResistanceTest`.

## Python Network Generation Pipeline

Scripts run **in sequence** (numbered 01–05); `run_04_parallel.py` parallelises step 04. Dependencies: `python/network-generation/requirements.txt`. Further scripts in the same folder: section extraction (`extract_representative_sections*.py`, `repair_section_chains.py`), link-length variants and section energy analysis (`generate_section_link_length_variants.py`, `run_section_energy_analysis.py`), elevation evaluation (`evaluate_elevation_resolution.py`, `matsim_grade_eval.py`), real-trip validation and plotting (`realtrip_*.py`, `plot_*.py`). All outputs go to `python/network-generation/data/`.

| Script | Input | Output | Purpose |
|--------|-------|--------|---------|
| `01_save_filtered_osm_locally.py` | OSM (via osmnx) | `germany_simplified_DF.gpkg`, `germany_detailed_sorted_DF.gpkg` | Download + sort OSM edges; match simplified to detailed segments |
| `02_generate_3d_roads_from_osm.py.py` | `.gpkg` files | 3D road geometry | **DEPRECATED (2026-06)** – Höhen kommen jetzt direkt aus dem DTM in 04 |
| `03_build_kdtree_from_filtered_points.py` | DTM `.tif` files (in `data/`) | `*.npz` | **DEPRECATED (2026-06)** – KD-Tree arbeitete im Grad-Raum (anisotrop) + float32; ersetzt durch direktes DTM-Sampling in 04 |
| `04_build_matsim_network_from_local_osm_and_kdtree.py` | `.gpkg` + **DTM `.tif`** | `Germany_max_*_long_V0.xml.gz` | Build MATSim XML network with 3D nodes; splits long links. Höhen via direktem bilinearem DTM-Sampling (`load_dtm`/`sample_heights`), kein npz mehr |
| `05_smooth_matsim_network.py` | `xml.gz` network | smoothed `xml.gz` network | **OPTIONAL/LEGACY (2026-06)** – Glättung + Brücken/Tunnel-Linearisierung sind jetzt in 04 integriert (auflösungsunabhängig). 05 nur noch für Netze ohne Kantengeometrie oder als Sensitivität; nach 04 NICHT nötig (sonst Doppelglättung) |

**DTM source data** (not in git, stored in `data/`):
- `DTM Germany 20m v3b by Sonny.tif` – primary elevation source
- `DTM Germany 50m v3b by Sonny.tif` – fallback

**Script 01 key logic:** Downloads both simplified and non-simplified OSM graphs, normalizes `reversed` flags, then iterates through simplified edges to find matching detailed sub-segments in order. This sorted detailed result becomes the network backbone.

**Script 04 key logic:** Enforces `max_allowed_link_length` by recursively splitting long simplified edges at detailed segment boundaries. Elevation is assigned via **direct bilinear DTM sampling** (`load_dtm`/`sample_heights`, CRS-correct EPSG:4326→DTM-CRS, thread-safe, block + tiled paths) — replaces the old anisotropic degree-space KDTree/npz lookup. By default heights use **`assign_heights_along_corridors`**: per corridor the DTM is sampled densely (~`sample_step_m`) along the road geometry, the profile is gently smoothed once (`smooth_rms_m`, resolution-independent), **bridge/tunnel spans are linearised** between the at-grade ends (OSM `bridge`/`tunnel`, avoids bare-earth valley/mountain artifacts), and node z is read at each arc-length position. This folds Script 05's smoothing into 04. Only OSM (`.gpkg`, topology) + DTM (`.tif`, elevation) are needed.

**Script 05 key logic:** Spline-smooths elevation along each network path using `scipy`. Config at top of file (INPUT_FILE, OUTPUT_FILE, SMOOTH_METHOD, MERGE_LINKS, etc.).

## Dependencies

- **Java:** MATSim 2024.0 with contribs: `ev`, `freight`, `application`, `simwrapper`, `vsp`
- **Python (network generation):** `osmnx`, `geopandas`, `scipy`, `numpy`, `pandas`, `shapely`, `tqdm`, `networkx`, `rasterio`
