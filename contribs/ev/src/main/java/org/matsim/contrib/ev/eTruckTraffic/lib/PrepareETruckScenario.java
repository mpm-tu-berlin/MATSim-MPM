package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.network.Network;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.network.NetworkUtils;
import org.matsim.core.population.io.PopulationWriter;
import org.matsim.core.scenario.ScenarioUtils;

import java.nio.file.Path;
import java.nio.file.Paths;

public class PrepareETruckScenario {

	final static String CHARGERS_CONFIG = "./input/EvTruckTraffic/raw_data/chargersConfiguration.csv";
	final static String RAW_PLANS = "C:/Users/josef/tubCloud/HoLa - Data/" +
			"00_PreProcessing_Data/Plans/TestSchedule_1pct_minDist300.csv";
	final static String CONFIG_FILE = "./input/EvTruckTraffic/config.xml";
	final static String DEFAULT_PATH = "./input/EvTruckTraffic/";

	public static void main(String[] args) throws Exception {
		Config config = ConfigUtils.loadConfig(CONFIG_FILE, new EvConfigGroup());
		String networkFile = DEFAULT_PATH + config.network().getInputFile();
		String plansFile = DEFAULT_PATH + config.plans().getInputFile();
		String vehicleFile = DEFAULT_PATH + config.vehicles().getVehiclesFile();
		String chargersFile = DEFAULT_PATH + config.getModules().get("ev").getParams().get("chargersFile");

		new RunETruckPoplationGenerator().run(RAW_PLANS, plansFile);
		new RunETruckGenerator().run(config, vehicleFile);
		new RunChargersGenerator().run(CHARGERS_CONFIG, chargersFile,  networkFile);
	}
}

