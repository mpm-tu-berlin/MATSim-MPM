/* *********************************************************************** *
 * project: org.matsim.*
 *                                                                         *
 * *********************************************************************** *
 *                                                                         *
 * copyright       : (C) 2015 by the members listed in the COPYING,        *
 *                   LICENSE and WARRANTY file.                            *
 * email           : info at matsim dot org                                *
 *                                                                         *
 * *********************************************************************** *
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *   See also COPYING, LICENSE and WARRANTY file                           *
 *                                                                         *
 * *********************************************************************** */
package org.matsim.mpm.routing;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.population.Activity;
import org.matsim.api.core.v01.population.Leg;
import org.matsim.api.core.v01.population.Person;
import org.matsim.api.core.v01.population.PlanElement;
import org.matsim.contrib.common.util.StraightLineKnnFinder;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.charging.VehicleChargingHandler;
import org.matsim.contrib.ev.discharging.AuxEnergyConsumption;
import org.matsim.contrib.ev.discharging.DriveEnergyConsumption;
import org.matsim.contrib.ev.fleet.ElectricFleetSpecification;
import org.matsim.contrib.ev.fleet.ElectricFleetUtils;
import org.matsim.contrib.ev.fleet.ElectricVehicle;
import org.matsim.contrib.ev.fleet.ElectricVehicleSpecification;
import org.matsim.contrib.ev.infrastructure.ChargerSpecification;
import org.matsim.contrib.ev.infrastructure.ChargingInfrastructureSpecification;
import org.matsim.core.gbl.Gbl;
import org.matsim.core.gbl.MatsimRandom;
import org.matsim.core.network.NetworkUtils;
import org.matsim.core.population.PopulationUtils;
import org.matsim.core.population.routes.NetworkRoute;
import org.matsim.core.router.DefaultRoutingRequest;
import org.matsim.core.router.LinkWrapperFacility;
import org.matsim.core.router.RoutingModule;
import org.matsim.core.router.RoutingRequest;
import org.matsim.core.router.util.LeastCostPathCalculator;
import org.matsim.core.router.util.TravelTime;
import org.matsim.facilities.Facility;
import org.matsim.vehicles.Vehicle;

import java.util.*;
import java.util.Comparator;
import java.util.stream.Collectors;

import org.matsim.mpm.stats.ChargerWaitingTimeTracker;

import static org.matsim.api.core.v01.TransportMode.car;

/**
 * This network Routing module adds stages for re-charging into the Route.
 * This wraps a "computer science" {@link LeastCostPathCalculator}, which routes from a node to another node, into something that
 * routes from a {@link Facility} to another {@link Facility}, as we need in MATSim.
 *
 * <p>Two-pass routing: pass 1 computes the route with no charger penalty to identify which charger(s)
 * would be used; pass 2 re-routes with a penalty on those specific charger links so the LCPC can find
 * an alternative corridor if a less congested charger is reachable at comparable travel cost.
 *
 * <p>Timing-first design: mandatory regulatory stops (4.5 h → 45-min break; another 4.5 h → 11-h rest)
 * are always inserted for trips exceeding 4.5 h. Energy stops are supplemental and only inserted when
 * SoC would drop below MIN_SOC within a segment. The pattern repeats as a loop after each rest stop.
 *
 * @author jfbischoff
 */

final class MpmEvNetworkRoutingModule implements RoutingModule {

    private final String mode;

    private final Network network;
    private final RoutingModule delegate;
    private final ElectricFleetSpecification electricFleet;
    private final ChargingInfrastructureSpecification chargingInfrastructureSpecification;
    private final Random random = MatsimRandom.getLocalInstance();
    private final TravelTime travelTime;
    private final DriveEnergyConsumption.Factory driveConsumptionFactory;
    private final AuxEnergyConsumption.Factory auxConsumptionFactory;
    private final String stageActivityModePrefix;
    private final String vehicleSuffix;
    private final EvConfigGroup evConfigGroup;
    private final ChargerWaitingTimeTracker waitingTimeTracker;
    private final ChargerPenaltyTravelDisutility chargerPenaltyDisutility;
    private final Set<Id<Link>> restAreaLinks;
    private static final double MIN_SOC = 0.2; // Minimum State of Charge
    private static final double MAX_DRIVE_TIME_WITHOUT_BREAK = 4.5 * 60 * 60; // Maximum driving time without a break in seconds
    private static final double MAX_OVERALL_DRIVE_TIME_PER_DAY = 9 * 60 * 60; // Maximum overall allowed driving time per day in seconds
    private static final double BREAK_DURATION = 45 * 60; // in seconds
    private static final double REST_DURATION = 11 * 60 * 60; // in seconds
    private static final double CHARGER_POWER = 720 * 1000; // in Watt
    private static final double CHARGING_OVERHEAD = 5 * 60; // seconds for plug/unplug/payment
    private static final double CHARGER_SEARCH_BUFFER = 0 * 60; // in seconds, additional time buffer to consider for charger search to not exceed 4,5h driving limit
    private static final double REST_AREA_GRACE = 15 * 60; // in seconds, grace period past 4.5h limit to still accept a rest area (Rastplatz)
    static final double MAX_VEHICLE_SPEED = 18.056; // in m/s (65 km/h)

    /**
     * Result of the stop-computation pass — the maps used by sub-leg routing plus energy data.
     *
     * <p>Energy fields (for dynamic charging time of energy stops):
     * <ul>
     *   <li>{@code totalTripConsumption}: sum of all link energy consumptions on the route.</li>
     *   <li>{@code cumulativeConsumptionToStop[i]}: cumulative energy from trip start up to stop i.</li>
     * </ul>
     * For an energy stop at index i, the minimum charging time is computed from these values
     * plus {@code ev.getInitialCharge()} in {@code calcRoute()}.
     */
    private static final class StopPlan {
        final List<Link> stopLocations;
        final Map<Link, Link> stopLocationToSearchLink;
        final Map<Link, String> stopReasons;
        final Map<Link, Link> stopLocationToRestAreaLink; // timingLink → rest area link on route (null = none found)
        final double totalTripConsumption;
        final double[] cumulativeConsumptionToStop; // length == stopLocations.size()

        StopPlan(List<Link> stopLocations, Map<Link, Link> stopLocationToSearchLink,
                 Map<Link, String> stopReasons, Map<Link, Link> stopLocationToRestAreaLink,
                 double totalTripConsumption, double[] cumulativeConsumptionToStop) {
            this.stopLocations = stopLocations;
            this.stopLocationToSearchLink = stopLocationToSearchLink;
            this.stopReasons = stopReasons;
            this.stopLocationToRestAreaLink = stopLocationToRestAreaLink;
            this.totalTripConsumption = totalTripConsumption;
            this.cumulativeConsumptionToStop = cumulativeConsumptionToStop;
        }

        boolean isEmpty() {
            return stopLocations.isEmpty();
        }
    }

    MpmEvNetworkRoutingModule(final String mode, final Network network, RoutingModule delegate,
                              ElectricFleetSpecification electricFleet,
                              ChargingInfrastructureSpecification chargingInfrastructureSpecification, TravelTime travelTime,
                              DriveEnergyConsumption.Factory driveConsumptionFactory, AuxEnergyConsumption.Factory auxConsumptionFactory,
                              EvConfigGroup evConfigGroup, ChargerWaitingTimeTracker waitingTimeTracker,
                              ChargerPenaltyTravelDisutility chargerPenaltyDisutility,
                              List<RestAreaSpecification> restAreas) {
        this.travelTime = travelTime;
        Gbl.assertNotNull(network);
        this.delegate = delegate;
        this.network = network;
        this.mode = mode;
        this.electricFleet = electricFleet;
        this.chargingInfrastructureSpecification = chargingInfrastructureSpecification;
        this.driveConsumptionFactory = driveConsumptionFactory;
        this.auxConsumptionFactory = auxConsumptionFactory;
        stageActivityModePrefix = mode + VehicleChargingHandler.CHARGING_IDENTIFIER;
        this.evConfigGroup = evConfigGroup;
        this.waitingTimeTracker = waitingTimeTracker;
        this.chargerPenaltyDisutility = chargerPenaltyDisutility;
        this.restAreaLinks = restAreas.stream()
                .map(RestAreaSpecification::linkId)
                .collect(Collectors.toSet());
        this.vehicleSuffix = mode.equals(car) ? "" : "_" + mode;
    }

    @Override
    public List<? extends PlanElement> calcRoute(RoutingRequest request) {
        final Facility fromFacility = request.getFromFacility();
        final Facility toFacility = request.getToFacility();
        final double departureTime = request.getDepartureTime();
        final Person person = request.getPerson();

        // Ensure clean penalty state from any previous (failed) routing call.
        chargerPenaltyDisutility.clearPenalties();

        List<? extends PlanElement> basicRoute = delegate.calcRoute(request);
        Id<Vehicle> evId = Id.create(person.getId() + vehicleSuffix, Vehicle.class);
        if (!electricFleet.getVehicleSpecifications().containsKey(evId)) {
            return basicRoute;
        } else {
            Leg basicLeg = (Leg) basicRoute.get(0);
            ElectricVehicleSpecification ev = electricFleet.getVehicleSpecifications().get(evId);

            // Pass 1: compute stop plan with no charger penalty.
            StopPlan stopPlan = computeStops(basicLeg, ev);
            if (stopPlan.isEmpty()) {
                return basicRoute;
            }

            boolean starts100 = ev.getInitialSoc() >= 1.0 - 1e-9;

            // Build penalty map: only stops that will definitely result in charging.
            // Skip timing/rest stops for 100%-SoC agents (they might not charge there).
            Map<Id<Link>, Double> penalties = new LinkedHashMap<>();
            for (Link stopLocation : stopPlan.stopLocations) {
                String r = stopPlan.stopReasons.get(stopLocation);
                if (starts100 && !"Energy".equals(r)) {
                    continue;  // 100% agents might not charge at timing/rest stops
                }
                String chargerType = "Rest".equals(r) ? "DC_slow" : "DC_fast";
                Link searchLink = stopPlan.stopLocationToSearchLink.getOrDefault(stopLocation, stopLocation);
                ChargerSpecification charger = selectCharger(searchLink, ev, chargerType);
                double wait = waitingTimeTracker.getAverageWaitingTime(charger.getId());
                if (wait > 0) {
                    penalties.put(charger.getLinkId(), wait);
                }
            }

            // Pass 2: re-route with penalty on congested charger links only.
            // The LCPC may find a different road corridor that avoids the congested zone.
            if (!penalties.isEmpty()) {
                chargerPenaltyDisutility.setPenalties(penalties);
                basicRoute = delegate.calcRoute(request);
                basicLeg = (Leg) basicRoute.get(0);
                stopPlan = computeStops(basicLeg, ev);
                chargerPenaltyDisutility.clearPenalties();
                if (stopPlan.isEmpty()) {
                    return basicRoute;
                }
            }

            //////////////////////////////////////////////////////////////////////////////////////////////
            // Include detours to the nearest charger (sub-leg routing, no penalties active)
            List<PlanElement> stagedRoute = new ArrayList<>();
            Facility lastFrom = fromFacility;
            double lastArrivaltime = departureTime;

            for (int stopIndex = 0; stopIndex < stopPlan.stopLocations.size(); stopIndex++) {
                Link stopLocation = stopPlan.stopLocations.get(stopIndex);
                String stopReason = stopPlan.stopReasons.get(stopLocation);

                // Decide: charging stop or resting stop?
                boolean isChargingStop;
                if ("Energy".equals(stopReason)) {
                    isChargingStop = true;
                } else if (starts100) {
                    // 100% agent: charge only if remaining route would drop below MIN_SOC without charging
                    double energyAtStop = Math.max(0.0,
                            ev.getInitialCharge() - stopPlan.cumulativeConsumptionToStop[stopIndex]);
                    double remaining = stopPlan.totalTripConsumption
                            - stopPlan.cumulativeConsumptionToStop[stopIndex];
                    isChargingStop = remaining > energyAtStop - MIN_SOC * ev.getBatteryCapacity();
                } else {
                    isChargingStop = true; // non-100% agents always charge
                }

                if (isChargingStop) {
                    // Route to nearest charger of the appropriate type for this stop category
                    String chargerType = "Rest".equals(stopReason) ? "DC_slow" : "DC_fast";
                    StraightLineKnnFinder<Link, ChargerSpecification> knnFinder = new StraightLineKnnFinder<>(
                            5, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
                    Link chargerSearchLink = stopPlan.stopLocationToSearchLink.getOrDefault(stopLocation, stopLocation);
                    List<ChargerSpecification> nearestChargers = knnFinder.findNearest(chargerSearchLink,
                            chargingInfrastructureSpecification.getChargerSpecifications()
                                    .values()
                                    .stream()
                                    .filter(c -> c.getChargerType().equals(chargerType)
                                                 && ev.getChargerTypes().contains(c.getChargerType())));
                    ChargerSpecification selectedCharger = nearestChargers.stream()
                            .min(Comparator.comparingDouble(c -> waitingTimeTracker.getAverageWaitingTime(c.getId())))
                            .orElse(nearestChargers.get(0));
                    Link selectedChargerLink = network.getLinks().get(selectedCharger.getLinkId());
                    Facility nexttoFacility = new LinkWrapperFacility(selectedChargerLink);
                    if (nexttoFacility.getLinkId().equals(lastFrom.getLinkId())) {
                        continue;
                    }
                    List<? extends PlanElement> routeSegment = delegate.calcRoute(DefaultRoutingRequest.of(lastFrom, nexttoFacility,
                            lastArrivaltime, person, request.getAttributes()));
                    Leg lastLeg = (Leg) routeSegment.get(0);
                    lastArrivaltime = lastLeg.getDepartureTime().seconds() + lastLeg.getTravelTime().seconds();
                    stagedRoute.add(lastLeg);

                    // Compute charging duration based on stop type
                    double chargingDuration;
                    if ("Energy".equals(stopReason)) {
                        // Charge only enough to reach the next timing/rest stop (or destination)
                        // so the agent arrives there with at least MIN_SOC.
                        int nextTimingIdx = -1;
                        for (int j = stopIndex + 1; j < stopPlan.stopLocations.size(); j++) {
                            String r = stopPlan.stopReasons.get(stopPlan.stopLocations.get(j));
                            if ("Timing".equals(r) || "Rest".equals(r)) { nextTimingIdx = j; break; }
                        }
                        double consumptionToNext = (nextTimingIdx >= 0)
                                ? stopPlan.cumulativeConsumptionToStop[nextTimingIdx]
                                  - stopPlan.cumulativeConsumptionToStop[stopIndex]
                                : stopPlan.totalTripConsumption - stopPlan.cumulativeConsumptionToStop[stopIndex];
                        double maxChargeable = ev.getBatteryCapacity() * (1.0 - MIN_SOC);
                        chargingDuration = Math.max(1.0, Math.min(consumptionToNext, maxChargeable)) / CHARGER_POWER + CHARGING_OVERHEAD;
                    } else if ("Rest".equals(stopReason)) {
                        chargingDuration = REST_DURATION - CHARGING_OVERHEAD;
                    } else { // "Timing"
                        chargingDuration = BREAK_DURATION - CHARGING_OVERHEAD;
                    }

                    // Encode the stop reason in the stage-activity type so MpmVehicleChargingHandler
                    // can select the correct charger deterministically (avoids findAny() ambiguity)
                    // and MpmChargingProceduresCSVWriter can write the stop type to chargingStats.csv.
                    String chargeActivityPrefix = mode + " " + stopReason + VehicleChargingHandler.CHARGING_IDENTIFIER;
                    Activity chargeAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(
                            selectedChargerLink.getCoord(), selectedChargerLink.getId(), chargeActivityPrefix);
                    chargeAct = PopulationUtils.createActivity(chargeAct);
                    chargeAct.setMaximumDuration(chargingDuration);
                    lastArrivaltime += chargeAct.getMaximumDuration().seconds();
                    stagedRoute.add(chargeAct);
                    lastFrom = nexttoFacility;

                } else {
                    // Resting stop: use the rest area identified on the route in computeStops(),
                    // or fall back to timingLink if no rest area was found in this segment.
                    Link restLink = stopPlan.stopLocationToRestAreaLink.get(stopLocation);
                    if (restLink == null) restLink = stopLocation; // fallback: timingLink

                    Facility stopFacility = new LinkWrapperFacility(restLink);
                    if (!stopFacility.getLinkId().equals(lastFrom.getLinkId())) {
                        List<? extends PlanElement> seg = delegate.calcRoute(DefaultRoutingRequest.of(
                                lastFrom, stopFacility, lastArrivaltime, person, request.getAttributes()));
                        Leg leg = (Leg) seg.get(0);
                        lastArrivaltime = leg.getDepartureTime().seconds() + leg.getTravelTime().seconds();
                        stagedRoute.add(leg);
                    }
                    double restDuration = "Rest".equals(stopReason) ? REST_DURATION : BREAK_DURATION;
                    Activity restAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(
                            restLink.getCoord(), restLink.getId(), "resting");
                    restAct = PopulationUtils.createActivity(restAct);
                    restAct.setMaximumDuration(restDuration);
                    lastArrivaltime += restDuration;
                    stagedRoute.add(restAct);
                    lastFrom = stopFacility;
                }
            }
            stagedRoute.addAll(delegate.calcRoute(DefaultRoutingRequest.of(lastFrom, toFacility, lastArrivaltime, person, request.getAttributes())));
            return stagedRoute;
        }
    }

    /**
     * Computes charging and rest stop locations for the given route leg using a timing-first loop.
     *
     * <p>The outer loop processes one 4.5-h driving segment per iteration:
     * <ol>
     *   <li>Find the next timing/rest stop (4.5 h from the current segment start).</li>
     *   <li>Find an energy stop within that segment if SoC would drop below MIN_SOC.</li>
     *   <li>Append stops to the plan and advance the segment-start pointer.</li>
     * </ol>
     *
     * <p>Timing stops use reason {@code "Timing"} (45-min break) and rest stops use {@code "Rest"}
     * (11-h overnight rest), alternating after each timing stop. Energy stops use reason {@code "Energy"}.
     * All stops carry their cumulative energy consumption from trip start in
     * {@link StopPlan#cumulativeConsumptionToStop}.
     */
    private StopPlan computeStops(Leg basicLeg, ElectricVehicleSpecification ev) {
        Map<Link, Double> estimatedEnergyConsumption = estimateConsumption(ev, basicLeg);
        Map<Link, Double> estimatedTravelTime = estimateTravelTime(basicLeg);

        double totalConsumption = estimatedEnergyConsumption.values().stream()
                .mapToDouble(Double::doubleValue).sum();

        List<Link> stopLocations = new ArrayList<>();
        Map<Link, Link> stopLocationToSearchLink = new LinkedHashMap<>();
        Map<Link, String> stopReasons = new LinkedHashMap<>();
        Map<Link, Link> stopLocationToRestAreaLink = new LinkedHashMap<>();
        List<Double> cumulativeList = new ArrayList<>();

        // Loop state
        Link segmentStart = null;   // null = trip origin
        double currentEnergy = ev.getInitialCharge();
        boolean nextIsRest = false;
        double cumConsFromStart = 0.0;

        while (true) {
            // A. Find the next timing/rest stop (4.5 h from segmentStart).
            // candidateLink           = last link with cumulative time <= 4.5h - buffer (charger search reference).
            // timingLink              = first link where cumulative time >= 4.5h (the regulatory limit).
            // lastRestAreaInSegment   = last route link within the timing window that has a rest area.
            boolean segStartFoundT = (segmentStart == null);
            double runningTime = 0.0;
            Link candidateLink = null;
            Link timingLink = null;
            Link lastRestAreaBefore415 = null;  // last rest area within 4:30h − grace  (< 4:15h)
            Link lastRestAreaBefore430 = null;  // last rest area within standard 4:30h window
            Link lastRestAreaBefore445 = null;  // last rest area within 4:30h + grace   (< 4:45h)

            for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) {
                if (!segStartFoundT) {
                    if (e.getKey().equals(segmentStart)) {
                        segStartFoundT = true;
                    }
                    continue;   // skip segmentStart and all links before it
                }
                runningTime += e.getValue();
                if (runningTime <= MAX_DRIVE_TIME_WITHOUT_BREAK - CHARGER_SEARCH_BUFFER) {
                    candidateLink = e.getKey();
                }
                if (timingLink == null && runningTime >= MAX_DRIVE_TIME_WITHOUT_BREAK) {
                    timingLink = e.getKey();
                }
                if (restAreaLinks.contains(e.getKey().getId())) {
                    if (runningTime <= MAX_DRIVE_TIME_WITHOUT_BREAK - REST_AREA_GRACE) {
                        lastRestAreaBefore415 = e.getKey();
                    }
                    if (runningTime <= MAX_DRIVE_TIME_WITHOUT_BREAK) {
                        lastRestAreaBefore430 = e.getKey();
                    }
                    if (runningTime <= MAX_DRIVE_TIME_WITHOUT_BREAK + REST_AREA_GRACE) {
                        lastRestAreaBefore445 = e.getKey();
                    }
                }
                if (timingLink != null && runningTime > MAX_DRIVE_TIME_WITHOUT_BREAK + REST_AREA_GRACE) {
                    break;
                }
            }

            // Grace applies only when no rest area was found in the [4:15h, 4:30h] band.
            boolean applyGrace = (lastRestAreaBefore430 == lastRestAreaBefore415);
            // Grace is only useful if it actually finds a better (further) rest area.
            boolean graceFindsBetter = applyGrace && (lastRestAreaBefore445 != lastRestAreaBefore415);

            Link lastRestAreaInSegment;
            if (!applyGrace) {
                lastRestAreaInSegment = lastRestAreaBefore430;       // rest area in [4:15h, 4:30h]
            } else if (graceFindsBetter) {
                lastRestAreaInSegment = lastRestAreaBefore445;       // grace improved result
            } else {
                lastRestAreaInSegment = null;                        // no improvement → fall back to timingLink
            }

            if (timingLink == null) {
                break;  // remaining trip fits in one 4.5-h window — no more timing stops needed
            }

            // B. Find energy stop within this segment (segmentStart → timingLink).
            // Also record the cumulative consumption to the rest area for correct next-segment accounting.
            double usableCapacity = currentEnergy - MIN_SOC * ev.getBatteryCapacity();
            double cumSegmentCons = 0.0;
            double cumConsToEnergyStop = 0.0;
            double cumConsToRestArea = 0.0;
            Link energyStop = null;
            boolean segStartFoundE = (segmentStart == null);
            boolean restAreaConsumed = false;
            boolean passedTimingLink = false;

            for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
                if (!segStartFoundE) {
                    if (e.getKey().equals(segmentStart)) {
                        segStartFoundE = true;
                    }
                    continue;   // skip segmentStart and all links before it
                }
                cumSegmentCons += e.getValue();
                // Only record energy stops before the timing link — not in the grace period.
                if (!passedTimingLink && energyStop == null && cumSegmentCons >= usableCapacity) {
                    energyStop = e.getKey();
                    cumConsToEnergyStop = cumSegmentCons;
                }
                if (!restAreaConsumed && lastRestAreaInSegment != null
                        && e.getKey().equals(lastRestAreaInSegment)) {
                    cumConsToRestArea = cumSegmentCons;
                    restAreaConsumed = true;
                }
                if (e.getKey().equals(timingLink)) {
                    if (lastRestAreaInSegment == null || restAreaConsumed) {
                        break; // rest area already found (or none exists) — done
                    }
                    passedTimingLink = true; // rest area is in grace period — keep scanning
                } else if (passedTimingLink && restAreaConsumed) {
                    break; // found rest area in grace period — done
                }
            }

            // Avoid adding energyStop at the same link as timingLink (would create a duplicate).
            if (energyStop != null && energyStop.equals(timingLink)) {
                energyStop = null;
            }

            // C. Append stops to the plan lists.
            if (energyStop != null) {
                stopLocations.add(energyStop);
                stopLocationToSearchLink.put(energyStop, energyStop);
                stopReasons.put(energyStop, "Energy");
                cumulativeList.add(cumConsFromStart + cumConsToEnergyStop);
            }
            stopLocations.add(timingLink);
            stopLocationToSearchLink.put(timingLink, candidateLink != null ? candidateLink : timingLink);
            stopReasons.put(timingLink, nextIsRest ? "Rest" : "Timing");
            stopLocationToRestAreaLink.put(timingLink, lastRestAreaInSegment); // null if none found
            cumulativeList.add(cumConsFromStart + cumSegmentCons);

            // D. Update loop state for the next segment.
            // If a rest area was found: the agent stops there (< 4.5h, no detour).
            // The next segment starts from the rest area so the next 4.5h window is measured correctly.
            boolean stopsAtRestArea = (lastRestAreaInSegment != null);

            // Energy arriving at the actual stop location (rest area or timing link).
            double energyAtStop;
            if (energyStop != null) {
                // After an energy top-up, battery arrives at timing/rest stop at MIN_SOC.
                energyAtStop = MIN_SOC * ev.getBatteryCapacity();
            } else {
                energyAtStop = currentEnergy - (stopsAtRestArea ? cumConsToRestArea : cumSegmentCons);
            }

            if (nextIsRest) {
                // After an 11-h rest, conservatively assume full charge (upper bound for planning).
                currentEnergy = ev.getBatteryCapacity();
            } else {
                // After a 45-min break: charge up from energyAtStop.
                currentEnergy = Math.min(
                        energyAtStop + (BREAK_DURATION - CHARGING_OVERHEAD) * CHARGER_POWER,
                        ev.getBatteryCapacity());
            }

            // Next segment starts from the rest area (if found) to correctly enforce the 4.5h window.
            // Falls back to candidateLink (charger search reference) if no rest area is available.
            segmentStart = stopsAtRestArea ? lastRestAreaInSegment : candidateLink;
            cumConsFromStart += stopsAtRestArea ? cumConsToRestArea : cumSegmentCons;
            nextIsRest = !nextIsRest;
        }

        double[] cumulative = cumulativeList.stream().mapToDouble(Double::doubleValue).toArray();
        return new StopPlan(stopLocations, stopLocationToSearchLink, stopReasons,
                stopLocationToRestAreaLink, totalConsumption, cumulative);
    }

    /**
     * Selects the charger with the lowest expected waiting time from the 5 nearest chargers
     * to {@code chargerSearchLink} that are compatible with {@code ev} and match {@code chargerType}.
     */
    private ChargerSpecification selectCharger(Link chargerSearchLink, ElectricVehicleSpecification ev,
                                               String chargerType) {
        StraightLineKnnFinder<Link, ChargerSpecification> finder = new StraightLineKnnFinder<>(
                5, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
        List<ChargerSpecification> nearest = finder.findNearest(chargerSearchLink,
                chargingInfrastructureSpecification.getChargerSpecifications().values().stream()
                        .filter(c -> c.getChargerType().equals(chargerType)
                                     && ev.getChargerTypes().contains(c.getChargerType())));
        return nearest.stream()
                .min(Comparator.comparingDouble(c -> waitingTimeTracker.getAverageWaitingTime(c.getId())))
                .orElse(nearest.get(0));
    }

    private Map<Link, Double> estimateConsumption(ElectricVehicleSpecification ev, Leg basicLeg) {
        Map<Link, Double> consumptions = new LinkedHashMap<>();
        NetworkRoute route = (NetworkRoute)basicLeg.getRoute();
        List<Link> links = NetworkUtils.getLinks(network, route.getLinkIds());
        ElectricVehicle pseudoVehicle = ElectricFleetUtils.create(ev, driveConsumptionFactory, auxConsumptionFactory,
                v -> charger -> {
                    throw new UnsupportedOperationException();
                } );
        DriveEnergyConsumption driveEnergyConsumption = pseudoVehicle.getDriveEnergyConsumption();
        AuxEnergyConsumption auxEnergyConsumption = pseudoVehicle.getAuxEnergyConsumption();
        double linkEnterTime = basicLeg.getDepartureTime().seconds();
        for (Link l : links) {
            //double travelT = travelTime.getLinkTravelTime(l, basicLeg.getDepartureTime().seconds(), null, null);
            double travelT = l.getLength() / Math.min(MAX_VEHICLE_SPEED, l.getFreespeed());

            double consumption = driveEnergyConsumption.calcEnergyConsumption(l, travelT, linkEnterTime)
                    + auxEnergyConsumption.calcEnergyConsumption(basicLeg.getDepartureTime().seconds(), travelT, l.getId());
            // to accomodate for ERS, where energy charge is directly implemented in the consumption model
            consumptions.put(l, consumption);
            linkEnterTime += travelT;
        }
        return consumptions;
    }

    private Map<Link, Double> estimateTravelTime(Leg basicLeg) {
        NetworkRoute route = (NetworkRoute)basicLeg.getRoute();
        List<Link> links = NetworkUtils.getLinks(network, route.getLinkIds());
        Map<Link, Double> travelTimes = new LinkedHashMap<>();
        for (Link l : links) {
            //double travelT = travelTime.getLinkTravelTime(l, basicLeg.getDepartureTime().seconds(), null, null);
            double travelT = l.getLength() / Math.min(MAX_VEHICLE_SPEED, l.getFreespeed());
            travelTimes.put(l, travelT);
        }
        return travelTimes;
    }

    @Override
    public String toString() {
        return "[NetworkRoutingModule: mode=" + this.mode + "]";
    }

}
