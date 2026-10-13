package org.matsim.mpm.run;

import java.util.Collections;
import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.network.Node;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.config.groups.QSimConfigGroup;
import org.matsim.core.config.groups.ScoringConfigGroup;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy.OverwriteFileSetting;
import org.matsim.core.network.NetworkUtils;
import org.matsim.core.network.io.MatsimNetworkReader;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.mpm.MpmEvModule;
import org.matsim.mpm.routing.MpmEvNetworkRoutingProvider;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;

/**
 * Runs a full MATSim simulation for a single section network.
 *
 * Programmatically generates vehicles, plans, chargers, and config XML files,
 * then runs the simulation with MpmEvModule and MpmEvNetworkRoutingProvider.
 * The MpmDischargingModule automatically writes resistance_debug.csv with
 * per-link energy breakdown.
 *
 * CLI args:
 *   --network        Path to section network XML(.gz)
 *   --output-dir     Output directory for simulation results
 *   --from-coord     Start endpoint as "x,y" (EPSG:4839) — finds nearest node
 *   --to-coord       End endpoint as "x,y" (EPSG:4839) — finds nearest node
 *
 * Single-vehicle mode:
 *   --mass           Vehicle tare mass [kg]
 *   --payload        Payload [kg]
 *   --cdXA           Drag area Cd*A [m^2]
 *   --rollingC       Rolling resistance coefficient [-]
 *   --maxMotorPower  Max motor power [W]
 *   --maxSpeed       Max vehicle speed [m/s]
 *
 * Multi-vehicle mode (overrides single-vehicle args):
 *   --vehicles-csv   Path to CSV with columns: id,mass,payload,cdXA,rollingC,maxMotorPower,maxSpeed
 *
 * Optional:
 *   --qsim-timestep  QSim time step size in seconds (default: 1)
 */
public class RunSectionScenario {

    public static void main(String[] args) {
        // --- Parse CLI arguments ---
        Map<String, String> argMap = parseArgs(args);

        String networkPath = requireArg(argMap, "network");
        String outputDir = requireArg(argMap, "output-dir");

        Path outputPath = Path.of(outputDir);
        try {
            Files.createDirectories(outputPath);
        } catch (IOException e) {
            throw new RuntimeException("Cannot create output directory: " + outputDir, e);
        }

        String fromCoordStr = argMap.get("from-coord");  // optional, "x,y"
        String toCoordStr = argMap.get("to-coord");      // optional, "x,y"

        // --- Load network and find ordered path ---
        Network network = NetworkUtils.createNetwork();
        new MatsimNetworkReader(network).readFile(networkPath);

        // Note: ensureBidirectional() removed — section networks now contain
        // exactly one directed link per path edge (in path direction), and
        // findOrderedPath() uses undirected BFS to handle any link direction.

        List<Id<Link>> orderedLinks = findOrderedPath(network, fromCoordStr, toCoordStr);
        if (orderedLinks.isEmpty()) {
            throw new RuntimeException("Could not find ordered path through section network");
        }

        Id<Link> firstLinkId = orderedLinks.get(0);
        Id<Link> lastLinkId = orderedLinks.get(orderedLinks.size() - 1);
        List<Id<Link>> intermediateLinks = orderedLinks.size() > 2
                ? orderedLinks.subList(1, orderedLinks.size() - 1)
                : Collections.emptyList();

        System.out.printf("[RunSectionScenario] Network: %s%n", networkPath);
        System.out.printf("[RunSectionScenario] Path: %d links, first=%s, last=%s%n",
                orderedLinks.size(), firstLinkId, lastLinkId);

        // --- Generate scenario files in a sibling _input directory ---
        // (MATSim's Controler with deleteDirectoryIfExists wipes the output dir,
        //  so generated input files must live outside it)
        Path inputDir = outputPath.resolveSibling(outputPath.getFileName() + "_input");
        try {
            Files.createDirectories(inputDir);
        } catch (IOException e) {
            throw new RuntimeException("Cannot create input directory: " + inputDir, e);
        }

        Path vehiclesFile = inputDir.resolve("vehicles.xml");
        Path plansFile = inputDir.resolve("plans.xml");
        Path chargersFile = inputDir.resolve("chargers.xml");

        String vehiclesCsv = argMap.get("vehicles-csv");
        if (vehiclesCsv != null) {
            // Multi-vehicle mode
            List<Map<String, String>> vehicles = parseVehiclesCsv(Path.of(vehiclesCsv));
            System.out.printf("[RunSectionScenario] Multi-vehicle mode: %d vehicles from %s%n",
                    vehicles.size(), vehiclesCsv);
            writeVehiclesXml(vehiclesFile, vehicles);
            writePlansXml(plansFile, vehicles, firstLinkId, lastLinkId, intermediateLinks, network);
        } else {
            // Single-vehicle mode (backwards compatible)
            double mass = Double.parseDouble(requireArg(argMap, "mass"));
            double payload = Double.parseDouble(requireArg(argMap, "payload"));
            double cdXA = Double.parseDouble(requireArg(argMap, "cdXA"));
            double rollingC = Double.parseDouble(requireArg(argMap, "rollingC"));
            double maxMotorPower = Double.parseDouble(requireArg(argMap, "maxMotorPower"));
            double maxSpeed = Double.parseDouble(requireArg(argMap, "maxSpeed"));
            writeVehiclesXml(vehiclesFile, mass, payload, cdXA, rollingC, maxMotorPower, maxSpeed);
            writePlansXml(plansFile, firstLinkId, lastLinkId, intermediateLinks, network);
        }
        writeChargersXml(chargersFile, firstLinkId);

        // --- Build config programmatically ---
        Config config = ConfigUtils.createConfig();

        // EV config
        EvConfigGroup evCfg = new EvConfigGroup();
        evCfg.chargersFile = chargersFile.toAbsolutePath().toString();
        evCfg.timeProfiles = true;
        evCfg.enforceChargingInteractionDuration = true;
        config.addModule(evCfg);

        // Network
        config.network().setInputFile(Path.of(networkPath).toAbsolutePath().toString());

        // Plans
        config.plans().setInputFile(plansFile.toAbsolutePath().toString());

        // Vehicles
        config.vehicles().setVehiclesFile(vehiclesFile.toAbsolutePath().toString());

        // Controller
        config.controller().setOutputDirectory(outputPath.toAbsolutePath().toString());
        config.controller().setFirstIteration(0);
        config.controller().setLastIteration(0);
        config.controller().setOverwriteFileSetting(OverwriteFileSetting.deleteDirectoryIfExists);
        config.controller().setRoutingAlgorithmType(
                org.matsim.core.config.groups.ControllerConfigGroup.RoutingAlgorithmType.SpeedyALT);
        config.controller().setCreateGraphsInterval(0);

        // QSim
        config.qsim().setStartTime(0);
        config.qsim().setEndTime(24 * 3600);
        config.qsim().setSimStarttimeInterpretation(QSimConfigGroup.StarttimeInterpretation.onlyUseStarttime);
        config.qsim().setNumberOfThreads(1);
        config.qsim().setFlowCapFactor(1.0);
        config.qsim().setStorageCapFactor(1.0);
        config.qsim().setVehiclesSource(QSimConfigGroup.VehiclesSource.fromVehiclesData);
        double qsimTimestep = Double.parseDouble(argMap.getOrDefault("qsim-timestep", "1"));
        config.qsim().setTimeStepSize(qsimTimestep);

        // Scoring: activity types
        ScoringConfigGroup scoring = config.scoring();
        ScoringConfigGroup.ActivityParams startParams = new ScoringConfigGroup.ActivityParams("start");
        startParams.setTypicalDuration(8 * 3600);
        startParams.setScoringThisActivityAtAll(false);
        scoring.addActivityParams(startParams);

        ScoringConfigGroup.ActivityParams endParams = new ScoringConfigGroup.ActivityParams("end");
        endParams.setTypicalDuration(8 * 3600);
        endParams.setScoringThisActivityAtAll(false);
        scoring.addActivityParams(endParams);

        ScoringConfigGroup.ActivityParams chargingParams = new ScoringConfigGroup.ActivityParams("car charging interaction");
        chargingParams.setTypicalDuration(45 * 60);
        chargingParams.setScoringThisActivityAtAll(false);
        scoring.addActivityParams(chargingParams);

        // Replanning: only SelectExpBeta, no ReRoute
        config.replanning().setMaxAgentPlanMemorySize(1);
        config.replanning().addStrategySettings(
                new org.matsim.core.config.groups.ReplanningConfigGroup.StrategySettings()
                        .setStrategyName("SelectExpBeta")
                        .setWeight(1.0)
        );

        // Global
        config.global().setCoordinateSystem("Atlantis");
        config.global().setNumberOfThreads(1);

        // Events manager
        config.eventsManager().setNumberOfThreads(1);

        // --- Load scenario and run ---
        Scenario scenario = ScenarioUtils.loadScenario(config);

        Controler controler = new Controler(scenario);
        controler.addOverridingModule(new AbstractModule() {
            @Override
            public void install() {
                install(new MpmEvModule());
                addRoutingModuleBinding(TransportMode.car).toProvider(
                        new MpmEvNetworkRoutingProvider(TransportMode.car));
            }
        });

        controler.run();

        System.out.println("[RunSectionScenario] Simulation complete. Output: " + outputPath.toAbsolutePath());
    }

    /**
     * Adds synthetic reverse links for any one-way link in the network.
     * Section networks may contain one-way motorway links that create directed dead-ends.
     * For section energy analysis, we need full bidirectional traversal of the road surface.
     */
    static void ensureBidirectional(Network network) {
        // Collect existing (from, to) pairs
        Set<String> existing = new HashSet<>();
        for (Link link : network.getLinks().values()) {
            existing.add(link.getFromNode().getId() + "->" + link.getToNode().getId());
        }

        // Find one-way links and add reverse counterparts
        List<Link> toAdd = new ArrayList<>();
        for (Link link : network.getLinks().values()) {
            String reverseKey = link.getToNode().getId() + "->" + link.getFromNode().getId();
            if (!existing.contains(reverseKey)) {
                toAdd.add(link);
                existing.add(reverseKey); // prevent duplicates
            }
        }

        org.matsim.api.core.v01.network.NetworkFactory factory = network.getFactory();
        for (Link original : toAdd) {
            Id<Link> reverseId = Id.createLinkId(original.getToNode().getId() + "-" + original.getFromNode().getId());
            Link reverse = factory.createLink(reverseId, original.getToNode(), original.getFromNode());
            reverse.setLength(original.getLength());
            reverse.setFreespeed(original.getFreespeed());
            reverse.setCapacity(original.getCapacity());
            reverse.setNumberOfLanes(original.getNumberOfLanes());
            reverse.setAllowedModes(original.getAllowedModes());
            network.addLink(reverse);
        }

        if (!toAdd.isEmpty()) {
            System.out.printf("[RunSectionScenario] Added %d reverse links to ensure bidirectional network%n", toAdd.size());
        }
    }

    /**
     * Finds an ordered path through the section network using directed BFS.
     *
     * Section networks are fully bidirectional (each edge has A->B and B->A links on disk),
     * so directed BFS can find a path in either direction. Returns link IDs that exist in
     * the network file, ensuring MATSim plans reference valid links.
     *
     * If fromCoord/toCoord are provided (as "x,y" strings in network CRS), finds the nearest
     * network node by Euclidean distance. Otherwise falls back to degree-1 endpoint detection.
     */
    static List<Id<Link>> findOrderedPath(Network network, String fromCoordStr, String toCoordStr) {
        // Build directed adjacency: node -> list of outgoing links
        Map<Id<Node>, List<Link>> outgoing = new HashMap<>();
        // Build undirected adjacency for endpoint detection fallback
        Map<Id<Node>, Set<Id<Node>>> undirectedNeighbors = new HashMap<>();

        for (Link link : network.getLinks().values()) {
            Id<Node> fromId = link.getFromNode().getId();
            Id<Node> toId = link.getToNode().getId();
            outgoing.computeIfAbsent(fromId, k -> new ArrayList<>()).add(link);
            undirectedNeighbors.computeIfAbsent(fromId, k -> new HashSet<>()).add(toId);
            undirectedNeighbors.computeIfAbsent(toId, k -> new HashSet<>()).add(fromId);
        }

        // --- Determine start and end nodes ---
        Id<Node> startNode = resolveNodeByCoord(network, fromCoordStr, "from-coord");
        Id<Node> endNode = resolveNodeByCoord(network, toCoordStr, "to-coord");

        // Fallback: detect degree-1 endpoints
        if (startNode == null || endNode == null) {
            List<Id<Node>> endpoints = new ArrayList<>();
            for (Map.Entry<Id<Node>, Set<Id<Node>>> entry : undirectedNeighbors.entrySet()) {
                if (entry.getValue().size() == 1) {
                    endpoints.add(entry.getKey());
                }
            }
            System.out.printf("[RunSectionScenario] Detected %d degree-1 endpoints: %s%n",
                    endpoints.size(), endpoints);
            if (startNode == null) {
                startNode = endpoints.size() >= 1 ? endpoints.get(0) : undirectedNeighbors.keySet().iterator().next();
            }
            if (endNode == null) {
                endNode = endpoints.size() >= 2 ? endpoints.get(1) : null;
            }
        }

        System.out.printf("[RunSectionScenario] Start node: %s, End node: %s%n", startNode, endNode);

        // --- Directed BFS from startNode to endNode ---
        List<Id<Link>> path = directedBfsLinks(outgoing, startNode, endNode);
        if (path.isEmpty()) {
            System.err.println("[RunSectionScenario] WARNING: Directed BFS found no path from " + startNode + " to " + endNode);
            // Try reverse direction
            path = directedBfsLinks(outgoing, endNode, startNode);
            if (!path.isEmpty()) {
                System.out.println("[RunSectionScenario] Found path in reverse direction (" + endNode + " -> " + startNode + ")");
            } else {
                System.err.println("[RunSectionScenario] WARNING: No directed path in either direction");
                return Collections.emptyList();
            }
        }
        System.out.printf("[RunSectionScenario] Directed path: %d links%n", path.size());

        return path;
    }

    /**
     * Directed BFS that returns a list of link IDs forming a topologically connected route.
     */
    private static List<Id<Link>> directedBfsLinks(Map<Id<Node>, List<Link>> outgoing,
                                                    Id<Node> start, Id<Node> end) {
        if (start == null || end == null) return Collections.emptyList();

        // BFS: track predecessor link for each visited node
        Map<Id<Node>, Link> predecessorLink = new HashMap<>();
        predecessorLink.put(start, null);
        Deque<Id<Node>> queue = new ArrayDeque<>();
        queue.add(start);

        while (!queue.isEmpty()) {
            Id<Node> current = queue.poll();
            if (current.equals(end)) {
                // Reconstruct path
                List<Id<Link>> path = new ArrayList<>();
                Id<Node> node = end;
                while (!node.equals(start)) {
                    Link link = predecessorLink.get(node);
                    path.add(link.getId());
                    node = link.getFromNode().getId();
                }
                Collections.reverse(path);
                return path;
            }
            for (Link link : outgoing.getOrDefault(current, Collections.emptyList())) {
                Id<Node> neighbor = link.getToNode().getId();
                if (!predecessorLink.containsKey(neighbor)) {
                    predecessorLink.put(neighbor, link);
                    queue.add(neighbor);
                }
            }
        }
        return Collections.emptyList();
    }

    /**
     * Resolve a node by coordinate string "x,y".
     * Finds the nearest network node by squared Euclidean distance.
     * Returns null if the coordinate string is null.
     */
    private static Id<Node> resolveNodeByCoord(Network network, String coordStr, String argName) {
        if (coordStr == null) return null;

        String[] parts = coordStr.split(",");
        if (parts.length != 2) {
            throw new IllegalArgumentException("--" + argName + " must be 'x,y' but got: " + coordStr);
        }
        double targetX = Double.parseDouble(parts[0].trim());
        double targetY = Double.parseDouble(parts[1].trim());

        Id<Node> bestId = null;
        double bestDistSq = Double.MAX_VALUE;
        for (Node node : network.getNodes().values()) {
            double dx = node.getCoord().getX() - targetX;
            double dy = node.getCoord().getY() - targetY;
            double distSq = dx * dx + dy * dy;
            if (distSq < bestDistSq) {
                bestDistSq = distSq;
                bestId = node.getId();
            }
        }

        double dist = Math.sqrt(bestDistSq);
        System.out.printf("[RunSectionScenario] --%s: resolved to %s (dist=%.1f m)%n", argName, bestId, dist);
        return bestId;
    }

    /**
     * BFS on undirected adjacency from start to end. Returns ordered node list.
     * If end is null, walks the entire connected component (for linear chains, this gives the full path).
     */
    private static List<Id<Node>> bfsPath(Map<Id<Node>, Set<Id<Node>>> adjacency,
                                            Id<Node> start, Id<Node> end) {
        if (start == null) return Collections.emptyList();

        // BFS with parent tracking
        Map<Id<Node>, Id<Node>> parent = new LinkedHashMap<>();
        parent.put(start, null);
        Queue<Id<Node>> queue = new LinkedList<>();
        queue.add(start);
        Id<Node> lastVisited = start;

        while (!queue.isEmpty()) {
            Id<Node> current = queue.poll();
            lastVisited = current;

            if (end != null && current.equals(end)) break;

            for (Id<Node> neighbor : adjacency.getOrDefault(current, Collections.emptySet())) {
                if (!parent.containsKey(neighbor)) {
                    parent.put(neighbor, current);
                    queue.add(neighbor);
                }
            }
        }

        // Reconstruct path
        Id<Node> target = (end != null && parent.containsKey(end)) ? end : lastVisited;
        List<Id<Node>> path = new ArrayList<>();
        Id<Node> cur = target;
        while (cur != null) {
            path.add(cur);
            cur = parent.get(cur);
        }
        Collections.reverse(path);
        return path;
    }

    /**
     * Parse a vehicles CSV file with columns: id,mass,payload,cdXA,rollingC,maxMotorPower,maxSpeed
     */
    static List<Map<String, String>> parseVehiclesCsv(Path csvFile) {
        List<Map<String, String>> vehicles = new ArrayList<>();
        try (BufferedReader reader = Files.newBufferedReader(csvFile)) {
            String headerLine = reader.readLine();
            if (headerLine == null) {
                throw new RuntimeException("Empty vehicles CSV: " + csvFile);
            }
            String[] headers = headerLine.split(",");
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;
                String[] values = line.split(",");
                Map<String, String> row = new LinkedHashMap<>();
                for (int i = 0; i < headers.length && i < values.length; i++) {
                    row.put(headers[i].trim(), values[i].trim());
                }
                vehicles.add(row);
            }
        } catch (IOException e) {
            throw new RuntimeException("Cannot read vehicles CSV: " + csvFile, e);
        }
        return vehicles;
    }

    /**
     * Write vehicles XML with multiple vehicle types (one per CSV row).
     */
    private static void writeVehiclesXml(Path file, List<Map<String, String>> vehicles) {
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(file))) {
            w.println("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
            w.println("<vehicleDefinitions xmlns=\"http://www.matsim.org/files/dtd\"");
            w.println("                    xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"");
            w.println("                    xsi:schemaLocation=\"http://www.matsim.org/files/dtd https://www.matsim.org/files/dtd/vehicleDefinitions_v2.0.xsd\">");

            // Schema requires all vehicleType elements before all vehicle elements
            for (Map<String, String> v : vehicles) {
                String id = v.get("id");
                String typeId = "bet_" + id;
                double mass = Double.parseDouble(v.get("mass"));
                double payload = Double.parseDouble(v.get("payload"));
                double cdXA = Double.parseDouble(v.get("cdXA"));
                double rollingC = Double.parseDouble(v.get("rollingC"));
                double maxMotorPowerW = Double.parseDouble(v.get("maxMotorPower"));
                double maxSpeedMs = Double.parseDouble(v.get("maxSpeed"));

                w.println();
                w.printf("\t<vehicleType id=\"%s\">%n", typeId);
                w.printf("\t\t<description>Section analysis BET (%s)</description>%n", id);
                w.println("\t\t<capacity seats=\"0\" standingRoomInPersons=\"0\"/>");
                w.println("\t\t<length meter=\"16.5\"/>");
                w.println("\t\t<width meter=\"2.55\"/>");
                w.printf(Locale.US, "\t\t<maximumVelocity meterPerSecond=\"%.4f\"/>%n", maxSpeedMs);
                w.println("\t\t<engineInformation>");
                w.println("\t\t\t<attributes>");
                w.println("\t\t\t\t<attribute name=\"HbefaTechnology\" class=\"java.lang.String\">electricity</attribute>");
                w.println("\t\t\t\t<attribute name=\"chargerTypes\" class=\"java.util.Arrays$ArrayList\">[\"DC\",\"default\"]</attribute>");
                w.println("\t\t\t\t<attribute name=\"energyCapacityInKWhOrLiters\" class=\"java.lang.Double\">10000.0</attribute>");
                w.println("\t\t\t\t<attribute name=\"energyConsumptionKWhPerMeter\" class=\"java.lang.Double\">0.0012</attribute>");
                w.printf(Locale.US, "\t\t\t\t<attribute name=\"mass\" class=\"java.lang.Double\">%.1f</attribute>%n", mass);
                w.printf(Locale.US, "\t\t\t\t<attribute name=\"payload\" class=\"java.lang.Double\">%.1f</attribute>%n", payload);
                w.printf(Locale.US, "\t\t\t\t<attribute name=\"cdXA\" class=\"java.lang.Double\">%.4f</attribute>%n", cdXA);
                w.printf(Locale.US, "\t\t\t\t<attribute name=\"rollingC\" class=\"java.lang.Double\">%.6f</attribute>%n", rollingC);
                w.printf(Locale.US, "\t\t\t\t<attribute name=\"maxMotorPowerW\" class=\"java.lang.Double\">%.1f</attribute>%n", maxMotorPowerW);
                w.println("\t\t\t</attributes>");
                w.println("\t\t</engineInformation>");
                w.printf("\t</vehicleType>%n");
            }

            for (Map<String, String> v : vehicles) {
                String id = v.get("id");
                String typeId = "bet_" + id;
                String vehicleId = "truck_" + id;
                w.println();
                w.printf("\t<vehicle id=\"%s\" type=\"%s\">%n", vehicleId, typeId);
                w.println("\t\t<attributes>");
                w.println("\t\t\t<attribute name=\"initialSoc\" class=\"java.lang.Double\">0.99</attribute>");
                w.println("\t\t</attributes>");
                w.printf("\t</vehicle>%n");
            }

            w.println();
            w.println("</vehicleDefinitions>");
        } catch (IOException e) {
            throw new RuntimeException("Cannot write vehicles file: " + file, e);
        }
    }

    /**
     * Write plans XML with multiple persons (one per CSV row), all driving the same route.
     */
    private static void writePlansXml(Path file, List<Map<String, String>> vehicles,
                                       Id<Link> firstLinkId, Id<Link> lastLinkId,
                                       List<Id<Link>> intermediateLinks, Network network) {
        StringBuilder routeStr = new StringBuilder();
        routeStr.append(firstLinkId.toString());
        for (Id<Link> linkId : intermediateLinks) {
            routeStr.append(" ").append(linkId.toString());
        }
        routeStr.append(" ").append(lastLinkId.toString());

        Link firstLink = network.getLinks().get(firstLinkId);
        Link lastLink = network.getLinks().get(lastLinkId);
        Coord startCoord = firstLink.getFromNode().getCoord();
        Coord endCoord = lastLink.getToNode().getCoord();

        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(file))) {
            w.println("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
            w.println("<!DOCTYPE population SYSTEM \"http://www.matsim.org/files/dtd/population_v6.dtd\">");
            w.println("<population>");

            for (int i = 0; i < vehicles.size(); i++) {
                Map<String, String> v = vehicles.get(i);
                String id = v.get("id");
                String personId = "driver_" + id;
                String vehicleId = "truck_" + id;
                // Stagger departure times by 60s to avoid simultaneous departures
                int departSeconds = 6 * 3600 + i * 60;
                int hours = departSeconds / 3600;
                int minutes = (departSeconds % 3600) / 60;
                int secs = departSeconds % 60;
                String endTime = String.format("%02d:%02d:%02d", hours, minutes, secs);

                w.println();
                w.printf("\t<person id=\"%s\">%n", personId);
                w.println("\t\t<attributes>");
                w.printf("\t\t\t<attribute name=\"vehicles\" class=\"org.matsim.vehicles.PersonVehicles\">{\"car\":\"%s\"}</attribute>%n", vehicleId);
                w.println("\t\t</attributes>");
                w.println("\t\t<plan selected=\"yes\">");
                w.printf(Locale.US, "\t\t\t<activity type=\"start\" link=\"%s\" x=\"%.1f\" y=\"%.1f\" end_time=\"%s\"/>%n",
                        firstLinkId, startCoord.getX(), startCoord.getY(), endTime);
                w.println("\t\t\t<leg mode=\"car\">");
                w.printf("\t\t\t\t<route type=\"links\" start_link=\"%s\" end_link=\"%s\">%s</route>%n",
                        firstLinkId, lastLinkId, routeStr);
                w.println("\t\t\t</leg>");
                w.printf(Locale.US, "\t\t\t<activity type=\"end\" link=\"%s\" x=\"%.1f\" y=\"%.1f\"/>%n",
                        lastLinkId, endCoord.getX(), endCoord.getY());
                w.println("\t\t</plan>");
                w.printf("\t</person>%n");
            }

            w.println();
            w.println("</population>");
        } catch (IOException e) {
            throw new RuntimeException("Cannot write plans file: " + file, e);
        }
    }

    private static void writeVehiclesXml(Path file, double mass, double payload,
                                          double cdXA, double rollingC,
                                          double maxMotorPowerW, double maxSpeedMs) {
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(file))) {
            w.println("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
            w.println("<vehicleDefinitions xmlns=\"http://www.matsim.org/files/dtd\"");
            w.println("                    xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"");
            w.println("                    xsi:schemaLocation=\"http://www.matsim.org/files/dtd https://www.matsim.org/files/dtd/vehicleDefinitions_v2.0.xsd\">");
            w.println();
            w.println("\t<vehicleType id=\"section_bet\">");
            w.println("\t\t<description>Section analysis BET</description>");
            w.println("\t\t<capacity seats=\"0\" standingRoomInPersons=\"0\"/>");
            w.println("\t\t<length meter=\"16.5\"/>");
            w.println("\t\t<width meter=\"2.55\"/>");
            w.printf(Locale.US, "\t\t<maximumVelocity meterPerSecond=\"%.4f\"/>%n", maxSpeedMs);
            w.println("\t\t<engineInformation>");
            w.println("\t\t\t<attributes>");
            w.println("\t\t\t\t<attribute name=\"HbefaTechnology\" class=\"java.lang.String\">electricity</attribute>");
            w.println("\t\t\t\t<attribute name=\"chargerTypes\" class=\"java.util.Arrays$ArrayList\">[\"DC\",\"default\"]</attribute>");
            w.println("\t\t\t\t<attribute name=\"energyCapacityInKWhOrLiters\" class=\"java.lang.Double\">10000.0</attribute>");
            w.println("\t\t\t\t<attribute name=\"energyConsumptionKWhPerMeter\" class=\"java.lang.Double\">0.0012</attribute>");
            w.printf(Locale.US, "\t\t\t\t<attribute name=\"mass\" class=\"java.lang.Double\">%.1f</attribute>%n", mass);
            w.printf(Locale.US, "\t\t\t\t<attribute name=\"payload\" class=\"java.lang.Double\">%.1f</attribute>%n", payload);
            w.printf(Locale.US, "\t\t\t\t<attribute name=\"cdXA\" class=\"java.lang.Double\">%.4f</attribute>%n", cdXA);
            w.printf(Locale.US, "\t\t\t\t<attribute name=\"rollingC\" class=\"java.lang.Double\">%.6f</attribute>%n", rollingC);
            w.printf(Locale.US, "\t\t\t\t<attribute name=\"maxMotorPowerW\" class=\"java.lang.Double\">%.1f</attribute>%n", maxMotorPowerW);
            w.println("\t\t\t</attributes>");
            w.println("\t\t</engineInformation>");
            w.println("\t</vehicleType>");
            w.println();
            w.println("\t<vehicle id=\"truck_1\" type=\"section_bet\">");
            w.println("\t\t<attributes>");
            w.println("\t\t\t<attribute name=\"initialSoc\" class=\"java.lang.Double\">0.99</attribute>");
            w.println("\t\t</attributes>");
            w.println("\t</vehicle>");
            w.println();
            w.println("</vehicleDefinitions>");
        } catch (IOException e) {
            throw new RuntimeException("Cannot write vehicles file: " + file, e);
        }
    }

    private static void writePlansXml(Path file, Id<Link> firstLinkId, Id<Link> lastLinkId,
                                       List<Id<Link>> intermediateLinks, Network network) {
        // Build route string: space-separated intermediate link IDs
        StringBuilder routeStr = new StringBuilder();
        routeStr.append(firstLinkId.toString());
        for (Id<Link> linkId : intermediateLinks) {
            routeStr.append(" ").append(linkId.toString());
        }
        routeStr.append(" ").append(lastLinkId.toString());

        // Get coordinates for activities
        Link firstLink = network.getLinks().get(firstLinkId);
        Link lastLink = network.getLinks().get(lastLinkId);
        Coord startCoord = firstLink.getFromNode().getCoord();
        Coord endCoord = lastLink.getToNode().getCoord();

        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(file))) {
            w.println("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
            w.println("<!DOCTYPE population SYSTEM \"http://www.matsim.org/files/dtd/population_v6.dtd\">");
            w.println("<population>");
            w.println();
            w.println("\t<person id=\"driver_1\">");
            w.println("\t\t<attributes>");
            w.println("\t\t\t<attribute name=\"vehicles\" class=\"org.matsim.vehicles.PersonVehicles\">{\"car\":\"truck_1\"}</attribute>");
            w.println("\t\t</attributes>");
            w.println("\t\t<plan selected=\"yes\">");
            w.printf(Locale.US, "\t\t\t<activity type=\"start\" link=\"%s\" x=\"%.1f\" y=\"%.1f\" end_time=\"06:00:00\"/>%n",
                    firstLinkId, startCoord.getX(), startCoord.getY());
            w.println("\t\t\t<leg mode=\"car\">");
            w.printf("\t\t\t\t<route type=\"links\" start_link=\"%s\" end_link=\"%s\">%s</route>%n",
                    firstLinkId, lastLinkId, routeStr);
            w.println("\t\t\t</leg>");
            w.printf(Locale.US, "\t\t\t<activity type=\"end\" link=\"%s\" x=\"%.1f\" y=\"%.1f\"/>%n",
                    lastLinkId, endCoord.getX(), endCoord.getY());
            w.println("\t\t</plan>");
            w.println("\t</person>");
            w.println();
            w.println("</population>");
        } catch (IOException e) {
            throw new RuntimeException("Cannot write plans file: " + file, e);
        }
    }

    private static void writeChargersXml(Path file, Id<Link> firstLinkId) {
        try (PrintWriter w = new PrintWriter(Files.newBufferedWriter(file))) {
            w.println("<!DOCTYPE chargers SYSTEM \"http://matsim.org/files/dtd/chargers_v1.dtd\">");
            w.println();
            w.println("<chargers>");
            // Dummy charger on first link (required by EV module, won't be used)
            w.printf("\t<charger id=\"dummy\" link=\"%s\" type=\"DC\" plug_power=\"720.0\" plug_count=\"1\"/>%n",
                    firstLinkId);
            w.println("</chargers>");
        } catch (IOException e) {
            throw new RuntimeException("Cannot write chargers file: " + file, e);
        }
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> map = new HashMap<>();
        for (int i = 0; i < args.length; i++) {
            if (args[i].startsWith("--") && i + 1 < args.length) {
                map.put(args[i].substring(2), args[i + 1]);
                i++;
            }
        }
        return map;
    }

    private static String requireArg(Map<String, String> args, String name) {
        String val = args.get(name);
        if (val == null) {
            throw new IllegalArgumentException("Missing required argument: --" + name);
        }
        return val;
    }
}
