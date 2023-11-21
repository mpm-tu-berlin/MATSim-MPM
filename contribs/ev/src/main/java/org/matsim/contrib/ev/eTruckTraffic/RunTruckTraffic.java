package org.matsim.contrib.ev.eTruckTraffic;

import org.matsim.api.core.v01.Scenario;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy;
import org.matsim.core.scenario.ScenarioUtils;

import java.io.IOException;

public class RunTruckTraffic {
	public static void main(String[] args) throws IOException {

		String config_paths[] = {
				"input/ETruckTraffic/1.0pctETrucks/config_NoElec.xml",
				"input/ETruckTraffic/5.0pctETrucks/config_NoElec.xml",
				"input/ETruckTraffic/10.0pctETrucks/config_NoElec.xml",
				"input/ETruckTraffic/15.0pctETrucks/config_NoElec.xml",
				"input/ETruckTraffic/20.0pctETrucks/config_NoElec.xml",
		};
		for (String config_path: config_paths){
			new RunTruckTraffic().run(config_path);
		}
	}

	public void run(String configUrl) {
		Config config = ConfigUtils.loadConfig(configUrl);
		config.controler().setOverwriteFileSetting( OutputDirectoryHierarchy.OverwriteFileSetting.deleteDirectoryIfExists );
		Scenario scenario = ScenarioUtils.loadScenario(config) ;
		Controler controler = new Controler( scenario ) ;
		controler.run();
	}
}
