package org.matsim.mpm.scoring;

import com.google.inject.Inject;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.population.Person;
import org.matsim.core.scoring.ScoringFunction;
import org.matsim.core.scoring.ScoringFunctionFactory;
import org.matsim.core.scoring.SumScoringFunction;
import org.matsim.core.scoring.functions.*;

/**
 * Custom scoring function factory that adds waiting time penalties to the standard scoring.
 */
public class ChargingWaitingScoringFunctionFactory implements ScoringFunctionFactory {

    private final ScoringParametersForPerson scoringParametersForPerson;
    private final Network network;
    private final ChargingQueueWaitingScoringHandler waitingHandler;

    @Inject
    public ChargingWaitingScoringFunctionFactory(ScoringParametersForPerson scoringParametersForPerson,
                                                  Network network,
                                                  ChargingQueueWaitingScoringHandler waitingHandler) {
        this.scoringParametersForPerson = scoringParametersForPerson;
        this.network = network;
        this.waitingHandler = waitingHandler;
    }

    @Override
    public ScoringFunction createNewScoringFunction(Person person) {
        SumScoringFunction sumScoringFunction = new SumScoringFunction();

        // Get scoring parameters for this person
        final ScoringParameters params = scoringParametersForPerson.getScoringParameters(person);

        // Add standard MATSim scoring components
        sumScoringFunction.addScoringFunction(new CharyparNagelActivityScoring(params));
        sumScoringFunction.addScoringFunction(new CharyparNagelLegScoring(params, network));
        sumScoringFunction.addScoringFunction(new CharyparNagelAgentStuckScoring(params));
        sumScoringFunction.addScoringFunction(new CharyparNagelMoneyScoring(params));

        // Add charging waiting time scoring (reads from shared handler)
        sumScoringFunction.addScoringFunction(new ChargingWaitingScoringFunction(person, waitingHandler));

        return sumScoringFunction;
    }
}
