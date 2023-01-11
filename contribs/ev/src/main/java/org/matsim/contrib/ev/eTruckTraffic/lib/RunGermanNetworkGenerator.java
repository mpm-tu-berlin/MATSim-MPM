package org.matsim.contrib.ev.eTruckTraffic.lib;


import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.network.Network;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.network.algorithms.NetworkCleaner;
import org.matsim.core.network.io.NetworkWriter;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;
import org.matsim.core.utils.io.OsmNetworkReader;


public class RunGermanNetworkGenerator {

	public static void main(String[] args) {

		// The input file name.
		String osm =
				"C:/Users/josef/Desktop/Uni/14.SemesterFINALE/Masterarbeit/" +
						"HoLa/00_Data/OSM/Deutschland/germany_allroads.osm";

		CoordinateTransformation ct =
				TransformationFactory.getCoordinateTransformation(TransformationFactory.WGS84, "EPSG:25832");

		Config config = ConfigUtils.createConfig();
		Scenario scenario = ScenarioUtils.createScenario(config);

		Network network = scenario.getNetwork();

		OsmNetworkReader onr = new OsmNetworkReader(network,ct);

		onr.setHighwayDefaults(1, "motorway",      2, 80.0/3.6, 1.0, 2000, true);
		onr.setHighwayDefaults(1, "motorway_link", 1,  40.0/3.6, 1.0, 1500, true);
		onr.setHighwayDefaults(2, "trunk",         1,  80.0/3.6, 1.0, 2000);
		onr.setHighwayDefaults(2, "trunk_link",    1,  50.0/3.6, 1.0, 1500);

		onr.setHighwayDefaults(3, "primary",       1,  80.0/3.6, 1.0, 1000);
		onr.setHighwayDefaults(3, "primary_link",  1,  60.0/3.6, 1.0, 1000);

		onr.parse(osm);

		/*
		 * Clean the Network. Cleaning means removing disconnected components, so that afterwards there is a route from every link
		 * to every other link. This may not be the case in the initial network converted from OpenStreetMap.
		 */
		new NetworkCleaner().run(network);

		/*
		 * Write the Network to a MATSim network file.
		 */
		new NetworkWriter(network).write("./input/EvTruckTraffic/germany_allroads.xml");

	}

}
