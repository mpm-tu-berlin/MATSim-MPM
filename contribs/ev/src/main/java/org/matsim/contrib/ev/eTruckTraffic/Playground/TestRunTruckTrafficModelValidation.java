package org.matsim.contrib.ev.eTruckTraffic.Playground;

import com.google.inject.Inject;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVPrinter;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.core.scenario.ScenarioUtils;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Map;


public class TestRunTruckTrafficModelValidation {
	@Inject
	CountAtCounterStationsCollector countAtCounterStationsCollector;
	public static void main(String[] args) throws IOException {

		new TestRunTruckTrafficModelValidation().run(
				"./input/TestETruckTraffic/config.xml");
	}

	public void run(String configUrl) throws IOException {
		Config config = ConfigUtils.loadConfig(configUrl);
		config.controler().setOverwriteFileSetting( OutputDirectoryHierarchy.OverwriteFileSetting.deleteDirectoryIfExists );
		Scenario scenario = ScenarioUtils.loadScenario(config) ;Controler controler = new Controler( scenario ) ;

		// Does not work

		controler.addOverridingModule(new AbstractModule() {
			@Override
			public void install() {
				installQSimModule(new AbstractQSimModule() {
					@Override
					protected void configureQSim() {
						bind(CountAtCounterStationsCollector.class).asEagerSingleton();
						addMobsimScopeEventHandlerBinding().to(CountAtCounterStationsCollector.class);
					}
				});
			}
		});

		controler.run();

		CSVPrinter csvPrinter1 = new CSVPrinter(Files.newBufferedWriter(Paths.get("counterStats.csv")), CSVFormat.DEFAULT.withDelimiter(';').
				withHeader("ChargerId", "Hour", "Count"));
		for (Map.Entry<Id, Map<Integer, Integer>> counterMap : countAtCounterStationsCollector.getLinkCounterMap().entrySet()) {
			for (var hourMap : counterMap.getValue().entrySet())
				csvPrinter1.printRecord(counterMap.getKey(), hourMap.getKey(), hourMap.getValue());
		}

	}

}
