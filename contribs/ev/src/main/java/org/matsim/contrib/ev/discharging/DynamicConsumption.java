

package org.matsim.contrib.ev.discharging;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Node;
import org.matsim.api.core.v01.Coord;
import org.matsim.contrib.ev.Germany3DParser;

import java.util.Map;



public final class DynamicConsumption implements DriveEnergyConsumption {

	private static final double g = 9.81; // g [m/s^2]
	private static final double m = 27500; // vehicle mass at half-load [kg]
	private static final double eff = 0.5; // drive train efficiency [-]
	private static final double cr = 0.0065; // rolling drag coefficient[-]
	private static final double ca = 0.6; // aerodynamic drag coefficient [kg/m]
	private static final double rho = 1.225; // air density [kg/m3] at 15ºC, atmospheric pressure
	private static final double a = 10; //area [m2]
	private static final double eff_reg = 0.5; //efficiency of energy recuperation through regenerative breaking
	private static final double battery_power = 150; // [kW]






	@Override
	public double calcEnergyConsumption(Link link, double travelTime, double linkEnterTime) {

		if (travelTime == 0) {
			return 0;
		}

		double avgSpeed = link.getLength() / travelTime;
		double slope = calcSlope(link);

		double power = calcPower(avgSpeed, slope);

		if (power < 0){

			if (power > (battery_power/eff_reg)*-1){     //if the extra power is lower than 300 kW,
				power = power * eff_reg;			     //half is recuperated
			}
			else{
				power = battery_power;					//if extra power is 300 kW or more, 150 kW is recuperated
			}
		}
		return power * travelTime;
	}

	private static double calcPower(double v, double phi) {

		return v * ( m * g * Math.sin(phi) + cr * m * g * Math.cos(phi) + ca * v * v * a * rho * 0.5 ) / eff;
	}



	public static double calcSlope(Link link) {

		Node fromNode = link.getFromNode();
		Node toNode = link.getToNode();

		Coord fromCoord = getCoordForNode(fromNode.getId());
		Coord toCoord = getCoordForNode(toNode.getId());


		double deltaZ = toCoord.getZ() - fromCoord.getZ();
		double horizontalDistance = link.getLength();

		return Math.atan(deltaZ / horizontalDistance);
	}


	static Map<Id<Node>, Coord> nodeIdToCoordMap;

	static {
		Germany3DParser parser = new Germany3DParser();
		parser.readFile("C:/Users/marcel.puyol/IdeaProjects/MATSim-MPM/input/germany_3d.xml");
		nodeIdToCoordMap = Germany3DParser.getNodeIdToCoordMap();
	}

	private static Coord getCoordForNode(Id<Node> nodeId){

		Coord coord = nodeIdToCoordMap.get(nodeId);
		if (coord == null) {
			throw new IllegalArgumentException("No Coord found for node ID: " + nodeId);
		}
		return coord;
	}
}



