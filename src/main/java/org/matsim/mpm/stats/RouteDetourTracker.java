package org.matsim.mpm.stats;

import com.google.inject.Inject;
import com.google.inject.Singleton;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.matsim.api.core.v01.Id;
import org.matsim.contrib.ev.charging.ChargingStartEvent;
import org.matsim.contrib.ev.charging.ChargingStartEventHandler;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEventHandler;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEventHandler;
import org.matsim.core.controler.events.IterationEndsEvent;
import org.matsim.core.controler.listener.IterationEndsListener;
import org.matsim.vehicles.Vehicle;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Tracks per-person route detour data (basic vs staged route distances/times) and actual
 * charger waiting time experienced in simulation. Writes a CSV per iteration.
 *
 * <p>Route data is recorded from {@code MpmEvNetworkRoutingModule.calcRoute()} (multi-threaded),
 * waiting time is accumulated from charging events during the QSim.
 */
@Singleton
public class RouteDetourTracker
        implements QueuedAtChargerEventHandler, ChargingStartEventHandler,
                   QuitQueueAtChargerEventHandler, IterationEndsListener {

    private static final Logger log = LogManager.getLogger(RouteDetourTracker.class);

    public record RouteRecord(double basicDistance, double basicTime,
                              double stagedDistance, double stagedTime, int numStops) {}

    private final ConcurrentHashMap<String, RouteRecord> routeRecords = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Double> waitingTimes = new ConcurrentHashMap<>();

    // Pending queue entries: vehicleId → queue start time
    private final ConcurrentHashMap<Id<Vehicle>, Double> pendingQueueStart = new ConcurrentHashMap<>();

    @Inject
    public RouteDetourTracker() {
        log.info("RouteDetourTracker initialized — will track per-person route detours and waiting times");
    }

    /**
     * Called from routing threads to record the basic vs staged route for a person.
     */
    public void recordRoute(String personId, double basicDistance, double basicTime,
                            double stagedDistance, double stagedTime, int numStops) {
        routeRecords.put(personId, new RouteRecord(basicDistance, basicTime,
                stagedDistance, stagedTime, numStops));
    }

    @Override
    public void handleEvent(QueuedAtChargerEvent event) {
        pendingQueueStart.put(event.getVehicleId(), event.getTime());
    }

    @Override
    public void handleEvent(ChargingStartEvent event) {
        Double queueStart = pendingQueueStart.remove(event.getVehicleId());
        if (queueStart != null) {
            double waitSeconds = event.getTime() - queueStart;
            String personId = stripVehicleSuffix(event.getVehicleId().toString());
            waitingTimes.merge(personId, waitSeconds, Double::sum);
        }
    }

    @Override
    public void handleEvent(QuitQueueAtChargerEvent event) {
        Double queueStart = pendingQueueStart.remove(event.getVehicleId());
        if (queueStart != null) {
            double waitSeconds = event.getTime() - queueStart;
            String personId = stripVehicleSuffix(event.getVehicleId().toString());
            waitingTimes.merge(personId, waitSeconds, Double::sum);
        }
    }

    @Override
    public void reset(int iteration) {
        // Only clear event-related maps here. routeRecords is populated during routing
        // (before QSim), and reset() is called at QSim start — clearing it here would
        // wipe all route data before notifyIterationEnds() can write the CSV.
        waitingTimes.clear();
        pendingQueueStart.clear();
    }

    @Override
    public void notifyIterationEnds(IterationEndsEvent event) {
        String csvPath = event.getServices().getControlerIO()
                .getIterationFilename(event.getIteration(), "routeDetours.csv");

        try (BufferedWriter writer = Files.newBufferedWriter(Paths.get(csvPath))) {
            writer.write("person_id;basic_distance_m;basic_time_s;staged_distance_m;staged_time_s;"
                    + "distance_detour_m;time_detour_s;num_stops;waiting_time_s");
            writer.newLine();

            for (Map.Entry<String, RouteRecord> entry : routeRecords.entrySet()) {
                String personId = entry.getKey();
                RouteRecord r = entry.getValue();
                double waitTime = waitingTimes.getOrDefault(personId, 0.0);
                writer.write(String.format("%s;%.1f;%.1f;%.1f;%.1f;%.1f;%.1f;%d;%.1f",
                        personId,
                        r.basicDistance(), r.basicTime(),
                        r.stagedDistance(), r.stagedTime(),
                        r.stagedDistance() - r.basicDistance(),
                        r.stagedTime() - r.basicTime(),
                        r.numStops(),
                        waitTime));
                writer.newLine();
            }

            log.info("Wrote route detour data for {} persons to {}", routeRecords.size(), csvPath);
        } catch (IOException ex) {
            log.error("Failed to write route detours CSV to {}", csvPath, ex);
        }

        // Keep routeRecords across iterations — only overwritten when an agent is re-routed.
        // This ensures every iteration's CSV contains all agents with their latest routing data.
    }

    /**
     * Strip vehicle suffix (e.g., "_car") to get back to the person ID.
     * MATSim vehicle IDs for car mode are typically just the person ID.
     */
    private static String stripVehicleSuffix(String vehicleId) {
        // For car mode the vehicleId equals personId (no suffix).
        // For other modes it would be personId + "_" + mode.
        return vehicleId;
    }
}
