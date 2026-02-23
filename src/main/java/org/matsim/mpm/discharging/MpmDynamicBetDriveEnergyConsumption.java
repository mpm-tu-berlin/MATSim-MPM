// java
package org.matsim.mpm.discharging;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Node;
import org.matsim.contrib.ev.discharging.DriveEnergyConsumption;

import java.io.PrintWriter;
import java.util.Locale;
import java.util.Objects;

/**
 * Verbrauchsmodell fuer batterie-elektrische Nutzfahrzeuge (BET).
 *
 * Energieberechnung pro Link:
 *   E_total = E_widerstand + E_steigung + Delta_E_kinetisch
 *
 * Alle Werte in SI-Einheiten.
 */
public final class MpmDynamicBetDriveEnergyConsumption implements DriveEnergyConsumption {

    private static final double G = 9.81; // [m/s^2]

    // Fahrzeugparameter
    private final double mSum;             // Masse + Zuladung [kg]
    private final double mInertia;         // Effektive Traegheitsmasse [kg]: mass*cb + payload (cb nur auf Fahrzeugmasse)
    private final double spr;              // Antriebswirkungsgrad [-]
    private final double ft;               // Rollwiderstandsbeiwert [-]
    private final double fa;               // 0.5 * rho * Cd * A [kg/m]

    // Rekuperation
    private final double recupEfficiency;  // Rekuperations-Wirkungsgrad [-]
    private final double maxRecupPowerW;   // Max. Rekuperationsleistung [W]
    private final double maxGradeAbs;      // Max. beruecksichtigte Steigung [-]

    // Motorleistung
    private final double maxMotorPowerW;   // Max. Antriebsleistung [W]

    // Vorberechnete Widerstandsleistung [W] ueber Geschwindigkeit
    private final int maxAvgSpeed;
    private final int stepsPerMps;
    private final double zeroSpeed;
    private final double[] resistPower;

    // Zustand: Geschwindigkeit des vorherigen Links
    private double vPrev = 0.0;

    // Debug-Ausgabe (null = deaktiviert)
    private final PrintWriter debugWriter;
    private final String vehicleId;

    /** CSV-Kopfzeile fuer die Debug-Ausgabe. */
    public static final String DEBUG_CSV_HEADER =
            "vehicleId,linkId,length_m,grade_pct,speed_kmh,travelTime_s," +
            "pRoll_W,pAero_W,pGrav_W,pKin_W,pMechTotal_W,pBattery_W,energy_Wh";

    /**
     * Konstruktor mit Parametern aus vehicleType.
     *
     * @param debugWriter CSV-Writer fuer Widerstandskomponenten pro Link; null = deaktiviert.
     * @param vehicleId   Fahrzeug-ID fuer die Debug-Ausgabe.
     */
    public MpmDynamicBetDriveEnergyConsumption(
            double mass, double payload, double drivetrainEfficiency,
            double rollingC, double cdXA, double inertiaC,
            int maxAvgSpeedMps, int stepsPerMps, double zeroSpeedMps,
            double recupEfficiency, double maxRecupPowerW, double maxGradeAbs,
            double maxMotorPowerW,
            PrintWriter debugWriter, String vehicleId
    ) {
        this.mSum = mass + payload;
        this.mInertia = mass * inertiaC + payload;  // cb nur auf Fahrzeugmasse (rotierende Massen), Payload = 1.0
        this.spr = drivetrainEfficiency;
        this.ft = rollingC;
        this.fa = cdXA;

        this.maxAvgSpeed = maxAvgSpeedMps;
        this.stepsPerMps = stepsPerMps;
        this.zeroSpeed = zeroSpeedMps;

        this.recupEfficiency = recupEfficiency;
        this.maxRecupPowerW = maxRecupPowerW;
        this.maxGradeAbs = maxGradeAbs;
        this.maxMotorPowerW = maxMotorPowerW;

        int size = maxAvgSpeedMps * stepsPerMps + 1;
        this.resistPower = new double[size];
        precomputeResistancePower();

        this.debugWriter = debugWriter;
        this.vehicleId = vehicleId;
    }

    /** Vorberechnung der Widerstandsleistung (Rolling + Aero) pro Geschwindigkeit */
    private void precomputeResistancePower() {
        resistPower[0] = calcResistancePower(zeroSpeed);
        for (int i = 1; i < resistPower.length; i++) {
            double v = (double) i / stepsPerMps;
            resistPower[i] = calcResistancePower(v);
        }
    }

    /** Widerstandsleistung: P_resist = v*(Cr*m*g + 0.5*rho*CdA*v^2)/eta */
    private double calcResistancePower(double v) {
        return v * (ft * mSum * G + fa * v * v) / spr;
    }

    @Override
    public double calcEnergyConsumption(Link link, double travelTime, double linkEnterTime) {
        Objects.requireNonNull(link, "link must not be null");
        if (travelTime <= 0) return 0.0;

        double v = link.getLength() / travelTime; // [m/s]

        // --- 1) Widerstandsleistung mechanisch ---
        double pRoll = ft * mSum * G * v;
        double pAero = fa * v * v * v;
        double pResistMech = pRoll + pAero;

        // --- 2) Steigungsleistung mechanisch ---
        double grade = computeGrade(link);
        double pGravMech = mSum * G * grade * v;  // positiv = bergauf, negativ = bergab

        // --- 3) Kinetische Leistung mechanisch ---
        double dKinEnergy = 0.5 * mInertia * (v * v - vPrev * vPrev);
        double pKinMech = dKinEnergy / travelTime;
        vPrev = v;

        // --- 4) Gesamtmechanische Leistung ---
        double pMechTotal = pResistMech + pGravMech + pKinMech;
        double pBattery;

        if (pMechTotal >= 0.0) {
            // Traktion
            pBattery = pMechTotal / spr;
            if (pBattery > maxMotorPowerW) {
                pBattery = maxMotorPowerW;
            }
        } else {
            // Rekuperation
            pBattery = pMechTotal * recupEfficiency;
            if (pBattery < -maxRecupPowerW) {
                pBattery = -maxRecupPowerW;
            }
        }

        double energyJ = pBattery * travelTime;

        if (debugWriter != null) {
            debugWriter.printf(Locale.US, "%s,%s,%.1f,%.2f,%.2f,%.3f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.4f%n",
                    vehicleId,
                    link.getId(),
                    link.getLength(),
                    grade * 100.0,          // Steigung [%]
                    v * 3.6,               // Geschwindigkeit [km/h]
                    travelTime,            // Fahrzeit [s]
                    pRoll,                 // Rollwiderstandsleistung [W]
                    pAero,                 // Luftwiderstandsleistung [W]
                    pGravMech,             // Steigungsleistung [W]
                    pKinMech,              // Kinetische Leistung [W]
                    pMechTotal,            // Gesamtmech. Leistung [W]
                    pBattery,              // Batterieleistung [W]
                    energyJ / 3600.0       // Energie [Wh]
            );
        }

        return energyJ; // [J]
    }

    /** Berechnet die Steigung des Links */
    private double computeGrade(Link link) {
        double len = link.getLength();
        if (!(len > 0)) return 0.0;

        double zFrom = safeZ(link.getFromNode());
        double zTo = safeZ(link.getToNode());
        if (!Double.isFinite(zFrom) || !Double.isFinite(zTo)) return 0.0;

        double grade = (zTo - zFrom) / len;
        if (grade > maxGradeAbs) grade = maxGradeAbs;
        else if (grade < -maxGradeAbs) grade = -maxGradeAbs;
        return grade;
    }

    /** Sicherer Zugriff auf Node-Z-Koordinaten */
    private static double safeZ(Node n) {
        if (n == null) return Double.NaN;
        Coord c = n.getCoord();
        if (c == null) return Double.NaN;
        double z = c.getZ();
        return Double.isFinite(z) ? z : Double.NaN;
    }
}