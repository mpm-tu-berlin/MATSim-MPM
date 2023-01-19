package org.matsim.contrib.ev.eTruckTraffic;

import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.charging.VehicleChargingHandler;
import org.matsim.contrib.ev.routing.EvNetworkRoutingProvider;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.core.scenario.ScenarioUtils;

public class TestETruckTraffic {
	static final String DEFAULT_CONFIG_FILE = "./input/TestETruckTraffic/config.xml";

	public static void main(String[] args) {

		Config config = ConfigUtils.loadConfig(DEFAULT_CONFIG_FILE, new EvConfigGroup());
		config.controler()
				.setOverwriteFileSetting(OutputDirectoryHierarchy.OverwriteFileSetting.deleteDirectoryIfExists);
		Scenario scenario = ScenarioUtils.loadScenario(config);
		Controler controler = new Controler(scenario);
		controler.addOverridingModule(new EvModule());
		controler.addOverridingModule(new AbstractModule() {
			@Override
			public void install() {
				addRoutingModuleBinding(TransportMode.car).toProvider(new EvNetworkRoutingProvider(TransportMode.car));
				installQSimModule(new AbstractQSimModule() {
					@Override
					protected void configureQSim() {
						bind(VehicleChargingHandler.class).asEagerSingleton();
						addMobsimScopeEventHandlerBinding().to(VehicleChargingHandler.class);
					}
				});
			}
		});
		controler.configureQSimComponents(components -> components.addNamedComponent(EvModule.EV_COMPONENT));

		controler.run();
	}

}
