package org.matsim.contrib.ev.eTruckTraffic.lib;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.contrib.ev.infrastructure.ChargerSpecification;
import org.matsim.contrib.ev.infrastructure.ChargerWriter;
import org.matsim.contrib.ev.infrastructure.ImmutableChargerSpecification;
import org.matsim.core.network.NetworkUtils;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.util.ArrayList;

import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;


public class RunChargersGenerator {

	public static void main(String[] args, Network network) throws Exception{

		ArrayList<ChargerSpecification> chargers = new ArrayList<>();

		// TODO: define Filename, bzw network übergeben
		// Network network = NetworkUtils.readNetwork("input\\EvTruckTraffic\\germany-europe-network.xml.gz");

		BufferedReader br = new BufferedReader(
				new FileReader("input\\EvTruckTraffic\\raw_data\\chargersConfiguration.csv")
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
					.plugPower(Integer.parseInt(record.get("PLUG_POWER")))
					.plugCount(Integer.parseInt(record.get("PLUG_COUNT")))
					.chargerType(record.get("CHARGER_TYPE"))
					.build();


			chargers.add(charger);
		}
		new ChargerWriter(chargers.stream()).write("input\\EvTruckTraffic\\eTruckChargers.xml");

	}
}
