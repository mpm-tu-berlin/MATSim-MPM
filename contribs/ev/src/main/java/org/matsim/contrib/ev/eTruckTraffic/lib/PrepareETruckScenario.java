package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;

public class PrepareETruckScenario {
	private static final String OSM_PDF_FILE_PATH = "C:/Users/josef/tubCloud/HoLa - Data/OSM/germany-latest.osm.pbf";
	private final static String CHARGERS_CONFIG = "./input/EvTruckTraffic/raw_data/chargersConfiguration.csv";
	private final static String RAW_PLANS = "C:/Users/josef/tubCloud/HoLa - Data/" +
			"00_PreProcessing_Data/Plans/TestSchedule_1pct_minDist300.csv";
	private final static String CONFIG_FILE = "./input/EvTruckTraffic/config.xml";
	private final static String DEFAULT_PATH = "./input/EvTruckTraffic/";

	public static void main(String[] args) throws Exception {
		Config config = ConfigUtils.loadConfig(CONFIG_FILE, new EvConfigGroup());
		String networkFile = DEFAULT_PATH + config.network().getInputFile();
		String plansFile = DEFAULT_PATH + config.plans().getInputFile();
		String vehicleFile = DEFAULT_PATH + config.vehicles().getVehiclesFile();
		String chargersFile = DEFAULT_PATH + config.getModules().get("ev").getParams().get("chargersFile");

		new RunETruckNetworkGenerator().run(OSM_PDF_FILE_PATH, networkFile);
		new RunETruckPoplationGenerator().run(RAW_PLANS, plansFile);
		new RunETruckGenerator().run(config, vehicleFile);
		new RunChargersGenerator().run(CHARGERS_CONFIG, chargersFile,  networkFile);
	}
}

