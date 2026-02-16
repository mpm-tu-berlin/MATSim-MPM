package org.matsim.mpm.scoring;

import org.matsim.api.core.v01.population.Person;
import org.matsim.core.scoring.SumScoringFunction;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

/**
 * Custom scoring function that adds waiting time penalties for charging queue waiting.
 * Reads the accumulated waiting score from the shared {@link ChargingQueueWaitingScoringHandler}.
 */
public class ChargingWaitingScoringFunction implements SumScoringFunction.BasicScoring {

    private static final Logger log = LogManager.getLogger(ChargingWaitingScoringFunction.class);

    private final Person person;
    private final ChargingQueueWaitingScoringHandler waitingHandler;

    public ChargingWaitingScoringFunction(Person person, ChargingQueueWaitingScoringHandler waitingHandler) {
        this.person = person;
        this.waitingHandler = waitingHandler;
    }

    @Override
    public void finish() {
        double score = getScore();
        if (score != 0.0) {
            log.info("Person {} charging waiting score: {} utils", person.getId(), score);
        }
    }

    @Override
    public double getScore() {
        return waitingHandler.getPersonWaitingScore(person.getId().toString());
    }

}
