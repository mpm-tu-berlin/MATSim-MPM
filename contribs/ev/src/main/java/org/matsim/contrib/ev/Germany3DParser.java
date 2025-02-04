package org.matsim.contrib.ev;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Node;
import org.matsim.core.utils.io.MatsimXmlParser;
import org.xml.sax.Attributes;

import java.util.HashMap;
import java.util.Map;
import java.util.Stack;

public class Germany3DParser extends MatsimXmlParser {

	//Declaring the empty map "nodeIdToCoordMap"
	private static final Map<Id<Node>, Coord> nodeIdToCoordMap = new HashMap<>();

	//Constructor
	public Germany3DParser() {
		super(ValidationType.DTD_ONLY);
		super.setValidating(false);
	}


	@Override
	public void startTag(String name, Attributes atts, Stack<String> context) {
		if (name.equals("node")) {
			Id<Node> nodeId = Id.create(atts.getValue("id"), Node.class);
			double x = Double.parseDouble(atts.getValue("x"));
			double y = Double.parseDouble(atts.getValue("y"));
			double z = Double.parseDouble(atts.getValue("z"));
			Coord coord = new Coord(x, y, z);
			nodeIdToCoordMap.put(nodeId, coord);
		}
	}

	@Override
	public void endTag(String name, String content, Stack<String> context) {

	}

	public static Map<Id<Node>, Coord> getNodeIdToCoordMap() {

		return nodeIdToCoordMap;
	}


}
