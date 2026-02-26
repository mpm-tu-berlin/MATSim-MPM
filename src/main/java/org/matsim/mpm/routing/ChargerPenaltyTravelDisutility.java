package org.matsim.mpm.routing;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.population.Person;
import org.matsim.core.router.util.TravelDisutility;
import org.matsim.vehicles.Vehicle;

import java.util.Collections;
import java.util.Map;

/**
 * Mutable {@link TravelDisutility} wrapper that adds expected charger waiting-time penalties
 * to specific charger links. Only the links that an agent would actually use for charging
 * are penalized — not every charger link on the network.
 *
 * <p>The penalty for a charger link is:
 * <pre>expectedWaitSeconds × marginalCostOfTime_s</pre>
 * using the same marginal cost of time as the base disutility, so that one second of expected
 * waiting costs the same as one second of driving in routing.
 *
 * <p>Thread safety: each routing thread gets its own {@link MpmEvNetworkRoutingModule} instance
 * (via {@link MpmEvNetworkRoutingProvider#get()}), and hence its own instance of this class.
 * Routing within a thread is sequential, so mutable state is safe.
 */
final class ChargerPenaltyTravelDisutility implements TravelDisutility {

    private final TravelDisutility base;
    private final double marginalCostOfTime_s;
    /** Wait-time penalty in seconds keyed by charger link id. Empty between routing requests. */
    private Map<Id<Link>, Double> penalties = Collections.emptyMap();

    ChargerPenaltyTravelDisutility(TravelDisutility base, double marginalCostOfTime_s) {
        this.base = base;
        this.marginalCostOfTime_s = marginalCostOfTime_s;
    }

    @Override
    public double getLinkTravelDisutility(Link link, double time, Person person, Vehicle vehicle) {
        double penalty = penalties.getOrDefault(link.getId(), 0.0);
        return base.getLinkTravelDisutility(link, time, person, vehicle)
                + penalty * marginalCostOfTime_s;
    }

    @Override
    public double getLinkMinimumTravelDisutility(Link link) {
        // Lower-bound estimate used by A* heuristics — we don't add the penalty here
        // because the penalty is uncertain and adding it could make the heuristic inadmissible.
        return base.getLinkMinimumTravelDisutility(link);
    }

    /** Set the per-link waiting-time penalties (in seconds) for the next routing call. */
    void setPenalties(Map<Id<Link>, Double> waitSecondsByLink) {
        this.penalties = waitSecondsByLink;
    }

    /** Clear all penalties. Must be called after each two-pass routing cycle. */
    void clearPenalties() {
        this.penalties = Collections.emptyMap();
    }
}
