package org.matsim.contrib.ev.eTruckTraffic;

import org.matsim.api.core.v01.Scenario;
import org.matsim.contrib.ev.eTruckTraffic.lib.RunETruckPoplationGenerator;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy;
import org.matsim.core.scenario.ScenarioUtils;


import java.io.IOException;


public class RunTruckTrafficModelValidation {
	public static void main(String[] args) throws IOException {

		String rawPlansFile1 = "C:\\Users\\josef\\tubCloud\\HoLa - Data\\00_PreProcessing_Data\\Plans\\HoLaPlans_OD_";
		String rawPlansFile2 = "_minDist_0.csv";

		String inputPath = "./input/TruckTrafficValidation/";
		String outputPath = "./output/TruckTrafficValidation/";
		String plansFile = "/plans.xml.gz";
		String configFile = "/config.xml";
		String counterFile = "/counterStats.csv";

		String models[] = {"BMDV_Full", "ETIS2010_Full"};

		for (String model : models) {
			String raw = rawPlansFile1 + model.substring(0, model.length()-5) + rawPlansFile2;
			String plan = inputPath + model + plansFile;
			String config = inputPath + model + configFile;

			RunETruckPoplationGenerator.run(raw, plan, 0.20);
			System.out.println("#########" + config);
			new RunTruckTrafficModelValidation().run(config);
			/*TruckCountAtCountingStations.run(
					outputPath +  model,
					"/ITERS/it.0/0.events.xml.gz",
					counterFile
					);
		 }*/
		}
	}

	public void run(String configUrl) {
		Config config = ConfigUtils.loadConfig(configUrl);
		config.controler().setOverwriteFileSetting( OutputDirectoryHierarchy.OverwriteFileSetting.deleteDirectoryIfExists );
		Scenario scenario = ScenarioUtils.loadScenario(config) ;Controler controler = new Controler( scenario ) ;
		controler.run();

	}

}
