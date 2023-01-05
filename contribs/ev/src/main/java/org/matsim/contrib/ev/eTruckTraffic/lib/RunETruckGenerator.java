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
import org.matsim.core.utils.io.IOUtils;
import org.matsim.vehicles.*;

import java.util.*;

public class RunETruckGenerator {

	static final double CAR_BATTERY_CAPACITY_kWh = 600.;
	static final double CAR_INITIAL_SOC = 1;
	static final String TRUCK_CHARGERS_TYPE = "DC";

	static final String CONFIG_PATH = "input/EvTruckTraffic/config.xml";
	static final String ETRUCK_FILE_PATH = "input/EvTruckTraffic/eTrucks.xml";

	public static void main(String[] args) {

		EvConfigGroup evConfigGroup = new EvConfigGroup();
		// using the Population of the EV example for comparison reason
		String pathToConfig = CONFIG_PATH;
		Config config = ConfigUtils.loadConfig( IOUtils.getFileUrl( pathToConfig ), evConfigGroup);
		config.qsim().setVehiclesSource(QSimConfigGroup.VehiclesSource.fromVehiclesData);

		Scenario scenario = ScenarioUtils.loadScenario(config);

		VehiclesFactory vehicleFactory = scenario.getVehicles().getFactory();

		VehicleType carVehicleType = vehicleFactory.createVehicleType(Id.create("600kWh_Long_Haul_Truck",
				VehicleType.class));
		VehicleUtils.setHbefaTechnology(carVehicleType.getEngineInformation(), "electricity");
		VehicleUtils.setEnergyCapacity(carVehicleType.getEngineInformation(), CAR_BATTERY_CAPACITY_kWh);
		ElectricVehicleSpecifications.setChargerTypes(carVehicleType.getEngineInformation(), Arrays.asList( TRUCK_CHARGERS_TYPE, "default" ));
		scenario.getVehicles().addVehicleType(carVehicleType);

		Random rn = new Random();
		for (Person person : scenario.getPopulation().getPersons().values()) {
			if (rn.nextInt(10) != 0){
				// first attempt 10% of trucks are electric
				continue;
			}

			Vehicle carVehicle = vehicleFactory.createVehicle(VehicleUtils.createVehicleId(person, TransportMode.truck),
					carVehicleType);
			ElectricVehicleSpecifications.setInitialSoc(carVehicle, CAR_INITIAL_SOC);
			scenario.getVehicles().addVehicle(carVehicle);

			MatsimVehicleWriter vehicleWriter = new MatsimVehicleWriter( scenario.getVehicles() );
			vehicleWriter.writeFile(ETRUCK_FILE_PATH);
		}
	}
}
