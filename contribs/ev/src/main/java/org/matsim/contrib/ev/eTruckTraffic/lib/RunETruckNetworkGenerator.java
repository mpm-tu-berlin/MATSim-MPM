package org.matsim.contrib.ev.eTruckTraffic.lib;


import org.matsim.api.core.v01.TransportMode;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.network.NetworkWriter;
import org.matsim.contrib.osm.networkReader.LinkProperties;
import org.matsim.contrib.osm.networkReader.SupersonicOsmNetworkReader;
import org.matsim.core.network.algorithms.NetworkCleaner;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;

import java.util.Collections;



public class RunETruckNetworkGenerator {

	private static final String INPUT_OSM = "C:/Users/josef/tubCloud/HoLa - Data/OSM/germany-latest.osm.pbf";
	private static final String OUTPUT_XML_GZ = "./input/TestETruckTraffic/german_motorway_network.xml.gz";


	private static final CoordinateTransformation coordinateTransformation =
			TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");

	public static void main(String[] args) {
		run(INPUT_OSM, OUTPUT_XML_GZ, LinkProperties.LEVEL_TRUNK);

	}

	public static void run(String inputPath, String outputPath, int linkProp) {

		Network network = new SupersonicOsmNetworkReader.Builder()
				.setCoordinateTransformation(coordinateTransformation)
				.setIncludeLinkAtCoordWithHierarchy((coord, hierachyLevel) -> hierachyLevel <= linkProp)
				.setPreserveNodeWithId(id -> id == LinkProperties.LEVEL_SECONDARY)
				.addOverridingLinkProperties(
						"motorway",new LinkProperties(LinkProperties.LEVEL_MOTORWAY,
						2, 80 / 3.6, 2000, true)
				)
				.addOverridingLinkProperties(
						"trunk",new LinkProperties(LinkProperties.LEVEL_TRUNK, 1, 60 / 3.6, 2000, false)
				)
				.addOverridingLinkProperties(
						"primary",new LinkProperties(LinkProperties.LEVEL_PRIMARY, 1, 60 / 3.6, 1500, false)
				)
				.setAfterLinkCreated((link, osmTags, isReverse) -> link.setAllowedModes(Collections.singleton(TransportMode.car)))
				.build()
				.read(inputPath);
		new NetworkCleaner().run(network);
		new NetworkWriter(network).write(outputPath);
	}
}
