package org.matsim.mpm.stats;

import com.google.inject.Inject;
import com.google.inject.Singleton;
import org.matsim.api.core.v01.Id;
import org.matsim.contrib.ev.charging.ChargingStartEvent;
import org.matsim.contrib.ev.charging.ChargingStartEventHandler;
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

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.DoubleSummaryStatistics;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Tracks per-charger average waiting times in hourly bins (0–95) across iterations.
 * The router reads the previous iteration's binned data to make time-aware,
 * congestion-aware charger selections.
 */
@Singleton
public class ChargerWaitingTimeTracker
        implements QueuedAtChargerEventHandler, ChargingStartEventHandler,
                   QuitQueueAtChargerEventHandler, IterationEndsListener {

    private static final Logger log = LogManager.getLogger(ChargerWaitingTimeTracker.class);
    private static final int NUM_BINS = 96;

    private record PendingEntry(Id<Charger> chargerId, double startTime) {}

    private final Map<Id<Vehicle>, PendingEntry> pending = new HashMap<>();
    /** charger → hour bin → list of individual waiting times (seconds) */
    private final Map<Id<Charger>, Map<Integer, List<Double>>> currentWaitTimes = new HashMap<>();
    /** charger → hour bin → average waiting time from previous iteration */
    private volatile Map<Id<Charger>, Map<Integer, Double>> previousIterationAverages = new HashMap<>();

    @Inject
    public ChargerWaitingTimeTracker() {
        log.info("ChargerWaitingTimeTracker initialized — will track per-charger hourly waiting times ({} bins)", NUM_BINS);
    }

    private static int timeToBin(double timeSeconds) {
        int bin = (int) (timeSeconds / 3600.0);
        return Math.max(0, Math.min(bin, NUM_BINS - 1));
    }

    @Override
    public void handleEvent(QueuedAtChargerEvent event) {
        pending.put(event.getVehicleId(),
                new PendingEntry(event.getChargerId(), event.getTime()));
    }

    @Override
    public void handleEvent(ChargingStartEvent event) {
        PendingEntry entry = pending.remove(event.getVehicleId());
        if (entry != null) {
            double waitSeconds = event.getTime() - entry.startTime();
            int bin = timeToBin(entry.startTime());
            currentWaitTimes
                .computeIfAbsent(entry.chargerId(), id -> new HashMap<>())
                .computeIfAbsent(bin, b -> new ArrayList<>())
                .add(waitSeconds);
        }
    }

    @Override
    public void handleEvent(QuitQueueAtChargerEvent event) {
        // Vehicle left the queue without charging (e.g., activity ended while waiting).
        // Still record this as waiting time for congestion-aware charger selection.
        PendingEntry entry = pending.remove(event.getVehicleId());
        if (entry != null) {
            double waitSeconds = event.getTime() - entry.startTime();
            int bin = timeToBin(entry.startTime());
            currentWaitTimes
                .computeIfAbsent(entry.chargerId(), id -> new HashMap<>())
                .computeIfAbsent(bin, b -> new ArrayList<>())
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
        Map<Id<Charger>, Map<Integer, Double>> newAverages = new HashMap<>();
        for (var chargerEntry : currentWaitTimes.entrySet()) {
            Map<Integer, Double> binAverages = new HashMap<>();
            for (var binEntry : chargerEntry.getValue().entrySet()) {
                double avg = binEntry.getValue().stream()
                              .mapToDouble(Double::doubleValue)
                              .average()
                              .orElse(0.0);
                binAverages.put(binEntry.getKey(), avg);
            }
            newAverages.put(chargerEntry.getKey(), binAverages);
            log.info("Charger {} — iteration {}: {} bins with queue data",
                    chargerEntry.getKey(), event.getIteration(), binAverages.size());
        }
        previousIterationAverages = newAverages;

        if (newAverages.isEmpty()) {
            log.info("No charger queuing events in iteration {}", event.getIteration());
        }

        // Write CSV with per-charger, per-bin waiting time statistics
        String csvPath = event.getServices().getControlerIO()
                .getIterationFilename(event.getIteration(), "charger_waiting_times.csv");
        try (BufferedWriter writer = Files.newBufferedWriter(Paths.get(csvPath))) {
            writer.write("charger_id;hour_bin;num_vehicles;avg_wait_seconds;min_wait_seconds;max_wait_seconds");
            writer.newLine();
            for (var chargerEntry : currentWaitTimes.entrySet()) {
                for (var binEntry : chargerEntry.getValue().entrySet()) {
                    DoubleSummaryStatistics stats = binEntry.getValue().stream()
                            .mapToDouble(Double::doubleValue)
                            .summaryStatistics();
                    writer.write(String.format("%s;%d;%d;%.1f;%.1f;%.1f",
                            chargerEntry.getKey(), binEntry.getKey(),
                            stats.getCount(), stats.getAverage(),
                            stats.getMin(), stats.getMax()));
                    writer.newLine();
                }
            }
        } catch (IOException ex) {
            log.error("Failed to write charger waiting times CSV to {}", csvPath, ex);
        }
    }

    /**
     * Returns the average waiting time (seconds) at the given charger for the hour bin
     * corresponding to the given arrival time, from the previous iteration.
     * Returns 0.0 if no data exists for this charger/bin.
     */
    public double getAverageWaitingTime(Id<Charger> chargerId, double arrivalTimeSeconds) {
        Map<Integer, Double> binMap = previousIterationAverages.get(chargerId);
        if (binMap == null) return 0.0;
        int bin = timeToBin(arrivalTimeSeconds);
        return binMap.getOrDefault(bin, 0.0);
    }

    /**
     * @deprecated Use {@link #getAverageWaitingTime(Id, double)} with arrival time instead.
     */
    @Deprecated
    public double getAverageWaitingTime(Id<Charger> chargerId) {
        Map<Integer, Double> binMap = previousIterationAverages.get(chargerId);
        if (binMap == null || binMap.isEmpty()) return 0.0;
        return binMap.values().stream().mapToDouble(Double::doubleValue).average().orElse(0.0);
    }
}
