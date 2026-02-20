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
    private static final double MIN_SOC = 0.2; // Minimum State of Charge
    private static final double MAX_DRIVE_TIME_WITHOUT_BREAK = 4.5 * 60 * 60; // Maximum driving time without a break in seconds
    private static final double MAX_OVERALL_DRIVE_TIME_PER_DAY = 9 * 60 * 60; // Maximum overall allowed driving time per day in seconds
    private static final double BREAK_DURATION = 45 * 60; // in seconds
    private static final double REST_DURATION = 11 * 60 * 60; // in seconds
    private static final double CHARGER_POWER = 640 * 1000; // in Watt
    private static final double CHARGER_SEARCH_BUFFER = 10 * 60; // in seconds, additional time buffer to consider for charger search to not exceed 4,5h driving limit
    static final double MAX_VEHICLE_SPEED = 18.056; // in m/s (65 km/h)

    /** Result of the stop-computation pass — the three maps used by sub-leg routing. */
    private static final class StopPlan {
        final List<Link> stopLocations;
        final Map<Link, Link> stopLocationToSearchLink;
        final Map<Link, String> stopReasons;

        StopPlan(List<Link> stopLocations, Map<Link, Link> stopLocationToSearchLink, Map<Link, String> stopReasons) {
            this.stopLocations = stopLocations;
            this.stopLocationToSearchLink = stopLocationToSearchLink;
            this.stopReasons = stopReasons;
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
                              ChargerPenaltyTravelDisutility chargerPenaltyDisutility) {
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

            // Build penalty map: only the charger(s) this agent would actually stop at.
            Map<Id<Link>, Double> penalties = new LinkedHashMap<>();
            for (Link stopLocation : stopPlan.stopLocations) {
                Link searchLink = stopPlan.stopLocationToSearchLink.getOrDefault(stopLocation, stopLocation);
                ChargerSpecification charger = selectCharger(searchLink, ev);
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

            for (Link stopLocation : stopPlan.stopLocations) {
                StraightLineKnnFinder<Link, ChargerSpecification> straightLineKnnFinder = new StraightLineKnnFinder<>(
                        5, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
                Link chargerSearchLink = stopPlan.stopLocationToSearchLink.getOrDefault(stopLocation, stopLocation);
                List<ChargerSpecification> nearestChargers = straightLineKnnFinder.findNearest(chargerSearchLink,
                        chargingInfrastructureSpecification.getChargerSpecifications()
                                .values()
                                .stream()
                                .filter(charger -> ev.getChargerTypes().contains(charger.getChargerType())));
                // Select charger with lowest average waiting time from previous iteration
                // (falls back to nearest if no congestion data exists)
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

                // Allocating a short break in the journey or a night-time standstill
                if ("Breaktime after 9h2".equals(stopPlan.stopReasons.get(stopLocation)) || "Breaktime after 9h3".equals(stopPlan.stopReasons.get(stopLocation))) {
                    Activity restAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(selectedChargerLink.getCoord(), stopLocation.getId(), "resting");
                    restAct = PopulationUtils.createActivity(restAct);
                    restAct.setMaximumDuration(REST_DURATION);
                    lastArrivaltime += restAct.getMaximumDuration().seconds();
                    stagedRoute.add(restAct);
                    lastFrom = nexttoFacility;
                } else {
                    Activity chargeAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(selectedChargerLink.getCoord(),
                            selectedChargerLink.getId(), stageActivityModePrefix);
                    chargeAct = PopulationUtils.createActivity(chargeAct);
                    chargeAct.setMaximumDuration(BREAK_DURATION);
                    lastArrivaltime += chargeAct.getMaximumDuration().seconds();
                    stagedRoute.add(chargeAct);
                    lastFrom = nexttoFacility;
                }
            }
            stagedRoute.addAll(delegate.calcRoute(DefaultRoutingRequest.of(lastFrom, toFacility, lastArrivaltime, person, request.getAttributes())));
            return stagedRoute;
        }
    }

    /**
     * Computes up to three charging/rest stop locations for the given route leg.
     * Returns an empty {@link StopPlan} when no stop is needed (route fits within battery and time limits).
     */
    private StopPlan computeStops(Leg basicLeg, ElectricVehicleSpecification ev) {
        Map<Link, Double> estimatedEnergyConsumption = estimateConsumption(ev, basicLeg);
        Map<Link, Double> estimatedTravelTime = estimateTravelTime(basicLeg);
        double initialSocAtStart = ev.getInitialSoc();
        double usableCapcityAfterFirstBreak = 0;
        double usableCapcityAfterSecondBreak = 0;
        List<Link> stopLocations = new ArrayList<>();
        Map<Link, Link> stopLocationToSearchLink = new LinkedHashMap<>();
        Map<Link, String> stopReasons = new LinkedHashMap<>();
        double currentConsumption = 0;
        double consumptionFirstPart = 0;
        double consumptionSecondPart = 0;
        Map<Link, Integer> stopSocOrBreakTime = new LinkedHashMap<>();
        double initialUsableCapacity = ev.getBatteryCapacity() * (initialSocAtStart - MIN_SOC);
        double currentTravelTime = 0;
        double absoluteTravelTime = 0;
        int counter = 0;
        boolean startFound = false;

        //////////////////////////////////////////////////////////////////////////////////////////////
        //First Stop
        for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) { //See when energy demand is too high for initialUsableCapacity
            currentConsumption += e.getValue();
            counter++;
            if (currentConsumption >= initialUsableCapacity) {
                stopSocOrBreakTime.put(e.getKey(), counter);
                stopReasons.put(e.getKey(), "Energy1");
                break;
            }
        }
        currentConsumption = 0;
        counter = 0;
        Link candidateLinkFirstStop = null;
        for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
            currentTravelTime += e.getValue();
            counter++;
            if (currentTravelTime <= MAX_DRIVE_TIME_WITHOUT_BREAK - CHARGER_SEARCH_BUFFER) {
                candidateLinkFirstStop = e.getKey();
            }
            if (currentTravelTime >= MAX_DRIVE_TIME_WITHOUT_BREAK) {
                stopSocOrBreakTime.put(e.getKey(), counter);
                stopReasons.put(e.getKey(), "Breaktime after 4.5h1");
                break;
            }
        }
        currentTravelTime = 0;
        counter = 0;
        //Saving the event that occurs first during the trip
        if (stopSocOrBreakTime.isEmpty()){
            return new StopPlan(stopLocations, stopLocationToSearchLink, stopReasons); // no stop needed
        } else{
            Link linkWithFirstBreakNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
            stopLocations.add(linkWithFirstBreakNecessity);
            String firstStopReason = stopReasons.get(linkWithFirstBreakNecessity);
            boolean firstStopIsTiming = firstStopReason != null && firstStopReason.startsWith("Breaktime");
            stopLocationToSearchLink.put(linkWithFirstBreakNecessity,
                    (firstStopIsTiming && candidateLinkFirstStop != null) ? candidateLinkFirstStop : linkWithFirstBreakNecessity);
            stopSocOrBreakTime.clear();
            for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
                consumptionFirstPart += e.getValue();
                if (e.getKey().equals(linkWithFirstBreakNecessity)) {
                    break;
                }
            }
        }
        //Remove elements from stopReasons that are not in stopLocations
        stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));

        //Calculate capacity after charging for onward journey; Check whether the battery is charging for 45 seconds or whether it may have reached 100% beforehand)
        usableCapcityAfterFirstBreak = Math.min(ev.getInitialCharge() - consumptionFirstPart + BREAK_DURATION * CHARGER_POWER, ev.getBatteryCapacity()) - MIN_SOC * ev.getBatteryCapacity();

        //////////////////////////////////////////////////////////////////////////////////////////////
        //Second stop:
        if(!stopReasons.get(stopLocations.get(0)).isEmpty()) {
            Link secondLegStart = stopLocationToSearchLink.getOrDefault(stopLocations.get(0), stopLocations.get(0));
            for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) { //See when energy demand is too high
                if (e.getKey().equals(secondLegStart)) {
                    startFound = true;
                }
                if (startFound) {
                    currentConsumption += e.getValue();
                    counter++;
                    if (currentConsumption >= usableCapcityAfterFirstBreak) {
                        stopSocOrBreakTime.put(e.getKey(), counter);
                        stopReasons.put(e.getKey(), "Energy2");
                        break;
                    }
                }
            }
            currentConsumption = 0;
            counter = 0;
            startFound = false;
            Link candidateLinkSecondStop = null;
            for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
                absoluteTravelTime += e.getValue();
                if (e.getKey().equals(secondLegStart)) {
                    currentTravelTime = 0;
                    startFound = true;
                }
                if (startFound) {
                    counter++;
                    currentTravelTime += e.getValue();
                    if (currentTravelTime <= MAX_DRIVE_TIME_WITHOUT_BREAK - CHARGER_SEARCH_BUFFER) {
                        candidateLinkSecondStop = e.getKey();
                    }
                    if (currentTravelTime >= MAX_DRIVE_TIME_WITHOUT_BREAK) {
                        stopSocOrBreakTime.put(e.getKey(), counter);
                        stopReasons.put(e.getKey(), "Breaktime after 4.5h2");
                        break;
                    }
                    if (absoluteTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_DAY) {
                        stopSocOrBreakTime.put(e.getKey(), counter);
                        stopReasons.put(e.getKey(), "Breaktime after 9h2");
                        break;
                    }
                }
            }
            counter = 0;
            startFound = false;
            absoluteTravelTime = 0;
            currentTravelTime = 0;

            //Saving the event that occurs first during this trip
            if (!stopSocOrBreakTime.isEmpty()) {
                Link linkWithSecondBreakNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
                stopLocations.add(linkWithSecondBreakNecessity);
                String secondStopReason2 = stopReasons.get(linkWithSecondBreakNecessity);
                boolean secondStopIsTiming = secondStopReason2 != null && secondStopReason2.startsWith("Breaktime");
                stopLocationToSearchLink.put(linkWithSecondBreakNecessity,
                        (secondStopIsTiming && candidateLinkSecondStop != null) ? candidateLinkSecondStop : linkWithSecondBreakNecessity);
                stopSocOrBreakTime.clear();
                for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
                    if (e.getKey().equals(secondLegStart)) {
                        startFound = true;
                    }
                    if (startFound){
                        consumptionSecondPart += e.getValue();
                        if (e.getKey().equals(linkWithSecondBreakNecessity)) {
                            break;
                        }
                    }
                }
                startFound = false;
                //Remove elements from stopReasons that are not in stopLocations
                stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));

                //Calculate capacity after second charging for onward journey; Check whether the battery is charging for 45 seconds or whether it may have reached 100% beforehand)
                usableCapcityAfterSecondBreak = Math.min(usableCapcityAfterFirstBreak - consumptionSecondPart + BREAK_DURATION * CHARGER_POWER, ev.getBatteryCapacity()) - MIN_SOC * ev.getBatteryCapacity();
            }
        }
        //////////////////////////////////////////////////////////////////////////////////////////////
        // Possible third stop (only if 9h travelling time has not been reached before)
        if (stopLocations.size() > 1) {
            String secondStopReason = stopReasons.get(stopLocations.get(1)); //Reason for the second stop
            if (secondStopReason.equals("Breaktime after 9h2")) {
                //Placeholder for further Implementations
            }
            else {
                Link thirdLegStart = stopLocationToSearchLink.getOrDefault(stopLocations.get(1), stopLocations.get(1));
                for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) { //See when energy demand is too high
                    if (e.getKey().equals(thirdLegStart)) {
                        startFound = true;
                    }
                    if (startFound) {
                        currentConsumption += e.getValue();
                        counter++;
                        if (currentConsumption >= usableCapcityAfterSecondBreak) {
                            stopSocOrBreakTime.put(e.getKey(), counter);
                            stopReasons.put(e.getKey(), "Energy3");
                            break;
                        }
                    }
                }
                counter = 0;
                startFound = false;
                Link candidateLinkThirdStop = null;
                for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
                    absoluteTravelTime += e.getValue();
                    if (e.getKey().equals(thirdLegStart)) {
                        currentTravelTime = 0;
                        startFound = true;
                    }
                    if (startFound) {
                        counter++;
                        currentTravelTime += e.getValue();
                        if (currentTravelTime <= MAX_DRIVE_TIME_WITHOUT_BREAK - CHARGER_SEARCH_BUFFER) {
                            candidateLinkThirdStop = e.getKey();
                        }
                        if (currentTravelTime >= MAX_DRIVE_TIME_WITHOUT_BREAK) {
                            stopSocOrBreakTime.put(e.getKey(), counter);
                            stopReasons.put(e.getKey(), "Breaktime after 4.5h3");
                            break;
                        }
                        if (absoluteTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_DAY) {
                            stopSocOrBreakTime.put(e.getKey(), counter);
                            stopReasons.put(e.getKey(), "Breaktime after 9h3");
                            break;
                        }
                    }
                }
                //Saving the event that occurs first during this trip
                if (!stopSocOrBreakTime.isEmpty()) {
                    Link linkWithFirstBreakNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
                    stopLocations.add(linkWithFirstBreakNecessity);
                    String thirdStopReason = stopReasons.get(linkWithFirstBreakNecessity);
                    boolean thirdStopIsTiming = thirdStopReason != null && thirdStopReason.startsWith("Breaktime");
                    stopLocationToSearchLink.put(linkWithFirstBreakNecessity,
                            (thirdStopIsTiming && candidateLinkThirdStop != null) ? candidateLinkThirdStop : linkWithFirstBreakNecessity);
                    stopSocOrBreakTime.clear();

                    //Remove elements from stopReasons that are not in stopLocations
                    stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));
                }
            }
        }
        //////////////////////////////////////////////////////////////////////////////////////////////
        // First break after 11h break
        // Placeholder for further Implementations

        return new StopPlan(stopLocations, stopLocationToSearchLink, stopReasons);
    }

    /**
     * Selects the charger with the lowest expected waiting time from the 5 nearest chargers
     * to {@code chargerSearchLink} that are compatible with {@code ev}.
     */
    private ChargerSpecification selectCharger(Link chargerSearchLink, ElectricVehicleSpecification ev) {
        StraightLineKnnFinder<Link, ChargerSpecification> finder = new StraightLineKnnFinder<>(
                5, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
        List<ChargerSpecification> nearest = finder.findNearest(chargerSearchLink,
                chargingInfrastructureSpecification.getChargerSpecifications().values().stream()
                        .filter(c -> ev.getChargerTypes().contains(c.getChargerType())));
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
