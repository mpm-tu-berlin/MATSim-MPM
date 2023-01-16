package org.matsim.contrib.ev.eTruckTraffic.lib;


import org.matsim.api.core.v01.TransportMode;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.network.NetworkWriter;
import org.matsim.contrib.osm.networkReader.LinkProperties;
import org.matsim.contrib.osm.networkReader.SupersonicOsmNetworkReader;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;

import java.util.Collections;



public class RunETruckNetworkGenerator {

	private static final String INPUT_OSM = "C:/Users/josef/tubCloud/HoLa - Data/OSM/berlin-latest.osm.pbf";
	private static final String OUTPUT_XML_GZ = "C:/Users/josef/tubCloud/HoLa - Data/OSM/berlin_test.xml.gz";


	private static final CoordinateTransformation coordinateTransformation =
			TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");

	public static void main(String[] args) {
		run(INPUT_OSM, OUTPUT_XML_GZ);

	}

	public static void run(String inputPath, String outputPath) {

		Network network = new SupersonicOsmNetworkReader.Builder()
				.setCoordinateTransformation(coordinateTransformation)
				.setIncludeLinkAtCoordWithHierarchy((coord, hierachyLevel) -> hierachyLevel <= LinkProperties.LEVEL_PRIMARY)
				.setPreserveNodeWithId(id -> id == LinkProperties.LEVEL_SECONDARY)
				.addOverridingLinkProperties(
						"motorway",new LinkProperties(LinkProperties.LEVEL_MOTORWAY,
						2, 80 / 3.6, 2000, true)
				)
				.setAfterLinkCreated((link, osmTags, isReverse) -> link.setAllowedModes(Collections.singleton(TransportMode.car)))
				.build()
				.read(inputPath);

		new NetworkWriter(network).write(outputPath);
	}
}
