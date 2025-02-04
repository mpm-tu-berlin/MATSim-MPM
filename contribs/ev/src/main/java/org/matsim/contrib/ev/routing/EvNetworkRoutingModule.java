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
//import org.matsim.contrib.ev.discharging.DynamicConsumption;
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
	private static final double MIN_SOC = 0.15; // Minimum State of Charge     (****CHANGED FROM 20 TO 15%****)
	private static final double MAX_DRIVE_TIME_WITHOUT_BREAK = 4.5 * 60 * 60; // Maximum driving time without a break in seconds
	//private static final double MAX_OVERALL_DRIVE_TIME_PER_TRIP = 6 * 60 * 60; // Maximum overall allowed driving time in one go in seconds
	private static final double MAX_OVERALL_DRIVE_TIME_PER_DAY = 9 * 60 * 60; // Maximum overall allowed driving time per day in seconds
	private static final double REFUELING_DURATION = 15 * 60; // in seconds     (****CHANGED FROM 45 TO 15 MIN****)
	private static final double RESTING_DURATION = 11 * 60 * 60; // in seconds
	//private static final double CHARGER_POWER =  3970 * 1000; //in Watt	0.85*35kg*33.33kwh/kg / 0.25h   =  3966.27 kw    (15 min = 0.25h)  (CHANGED FROM 640 TO 3970)
	private static final double MAX_VEHICLE_SPEED = 22.222; // in m/s (80 km/h)


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
			double usableCapacityAfterFirstBreak = 0;

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

			//New variables (hydrogen)

			Link linkWithFirstStopNecessity;
			Link FirstBreakLink = null;
			Link linkWithSecondStopNecessity = null;
			int breakCounter = 0;
			double consumptionAfterRefueling = 0;



//////////////////////////////////////////////////////////////////////////////////////////////
//First stop:

			for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
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

			for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) {
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
			if (stopSocOrBreakTime.isEmpty()) {
				return basicRoute;
			} else {
				linkWithFirstStopNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
				stopLocations.add(linkWithFirstStopNecessity);
				breakCounter += 1;                         //Regardless of the reason for the stop, breakCounter is increased
				stopSocOrBreakTime.clear();
			}

			//Remove elements from stopReasons that are not in stopLocations
			stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));



			//Calculating energy consumption in case first stop due to travel time

			if("Breaktime after 4.5h1".equals(stopReasons.get(stopLocations.get(0)))) {

				FirstBreakLink = linkWithFirstStopNecessity;

				for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
					consumptionFirstPart += e.getValue();
					if (e.getKey().equals(linkWithFirstStopNecessity)) {
						break;
					}
				}
				usableCapacityAfterFirstBreak = ev.getInitialCharge() - consumptionFirstPart;
			}


			//Instructions to follow in case first stop due to low SOC

			if ("Energy1".equals(stopReasons.get(stopLocations.get(0)))) {


				for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) {

					absoluteTravelTime += e.getValue();
					counter++;

					if (absoluteTravelTime >= MAX_DRIVE_TIME_WITHOUT_BREAK) {
						stopSocOrBreakTime.put(e.getKey(), counter);
						stopReasons.put(e.getKey(), "Breaktime after 4.5h1");
						break;
					}
				}

				absoluteTravelTime = 0;
				counter = 0;


				if (stopSocOrBreakTime.isEmpty()) {
					return basicRoute;
				}
				else{
					FirstBreakLink = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
					stopLocations.add(FirstBreakLink);
					stopSocOrBreakTime.clear();
				}

				for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {

					if (e.getKey().equals(linkWithFirstStopNecessity)) {
						startFound = true;
					}
					if (startFound) {
						consumptionAfterRefueling += e.getValue();
						if (e.getKey().equals(FirstBreakLink)) {
							break;
						}

					}
				}
				startFound = false;
				usableCapacityAfterFirstBreak = ev.getBatteryCapacity() * (1- MIN_SOC) - consumptionAfterRefueling;

				//Remove elements from stopReasons that are not in stopLocations
				stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));

			}


//////////////////////////////////////////////////////////////////////////////////////////////
//Second stop:


			if(breakCounter == 1) {

				for (Map.Entry<Link, Double> e : estimatedEnergyConsumption.entrySet()) {
					if (e.getKey().equals(FirstBreakLink)) {
						startFound = true;
					}
					if (startFound) {
						currentConsumption += e.getValue();
						counter++;
						if (currentConsumption >= usableCapacityAfterFirstBreak) {
							stopSocOrBreakTime.put(e.getKey(), counter);
							stopReasons.put(e.getKey(), "Energy2");
							break;
						}
					}
				}
				currentConsumption = 0;
				counter = 0;
				startFound = false;

				for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) {

					absoluteTravelTime += e.getValue();

					if (e.getKey().equals(FirstBreakLink)) {
						startFound = true;
					}
					if (startFound) {
						counter++;
						if (absoluteTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_DAY) {
							stopSocOrBreakTime.put(e.getKey(), counter);
							stopReasons.put(e.getKey(), "Breaktime after 9h2");
							break;
						}
					}
				}
				counter = 0;
				absoluteTravelTime = 0;
				startFound = false;


				//Saving the event that occurs first during this trip
				if (!stopSocOrBreakTime.isEmpty()) {
					linkWithSecondStopNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
					stopLocations.add(linkWithSecondStopNecessity);
					breakCounter += 1;
					stopSocOrBreakTime.clear();


					//Remove elements from stopReasons that are not in stopLocations
					stopReasons.entrySet().removeIf(entry -> !stopLocations.contains(entry.getKey()));
				}
			}


//////////////////////////////////////////////////////////////////////////////////////////////
// Possible third stop (only if 9h travelling time has not been reached before)


			if (breakCounter > 1) { //If 2 stops have been made, and the second one was due to low SOC, we can consider a third stop

				if ("Energy2".equals(stopReasons.get(linkWithSecondStopNecessity))) {

					for (Map.Entry<Link, Double> e : estimatedTravelTime.entrySet()) {

						absoluteTravelTime += e.getValue();

						if (e.getKey().equals(linkWithSecondStopNecessity)) {
							startFound = true;
						}
						if (startFound) {
							counter++;
							if (absoluteTravelTime >= MAX_OVERALL_DRIVE_TIME_PER_DAY) {
								stopSocOrBreakTime.put(e.getKey(), counter);
								stopReasons.put(e.getKey(), "Breaktime after 9h3");
								break;
							}
						}
					}

					//Saving the event that occurs first during this trip
					if (!stopSocOrBreakTime.isEmpty()) {
						Link linkWithThirdStopNecessity = Collections.min(stopSocOrBreakTime.entrySet(), Map.Entry.comparingByValue()).getKey();
						stopLocations.add(linkWithThirdStopNecessity);
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
					restAct.setMaximumDuration(RESTING_DURATION);
					lastArrivaltime += restAct.getMaximumDuration().seconds();
					stagedRoute.add(restAct);
					lastFrom = nexttoFacility;
				}else {
					Activity chargeAct = PopulationUtils.createStageActivityFromCoordLinkIdAndModePrefix(selectedChargerLink.getCoord(),
						selectedChargerLink.getId(), stageActivityModePrefix);
					chargeAct = PopulationUtils.createActivity(chargeAct);
					chargeAct.setMaximumDuration(REFUELING_DURATION);
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



	/*
	private Map<Link, Double> estimateConsumption(ElectricVehicleSpecification ev, Leg basicLeg) {
		Map<Link, Double> consumptions = new LinkedHashMap<>();
		NetworkRoute route = (NetworkRoute) basicLeg.getRoute();
		List<Link> links = NetworkUtils.getLinks(network, route.getLinkIds());
		double linkEnterTime = basicLeg.getDepartureTime().seconds();
		for (Link l : links) {
			double travelT = l.getLength() / Math.min(MAX_VEHICLE_SPEED, l.getFreespeed());
			double consumption = dynamicConsumption.calcEnergyConsumption(l, travelT, linkEnterTime);
			consumptions.put(l, consumption);
			linkEnterTime += travelT;
		}
		return consumptions;
	}
*/

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
