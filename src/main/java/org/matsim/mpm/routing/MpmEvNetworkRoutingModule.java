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
import org.matsim.mpm.stats.RouteDetourTracker;

import static org.matsim.api.core.v01.TransportMode.car;

/**
 * This network Routing module adds stages for re-charging into the Route.
 * This wraps a "computer science" {@link LeastCostPathCalculator}, which routes from a node to another node, into something that
 * routes from a {@link Facility} to another {@link Facility}, as we need in MATSim.
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
    private final RouteDetourTracker routeDetourTracker;
    private final Set<Id<Link>> restAreaLinks;
    private static final double MIN_SOC = 0.2; // Minimum State of Charge
    private static final double MAX_DRIVE_TIME_WITHOUT_BREAK = 4.5 * 60 * 60; // Maximum driving time without a break in seconds
    private static final double MAX_OVERALL_DRIVE_TIME_PER_DAY = 9 * 60 * 60; // Maximum overall allowed driving time per day in seconds
    private static final double BREAK_DURATION = 45 * 60; // in seconds
    private static final double REST_DURATION = 11 * 60 * 60; // in seconds
    private static final double CHARGING_OVERHEAD = 5 * 60; // seconds for plug/unplug/payment
    /** Planning-only estimate used in {@code computeStops()} to approximate SoC after a timing break.
     *  The actual charger power is read from {@link ChargerSpecification#getPlugPower()} in {@code calcRoute()}. */
    private static final double PLANNING_CHARGER_POWER = 720_000; // W
    private static final double CHARGER_SEARCH_BUFFER = 10 * 60; // in seconds, additional time buffer to consider for charger search to not exceed 4,5h driving limit
    private static final double REST_AREA_GRACE = 15 * 60; // in seconds, grace period past 4.5h limit to still accept a rest area (Rastplatz)
    private static final double CHARGER_SELECTION_MAX_DETOUR = 10_000.0; // 10 km in meters — max allowed detour for charger candidate selection
    private static final double CHARGER_MIN_SEGMENT_TIME = 4.0 * 3600; // 4h — don't stop too early in segment
    private static final double CHARGER_MAX_SEGMENT_TIME = 5.0 * 3600; // 5h — max grace beyond 4.5h limit
    static final double MAX_VEHICLE_SPEED = 18.056; // in m/s (65 km/h)
    private static final double MAX_FAST_SOC = 0.9; // Schnellladen nur bis 90% SoC
    /** Safety buffer added to energy-stop charging to absorb extra consumption caused by the
     *  off-route charger detour. Max detour = 10 km × 4320 J/m ≈ 43 MJ → 50 MJ gives margin. */
    private static final double ENERGY_STOP_BUFFER_J = 50_000_000; // J ≈ 14 kWh

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
                              List<RestAreaSpecification> restAreas,
                              RouteDetourTracker routeDetourTracker) {
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
        this.routeDetourTracker = routeDetourTracker;
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

        List<? extends PlanElement> basicRoute = delegate.calcRoute(request);
        Id<Vehicle> evId = Id.create(person.getId() + vehicleSuffix, Vehicle.class);
        if (!electricFleet.getVehicleSpecifications().containsKey(evId)) {
            return basicRoute;
        } else {
            Leg basicLeg = (Leg) basicRoute.get(0);
            double basicDistance = basicLeg.getRoute().getDistance();
            double basicTime = basicLeg.getTravelTime().seconds();
            ElectricVehicleSpecification ev = electricFleet.getVehicleSpecifications().get(evId);

            StopPlan stopPlan = computeStops(basicLeg, ev, ev.getInitialCharge(), false, 0.0);
            if (stopPlan.isEmpty()) {
                return basicRoute;
            }

            boolean starts100 = ev.getInitialSoc() >= 1.0 - 1e-9;

            //////////////////////////////////////////////////////////////////////////////////////////////
            // Iterative sub-leg routing: after each stop, recompute the remaining route from the
            // actual stop location so the next timing window is measured from the true charger position.
            List<PlanElement> stagedRoute = new ArrayList<>();
            Facility lastFrom = fromFacility;
            double lastArrivaltime = departureTime;
            double currentEnergy = ev.getInitialCharge();
            boolean nextIsRest = false;
            double timeInCurrentSegment = 0.0; // accumulated driving time since last timing/rest stop

            int whileLoopCount = 0;
            while (true) {
                if (++whileLoopCount > 500) {
                    throw new RuntimeException("MpmEvNetworkRoutingModule: while-loop exceeded 500 iterations"
                            + " for person=" + person.getId()
                            + " lastFrom=" + lastFrom.getLinkId()
                            + " toFacility=" + toFacility.getLinkId()
                            + " currentEnergy=" + currentEnergy
                            + " nextIsRest=" + nextIsRest
                            + " timeInCurrentSegment=" + timeInCurrentSegment);
                }
                // Recompute remaining basic route from current position to destination.
                List<? extends PlanElement> remainingBasicRoute = delegate.calcRoute(
                        DefaultRoutingRequest.of(lastFrom, toFacility, lastArrivaltime, person, request.getAttributes()));
                Leg remainingBasicLeg = (Leg) remainingBasicRoute.get(0);

                StopPlan plan = computeStops(remainingBasicLeg, ev, currentEnergy, nextIsRest, timeInCurrentSegment);

                if (plan.isEmpty()) {
                    stagedRoute.addAll(remainingBasicRoute); // final direct leg — no more stops needed
                    break;
                }

                // Process only the first stop from this plan.
                Link stopLocation = plan.stopLocations.get(0);
                String stopReason = plan.stopReasons.get(stopLocation);

                // Promote late energy stops: if the agent has been driving >= 4h by the time
                // it reaches this energy stop, combine it with the mandatory timing/rest break.
                // This avoids a separate timing stop a few minutes later.
                if ("Energy".equals(stopReason)) {
                    double timeToStop = 0.0;
                    for (Map.Entry<Link, Double> ttEntry : estimateTravelTime(remainingBasicLeg).entrySet()) {
                        timeToStop += ttEntry.getValue();
                        if (ttEntry.getKey().equals(stopLocation)) break;
                    }
                    if (timeInCurrentSegment + timeToStop >= CHARGER_MIN_SEGMENT_TIME) {
                        stopReason = nextIsRest ? "Rest" : "Timing";
                    }
                }

                // Decide: charging stop or resting stop?
                boolean isChargingStop;
                if ("Energy".equals(stopReason)) {
                    isChargingStop = true;
                } else if (starts100) {
                    // 100% agent: charge only if remaining route would drop below MIN_SOC without charging.
                    double consumptionToStop = plan.cumulativeConsumptionToStop[0];
                    double energyAtStop = Math.max(0.0, currentEnergy - consumptionToStop);
                    double remaining = plan.totalTripConsumption - consumptionToStop;
                    isChargingStop = remaining > energyAtStop - MIN_SOC * ev.getBatteryCapacity();
                } else {
                    isChargingStop = true; // non-100% agents always charge
                }

                if (isChargingStop) {
                    // Route to nearest charger of the appropriate type for this stop category.
                    String chargerType = "Rest".equals(stopReason) ? "DC_slow" : "DC_fast";
                    Link chargerSearchLink = plan.stopLocationToSearchLink.getOrDefault(stopLocation, stopLocation);
                    List<ChargerSpecification> chargerCandidates = selectChargerCandidates(chargerSearchLink, ev, chargerType);
                    double referenceDistance = remainingBasicLeg.getRoute().getDistance();
                    double referenceTime = remainingBasicLeg.getTravelTime().seconds();

                    List<ChargerSpecification> feasibleChargers = new ArrayList<>();
                    List<Leg> feasibleLegs = new ArrayList<>();
                    List<Double> feasibleScores = new ArrayList<>();
                    List<Double> feasibleD1Times = new ArrayList<>();
                    // detourFallback: best candidate passing only the detour check (restores pre-filter behaviour).
                    ChargerSpecification detourFallbackCharger = null;
                    Leg detourFallbackLeg = null;
                    double detourFallbackScore = Double.MAX_VALUE;
                    // globalFallback: absolute last resort — never picks a charger at the current position
                    // (co-located chargers would produce subLeg==null and stall the while-loop).
                    ChargerSpecification fallbackCharger = null;
                    Leg fallbackLeg = null;
                    double fallbackScore = Double.MAX_VALUE;

                    for (ChargerSpecification candidate : chargerCandidates) {
                        Link candLink = network.getLinks().get(candidate.getLinkId());
                        Facility candFacility = new LinkWrapperFacility(candLink);

                        // d1: lastFrom → Charger
                        double d1, d1Time;
                        Leg toChargerLeg = null;
                        if (candFacility.getLinkId().equals(lastFrom.getLinkId())) {
                            d1 = 0.0;
                            d1Time = 0.0;
                        } else {
                            List<? extends PlanElement> seg1 = delegate.calcRoute(DefaultRoutingRequest.of(
                                    lastFrom, candFacility, lastArrivaltime, person, request.getAttributes()));
                            toChargerLeg = (Leg) seg1.get(0);
                            d1 = toChargerLeg.getRoute().getDistance();
                            d1Time = toChargerLeg.getTravelTime().seconds();
                        }

                        // d2: Charger → toFacility
                        double d2, d2Time;
                        if (candFacility.getLinkId().equals(toFacility.getLinkId())) {
                            d2 = 0.0;
                            d2Time = 0.0;
                        } else {
                            List<? extends PlanElement> seg2 = delegate.calcRoute(DefaultRoutingRequest.of(
                                    candFacility, toFacility, lastArrivaltime, person, request.getAttributes()));
                            Leg seg2Leg = (Leg) seg2.get(0);
                            d2 = seg2Leg.getRoute().getDistance();
                            d2Time = seg2Leg.getTravelTime().seconds();
                        }

                        double detour = (d1 + d2) - referenceDistance;
                        double score = (d1Time + d2Time - referenceTime)
                                + waitingTimeTracker.getAverageWaitingTime(candidate.getId());

                        boolean isAtCurrentPosition = candFacility.getLinkId().equals(lastFrom.getLinkId());
                        boolean withinTimeWindow = "Energy".equals(stopReason)
                                || (timeInCurrentSegment + d1Time >= CHARGER_MIN_SEGMENT_TIME
                                    && timeInCurrentSegment + d1Time <= CHARGER_MAX_SEGMENT_TIME);
                        if (detour <= CHARGER_SELECTION_MAX_DETOUR && withinTimeWindow) {
                            feasibleChargers.add(candidate);
                            feasibleLegs.add(toChargerLeg);
                            feasibleScores.add(score);
                            feasibleD1Times.add(d1Time);
                        }
                        // Detour-only fallback: restores original pre-filter behaviour when time window excludes all.
                        if (detour <= CHARGER_SELECTION_MAX_DETOUR && !isAtCurrentPosition && score < detourFallbackScore) {
                            detourFallbackScore = score;
                            detourFallbackCharger = candidate;
                            detourFallbackLeg = toChargerLeg;
                        }
                        // Global fallback: never pick a charger at the current position (subLeg==null → no progress).
                        if (!isAtCurrentPosition && score < fallbackScore) {
                            fallbackScore = score;
                            fallbackCharger = candidate;
                            fallbackLeg = toChargerLeg;
                        }
                    }

                    // Choose best candidate: minimum score (detour time + waiting time).
                    ChargerSpecification selectedCharger;
                    Leg subLeg;
                    if (!feasibleChargers.isEmpty()) {
                        int bestIdx = 0;
                        for (int i = 1; i < feasibleChargers.size(); i++) {
                            double scoreI    = feasibleScores.get(i);
                            double scoreBest = feasibleScores.get(bestIdx);
                            if (scoreI < scoreBest) {
                                bestIdx = i;
                            } else if (scoreI == scoreBest) {
                                // Tiebreak: prefer charger closest to the 4.5h driving limit
                                double timediffI    = Math.abs(timeInCurrentSegment + feasibleD1Times.get(i)       - MAX_DRIVE_TIME_WITHOUT_BREAK);
                                double timediffBest = Math.abs(timeInCurrentSegment + feasibleD1Times.get(bestIdx) - MAX_DRIVE_TIME_WITHOUT_BREAK);
                                if (timediffI < timediffBest) bestIdx = i;
                            }
                        }
                        selectedCharger = feasibleChargers.get(bestIdx);
                        subLeg = feasibleLegs.get(bestIdx);
                    } else if (detourFallbackCharger != null) {
                        // Detour-only fallback: time-window too strict for all candidates.
                        selectedCharger = detourFallbackCharger;
                        subLeg = detourFallbackLeg;
                    } else {
                        // Global fallback: no detour or time-window filter (never co-located = always subLeg!=null).
                        selectedCharger = fallbackCharger;
                        subLeg = fallbackLeg;
                    }
                    double subLegTime = (subLeg != null) ? subLeg.getTravelTime().seconds() : 0.0;
                    double chargerPower = selectedCharger.getPlugPower();
                    Link selectedChargerLink = network.getLinks().get(selectedCharger.getLinkId());
                    Facility nextFacility = new LinkWrapperFacility(selectedChargerLink);
                    boolean isFastCharger = "DC_fast".equals(selectedCharger.getChargerType());
                    double maxEnergyAtStop = isFastCharger ? ev.getBatteryCapacity() * MAX_FAST_SOC : ev.getBatteryCapacity();

                    if (subLeg == null) {
                        // Charger is at current position — no sub-leg needed; update state to avoid infinite loop.
                        double chargingDurationNull;
                        if ("Energy".equals(stopReason)) {
                            // Charge enough to reach the next timing/rest stop (same logic as normal flow).
                            int nextTimingIdxNull = -1;
                            for (int j = 1; j < plan.stopLocations.size(); j++) {
                                String r = plan.stopReasons.get(plan.stopLocations.get(j));
                                if ("Timing".equals(r) || "Rest".equals(r)) { nextTimingIdxNull = j; break; }
                            }
                            double consumptionToNextNull = (nextTimingIdxNull >= 0)
                                    ? plan.cumulativeConsumptionToStop[nextTimingIdxNull] - plan.cumulativeConsumptionToStop[0]
                                    : plan.totalTripConsumption - plan.cumulativeConsumptionToStop[0];
                            double maxChargeableNull = Math.max(0.0, maxEnergyAtStop - currentEnergy);
                            double energyChargedNull = Math.min(consumptionToNextNull + ENERGY_STOP_BUFFER_J, maxChargeableNull);
                            currentEnergy = Math.min(currentEnergy + energyChargedNull, maxEnergyAtStop);
                            chargingDurationNull = Math.max(1.0, energyChargedNull) / chargerPower + CHARGING_OVERHEAD;
                            // timeInCurrentSegment unchanged (zero travel time to charger)
                        } else if ("Rest".equals(stopReason)) {
                            currentEnergy = ev.getBatteryCapacity();
                            timeInCurrentSegment = 0.0;
                            nextIsRest = !nextIsRest;
                            chargingDurationNull = REST_DURATION;
                        } else { // "Timing"
                            currentEnergy = Math.min(
                                    currentEnergy + (BREAK_DURATION - CHARGING_OVERHEAD) * chargerPower,
                                    maxEnergyAtStop);
                            timeInCurrentSegment = 0.0;
                            nextIsRest = !nextIsRest;
                            chargingDurationNull = BREAK_DURATION;
                        }

                        // Add charging activity to stagedRoute (same pattern as normal flow).
                        String chargeActivityPrefixNull = mode + " " + stopReason + VehicleChargingHandler.CHARGING_IDENTIFIER;
                        Activity chargeActNull = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(
                                selectedChargerLink.getCoord(), selectedChargerLink.getId(), chargeActivityPrefixNull);
                        chargeActNull = PopulationUtils.createActivity(chargeActNull);
                        chargeActNull.setMaximumDuration(chargingDurationNull);
                        stagedRoute.add(chargeActNull);

                        lastArrivaltime += "Rest".equals(stopReason) ? REST_DURATION : BREAK_DURATION;
                        continue;
                    }

                    // Actual energy consumed on this sub-leg for accurate state tracking.
                    double subLegConsumption = estimateConsumption(ev, subLeg)
                            .values().stream().mapToDouble(Double::doubleValue).sum();
                    double energyAtCharger = Math.max(0.0, currentEnergy - subLegConsumption);

                    lastArrivaltime = subLeg.getDepartureTime().seconds() + subLegTime;
                    stagedRoute.add(subLeg);

                    // Charging duration based on stop type.
                    double chargingDuration;
                    if ("Energy".equals(stopReason)) {
                        // Charge only enough to reach the next timing/rest stop (or destination)
                        // so the agent arrives there with at least MIN_SOC.
                        int nextTimingIdx = -1;
                        for (int j = 1; j < plan.stopLocations.size(); j++) {
                            String r = plan.stopReasons.get(plan.stopLocations.get(j));
                            if ("Timing".equals(r) || "Rest".equals(r)) { nextTimingIdx = j; break; }
                        }
                        double consumptionToNext = (nextTimingIdx >= 0)
                                ? plan.cumulativeConsumptionToStop[nextTimingIdx] - plan.cumulativeConsumptionToStop[0]
                                : plan.totalTripConsumption - plan.cumulativeConsumptionToStop[0];
                        double maxChargeable = ev.getBatteryCapacity() * (1.0 - MIN_SOC);
                        chargingDuration = Math.max(1.0, Math.min(consumptionToNext + ENERGY_STOP_BUFFER_J, maxChargeable)) / chargerPower + CHARGING_OVERHEAD;
                    } else if ("Rest".equals(stopReason)) {
                        chargingDuration = REST_DURATION;
                    } else { // "Timing"
                        chargingDuration = BREAK_DURATION;
                    }

                    // Encode the stop reason in the stage-activity type so MpmVehicleChargingHandler
                    // can select the correct charger deterministically (avoids findAny() ambiguity)
                    // and MpmChargingProceduresCSVWriter can write the stop type to chargingStats.csv.
                    String chargeActivityPrefix = mode + " " + stopReason + VehicleChargingHandler.CHARGING_IDENTIFIER;
                    Activity chargeAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(
                            selectedChargerLink.getCoord(), selectedChargerLink.getId(), chargeActivityPrefix);
                    chargeAct = PopulationUtils.createActivity(chargeAct);
                    chargeAct.setMaximumDuration(chargingDuration);
                    lastArrivaltime += chargingDuration;
                    stagedRoute.add(chargeAct);
                    lastFrom = nextFacility;

                    // Update energy and timing state after this stop.
                    if ("Energy".equals(stopReason)) {
                        double energyCharged = (chargingDuration - CHARGING_OVERHEAD) * chargerPower;
                        currentEnergy = Math.min(energyAtCharger + energyCharged, maxEnergyAtStop);
                        timeInCurrentSegment += subLegTime;                // driving clock does NOT reset for energy stop
                    } else if ("Rest".equals(stopReason)) {
                        currentEnergy = ev.getBatteryCapacity();           // assume full after 11-h rest
                        timeInCurrentSegment = 0.0;                        // driving clock resets
                        nextIsRest = !nextIsRest;
                    } else { // "Timing"
                        currentEnergy = Math.min(
                                energyAtCharger + (BREAK_DURATION - CHARGING_OVERHEAD) * chargerPower,
                                maxEnergyAtStop);
                        timeInCurrentSegment = 0.0;                        // driving clock resets
                        nextIsRest = !nextIsRest;
                    }

                } else {
                    // Resting stop (no charging): drive to rest area identified by computeStops(),
                    // or fall back to timingLink if no rest area was found in this segment.
                    Link restLink = plan.stopLocationToRestAreaLink.get(stopLocation);
                    if (restLink == null) restLink = stopLocation; // fallback: timingLink

                    Facility stopFacility = new LinkWrapperFacility(restLink);
                    if (!stopFacility.getLinkId().equals(lastFrom.getLinkId())) {
                        List<? extends PlanElement> seg = delegate.calcRoute(DefaultRoutingRequest.of(
                                lastFrom, stopFacility, lastArrivaltime, person, request.getAttributes()));
                        Leg leg = (Leg) seg.get(0);
                        // Update energy from actual sub-leg consumption.
                        double legConsumption = estimateConsumption(ev, leg)
                                .values().stream().mapToDouble(Double::doubleValue).sum();
                        currentEnergy = Math.max(0.0, currentEnergy - legConsumption);
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

                    // Resting break resets the regulatory driving clock.
                    timeInCurrentSegment = 0.0;
                    nextIsRest = !nextIsRest;
                }
            }
            // Record route detour data for statistics.
            if (routeDetourTracker != null) {
                double stagedDistance = 0.0;
                double stagedTime = 0.0;
                int numStops = 0;
                for (PlanElement pe : stagedRoute) {
                    if (pe instanceof Leg leg) {
                        stagedDistance += leg.getRoute().getDistance();
                        stagedTime += leg.getTravelTime().seconds();
                    } else if (pe instanceof Activity) {
                        numStops++;
                    }
                }
                routeDetourTracker.recordRoute(person.getId().toString(),
                        basicDistance, basicTime, stagedDistance, stagedTime, numStops);
            }
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
    private StopPlan computeStops(Leg basicLeg, ElectricVehicleSpecification ev,
                                   double initialEnergy, boolean firstIsRest,
                                   double timeAlreadyDrivenInSegment) {
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
        double currentEnergy = initialEnergy;
        boolean nextIsRest = firstIsRest;
        double cumConsFromStart = 0.0;
        // Only the first segment uses timeAlreadyDrivenInSegment; subsequent segments start fresh.
        boolean firstSegment = true;

        while (true) {
            // A. Find the next timing/rest stop (4.5 h from segmentStart).
            // candidateLink           = last link with cumulative time <= 4.5h - buffer (charger search reference).
            // timingLink              = first link where cumulative time >= 4.5h (the regulatory limit).
            // lastRestAreaInSegment   = last route link within the timing window that has a rest area.
            boolean segStartFoundT = (segmentStart == null);
            double runningTime = firstSegment ? timeAlreadyDrivenInSegment : 0.0;
            firstSegment = false;
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
                // Final segment energy check: insert energy stop(s) if needed.
                Link localSegStart = segmentStart;
                double localCumFromStart = cumConsFromStart;
                double localEnergy = currentEnergy;

                while (true) {
                    double localUsable = localEnergy - MIN_SOC * ev.getBatteryCapacity();
                    double localCumCons = 0.0;
                    Link finalEnergyStop = null;
                    double cumConsToStop = 0.0;
                    boolean startFound = (localSegStart == null);

                    for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
                        if (!startFound) {
                            if (e.getKey().equals(localSegStart)) startFound = true;
                            continue;
                        }
                        localCumCons += e.getValue();
                        if (localCumCons >= localUsable) {
                            finalEnergyStop = e.getKey();
                            cumConsToStop = localCumCons;
                            break;
                        }
                    }

                    if (finalEnergyStop == null) break; // enough energy — done

                    stopLocations.add(finalEnergyStop);
                    stopLocationToSearchLink.put(finalEnergyStop, finalEnergyStop);
                    stopReasons.put(finalEnergyStop, "Energy");
                    cumulativeList.add(localCumFromStart + cumConsToStop);

                    // Estimate post-charge energy (same assumptions as existing code)
                    double energyAtStop = MIN_SOC * ev.getBatteryCapacity();
                    double remainingCons = totalConsumption - (localCumFromStart + cumConsToStop);
                    double maxChargeable = ev.getBatteryCapacity() * MAX_FAST_SOC - energyAtStop;
                    double charged = Math.min(remainingCons + ENERGY_STOP_BUFFER_J, Math.max(0, maxChargeable));
                    localEnergy = energyAtStop + charged;

                    localSegStart = finalEnergyStop;
                    localCumFromStart += cumConsToStop;
                }
                break;
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
                // Use PLANNING_CHARGER_POWER here — computeStops() is a geometric planning pass
                // and does not know which charger will actually be selected in calcRoute().
                currentEnergy = Math.min(
                        energyAtStop + (BREAK_DURATION - CHARGING_OVERHEAD) * PLANNING_CHARGER_POWER,
                        ev.getBatteryCapacity() * MAX_FAST_SOC);
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

    /**
     * Returns up to 5 nearest compatible chargers sorted by ascending KNN distance (closest first).
     */
    private List<ChargerSpecification> selectChargerCandidates(Link chargerSearchLink,
                                                               ElectricVehicleSpecification ev,
                                                               String chargerType) {
        StraightLineKnnFinder<Link, ChargerSpecification> finder = new StraightLineKnnFinder<>(
                5, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
        List<ChargerSpecification> nearest = finder.findNearest(chargerSearchLink,
                chargingInfrastructureSpecification.getChargerSpecifications().values().stream()
                        .filter(c -> c.getChargerType().equals(chargerType)
                                     && ev.getChargerTypes().contains(c.getChargerType())));
        return nearest; // KNN-order = closest first
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

