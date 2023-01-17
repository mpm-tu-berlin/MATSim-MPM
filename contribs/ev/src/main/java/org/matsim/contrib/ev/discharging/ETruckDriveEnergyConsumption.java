package org.matsim.contrib.ev.discharging;

import org.matsim.api.core.v01.network.Link;

public class ETruckDriveEnergyConsumption implements DriveEnergyConsumption {
	private final static double AVG_CONSUMPTION_PER_KM = 1200 * 3.6; // 1200 Wh/km * 60^2/1000 = Ws/m

	@Override
	public double calcEnergyConsumption(Link link, double travelTime, double linkEnterTime) {
		if (travelTime == 0) {
			return 0;
		}
		return AVG_CONSUMPTION_PER_KM * link.getLength();
	}
}
