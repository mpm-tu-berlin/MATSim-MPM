package org.matsim.contrib.ev.eTruckTraffic.EventHandler;

import org.apache.commons.csv.CSVFormat;
import org.matsim.core.events.EventsUtils;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.Paths;

public class TruckCountAtCountingStations {

	private static final String PATH =
			"./";
	private static final String EVENT_FILE = "output_events.xml.gz";
	private static final String OUTPUT_FILE = "counter_distribution.csv";

	public static void main(String[] args) {

		run(PATH, EVENT_FILE, OUTPUT_FILE);
	}
	public static void run(String path, String event_file, String output_file){
		var handler = new LinkCounterEventHandler();
		// var manager = EventsUtils.createEventsManager();
		var manager = EventsUtils.createParallelEventsManager();
		manager.addHandler(handler);
		manager.initProcessing();
		EventsUtils.readEvents(manager, path + event_file);
		manager.finishProcessing();

		var counterStats = handler.getCounterList();
		try (
				var writer = Files.newBufferedWriter(Paths.get(path + output_file));
				var printer = CSVFormat.DEFAULT.withDelimiter(';').withHeader("COUNTER_ID", "STREET_CLASS", "HOUR",
						"BAST_COUNT_1", "BAST_COUNT_2", "LON", "LAT", "LINK_ID", "MODEL_COUNT").print(writer)){

			for (var counterStat : counterStats) {
				printer.printRecord(
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).COUNTER_ID,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).STREET_CLASS,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).HOUR,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).BAST_COUNT_1,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).BAST_COUNT_2,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).LON,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).LAT,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).LINK_ID,
						((LinkCounterEventHandler.BASTCounterEntry) counterStat).MODEL_COUNT
				);
			}

		} catch (IOException e) {
			e.printStackTrace();
		}



	}
}


