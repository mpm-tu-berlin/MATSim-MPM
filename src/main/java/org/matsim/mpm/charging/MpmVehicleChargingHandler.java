package org.matsim.mpm.charging;

import com.google.common.base.Preconditions;
import com.google.common.collect.ImmutableListMultimap;
import com.google.inject.Inject;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.events.ActivityEndEvent;
import org.matsim.api.core.v01.events.ActivityStartEvent;
import org.matsim.api.core.v01.events.PersonLeavesVehicleEvent;
import org.matsim.api.core.v01.events.handler.ActivityEndEventHandler;
import org.matsim.api.core.v01.events.handler.ActivityStartEventHandler;
import org.matsim.api.core.v01.events.handler.PersonLeavesVehicleEventHandler;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.population.Activity;
import org.matsim.api.core.v01.population.Person;
import org.matsim.api.core.v01.population.PlanElement;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.charging.ChargingEndEvent;
import org.matsim.contrib.ev.charging.ChargingEndEventHandler;
import org.matsim.contrib.ev.charging.ChargingStartEvent;
import org.matsim.contrib.ev.charging.ChargingStartEventHandler;
import org.matsim.contrib.ev.charging.QueuedAtChargerEvent;
import org.matsim.contrib.ev.charging.QueuedAtChargerEventHandler;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEvent;
import org.matsim.contrib.ev.charging.QuitQueueAtChargerEventHandler;
import org.matsim.contrib.ev.charging.VehicleChargingHandler;
import org.matsim.contrib.ev.fleet.ElectricFleet;
import org.matsim.contrib.ev.fleet.ElectricVehicle;
import org.matsim.contrib.ev.infrastructure.Charger;
import org.matsim.contrib.ev.infrastructure.ChargingInfrastructure;
import org.matsim.core.events.MobsimScopeEventHandler;
import org.matsim.core.mobsim.framework.MobsimAgent;
import org.matsim.core.mobsim.framework.events.MobsimBeforeSimStepEvent;
import org.matsim.core.mobsim.framework.listeners.MobsimBeforeSimStepListener;
import org.matsim.core.mobsim.qsim.QSim;
import org.matsim.core.mobsim.qsim.agents.WithinDayAgentUtils;
import org.matsim.vehicles.Vehicle;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Replacement for {@link VehicleChargingHandler} that selects chargers deterministically
 * based on the charger type encoded in the stage-activity type.
 *
 * <p>Activity type convention:
 * <ul>
 *   <li>{@code "car DC_fast charging interaction"} → select a DC_fast charger</li>
 *   <li>{@code "car DC_slow charging interaction"} → select a DC_slow charger</li>
 *   <li>{@code "car charging interaction"} (legacy) → use any compatible charger</li>
 * </ul>
 *
 * <p>This avoids the non-deterministic {@code findAny()} in {@link VehicleChargingHandler}
 * that causes REST-stop trucks to randomly occupy DC_fast charger slots when both charger
 * types coexist on the same network link.
 */
public class MpmVehicleChargingHandler
        implements ActivityStartEventHandler, ActivityEndEventHandler,
        PersonLeavesVehicleEventHandler,
        QueuedAtChargerEventHandler, ChargingStartEventHandler,
        ChargingEndEventHandler, QuitQueueAtChargerEventHandler,
        MobsimBeforeSimStepListener, MobsimScopeEventHandler {

    /** Suffix that identifies charging stage activities (includes leading space). */
    private static final String CHARGING_SUFFIX = VehicleChargingHandler.CHARGING_INTERACTION;

    private final Map<Id<Vehicle>, Id<Person>> lastDriver = new HashMap<>();
    private final Map<Id<Person>, Id<Vehicle>> lastVehicleUsed = new HashMap<>();
    private final Map<Id<Vehicle>, Id<Charger>> vehiclesAtChargers = new HashMap<>();
    private final Set<Id<Person>> agentsInChargerQueue = ConcurrentHashMap.newKeySet();

    private final ChargingInfrastructure chargingInfrastructure;
    private final ElectricFleet electricFleet;
    private final ImmutableListMultimap<Id<Link>, Charger> chargersAtLinks;
    private final EvConfigGroup evCfg;

    @Inject
    public MpmVehicleChargingHandler(ChargingInfrastructure chargingInfrastructure,
                                     ElectricFleet electricFleet,
                                     EvConfigGroup evCfg) {
        this.chargingInfrastructure = chargingInfrastructure;
        this.electricFleet = electricFleet;
        this.evCfg = evCfg;
        ImmutableListMultimap.Builder<Id<Link>, Charger> builder = ImmutableListMultimap.builder();
        for (Charger c : chargingInfrastructure.getChargers().values()) {
            builder.put(c.getLink().getId(), c);
        }
        this.chargersAtLinks = builder.build();
    }

    /**
     * Maps the stop reason encoded in the activity type to the required charger type.
     *
     * <p>Activity type → charger type:
     * <ul>
     *   <li>"car Energy charging interaction" → "DC_fast"</li>
     *   <li>"car Timing charging interaction" → "DC_fast"</li>
     *   <li>"car Rest charging interaction"   → "DC_slow"</li>
     *   <li>"car DC_fast/DC_slow ..." (legacy) → literal charger type (backward compat)</li>
     *   <li>"car charging interaction" (legacy) → "" (findAny fallback)</li>
     * </ul>
     */
    private static String extractRequiredChargerType(String actType) {
        int modeEnd = actType.indexOf(' ');
        if (modeEnd < 0) return "";
        // strip " charging interaction" suffix: "car Energy charging interaction" -> "car Energy"
        String withoutSuffix = actType.substring(0, actType.length() - CHARGING_SUFFIX.length());
        // if nothing remains after the mode prefix, no charger type is specified
        if (withoutSuffix.length() <= modeEnd) return "";
        String token = withoutSuffix.substring(modeEnd + 1); // "Energy", "Timing", "Rest", "DC_fast", ...
        return switch (token) {
            case "Rest"             -> "DC_slow";
            case "Energy", "Timing" -> "DC_fast";
            default                 -> token; // legacy: literal charger type (DC_fast / DC_slow)
        };
    }

    @Override
    public void handleEvent(ActivityStartEvent event) {
        if (!event.getActType().endsWith(CHARGING_SUFFIX)) return;
        Id<Vehicle> vehicleId = lastVehicleUsed.get(event.getPersonId());
        if (vehicleId == null) return;
        Id<Vehicle> evId = Id.create(vehicleId, Vehicle.class);
        if (!electricFleet.getElectricVehicles().containsKey(evId)) return;
        ElectricVehicle ev = electricFleet.getElectricVehicles().get(evId);
        List<Charger> chargers = chargersAtLinks.get(event.getLinkId());
        String requiredType = extractRequiredChargerType(event.getActType());
        Charger c;
        if (requiredType.isEmpty()) {
            // Legacy / fallback: pick any compatible charger type
            c = chargers.stream()
                    .filter(ch -> ev.getChargerTypes().contains(ch.getChargerType()))
                    .findAny()
                    .orElseThrow(() -> new IllegalStateException(
                            "No compatible charger at link " + event.getLinkId() + " for vehicle " + evId));
        } else {
            c = chargers.stream()
                    .filter(ch -> requiredType.equals(ch.getChargerType()))
                    .findFirst()
                    .orElseThrow(() -> new IllegalStateException(
                            "No charger of type '" + requiredType + "' at link " + event.getLinkId()));
        }
        c.getLogic().addVehicle(ev, event.getTime());
        vehiclesAtChargers.put(evId, c.getId());
    }

    @Override
    public void handleEvent(ActivityEndEvent event) {
        if (!event.getActType().endsWith(CHARGING_SUFFIX)) return;
        Id<Vehicle> vehicleId = lastVehicleUsed.get(event.getPersonId());
        if (vehicleId == null) return;
        Id<Vehicle> evId = Id.create(vehicleId, Vehicle.class);
        Id<Charger> chargerId = vehiclesAtChargers.remove(evId);
        if (chargerId != null) {
            Charger c = chargingInfrastructure.getChargers().get(chargerId);
            c.getLogic().removeVehicle(electricFleet.getElectricVehicles().get(evId), event.getTime());
        }
    }

    @Override
    public void handleEvent(PersonLeavesVehicleEvent event) {
        lastDriver.put(event.getVehicleId(), event.getPersonId());
        lastVehicleUsed.put(event.getPersonId(), event.getVehicleId());
    }

    @Override
    public void handleEvent(QueuedAtChargerEvent event) {
        Id<Person> personId = lastDriver.get(event.getVehicleId());
        if (personId != null) agentsInChargerQueue.add(personId);
    }

    @Override
    public void handleEvent(ChargingStartEvent event) {
        Id<Person> personId = lastDriver.get(event.getVehicleId());
        if (personId != null) agentsInChargerQueue.remove(personId);
    }

    @Override
    public void handleEvent(ChargingEndEvent event) {
        // nothing needed
    }

    @Override
    public void handleEvent(QuitQueueAtChargerEvent event) {
        if (evCfg.enforceChargingInteractionDuration) {
            throw new RuntimeException(
                    "QuitQueueAtChargerEvent fired while enforceChargingInteractionDuration=true: " + event);
        }
        Id<Person> personId = lastDriver.get(event.getVehicleId());
        if (personId != null) agentsInChargerQueue.remove(personId);
    }

    @Override
    public void notifyMobsimBeforeSimStep(MobsimBeforeSimStepEvent e) {
        QSim qsim = (QSim) e.getQueueSimulation();
        for (Id<Person> agentId : agentsInChargerQueue) {
            MobsimAgent mobsimAgent = qsim.getAgents().get(agentId);
            PlanElement currentPlanElement = WithinDayAgentUtils.getCurrentPlanElement(mobsimAgent);
            if (currentPlanElement instanceof Activity act) {
                Preconditions.checkState(act.getType().endsWith(CHARGING_SUFFIX),
                        "Agent %s in charger queue but current activity is not a charging interaction: %s",
                        agentId, act.getType());
                WithinDayAgentUtils.resetCaches(mobsimAgent);
                WithinDayAgentUtils.rescheduleActivityEnd(mobsimAgent, qsim);
            } else {
                throw new IllegalStateException(
                        "Agent " + agentId + " in charger queue but not at an activity: " + currentPlanElement);
            }
        }
    }
}
