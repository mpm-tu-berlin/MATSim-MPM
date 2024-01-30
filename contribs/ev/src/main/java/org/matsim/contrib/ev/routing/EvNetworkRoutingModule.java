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
package org.matsim.contrib.ev.routing;

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

import static org.matsim.api.core.v01.TransportMode.car;

/**
 * This network Routing module adds stages for re-charging into the Route.
 * This wraps a "computer science" {@link LeastCostPathCalculator}, which routes from a node to another node, into something that
 * routes from a {@link Facility} to another {@link Facility}, as we need in MATSim.
 *
 * @author jfbischoff
 */

final class EvNetworkRoutingModule implements RoutingModule {

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
	private static final double MIN_SOC = 0.2; // Minimum State of Charge
	private static final double MAX_DRIVE_TIME_WITHOUT_BREAK = 4.5 * 60 * 60; // Maximum driving time without a break in seconds
	private static final double MAX_OVERALL_DRIVE_TIME_PER_TRIP = 6 * 60 * 60; // Maximum overall allowed driving time in one go in seconds
	private static final double MAX_OVERALL_DRIVE_TIME_PER_DAY = 9 * 60 * 60; // Maximum overall allowed driving time per day in seconds
	private static final double BREAK_DURATION = 45 * 60; // in seconds
	private static final double REST_DURATION = 11 * 60 * 60; // in seconds
	private static final double CHARGER_POWER = 720 * 1000; // in Watt

	EvNetworkRoutingModule(final String mode, final Network network, RoutingModule delegate,
			ElectricFleetSpecification electricFleet,
			ChargingInfrastructureSpecification chargingInfrastructureSpecification, TravelTime travelTime,
			DriveEnergyConsumption.Factory driveConsumptionFactory, AuxEnergyConsumption.Factory auxConsumptionFactory,
			EvConfigGroup evConfigGroup) {
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
			ElectricVehicleSpecification ev = electricFleet.getVehicleSpecifications().get(evId);

			Map<Link, Double> estimatedEnergyConsumption = estimateConsumption(ev, basicLeg);
			Map<Link, Double> estimatedTravelTime = estimateTravelTime(basicLeg);
			double initialSocAtSTart = ev.getInitialSoc();
			double usableCapcityAfterFirstBreak = 0;
			double usableCapcityAfterSecondBreak = 0;
			List<Link> stopLocations = new ArrayList<>();
			Map<Link, String> stopReasons = new LinkedHashMap<>();
			double currentConsumption = 0;
			double consumptionFirstPart = 0;
			double consumptionSecondPart = 0;
			Map<Link, Integer> stopSocOrBreakTime = new LinkedHashMap<>();
			double initialUsableCapacity = ev.getBatteryCapacity() * (initialSocAtSTart - MIN_SOC);
			double currentTravelTime = 0;   //Journey time since the last stop
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
			for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
				currentTravelTime += e.getValue();
				counter++;
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
				return basicRoute;
			} else{
				Link linkWithFirstBreakNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
				stopLocations.add(linkWithFirstBreakNecessity);
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
				for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) { //See when energy demand is too high
					if (e.getKey().equals(stopLocations.get(0))) {
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
				for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
					absoluteTravelTime += e.getValue();
					if (e.getKey().equals(stopLocations.get(0))) {
						currentTravelTime = 0;
						startFound = true;
					}
					if (startFound) {
						counter++;
						currentTravelTime += e.getValue();
						if (currentTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_TRIP) {
							stopSocOrBreakTime.put(e.getKey(), counter);
							stopReasons.put(e.getKey(), "Breaktime after 6h2");
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
					stopSocOrBreakTime.clear();
					for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
						if (e.getKey().equals(stopLocations.get(0))) {
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
					for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) { //See when energy demand is too high
						if (e.getKey().equals(stopLocations.get(1))) {
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
					for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) { //Check when the journey duration is longer than permitted
						absoluteTravelTime += e.getValue();
						if (e.getKey().equals(stopLocations.get(1))) {
							currentTravelTime = 0;
							startFound = true;
						}
						if (startFound) {
							counter++;
							currentTravelTime += e.getValue();
							if (currentTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_TRIP) {
								stopSocOrBreakTime.put(e.getKey(), counter);
								stopReasons.put(e.getKey(), "Breaktime after 6h3");
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
						stopSocOrBreakTime.clear();

						//Remove elements from stopReasons that are not in stopLocations
						stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));
					}
				}
			}
			//////////////////////////////////////////////////////////////////////////////////////////////
			// First break after 11h break
			// Placeholder for further Implementations

			//////////////////////////////////////////////////////////////////////////////////////////////
			// Include detours to the nearest charger
			List<PlanElement> stagedRoute = new ArrayList<>();
			Facility lastFrom = fromFacility;
			double lastArrivaltime = departureTime;

			for (Link stopLocation : stopLocations) {
				StraightLineKnnFinder<Link, ChargerSpecification> straightLineKnnFinder = new StraightLineKnnFinder<>(
					2, Link::getCoord, s -> network.getLinks().get(s.getLinkId()).getCoord());
				List<ChargerSpecification> nearestChargers = straightLineKnnFinder.findNearest(stopLocation, // Auswahl nächstgelegener Charger
					chargingInfrastructureSpecification.getChargerSpecifications()
						.values()
						.stream()
						.filter(charger -> ev.getChargerTypes().contains(charger.getChargerType())));
				ChargerSpecification selectedCharger = nearestChargers.get(random.nextInt(1));
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
				if ("Breaktime after 9h2".equals(stopReasons.get(stopLocation)) || "Breaktime after 9h3".equals(stopReasons.get(stopLocation))) {
					Activity restAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(selectedChargerLink.getCoord(), stopLocation.getId(), "resting");
					restAct = PopulationUtils.createActivity(restAct);
					restAct.setMaximumDuration(REST_DURATION);
					lastArrivaltime += restAct.getMaximumDuration().seconds();
					stagedRoute.add(restAct);
					lastFrom = nexttoFacility;
				}else {
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

			//double numberOfChargingStops = Math.floor(estimatedOverallConsumption / initialSOC);
			//double socAfterCharging = initialSocForRouting -;
			//List<Link> stopLocations = new ArrayList<>();
		}
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
			double travelT = travelTime.getLinkTravelTime(l, basicLeg.getDepartureTime().seconds(), null, null);

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
			double travelT = travelTime.getLinkTravelTime(l, basicLeg.getDepartureTime().seconds(), null, null);
			travelTimes.put(l, travelT);
		}
		return travelTimes;
	}

	@Override
	public String toString() {
		return "[NetworkRoutingModule: mode=" + this.mode + "]";
	}

}
