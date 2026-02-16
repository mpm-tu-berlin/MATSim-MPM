package org.matsim.mpm.scoring;

import com.google.inject.Inject;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.events.ActivityStartEvent;
import org.matsim.api.core.v01.events.handler.ActivityStartEventHandler;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEventHandler;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEventHandler;
import org.matsim.contrib.ev.charging.ChargingStartEvent;
import org.matsim.contrib.ev.charging.ChargingStartEventHandler;
import org.matsim.contrib.ev.charging.ChargingEndEvent;
import org.matsim.contrib.ev.charging.ChargingEndEventHandler;
import org.matsim.core.config.groups.ScoringConfigGroup;
import org.matsim.core.events.MobsimScopeEventHandler;
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
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * This handler tracks waiting time in charging queues and applies a negative score
 * based on the waiting time parameter in the scoring config.
 *
 * @author MATSim-MPM
 */
public class ChargingQueueWaitingScoringHandler
        implements QueuedAtChargerEventHandler, QuitQueueAtChargerEventHandler,
                   ChargingStartEventHandler, ChargingEndEventHandler,
                   ActivityStartEventHandler,
                   MobsimScopeEventHandler, IterationEndsListener {

    private static final Logger log = LogManager.getLogger(ChargingQueueWaitingScoringHandler.class);

    private static class WaitingEvent {
        final double time;
        final String personId;
        final double waitingTimeSeconds;
        final double score;

        WaitingEvent(double time, String personId, double waitingTimeSeconds, double score) {
            this.time = time;
            this.personId = personId;
            this.waitingTimeSeconds = waitingTimeSeconds;
            this.score = score;
        }
    }

    private final double waitingScorePerHour;
    private final Map<Id<Vehicle>, Double> queueStartTimes = new HashMap<>();
    private final Map<Id<Vehicle>, Double> arrivalAtChargerTimes = new HashMap<>();
    private final List<WaitingEvent> waitingEvents = new ArrayList<>();
    private final Map<String, Double> personWaitingScores = new HashMap<>();
    private int queueEventsCount = 0;
    private int chargingEventsCount = 0;

    @Inject
    public ChargingQueueWaitingScoringHandler(ScoringConfigGroup scoringConfig) {

        // Use the waiting parameter directly from config
        // This corresponds to <param name="waiting" value="-6"/> in the config.xml
        this.waitingScorePerHour = scoringConfig.getMarginalUtlOfWaiting_utils_hr();

        // Log the configured value for debugging
        log.info("ChargingQueueWaitingScoringHandler initialized. Waiting score: {} utils/hour " +
                 "(from config parameter 'waiting')", this.waitingScorePerHour);

        // Warn if not configured
        if (this.waitingScorePerHour == 0.0) {
            log.warn("Waiting parameter is 0.0! Waiting time will not be penalized. " +
                     "Set <param name='waiting' value='-6'/> in planCalcScore module to enable penalties.");
        }

        log.info("Handler will track both Queue events and Charging events to detect waiting times");
    }

    @Override
    public void handleEvent(ActivityStartEvent event) {
        // Track when a vehicle arrives at a charging station
        if (event.getActType().contains("charging")) {
            // Extract vehicle ID from person ID (assuming format "personId" or with "_car" suffix)
            Id<Vehicle> vehicleId = Id.createVehicleId(event.getPersonId().toString() + "_car");

            // Record arrival time at charger
            if (!arrivalAtChargerTimes.containsKey(vehicleId)) {
                arrivalAtChargerTimes.put(vehicleId, event.getTime());
                log.debug("Vehicle {} arrived at charging activity at time {}", vehicleId, event.getTime());
            }
        }
    }

    @Override
    public void handleEvent(ChargingStartEvent event) {
        // Track when a vehicle starts charging - this might be after waiting in queue
        chargingEventsCount++;
        Double arrivalTime = arrivalAtChargerTimes.get(event.getVehicleId());

        if (arrivalTime != null) {
            // Vehicle arrived and waited before charging started
            double waitingTime = event.getTime() - arrivalTime;

            if (waitingTime > 1.0) { // More than 1 second = actual waiting
                String vehicleIdString = event.getVehicleId().toString();
                String personIdString = vehicleIdString.replace("_car", "");

                double waitingTimeHours = waitingTime / 3600.0;
                double waitingScore = waitingScorePerHour * waitingTimeHours;

                log.info("Vehicle {} waited {} seconds before charging started at time {}. Waiting penalty tracked: {} utils (applied by ChargingWaitingScoringFunction)",
                    event.getVehicleId(), waitingTime, event.getTime(), waitingScore);

                // Store waiting event for CSV export
                waitingEvents.add(new WaitingEvent(
                    event.getTime(),
                    personIdString,
                    waitingTime,
                    waitingScore
                ));

                // Accumulate per-person waiting score for the scoring function
                personWaitingScores.merge(personIdString, waitingScore, Double::sum);
            }
        }
    }

    @Override
    public void handleEvent(ChargingEndEvent event) {
        // Clean up
        arrivalAtChargerTimes.remove(event.getVehicleId());
    }

    @Override
    public void handleEvent(QueuedAtChargerEvent event) {
        // Traditional queue event - vehicle enters explicit queue
        queueEventsCount++;
        queueStartTimes.put(event.getVehicleId(), event.getTime());
        // Also track as arrival time
        if (!arrivalAtChargerTimes.containsKey(event.getVehicleId())) {
            arrivalAtChargerTimes.put(event.getVehicleId(), event.getTime());
        }
        log.info("Vehicle {} entered queue at charger {} at time {}",
            event.getVehicleId(), event.getChargerId(), event.getTime());
    }

    @Override
    public void handleEvent(QuitQueueAtChargerEvent event) {
        // Calculate waiting time
        Double queueStartTime = queueStartTimes.remove(event.getVehicleId());

        if (queueStartTime != null) {
            double waitingTimeSeconds = event.getTime() - queueStartTime;
            double waitingTimeHours = waitingTimeSeconds / 3600.0;

            // Calculate score penalty
            double waitingScore = waitingScorePerHour * waitingTimeHours;

            // Extract person ID from vehicle ID
            String vehicleIdString = event.getVehicleId().toString();
            String personIdString = vehicleIdString.replace("_car", "");

            log.info("Vehicle {} left queue at charger {} at time {}. Waiting time: {} seconds ({} hours). Waiting penalty tracked: {} utils (applied by ChargingWaitingScoringFunction)",
                event.getVehicleId(), event.getChargerId(), event.getTime(),
                waitingTimeSeconds, waitingTimeHours, waitingScore);

            // Store waiting event for CSV export
            waitingEvents.add(new WaitingEvent(
                event.getTime(),
                personIdString,
                waitingTimeSeconds,
                waitingScore
            ));

            // Accumulate per-person waiting score for the scoring function
            personWaitingScores.merge(personIdString, waitingScore, Double::sum);
        } else {
            log.warn("Vehicle {} left queue at charger {} but was never recorded as entering the queue!",
                event.getVehicleId(), event.getChargerId());
        }
    }

    /**
     * Returns the accumulated waiting score for a given person in the current iteration.
     */
    public double getPersonWaitingScore(String personId) {
        return personWaitingScores.getOrDefault(personId, 0.0);
    }

    @Override
    public void reset(int iteration) {
        queueStartTimes.clear();
        arrivalAtChargerTimes.clear();
        waitingEvents.clear();
        personWaitingScores.clear();
        queueEventsCount = 0;
        chargingEventsCount = 0;
    }

    @Override
    public void notifyIterationEnds(IterationEndsEvent event) {
        log.info("Iteration {} ended. Queue events: {}, Charging events: {}, Waiting events recorded: {}",
                 event.getIteration(), queueEventsCount, chargingEventsCount, waitingEvents.size());

        // Write waiting events to CSV
        if (waitingEvents.isEmpty()) {
            log.info("No waiting events to write for iteration {}", event.getIteration());
            return;
        }

        String filename = event.getServices().getControlerIO().getIterationFilename(
            event.getIteration(), "charging_queue_waiting_scores.csv"
        );

        try (BufferedWriter writer = Files.newBufferedWriter(Paths.get(filename))) {
            // Write header
            writer.write("time;person_id;waiting_time_seconds;waiting_score\n");

            // Write data
            for (WaitingEvent we : waitingEvents) {
                writer.write(String.format("%.1f;%s;%.1f;%.4f\n",
                    we.time, we.personId, we.waitingTimeSeconds, we.score));
            }

            log.info("Wrote {} charging queue waiting events to {}",
                    waitingEvents.size(), filename);

        } catch (IOException e) {
            log.error("Error writing charging queue waiting scores", e);
        }

        // Clear for next iteration
        waitingEvents.clear();
        queueEventsCount = 0;
        chargingEventsCount = 0;
    }
}
