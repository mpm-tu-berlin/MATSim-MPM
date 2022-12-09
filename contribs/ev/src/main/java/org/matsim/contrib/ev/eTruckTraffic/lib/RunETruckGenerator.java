package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.api.core.v01.population.Person;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.fleet.*;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.config.groups.QSimConfigGroup;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.core.utils.io.MatsimXmlWriter;
import org.matsim.vehicles.*;

import java.util.*;

public class RunETruckGenerator {

	static final double CAR_BATTERY_CAPACITY_kWh = 600.;
	static final double CAR_INITIAL_SOC = 1;
	static final String TRUCK_CHARGERS_TYPE = "DC";

	public static void main(String[] args) {

		EvConfigGroup evConfigGroup = new EvConfigGroup();
		// using the Population of the EV example for comparison reason
		String pathToConfig = "contribs\\ev\\test\\input\\org\\matsim\\contrib\\ev\\example\\RunEvExample\\config.xml";
		Config config = ConfigUtils.loadConfig(pathToConfig, evConfigGroup);
		config.qsim().setVehiclesSource(QSimConfigGroup.VehiclesSource.fromVehiclesData);

		Scenario scenario = ScenarioUtils.loadScenario(config);

		VehiclesFactory vehicleFactory = scenario.getVehicles().getFactory();

		for (Person person : scenario.getPopulation().getPersons().values()) {

			VehicleType carVehicleType = vehicleFactory.createVehicleType(Id.create(person.getId().toString(),
					VehicleType.class));
			VehicleUtils.setHbefaTechnology(carVehicleType.getEngineInformation(), "electricity");
			VehicleUtils.setEnergyCapacity(carVehicleType.getEngineInformation(), CAR_BATTERY_CAPACITY_kWh);
			// TODO why do I need collection.singleton?
			ElectricVehicleSpecifications.setChargerTypes(carVehicleType.getEngineInformation(), Collections.singleton(TRUCK_CHARGERS_TYPE));
			scenario.getVehicles().addVehicleType(carVehicleType);
			Vehicle carVehicle = vehicleFactory.createVehicle(VehicleUtils.createVehicleId(person, TransportMode.truck),
					carVehicleType);
			ElectricVehicleSpecifications.setInitialSoc(carVehicle, CAR_INITIAL_SOC);
			scenario.getVehicles().addVehicle(carVehicle);

			//TODO How to create evehicles.xml?
			// With MatsimVehicleWriter.java
			// MatsimVehicleWriter(carVehicle)
		}
	}
}
