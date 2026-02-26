package org.matsim.mpm.stats;

import com.google.inject.Inject;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.events.ActivityStartEvent;
import org.matsim.api.core.v01.events.PersonLeavesVehicleEvent;
import org.matsim.api.core.v01.events.handler.ActivityStartEventHandler;
import org.matsim.api.core.v01.events.handler.PersonLeavesVehicleEventHandler;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.population.Person;
import org.matsim.contrib.ev.charging.ChargingEndEvent;
import org.matsim.contrib.ev.charging.ChargingEventSequenceCollector;
import org.matsim.contrib.ev.charging.ChargingEventSequenceCollector.ChargingSequence;
import org.matsim.contrib.ev.charging.ChargingStartEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.VehicleChargingHandler;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.contrib.ev.infrastructure.ChargingInfrastructureSpecification;
import org.matsim.core.controler.events.IterationEndsEvent;
import org.matsim.core.controler.listener.IterationEndsListener;
import org.matsim.core.utils.misc.Time;
import org.matsim.vehicles.Vehicle;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Replacement for {@code ChargingProceduresCSVWriter} (library final class) that adds
 * a {@code stopType} column (Energy / Timing / Rest) to {@code chargingStats.csv}.
 *
 * <p>The stop type is extracted from the stage-activity type produced by the routing module:
 * {@code "car Energy/Timing/Rest charging interaction"}.
 */
public class MpmChargingProceduresCSVWriter
        implements ActivityStartEventHandler, PersonLeavesVehicleEventHandler,
        IterationEndsListener {

    private static final String CHARGING_SUFFIX = VehicleChargingHandler.CHARGING_INTERACTION;

    @Inject
    private ChargingEventSequenceCollector collector;
    @Inject
    private ChargingInfrastructureSpecification chargingInfrastructureSpecification;

    /** Maps person → last vehicle used (populated from PersonLeavesVehicleEvent). */
    private final Map<Id<Person>, Id<Vehicle>> lastVehicleUsed = new HashMap<>();
    /**
     * Maps vehicle → ordered queue of stop types collected from ActivityStartEvents.
     * Using a per-vehicle deque avoids time-matching issues: ActivityStartEvent fires at
     * activity start, while QueuedAtChargerEvent/ChargingStartEvent fire one simulation step
     * later (in chargeVehicles()). The deque preserves chronological order per vehicle, which
     * matches the order of ChargingSequences when sorted by start time.
     */
    private final Map<Id<Vehicle>, Deque<String>> stopTypesByVehicle = new HashMap<>();

    @Override
    public void handleEvent(PersonLeavesVehicleEvent event) {
        lastVehicleUsed.put(event.getPersonId(), event.getVehicleId());
    }

    @Override
    public void handleEvent(ActivityStartEvent event) {
        if (!event.getActType().endsWith(CHARGING_SUFFIX)) return;
        Id<Vehicle> vehicleId = lastVehicleUsed.get(event.getPersonId());
        if (vehicleId == null) return;
        stopTypesByVehicle.computeIfAbsent(vehicleId, k -> new ArrayDeque<>())
                .add(extractStopReason(event.getActType()));
    }

    /**
     * Extracts the stop reason from the stage-activity type.
     * "car Energy charging interaction" → "Energy", "car Timing ..." → "Timing", etc.
     */
    private static String extractStopReason(String actType) {
        int modeEnd = actType.indexOf(' ');
        if (modeEnd < 0) return "unknown";
        String withoutSuffix = actType.substring(0, actType.length() - CHARGING_SUFFIX.length());
        if (withoutSuffix.length() <= modeEnd) return "unknown";
        return withoutSuffix.substring(modeEnd + 1); // "Energy", "Timing", "Rest", etc.
    }

    @Override
    public void notifyIterationEnds(IterationEndsEvent event) {
        // Group sequences by vehicle, sort each group chronologically
        Map<Id<Vehicle>, List<ChargingSequence>> seqsByVehicle = new HashMap<>();
        for (ChargingSequence seq : collector.getCompletedSequences()) {
            seqsByVehicle.computeIfAbsent(vehicleIdOf(seq), k -> new ArrayList<>()).add(seq);
        }
        for (ChargingSequence seq : collector.getOnGoingSequences()) {
            seqsByVehicle.computeIfAbsent(vehicleIdOf(seq), k -> new ArrayList<>()).add(seq);
        }
        for (List<ChargingSequence> seqs : seqsByVehicle.values()) {
            seqs.sort(Comparator.comparingDouble(MpmChargingProceduresCSVWriter::startTimeOf));
        }

        String filename = event.getServices().getControlerIO()
                .getIterationFilename(event.getIteration(), "chargingStats.csv");
        try (BufferedWriter w = Files.newBufferedWriter(Path.of(filename))) {
            w.write("chargerId;vehicleId;linkId;" +
                    "waitStartTime;waitEndTime;waitDuration;" +
                    "chargeStartTime;chargeEndTime;chargingDuration;" +
                    "energyTransmitted_kWh;stopType\n");
            for (Map.Entry<Id<Vehicle>, List<ChargingSequence>> entry : seqsByVehicle.entrySet()) {
                Deque<String> stopTypes = stopTypesByVehicle.getOrDefault(entry.getKey(), new ArrayDeque<>());
                for (ChargingSequence seq : entry.getValue()) {
                    String stopType = stopTypes.isEmpty() ? "unknown" : stopTypes.poll();
                    writeRow(w, seq, stopType);
                }
            }
        } catch (IOException e) {
            throw new RuntimeException("Failed to write chargingStats.csv", e);
        }
    }

    private static Id<Vehicle> vehicleIdOf(ChargingSequence seq) {
        return seq.getQueuedAtCharger().map(QueuedAtChargerEvent::getVehicleId)
                .orElseGet(() -> seq.getChargingStart().map(ChargingStartEvent::getVehicleId).orElseThrow());
    }

    private static double startTimeOf(ChargingSequence seq) {
        return seq.getQueuedAtCharger().map(QueuedAtChargerEvent::getTime)
                .orElseGet(() -> seq.getChargingStart().map(ChargingStartEvent::getTime).orElse(0.0));
    }

    private void writeRow(BufferedWriter w, ChargingSequence seq, String stopType) throws IOException {
        var queuedAt    = seq.getQueuedAtCharger();
        var quitQueue   = seq.getQuitQueueAtChargerEvent();
        var chargeStart = seq.getChargingStart();
        var chargeEnd   = seq.getChargingEnd();

        // Resolve charger ID and vehicle ID (one of the two events is always present)
        Id<Charger> chargerId;
        Id<Vehicle> vehicleId;
        if (queuedAt.isPresent()) {
            chargerId = queuedAt.get().getChargerId();
            vehicleId = queuedAt.get().getVehicleId();
        } else {
            chargerId = chargeStart.get().getChargerId();
            vehicleId = chargeStart.get().getVehicleId();
        }

        Id<Link> linkId = chargingInfrastructureSpecification
                .getChargerSpecifications().get(chargerId).getLinkId();

        // Wait times
        double waitStart = queuedAt.map(QueuedAtChargerEvent::getTime).orElse(Double.NaN);
        double waitEnd;
        if (queuedAt.isEmpty()) {
            waitEnd = Double.NaN;
        } else if (quitQueue.isPresent()) {
            waitEnd = quitQueue.get().getTime();
        } else if (chargeStart.isPresent()) {
            waitEnd = chargeStart.get().getTime();
        } else {
            waitEnd = Double.NaN; // still queued at end of simulation
        }
        double waitDuration = (Double.isNaN(waitStart) || Double.isNaN(waitEnd))
                ? Double.NaN : waitEnd - waitStart;

        // Charge times
        double chargeStartTime = chargeStart.map(ChargingStartEvent::getTime).orElse(Double.NaN);
        double chargeEndTime   = chargeEnd.map(ChargingEndEvent::getTime).orElse(Double.NaN);
        double chargingDuration = (Double.isNaN(chargeStartTime) || Double.isNaN(chargeEndTime))
                ? Double.NaN : chargeEndTime - chargeStartTime;

        // Energy (Joules → kWh, 1 decimal)
        String energyStr = "NaN";
        if (chargeStart.isPresent() && chargeEnd.isPresent()) {
            double deltaJ = chargeEnd.get().getCharge() - chargeStart.get().getCharge();
            energyStr = String.format("%.1f", deltaJ / 3_600_000.0);
        }

        w.write(chargerId + ";" + vehicleId + ";" + linkId + ";"
                + writeTime(waitStart) + ";" + writeTime(waitEnd) + ";" + writeTime(waitDuration) + ";"
                + writeTime(chargeStartTime) + ";" + writeTime(chargeEndTime) + ";" + writeTime(chargingDuration) + ";"
                + energyStr + ";" + stopType + "\n");
    }

    private static String writeTime(double seconds) {
        return Double.isNaN(seconds) ? "undefined" : Time.writeTime(seconds);
    }

    @Override
    public void reset(int iteration) {
        lastVehicleUsed.clear();
        stopTypesByVehicle.clear();
    }
}
