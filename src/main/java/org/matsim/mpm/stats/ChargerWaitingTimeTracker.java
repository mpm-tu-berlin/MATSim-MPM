package org.matsim.mpm.stats;

import com.google.inject.Inject;
import com.google.inject.Singleton;
import org.matsim.api.core.v01.Id;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEventHandler;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEventHandler;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.core.controler.events.IterationEndsEvent;
import org.matsim.core.controler.listener.IterationEndsListener;
import org.matsim.vehicles.Vehicle;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Tracks per-charger average waiting times across iterations.
 * The router reads the previous iteration's data to make congestion-aware charger selections.
 */
@Singleton
public class ChargerWaitingTimeTracker
        implements QueuedAtChargerEventHandler, QuitQueueAtChargerEventHandler,
                   IterationEndsListener {

    private static final Logger log = LogManager.getLogger(ChargerWaitingTimeTracker.class);

    private record PendingEntry(Id<Charger> chargerId, double startTime) {}

    private final Map<Id<Vehicle>, PendingEntry> pending = new HashMap<>();
    private final Map<Id<Charger>, List<Double>> currentWaitTimes = new HashMap<>();
    private volatile Map<Id<Charger>, Double> previousIterationAverages = new HashMap<>();

    @Inject
    public ChargerWaitingTimeTracker() {
        log.info("ChargerWaitingTimeTracker initialized — will track per-charger average waiting times");
    }

    @Override
    public void handleEvent(QueuedAtChargerEvent event) {
        pending.put(event.getVehicleId(),
                new PendingEntry(event.getChargerId(), event.getTime()));
    }

    @Override
    public void handleEvent(QuitQueueAtChargerEvent event) {
        PendingEntry entry = pending.remove(event.getVehicleId());
        if (entry != null) {
            double waitSeconds = event.getTime() - entry.startTime();
            currentWaitTimes
                .computeIfAbsent(entry.chargerId(), id -> new ArrayList<>())
                .add(waitSeconds);
        }
    }

    @Override
    public void reset(int iteration) {
        pending.clear();
        currentWaitTimes.clear();
        // Do NOT clear previousIterationAverages — reset() is called at iteration start
        // before routing, so the router still needs the previous snapshot.
    }

    @Override
    public void notifyIterationEnds(IterationEndsEvent event) {
        Map<Id<Charger>, Double> newAverages = new HashMap<>();
        for (Map.Entry<Id<Charger>, List<Double>> e : currentWaitTimes.entrySet()) {
            double avg = e.getValue().stream()
                          .mapToDouble(Double::doubleValue)
                          .average()
                          .orElse(0.0);
            newAverages.put(e.getKey(), avg);
            log.info("Charger {} — avg waiting time in iteration {}: {} seconds ({} vehicles queued)",
                    e.getKey(), event.getIteration(), String.format("%.1f", avg), e.getValue().size());
        }
        previousIterationAverages = newAverages;

        if (newAverages.isEmpty()) {
            log.info("No charger queuing events in iteration {}", event.getIteration());
        }
    }

    /**
     * Returns the average waiting time (seconds) at the given charger from the previous iteration.
     * Returns 0.0 if no data exists (e.g., iteration 0 or charger had no queue).
     */
    public double getAverageWaitingTime(Id<Charger> chargerId) {
        return previousIterationAverages.getOrDefault(chargerId, 0.0);
    }
}
