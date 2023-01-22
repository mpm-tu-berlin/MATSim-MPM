package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.osm.networkReader.LinkProperties;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;

public class PrepareETruckScenario {
	private static final String OSM_PDF_FILE_PATH = "C:/Users/josef/tubCloud/HoLa - Data/OSM/germany-latest.osm.pbf";
	private final static String CHARGERS_CONFIG = "./input/ETruckTraffic/raw_data/chargersConfiguration.csv";
	private final static String RAW_PLANS = "C:/Users/josef/tubCloud/HoLa - Data/" +
			"00_PreProcessing_Data/Plans/HoLaPlans_OD_BMDV_minDist_300.csv";
	private final static String CONFIG_FILE = "./input/ETruckTraffic/config.xml";
	private final static String DEFAULT_PATH = "./input/ETruckTraffic/";
	private final static double SHARE_OF_EV_IN_TOTAL_PLANS = 0.05;


	public static void main(String[] args) throws Exception {
		Config config = ConfigUtils.loadConfig(CONFIG_FILE, new EvConfigGroup());
		String networkFile = DEFAULT_PATH + config.network().getInputFile();
		String plansFile = DEFAULT_PATH + config.plans().getInputFile();
		String vehicleFile = DEFAULT_PATH + config.vehicles().getVehiclesFile();
		String chargersFile = DEFAULT_PATH + config.getModules().get("ev").getParams().get("chargersFile");

		System.out.println("######## Start: Network Generator ########");
		//new RunETruckNetworkGenerator().run(OSM_PDF_FILE_PATH, networkFile, LinkProperties.LEVEL_PRIMARY);
		System.out.println("######## Start: Chargers Generator ########");
		//new RunChargersGenerator().run(CHARGERS_CONFIG, chargersFile,  networkFile);
		System.out.println("######## Start: Plans Generator ########");
		new RunETruckPoplationGenerator().run(RAW_PLANS, plansFile, SHARE_OF_EV_IN_TOTAL_PLANS);
		System.out.println("######## Start: ETruck Generator ########");
		new RunETruckGenerator().run(config, vehicleFile);

	}
}

