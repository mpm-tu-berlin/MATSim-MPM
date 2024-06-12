package org.matsim.contrib.ev.eTruckTraffic;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.example.RunEvExample;
import org.matsim.contrib.ev.routing.EvNetworkRoutingProvider;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy;
import org.matsim.core.scenario.ScenarioUtils;

import java.io.IOException;

public class RunETruckTraffic {
	static final String DEFAULT_CONFIG_FILE = "input/EVS/1.0pctETrucks_1Iteration_unlimited/config.xml";
	private static final Logger log = LogManager.getLogger(RunEvExample.class);

	public static void main(String[] args) throws IOException {
/*		final URL configUrl;
		if (args.length > 0) {
			log.info("Starting simulation run with the following arguments:");
			configUrl = new URL(args[0]);
			log.info("config URL: " + configUrl);
		} else {
			File localConfigFile = new File(DEFAULT_CONFIG_FILE);
			if (localConfigFile.exists()) {
				log.info("Starting simulation run with the local example config file");
				configUrl = localConfigFile.toURI().toURL();
			} else {
				log.info("Starting simulation run with the example config file from GitHub repository");
				configUrl = new URL("https://raw.githubusercontent.com/matsim-org/matsim/master/contribs/ev/"
						+ DEFAULT_CONFIG_FILE);
			}
		}*/

		String config_paths[] = {
				"input/BETScenarios/1.0pctETrucks_1Iteration_unlimited/config.xml"
				//"input/BETScenarios/5.0pctETrucks_1Iteration_unlimited/config.xml",
				//"input/BETScenarios/10.0pctETrucks_1Iteration_unlimited/config.xml",
				//"input/BETScenarios/15.0pctETrucks_1Iteration_unlimited/config.xml",
				//"input/BETScenarios/20.0pctETrucks_1Iteration_unlimited/config.xml",
		};
		for (String config_path: config_paths){
			new RunETruckTraffic().run(config_path);
		}
	}

	public void run(String configUrl) {
		Config config = ConfigUtils.loadConfig(configUrl, new EvConfigGroup());
		config.controller().setOverwriteFileSetting(OutputDirectoryHierarchy.OverwriteFileSetting.deleteDirectoryIfExists);
		Scenario scenario = ScenarioUtils.loadScenario(config);
		Controler controler = new Controler(scenario);

		controler.addOverridingModule(new AbstractModule(){

			@Override public void install(){
				install( new EvModule() );

				addRoutingModuleBinding(TransportMode.car).toProvider(new EvNetworkRoutingProvider(TransportMode.car));
			}
		} );

		controler.run();

	}
}
