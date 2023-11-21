package org.matsim.contrib.ev.eTruckTraffic.stats;

import com.google.inject.Inject;
import org.matsim.api.core.v01.Id;

import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEventHandler;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEventHandler;
import org.matsim.contrib.ev.fleet.ElectricFleet;
import org.matsim.contrib.ev.fleet.ElectricVehicle;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.contrib.ev.infrastructure.ChargingInfrastructure;
import org.matsim.core.events.MobsimScopeEventHandler;
import org.matsim.core.utils.misc.Time;
import org.matsim.vehicles.Vehicle;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class ChargerQueuingCollector
		implements QueuedAtChargerEventHandler, QuitQueueAtChargerEventHandler, MobsimScopeEventHandler {
	private final ChargingInfrastructure chargingInfrastructure;
	private final ElectricFleet fleet;

	private record TimeQueue(double time, double queue) {
	}

	private final Map<Id<Vehicle>, TimeQueue> queueBeginQueue = new HashMap<>();

	private final List<QueuingLogEntry> logList = new ArrayList<>();

	@Inject
	public ChargerQueuingCollector(ChargingInfrastructure chargingInfrastructure, ElectricFleet fleet) {
		this.fleet = fleet;
		this.chargingInfrastructure = chargingInfrastructure;
	}

	@Override
	public void handleEvent(QuitQueueAtChargerEvent event) {
		var queuingStart = queueBeginQueue.remove(event.getVehicleId());
		if (queuingStart != null){
			QueuingLogEntry loge = new QueuingLogEntry(queuingStart.time, event.getTime(),
					chargingInfrastructure.getChargers().get(event.getChargerId()),
					event.getVehicleId()
			);
			logList.add(loge);
		}else
			throw new NullPointerException(event.getVehicleId().toString() +
					" has never started queuing");
	}

	@Override
	public void handleEvent(QueuedAtChargerEvent event) {
		ElectricVehicle ev = this.fleet.getElectricVehicles().get(event.getVehicleId());
		if (ev != null) {
			this.queueBeginQueue.put(event.getVehicleId(),
					new TimeQueue(event.getTime(), ev.getBattery().getCharge())
			);
		} else
			throw new NullPointerException(event.getVehicleId().toString() + " is not in list");
	}

	public List<QueuingLogEntry> getLogList(){ return logList;}

	public static class QueuingLogEntry implements Comparable<QueuingLogEntry> {
		private final double queueStart;
		private final double queueEnd;
		private final Charger charger;
		private final Id<Vehicle> vehicleId;

		public QueuingLogEntry(double queueStart, double queueEnd,
							   Charger charger, Id<Vehicle> vehicleId) {
			this.queueStart = queueStart;
			this.queueEnd = queueEnd;
			this.charger = charger;
			this.vehicleId = vehicleId;
		}

		public double getQueueStart() {	return queueStart; }
		public double getQueueEnd(){ return queueEnd; }
		public Charger getCharger(){ return charger;}
		public Id<Vehicle> getVehicleId() {
			return vehicleId;
		}


		public String toString() {
			return charger.getId().toString()
					+ ";"
					+ Time.writeTime(queueStart)
					+ ";"
					+ Time.writeTime(queueEnd)
					+ ";"
					+ Time.writeTime(queueEnd - queueStart)
					+ ";"
					+ charger.getCoord().getX()
					+ ";"
					+ charger.getCoord().getY()
					+ ";"
					+ vehicleId.toString();
		}

		@Override
		public int compareTo(QueuingLogEntry o) {return Double.compare(queueStart, o.queueStart);}
	}

}
