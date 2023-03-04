package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.osm.networkReader.LinkProperties;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;

import java.nio.file.Path;

public class PrepareETruckScenario {
	private static final String OSM_PDF_FILE_PATH =
			"C:/Users/josef/tubCloud/HoLa - Data/OSM/germany-latest.osm.pbf";
	private final static String CHARGERS_CONFIG =
			"C:/Users/josef/tubCloud/HoLa - Data/00_PreProcessing_Data/chargersConfiguration.csv";
	private final static String RAW_PLANS =
			"C:/Users/josef/tubCloud/HoLa - Data/00_PreProcessing_Data/Plans/HoLaPlans_OD_BMDV_minDist_300.csv";

	private final static String DEFAULT_PATH = "./input/ETruckTraffic/";
	private final static String CONFIG_FILE = "config.xml";
	private final static String NETWORK_FILE = "german_etruck_network.xml.gz";
	private final static double SHARE_OF_EV_IN_TOTAL_PLANS[] = {0.01, 0.05, 0.1, 0.15, 0.2}; //,
	private final static String SUB_FOLDER = "pctETrucks/";


	public static void main(String[] args) throws Exception {
		String networkFile = DEFAULT_PATH + NETWORK_FILE;
		System.out.println("######## Start: Network Generator ########");
		// new RunETruckNetworkGenerator().run(OSM_PDF_FILE_PATH, networkFile, LinkProperties.LEVEL_PRIMARY);

		for (double share : SHARE_OF_EV_IN_TOTAL_PLANS) {
			String configFile = sub_path_parser(CONFIG_FILE, share);
			Config config = ConfigUtils.loadConfig(configFile, new EvConfigGroup());


			String chargersFile = sub_path_parser(config.getModules().get("ev").getParams().get("chargersFile"), share);
			String plansFile = sub_path_parser(Path.of(config.plans().getInputFile()).getFileName().toString(), share);
			String vehicleFile = sub_path_parser(Path.of(config.vehicles().getVehiclesFile()).getFileName().toString(), share);


			System.out.println("######## Start: Chargers Generator ########");
			// new RunChargersGenerator().run(CHARGERS_CONFIG, chargersFile,  networkFile);
			System.out.println("######## Start: Plans Generator ########");
			new RunETruckPoplationGenerator().run(RAW_PLANS, plansFile, share);
			System.out.println("######## Start: ETruck Generator ########");
			new RunETruckGenerator().run(config, vehicleFile);
		}
	}

	private static String sub_path_parser(String file, double share){
		return DEFAULT_PATH + 100*share + SUB_FOLDER + file;
	}
}

