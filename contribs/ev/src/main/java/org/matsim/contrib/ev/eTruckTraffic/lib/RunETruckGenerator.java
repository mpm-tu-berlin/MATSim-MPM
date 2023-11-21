package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.population.Person;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.fleet.*;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.config.groups.QSimConfigGroup;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.vehicles.*;

import java.util.*;

public class RunETruckGenerator {

	static final double CAR_BATTERY_CAPACITY_kWh = 600.;
	static final double CAR_INITIAL_SOC = 1;
	static final String TRUCK_CHARGERS_TYPE = "DC";
	static final String VEHICLE_TYPE_ID = "600kWh_eTruck";
	final static String CONFIG_FILE = "./input/TestETruckTraffic/config.xml";
	final static String DEFAULT_PATH = "./input/TestETruckTraffic/";

	public static void main(String[] args) {

		Config config = ConfigUtils.loadConfig(CONFIG_FILE, new EvConfigGroup());
		String vehicleFile = DEFAULT_PATH + config.vehicles().getVehiclesFile();
		RunETruckGenerator.run(config, vehicleFile);
	}
	public static void run(Config config, String eVehicleFile) {

		// using the Population of the EV example for comparison reason
		config.qsim().setVehiclesSource(QSimConfigGroup.VehiclesSource.fromVehiclesData);

		Scenario scenario = ScenarioUtils.loadScenario(config);
		VehiclesFactory vehicleFactory = scenario.getVehicles().getFactory();


		for (Id vId : scenario.getVehicles().getVehicles().keySet()) {
			scenario.getVehicles().removeVehicle(Id.createVehicleId(vId));
		}

		scenario.getVehicles().removeVehicleType(Id.create(VEHICLE_TYPE_ID, VehicleType.class));


		scenario.getVehicles().removeVehicleType(Id.create(VEHICLE_TYPE_ID, VehicleType.class));
		VehicleType vehicleType_eTruck = vehicleFactory.createVehicleType(Id.create(VEHICLE_TYPE_ID, VehicleType.class));

		VehicleUtils.setHbefaTechnology(vehicleType_eTruck.getEngineInformation(), "electricity");
		VehicleUtils.setEnergyCapacity(vehicleType_eTruck.getEngineInformation(), CAR_BATTERY_CAPACITY_kWh);
		VehicleUtils.setEnergyConsumptionKWhPerMeter(vehicleType_eTruck.getEngineInformation(),0.0012);
		vehicleType_eTruck.setDescription("600kWh_Long_Haul_eTruck");
		ElectricVehicleSpecifications.setChargerTypes(vehicleType_eTruck.getEngineInformation(), Arrays.asList( TRUCK_CHARGERS_TYPE, "default" ));
		scenario.getVehicles().addVehicleType(vehicleType_eTruck);

		Random rn = new Random();
		for (Person person : scenario.getPopulation().getPersons().values()) {
//			if (rn.nextInt(10) != 0){
//				// first attempt 10% of trucks are electric
//				continue;
//			}
			Vehicle truckVehicle = vehicleFactory.createVehicle(Id.createVehicleId(person.getId()),
					vehicleType_eTruck);
			ElectricVehicleSpecifications.setInitialSoc(truckVehicle, CAR_INITIAL_SOC);
			scenario.getVehicles().addVehicle(truckVehicle);

		}
	MatsimVehicleWriter vehicleWriter = new MatsimVehicleWriter( scenario.getVehicles() );
	vehicleWriter.writeFile(eVehicleFile);


	}
}
