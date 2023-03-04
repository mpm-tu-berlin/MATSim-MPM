package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.contrib.ev.infrastructure.ChargerSpecification;
import org.matsim.contrib.ev.infrastructure.ChargerWriter;
import org.matsim.contrib.ev.infrastructure.ImmutableChargerSpecification;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.network.NetworkUtils;

import java.io.BufferedReader;
import java.io.FileReader;
import java.util.ArrayList;

import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;


public class 	RunChargersGenerator {
	private final static String CONFIG_FILE = "./input/TestETruckTraffic/config.xml";
	private final static String DEFAULT_PATH = "./input/TestETruckTraffic/";
	private final static String CHARGERS_CONFIG = "C:\\Users\\josef\\tubCloud\\HoLa - Data\\00_PreProcessing_Data\\chargersConfiguration.csv";


	public static void main(String[] args) throws Exception {
		Config config = ConfigUtils.loadConfig(CONFIG_FILE, new EvConfigGroup());

		String networkFile = DEFAULT_PATH + config.network().getInputFile();
		String chargersFile = DEFAULT_PATH + config.getModules().get("ev").getParams().get("chargersFile");

		run(CHARGERS_CONFIG, chargersFile,  networkFile);

	}

	public static void run(String chargers_config, String chargers_file, String networkFile) throws Exception{

		ArrayList<ChargerSpecification> chargers = new ArrayList<>();
		Network network = NetworkUtils.readNetwork(networkFile);


		BufferedReader br = new BufferedReader(
				new FileReader(chargers_config)
		);

		// read chargers configuration file
		CSVParser parser = CSVFormat.DEFAULT.withHeader().parse(br);

		for(CSVRecord record : parser) {

			// read and set coords
			Coord coord = new Coord(
					Double.parseDouble(record.get("LON")),
					Double.parseDouble(record.get("LAT"))
			);

			CoordinateTransformation ct = TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");
			coord = ct.transform(coord);

			// read and set ID
			Id id = Id.create(
					Integer.parseInt(record.get("CHARGER_ID")),
					Charger.class
			);



			// search nearest Link
			Link link  = NetworkUtils.getNearestLink(network, coord);

			ImmutableChargerSpecification.Builder builder = ImmutableChargerSpecification.newBuilder();
			ImmutableChargerSpecification charger = builder.id(Id.create("TruckChargers", Charger.class))
					.id(id)
					.linkId(link.getId())
					.plugPower(Integer.parseInt(record.get("PLUG_POWER"))*1000) // kW -> W
					.plugCount(Integer.parseInt(record.get("PLUG_COUNT")))
					.chargerType(record.get("CHARGER_TYPE"))
					.build();


			chargers.add(charger);
		}
		new ChargerWriter(chargers.stream()).write(chargers_file);

	}
}
