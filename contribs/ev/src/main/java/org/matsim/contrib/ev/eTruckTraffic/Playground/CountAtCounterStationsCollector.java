package org.matsim.contrib.ev.eTruckTraffic.Playground;

import com.google.inject.Inject;
import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.events.LinkLeaveEvent;
import org.matsim.api.core.v01.events.handler.LinkLeaveEventHandler;
import org.matsim.api.core.v01.network.Network;
import org.matsim.core.events.MobsimScopeEventHandler;
import org.matsim.core.network.NetworkUtils;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;

import java.io.*;
import java.nio.charset.Charset;
import java.util.HashMap;
import java.util.Map;

public class CountAtCounterStationsCollector implements LinkLeaveEventHandler, MobsimScopeEventHandler {
	private static final String COUNTER_FILE_PATH =
			"./contribs/ev/src/main/java/org/matsim/contrib/ev/eTruckTraffic/Playground/ExportCounterHPerWeek.csv";
	private static String separator = ",";
	private static Charset charset = Charset.forName("UTF-8");
	private final Network network;
	@Inject
	public CountAtCounterStationsCollector(Network network){
		this.network = network;
	}
	private final Map<Id,  Map<Integer, Integer>> LINK_COUNT_MAP = get_link_counter_map(COUNTER_FILE_PATH);
	public Map<Id,  Map<Integer, Integer>> getLinkCounterMap(){return LINK_COUNT_MAP;}
	@Override
	public void handleEvent(LinkLeaveEvent event) {
		if (LINK_COUNT_MAP.containsKey(event.getLinkId())){
			int hour = ((int) (event.getTime() / 3600));
			int counter = LINK_COUNT_MAP.get(event.getLinkId()).get(hour)+1 ;
			LINK_COUNT_MAP.get(event.getLinkId()).put(hour, counter);
		}
	}

	public Map get_link_counter_map(String filePath) {
		Map<Id, Map<Integer, Integer>> countersLink_count_map = new HashMap<>();

		FileInputStream fis = null;
		InputStreamReader isr = null;
		BufferedReader br = null;

		CoordinateTransformation ct = TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");

		try {
			fis = new FileInputStream(filePath);
			isr = new InputStreamReader(fis, charset);
			br = new BufferedReader(isr);

			// skip first Line
			br.readLine();
			String line;

			while ((line = br.readLine()) != null) {
				String[] cols = line.split(separator);
				Coord coord = ct.transform(new Coord(parseDouble(cols[6]), parseDouble(cols[7])));
				Map<Integer, Integer> tmp = new HashMap<>();
				for (int hour=0; hour<24; hour++){
					tmp.put(hour, 0);
				}
				countersLink_count_map.put(NetworkUtils.getNearestLink(this.network, coord).getId(),tmp);
			}
		} catch (FileNotFoundException e) {
			throw new RuntimeException(e);
		} catch (IOException e) {
			throw new RuntimeException(e);
		}
		return countersLink_count_map;
	}
	private static double parseDouble(String string) {
		if (string == null) return 0.0;
		else if (string.trim().isEmpty()) return 0.0;
		else return Double.valueOf(string);
	}
}
