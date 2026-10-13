package org.matsim.mpm.scoring;

import org.junit.jupiter.api.Test;
import org.matsim.api.core.v01.Id;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.core.api.experimental.events.EventsManager;
import org.matsim.core.config.groups.ScoringConfigGroup;
import org.matsim.core.events.EventsUtils;
import org.matsim.vehicles.Vehicle;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for ChargingQueueWaitingScoringHandler
 */
public class ChargingQueueWaitingScoringHandlerTest {

    @Test
    public void testWaitingTimeScoring() {
        // Setup with explicit waiting penalty
        ScoringConfigGroup scoringConfig = new ScoringConfigGroup();
        scoringConfig.setMarginalUtlOfWaiting_utils_hr(-6.0);

        ChargingQueueWaitingScoringHandler handler =
            new ChargingQueueWaitingScoringHandler(scoringConfig);
        EventsManager eventsManager = EventsUtils.createEventsManager();
        eventsManager.addHandler(handler);

        // Simulate: Vehicle enters queue at time 1000
        Id<Vehicle> vehicleId = Id.createVehicleId("testPerson_car");
        Id<Charger> chargerId = Id.create("charger1", Charger.class);

        eventsManager.processEvent(new QueuedAtChargerEvent(1000.0, chargerId, vehicleId));

        // Simulate: Vehicle leaves queue at time 2800 (1800 seconds = 0.5 hours later)
        eventsManager.processEvent(new QuitQueueAtChargerEvent(2800.0, chargerId, vehicleId));

        // Verify: Per-person waiting score was accumulated
        double score = handler.getPersonWaitingScore("testPerson");

        // Score should be negative (waiting penalty)
        assertTrue(score < 0, "Waiting should result in negative score");

        // Verify: Score magnitude (1800 seconds = 0.5 hours, -6 per hour = -3)
        assertEquals(-3.0, score, 0.01,
            "Score should be -6 utils/h * 0.5h = -3 utils");
    }

    @Test
    public void testNoWaitingNoScoring() {
        // Setup
        ScoringConfigGroup scoringConfig = new ScoringConfigGroup();
        scoringConfig.setMarginalUtlOfWaiting_utils_hr(-6.0);

        ChargingQueueWaitingScoringHandler handler =
            new ChargingQueueWaitingScoringHandler(scoringConfig);
        EventsManager eventsManager = EventsUtils.createEventsManager();
        eventsManager.addHandler(handler);

        // Simulate: Vehicle leaves queue without entering (shouldn't happen in practice)
        Id<Vehicle> vehicleId = Id.createVehicleId("testPerson_car");
        Id<Charger> chargerId = Id.create("charger1", Charger.class);

        QuitQueueAtChargerEvent quitEvent = new QuitQueueAtChargerEvent(2800.0, chargerId, vehicleId);
        eventsManager.processEvent(quitEvent);

        // Verify: No score accumulated
        assertEquals(0.0, handler.getPersonWaitingScore("testPerson"), 0.001,
            "Should not score if vehicle never entered queue");
    }

    @Test
    public void testResetClearsState() {
        // Setup
        ScoringConfigGroup scoringConfig = new ScoringConfigGroup();
        scoringConfig.setMarginalUtlOfWaiting_utils_hr(-6.0);

        ChargingQueueWaitingScoringHandler handler =
            new ChargingQueueWaitingScoringHandler(scoringConfig);
        EventsManager eventsManager = EventsUtils.createEventsManager();
        eventsManager.addHandler(handler);

        // Simulate: Vehicle enters and leaves queue
        Id<Vehicle> vehicleId = Id.createVehicleId("testPerson_car");
        Id<Charger> chargerId = Id.create("charger1", Charger.class);

        eventsManager.processEvent(new QueuedAtChargerEvent(1000.0, chargerId, vehicleId));
        eventsManager.processEvent(new QuitQueueAtChargerEvent(2800.0, chargerId, vehicleId));

        // Verify score exists before reset
        assertTrue(handler.getPersonWaitingScore("testPerson") < 0);

        // Reset between iterations
        handler.reset(1);

        // Verify: Score cleared after reset
        assertEquals(0.0, handler.getPersonWaitingScore("testPerson"), 0.001,
            "Should have no score after reset");
    }
}
