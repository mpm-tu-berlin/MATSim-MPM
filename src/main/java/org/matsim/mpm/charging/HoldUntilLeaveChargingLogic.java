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
 * Charging logic that keeps a fully-charged vehicle plugged in (occupying the charger slot)
 * until {@link #removeVehicle} is called, which happens when the agent's charging activity ends.
 * This prevents the charger from being freed before the vehicle physically leaves.
 * <p>
 * When charging completes, energy transfer stops but no {@link ChargingEndEvent} is fired yet.
 * The event fires only when {@code removeVehicle()} is called, keeping the event sequence
 * consistent with what {@link org.matsim.contrib.ev.charging.VehicleChargingHandler} and
 * {@link ChargingEventSequenceCollector} expect.
 * <p>
 * Based on {@link ChargingWithQueueingLogic} from the MATSim EV contrib.
 */
public class HoldUntilLeaveChargingLogic implements ChargingLogic {

    private final ChargerSpecification charger;
    private final ChargingStrategy chargingStrategy;
    private final EventsManager eventsManager;

    private final Map<Id<Vehicle>, ElectricVehicle> pluggedVehicles = new LinkedHashMap<>();
    private final Set<Id<Vehicle>> chargedButHolding = new HashSet<>();
    private final Queue<ElectricVehicle> queuedVehicles = new LinkedList<>();
    private final Queue<ElectricVehicle> arrivingVehicles = new LinkedBlockingQueue<>();
    private final Map<Id<Vehicle>, ChargingListener> listeners = new LinkedHashMap<>();

    public HoldUntilLeaveChargingLogic(ChargerSpecification charger, ChargingStrategy chargingStrategy,
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

            // Skip energy transfer for vehicles that already finished charging but are still holding the slot
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
                // Mark as done but do NOT fire ChargingEndEvent yet — hold the slot.
                // ChargingEndEvent fires when removeVehicle() is called (on ActivityEndEvent).
                chargedButHolding.add(ev.getId());
            }
        }

        // Promote queued vehicles only into genuinely free slots
        int freeSlots = charger.getPlugCount() - pluggedVehicles.size();
        int queuedToPluggedCount = Math.min(queuedVehicles.size(), freeSlots);
        for (int i = 0; i < queuedToPluggedCount; i++) {
            plugVehicle(queuedVehicles.poll(), now);
        }

        var arrivingVehiclesIter = arrivingVehicles.iterator();
        while (arrivingVehiclesIter.hasNext()) {
            var ev = arrivingVehiclesIter.next();
            if (pluggedVehicles.size() < charger.getPlugCount()) {
                plugVehicle(ev, now);
            } else {
                queueVehicle(ev, now);
            }
            arrivingVehiclesIter.remove();
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
            // Vehicle finished charging earlier — now fire ChargingEndEvent and free the slot
            pluggedVehicles.remove(ev.getId());
            eventsManager.processEvent(
                    new ChargingEndEvent(now, charger.getId(), ev.getId(), ev.getBattery().getCharge()));
            listeners.remove(ev.getId()).notifyChargingEnded(ev, now);

            if (!queuedVehicles.isEmpty()) {
                plugVehicle(queuedVehicles.poll(), now);
            }
        } else if (pluggedVehicles.remove(ev.getId()) != null) {
            // Vehicle is still charging — fire ChargingEndEvent now
            eventsManager.processEvent(
                    new ChargingEndEvent(now, charger.getId(), ev.getId(), ev.getBattery().getCharge()));
            listeners.remove(ev.getId()).notifyChargingEnded(ev, now);

            if (!queuedVehicles.isEmpty()) {
                plugVehicle(queuedVehicles.poll(), now);
            }
        } else {
            // Vehicle must be in the queue
            Preconditions.checkState(queuedVehicles.remove(ev),
                    "Vehicle (%s) is neither queued nor plugged at charger (%s)", ev.getId(), charger.getId());
            eventsManager.processEvent(new QuitQueueAtChargerEvent(now, charger.getId(), ev.getId()));
        }
    }

    private void queueVehicle(ElectricVehicle ev, double now) {
        queuedVehicles.add(ev);
        eventsManager.processEvent(new QueuedAtChargerEvent(now, charger.getId(), ev.getId()));
        listeners.get(ev.getId()).notifyVehicleQueued(ev, now);
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

    private final Collection<ElectricVehicle> unmodifiableQueuedVehicles =
            Collections.unmodifiableCollection(queuedVehicles);

    @Override
    public Collection<ElectricVehicle> getQueuedVehicles() {
        return unmodifiableQueuedVehicles;
    }

    @Override
    public ChargingStrategy getChargingStrategy() {
        return chargingStrategy;
    }
}
