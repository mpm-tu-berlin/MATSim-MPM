package org.matsim.mpm.discharging;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.api.core.v01.network.Node;
import org.matsim.contrib.ev.discharging.DriveEnergyConsumption;
import org.matsim.core.network.NetworkUtils;

import java.util.Locale;

/**
 * Mikrobenchmark: Kosten pro calcEnergyConsumption-Aufruf (ns/Link-Event)
 * fuer das dynamische Modell vs. das Fixed-Rate-Modell.
 *
 * Kein JUnit-Test (laeuft nicht im CI-verify); Start ueber die IDE
 * (Run 'EnergyModelBenchmark.main()') oder nach mvnw.cmd test-compile
 * mit dem Test-Classpath.
 *
 * Methodik: synthetische Kette aus 1000 Links (250 m, Steigungszyklus
 * -4..+4 %, Freespeed 80 km/h), sequentiell durchlaufen wie eine Route.
 * 5 Messrunden a 2 Mio. Aufrufe nach Warmup; Median der Runden.
 * Parameter = kalibrierte Defaults aus CalibrationParams.
 */
public final class EnergyModelBenchmark {

    private static final int N_LINKS = 1000;
    private static final long CALLS_PER_ROUND = 2_000_000L;
    private static final int ROUNDS = 5;

    public static void main(String[] args) {
        Link[] links = buildChain();
        CalibrationParams cal = CalibrationParams.defaults();
        double ratedPowerW = 400_000.0;

        DriveEnergyConsumption dynamic = new MpmDynamicBetDriveEnergyConsumption(
                19_000.0,                            // mass [kg]
                6_000.0,                             // payload [kg]
                cal.tractionEfficiency,
                0.0055,                              // rollingC
                3.6,                                 // 0.5*rho*Cd*A [kg/m]
                cal.inertiaC,
                cal.recupEfficiency,
                cal.maxRecupPowerFraction * ratedPowerW,
                0.10,                                // maxGradeAbs
                ratedPowerW,
                85.0 / 3.6,                          // vehicleMaxSpeed [m/s]
                null, "bench");
        DriveEnergyConsumption fixed = new BetDriveEnergyConsumption();

        // Warmup (JIT)
        runRound(dynamic, links, 500_000L);
        runRound(fixed, links, 500_000L);

        double nsDyn = median(measure(dynamic, links));
        double nsFix = median(measure(fixed, links));

        System.out.printf(Locale.US, "%nDynamisches Modell: %6.1f ns/Link-Event%n", nsDyn);
        System.out.printf(Locale.US, "Fixed-Rate-Modell:  %6.1f ns/Link-Event%n", nsFix);
        System.out.printf(Locale.US, "Mehraufwand:        %6.1f ns/Link-Event%n", nsDyn - nsFix);
        // 100-km-Trip auf 250-m-Netz = 400 Link-Events
        System.out.printf(Locale.US, "pro 100-km-Trip (400 Links): %.3f ms vs. %.3f ms%n",
                nsDyn * 400 / 1e6, nsFix * 400 / 1e6);
    }

    private static double[] measure(DriveEnergyConsumption model, Link[] links) {
        double[] nsPerCall = new double[ROUNDS];
        for (int r = 0; r < ROUNDS; r++) {
            long t0 = System.nanoTime();
            double blackhole = runRound(model, links, CALLS_PER_ROUND);
            long t1 = System.nanoTime();
            nsPerCall[r] = (t1 - t0) / (double) CALLS_PER_ROUND;
            System.out.printf(Locale.US, "%s Runde %d: %.1f ns/Aufruf (Kontrollsumme %.3e)%n",
                    model.getClass().getSimpleName(), r + 1, nsPerCall[r], blackhole);
        }
        return nsPerCall;
    }

    private static double runRound(DriveEnergyConsumption model, Link[] links, long calls) {
        double sum = 0.0;
        int i = 0;
        for (long c = 0; c < calls; c++) {
            Link link = links[i];
            double travelTime = link.getLength() / link.getFreespeed();
            sum += model.calcEnergyConsumption(link, travelTime, c);
            i++;
            if (i == links.length) i = 0;
        }
        return sum;
    }

    private static double median(double[] v) {
        double[] s = v.clone();
        java.util.Arrays.sort(s);
        return s[s.length / 2];
    }

    /** Kette aus N_LINKS Links a 250 m mit Steigungen -4..+4 % (z-Koordinaten). */
    private static Link[] buildChain() {
        Network net = NetworkUtils.createNetwork();
        double length = 250.0;
        Link[] links = new Link[N_LINKS];
        double z = 0.0;
        Node prev = NetworkUtils.createAndAddNode(net, Id.createNodeId("n0"),
                new Coord(0.0, 0.0, 0.0));
        for (int i = 0; i < N_LINKS; i++) {
            // Steigungszyklus -4, -2, 0, +2, +4 % -> beide Effizienzzweige + Newton-Solver
            double grade = ((i % 5) - 2) * 0.02;
            z += grade * length;
            Node next = NetworkUtils.createAndAddNode(net, Id.createNodeId("n" + (i + 1)),
                    new Coord((i + 1) * length, 0.0, z));
            links[i] = NetworkUtils.createAndAddLink(net, Id.createLinkId("l" + i),
                    prev, next, length, 80.0 / 3.6, 2000.0, 1.0);
            prev = next;
        }
        return links;
    }

    private EnergyModelBenchmark() {
    }
}
