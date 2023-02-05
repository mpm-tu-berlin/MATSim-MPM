package org.matsim.contrib.ev.eTruckTraffic.EventHandler;

import org.apache.commons.csv.CSVFormat;
import org.matsim.core.events.EventsUtils;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.Paths;

public class TruckCountAtCountingStations {

	private static final String EVENT_FILE_PATH =
			"C:\\Users\\josef\\Desktop\\matsim-libs-HoLa\\output\\TestETruckTraffic\\ITERS\\it.0\\0.events.xml.gz";

	public static void main(String[] args) {

		var handler = new LinkCounterEventHandler();
		var manager = EventsUtils.createEventsManager();
		manager.addHandler(handler);

		EventsUtils.readEvents(manager, EVENT_FILE_PATH);

		var counterStats = handler.getCounterList();
		try (
				var writer = Files.newBufferedWriter(Paths.get("test.csv"));
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


