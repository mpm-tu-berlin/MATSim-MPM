package org.matsim.contrib.ev.eTruckTraffic.EventHandler;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.events.LinkLeaveEvent;
import org.matsim.api.core.v01.events.handler.LinkLeaveEventHandler;
import org.matsim.api.core.v01.network.Network;
import org.matsim.core.network.NetworkUtils;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;


import java.io.*;
import java.nio.charset.Charset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class LinkCounterEventHandler implements LinkLeaveEventHandler {
	private static final String COUNTER_FILE_PATH = "C:\\Users\\josef\\tubCloud\\HoLa - Data\\ExportCounterHPerWeek.csv";
	private static final Network NETWORK = NetworkUtils.readNetwork(
			"C:\\Users\\josef\\Desktop\\matsim-libs-HoLa\\input\\ETruckTraffic\\german_etruck_network.xml.gz");
	private static String separator = ",";
	private static Charset charset = Charset.forName("UTF-8");
	static List COUNTER_LIST = read_counter_stat_from_csv(COUNTER_FILE_PATH, NETWORK);
	static List LINKS = get_link_list(COUNTER_LIST);

	List getCounterList(){
		return COUNTER_LIST;
	}
	@Override
	public void handleEvent(LinkLeaveEvent event) {
		if (LINKS.contains(event.getLinkId())){
			int idx = LINKS.indexOf(event.getLinkId());
			int hour = secToHour(event.getTime());
			((BASTCounterEntry) COUNTER_LIST.get(idx+hour)).MODEL_COUNT ++;
		}
	}
	private String timeToString(double time) {
		return Integer.toString((int) (time / 3600));
	}
	public static List get_link_list(List counter_stats){
		List links = new ArrayList();
		for (int i=0; i<counter_stats.size(); i++ ){
			links.add(((BASTCounterEntry) counter_stats.get(i)).LINK_ID);
		}
		return links;
	}
	public static List<BASTCounterEntry> read_counter_stat_from_csv(String filePath, Network network){
		List<BASTCounterEntry> counterEntries = new ArrayList<>();

		FileInputStream fis = null;
		InputStreamReader isr = null;
		BufferedReader br = null;

		CoordinateTransformation ct = TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");

		try{
			fis = new FileInputStream(filePath);
			isr = new InputStreamReader(fis, charset);
			br = new BufferedReader(isr);

			// skip first Line
			br.readLine();
			String line;

			while ((line = br.readLine()) != null) {
				BASTCounterEntry entry = new BASTCounterEntry();
				String[] cols = line.split(separator);
				// read important data from file
				entry.COUNTER_ID = parseInteger(cols[1]);
				entry.STREET_CLASS = cols[2];
				entry.HOUR = parseInteger(cols[3]);
				entry.BAST_COUNT_1 = parseInteger(cols[4]);
				entry.BAST_COUNT_2 = parseInteger(cols[5]);
				entry.LON = parseDouble(cols[6]);
				entry.LAT = parseDouble(cols[7]);
				// get nearest link from lat lot
				Coord coord = ct.transform(new Coord(entry.LON, entry.LAT));
				entry.LINK_ID = NetworkUtils.getNearestLink(network, coord).getId();

				counterEntries.add(entry);
			}
		} catch (FileNotFoundException e) {
			throw new RuntimeException(e);
		} catch (IOException e) {
			throw new RuntimeException(e);
		}
		return counterEntries;
	}
	static Integer secToHour(double sec ){
		return ((int) (sec / 3600));
	}
	private static int parseInteger(String string)
	{
		if (string == null) return 0;
		else if (string.trim().isEmpty()) return 0;
		else return Integer.valueOf(string);
	}
	private static double parseDouble(String string) {
		if (string == null) return 0.0;
		else if (string.trim().isEmpty()) return 0.0;
		else return Double.valueOf(string);
	}
	public static class BASTCounterEntry {
		int COUNTER_ID;
		String STREET_CLASS;
		int HOUR;
		int BAST_COUNT_1;
		int BAST_COUNT_2;
		double LON;
		double LAT;
		Id LINK_ID;
		int MODEL_COUNT;
	}
}
