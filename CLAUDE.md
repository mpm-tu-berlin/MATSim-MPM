# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a MATSim-based simulation project for Battery Electric Trucks (BETs) in Germany. It models EV routing with charging stops and mandatory driver breaks (EU regulations) on the German motorway/trunk/primary road network. The project has two major components:

1. **Java/MATSim simulation** (`src/`) – the actual agent-based transport simulation
2. **Python network generation pipeline** (`python/network-generation/`) – builds the 3D road network from OSM + DTM elevation data

## Building & Running (Java)

Build the executable JAR (Windows):
```sh
mvnw.cmd clean package
```

Run the simulation (default scenario: `scenarios/BETs/consumption_test/config.xml`):
```sh
java -jar matsim-example-project-0.0.1-SNAPSHOT.jar
```

Or run directly from `RunBetScenario.java` in IntelliJ with no args (uses the above default config).

Run tests:
```sh
mvnw.cmd test
```

Run a single test:
```sh
mvnw.cmd test -Dtest=RunMatsimTest
```

**Java version:** 17. **MATSim version:** 2024.0.

## Java Architecture

All custom code is in `org.matsim.mpm.*`:

- **`run/RunBetScenario`** – entry point; loads config, installs `MpmEvModule` and `MpmEvNetworkRoutingProvider` for `car` mode
- **`MpmEvModule`** → installs `MpmEvBaseModule` + QSim `VehicleChargingHandler`
- **`MpmEvBaseModule`** → installs MATSim EV contrib modules: `ElectricFleetModule`, `ChargingInfrastructureModule`, `ChargingModule`, `MpmDischargingModule`, `MpmEvStatsModule`
- **`discharging/BetDriveEnergyConsumption`** – flat consumption model: 1200 Wh/km (per-link, ignores grade currently)
- **`routing/MpmEvNetworkRoutingModule`** – core routing logic: wraps the standard router, inserts charging/rest stops based on:
  - Battery SoC (MIN_SOC = 20%, CHARGER_POWER = 640 kW)
  - EU HGV driving time rules: max 4.5h continuous, max 6h per trip, max 9h/day; 45-min break or 11h rest
  - Nearest charger selected via `StraightLineKnnFinder`
- **`routing/MpmEvNetworkRoutingProvider`** – Guice provider that wires `MpmEvNetworkRoutingModule`
- **`stats/ChargerQueuingCollector`** – event handler that logs queuing times at chargers
- **`archive/`** – old/unused run classes, ignore

Key constants in `MpmEvNetworkRoutingModule`:
- `MAX_VEHICLE_SPEED = 18.056 m/s` (65 km/h) – trucks are speed-capped
- `MIN_SOC = 0.2` – routing triggers a stop before SoC drops below 20%

## Python Network Generation Pipeline

Scripts run **in sequence** (numbered 01–05). All outputs go to `python/network-generation/data/`.

| Script | Input | Output | Purpose |
|--------|-------|--------|---------|
| `01_save_filtered_osm_locally.py` | OSM (via osmnx) | `germany_simplified_DF.gpkg`, `germany_detailed_sorted_DF.gpkg` | Download + sort OSM edges; match simplified to detailed segments |
| `02_generate_3d_roads_from_osm.py.py` | `.gpkg` files | 3D road geometry | **DEPRECATED (2026-06)** – Höhen kommen jetzt direkt aus dem DTM in 04 |
| `03_build_kdtree_from_filtered_points.py` | DTM `.tif` files (in `data/`) | `*.npz` | **DEPRECATED (2026-06)** – KD-Tree arbeitete im Grad-Raum (anisotrop) + float32; ersetzt durch direktes DTM-Sampling in 04 |
| `04_build_matsim_network_from_local_osm_and_kdtree.py` | `.gpkg` + **DTM `.tif`** | `Germany_max_*_long_V0.xml.gz` | Build MATSim XML network with 3D nodes; splits long links. Höhen via direktem bilinearem DTM-Sampling (`load_dtm`/`sample_heights`), kein npz mehr |
| `05_smooth_matsim_network.py` | `xml.gz` network | smoothed `xml.gz` network | Spline-smooth elevation profiles; optionally merge links |

**DTM source data** (not in git, stored in `data/`):
- `DTM Germany 20m v3b by Sonny.tif` – primary elevation source
- `DTM Germany 50m v3b by Sonny.tif` – fallback

**Script 01 key logic:** Downloads both simplified and non-simplified OSM graphs, normalizes `reversed` flags, then iterates through simplified edges to find matching detailed sub-segments in order. This sorted detailed result becomes the network backbone.

**Script 04 key logic:** Enforces `max_allowed_link_length` by recursively splitting long simplified edges at detailed segment boundaries. Elevation assigned via **direct bilinear DTM sampling** (`load_dtm`/`sample_heights`, CRS-correct EPSG:4326→DTM-CRS, thread-safe, block + tiled paths) — replaces the old anisotropic degree-space KDTree/npz lookup. Only OSM (`.gpkg`, topology) + DTM (`.tif`, elevation) are needed now.

**Script 05 key logic:** Spline-smooths elevation along each network path using `scipy`. Config at top of file (INPUT_FILE, OUTPUT_FILE, SMOOTH_METHOD, MERGE_LINKS, etc.).

## Scenarios

Scenarios are under `scenarios/BETs/`. Each subdirectory contains:
- `config.xml` – MATSim config
- `*_plans.xml.gz` – agent population (BET drivers)
- `*_Vehicles.xml.gz` – vehicle fleet with battery specs
- `*_Chargers*.xml` – charger infrastructure

The shared network file is `scenarios/german_etruck_network.xml.gz` (referenced from configs).

## Dependencies

- **Java:** MATSim 2024.0 with contribs: `ev`, `freight`, `application`, `simwrapper`, `vsp`
- **Python:** `osmnx`, `geopandas`, `scipy`, `numpy`, `pandas`, `shapely`, `tqdm`, `networkx`
