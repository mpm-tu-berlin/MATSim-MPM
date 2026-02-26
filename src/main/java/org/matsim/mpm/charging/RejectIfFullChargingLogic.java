package org.matsim.mpm.charging;

import com.google.common.base.Preconditions;
import org.matsim.api.core.v01.Id;
import org.matsim.contrib.ev.charging.*;
import org.matsim.contrib.ev.fleet.ElectricVehicle;
import org.matsim.contrib.ev.infrastructure.ChargerSpecification;
import org.matsim.core.api.experimental.events.EventsManager;
import org.matsim.vehicles.Vehicle;

import java.util.*;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Charging logic for chargers where queuing is not desired (e.g. overnight rest stops).
 *
 * <p>If a plug is available when a vehicle arrives, it is charged normally and the slot is
 * held until {@link #removeVehicle} is called (same hold-until-leave semantics as
 * {@link HoldUntilLeaveChargingLogic}). If all plugs are occupied the vehicle is silently
 * rejected — no {@code QueuedAtChargerEvent} or {@code QuitQueueAtChargerEvent} is fired,
 * so {@code ChargingEventSequenceCollector} never creates an incomplete sequence for it.
 *
 * <p>This avoids the {@code NoSuchElementException} in {@code ChargingProceduresCSVWriter}
 * that occurs when vehicles are still queued at simulation end.
 */
public class RejectIfFullChargingLogic implements ChargingLogic {

    private final ChargerSpecification charger;
    private final ChargingStrategy chargingStrategy;
    private final EventsManager eventsManager;

    private final Map<Id<Vehicle>, ElectricVehicle> pluggedVehicles = new LinkedHashMap<>();
    private final Set<Id<Vehicle>> chargedButHolding = new HashSet<>();
    private final Set<Id<Vehicle>> rejectedVehicles = new HashSet<>();
    private final Queue<ElectricVehicle> arrivingVehicles = new LinkedBlockingQueue<>();
    private final Map<Id<Vehicle>, ChargingListener> listeners = new LinkedHashMap<>();

    public RejectIfFullChargingLogic(ChargerSpecification charger, ChargingStrategy chargingStrategy,
                                     EventsManager eventsManager) {
        this.chargingStrategy = Objects.requireNonNull(chargingStrategy);
        this.charger = Objects.requireNonNull(charger);
        this.eventsManager = Objects.requireNonNull(eventsManager);
    }

    @Override
    public void chargeVehicles(double chargePeriod, double now) {
        Iterator<ElectricVehicle> evIter = pluggedVehicles.values().iterator();
        while (evIter.hasNext()) {
            ElectricVehicle ev = evIter.next();

            if (chargedButHolding.contains(ev.getId())) {
                continue;
            }

            double oldCharge = ev.getBattery().getCharge();
            double energy = ev.getChargingPower().calcChargingPower(charger) * chargePeriod;
            double newCharge = Math.min(oldCharge + energy, ev.getBattery().getCapacity());
            ev.getBattery().setCharge(newCharge);
            eventsManager.processEvent(
                    new EnergyChargedEvent(now, charger.getId(), ev.getId(), newCharge - oldCharge, newCharge));

            if (chargingStrategy.isChargingCompleted(ev)) {
                chargedButHolding.add(ev.getId());
            }
        }

        // Process arriving vehicles: plug in if a slot is free, otherwise reject silently.
        var arrivingIter = arrivingVehicles.iterator();
        while (arrivingIter.hasNext()) {
            var ev = arrivingIter.next();
            if (pluggedVehicles.size() < charger.getPlugCount()) {
                plugVehicle(ev, now);
            } else {
                rejectedVehicles.add(ev.getId()); // no slot available — reject without queuing
            }
            arrivingIter.remove();
        }
    }

    @Override
    public void addVehicle(ElectricVehicle ev, double now) {
        addVehicle(ev, new ChargingListener() {}, now);
    }

    @Override
    public void addVehicle(ElectricVehicle ev, ChargingListener chargingListener, double now) {
        arrivingVehicles.add(ev);
        listeners.put(ev.getId(), chargingListener);
    }

    @Override
    public void removeVehicle(ElectricVehicle ev, double now) {
        if (chargedButHolding.remove(ev.getId())) {
            pluggedVehicles.remove(ev.getId());
            eventsManager.processEvent(
                    new ChargingEndEvent(now, charger.getId(), ev.getId(), ev.getBattery().getCharge()));
            listeners.remove(ev.getId()).notifyChargingEnded(ev, now);
        } else if (pluggedVehicles.remove(ev.getId()) != null) {
            eventsManager.processEvent(
                    new ChargingEndEvent(now, charger.getId(), ev.getId(), ev.getBattery().getCharge()));
            listeners.remove(ev.getId()).notifyChargingEnded(ev, now);
        } else {
            // Vehicle was rejected (no slot was available) — clean up without firing events.
            Preconditions.checkState(rejectedVehicles.remove(ev.getId()),
                    "Vehicle (%s) is neither plugged nor rejected at charger (%s)", ev.getId(), charger.getId());
            listeners.remove(ev.getId());
        }
    }

    private void plugVehicle(ElectricVehicle ev, double now) {
        if (pluggedVehicles.put(ev.getId(), ev) != null) {
            throw new IllegalArgumentException();
        }
        eventsManager.processEvent(
                new ChargingStartEvent(now, charger.getId(), ev.getId(), ev.getBattery().getCharge()));
        listeners.get(ev.getId()).notifyChargingStarted(ev, now);
    }

    private final Collection<ElectricVehicle> unmodifiablePluggedVehicles =
            Collections.unmodifiableCollection(pluggedVehicles.values());

    @Override
    public Collection<ElectricVehicle> getPluggedVehicles() {
        return unmodifiablePluggedVehicles;
    }

    @Override
    public Collection<ElectricVehicle> getQueuedVehicles() {
        return Collections.emptyList();
    }

    @Override
    public ChargingStrategy getChargingStrategy() {
        return chargingStrategy;
    }
}
