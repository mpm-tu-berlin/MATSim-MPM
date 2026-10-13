// java
package org.matsim.mpm.discharging;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Node;
import org.matsim.contrib.ev.discharging.DriveEnergyConsumption;

import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Locale;
import java.util.Objects;

/**
 * Verbrauchsmodell fuer batterie-elektrische Nutzfahrzeuge (BET).
 *
 * Energieberechnung pro Link:
 *   E_total = E_widerstand + E_steigung + Delta_E_kinetisch
 *
 * Kernidee: Das Modell berechnet fuer jeden Link die physikalisch erreichbare
 * Austrittsgeschwindigkeit v_exit, die sich ergibt, wenn der Motor mit maximaler
 * Leistung betrieben wird. v_exit wird als Einfahrtgeschwindigkeit des naechsten
 * Links verwendet. Dadurch wird eine realistische Beschleunigung ueber mehrere
 * Links hinweg abgebildet, ohne dass kinetische Energie kuenstlich gecappt wird.
 *
 * Bei Verzoegerung wird v_exit = v_target gesetzt (Reibungsbremse immer moeglich).
 * Nicht rekuperable Bremsenergie (> maxRecupPower) geht an die Reibungsbremse.
 *
 * Alle Werte in SI-Einheiten.
 */
public final class MpmDynamicBetDriveEnergyConsumption implements DriveEnergyConsumption {

    private static final double G = 9.81;           // [m/s^2]
    private static final double MIN_SPEED = 0.1;    // [m/s] – Untergrenze zur Vermeidung von Division durch 0

    // Fahrzeugparameter
    private final double mSum;             // Masse + Zuladung [kg]
    private final double mInertia;         // Effektive Traegheitsmasse [kg]: mass*cb + payload (cb nur auf Fahrzeugmasse)
    private final double tractionEfficiency; // Gesamteffizienz Batterie->Rad bei Traktion [-]
    private final double ft;               // Rollwiderstandsbeiwert [-]
    private final double fa;               // 0.5 * rho * Cd * A [kg/m]

    // Rekuperation
    private final double recupEfficiency;  // Rekuperations-Wirkungsgrad [-]
    private final double maxRecupPowerW;   // Max. Rekuperationsleistung [W]
    private final double maxGradeAbs;      // Max. beruecksichtigte Steigung [-]

    // Motorleistung
    private final double maxMotorPowerW;   // Max. Antriebsleistung [W]

    // Fahrzeug-Hoechstgeschwindigkeit
    private final double vehicleMaxSpeedMs; // Max. Fahrzeuggeschwindigkeit [m/s]

    // Zustand: physikalisch erreichte Geschwindigkeit am Ende des letzten Links
    private double vPrev = 0.0;

    // Debug-Ausgabe (debugCsvPath null = deaktiviert, lazy geoeffnet)
    private final Path debugCsvPath;
    private PrintWriter debugWriter;
    private boolean debugWriterInitialized = false;
    private final String vehicleId;

    /** CSV-Kopfzeile fuer die Debug-Ausgabe. */
    public static final String DEBUG_CSV_HEADER =
            "vehicleId,linkId,length_m,grade_pct,vEntry_kmh,vExit_kmh,tPhysical_s," +
            "pRoll_W,pAero_W,pGrav_W,pKin_W,pMechTotal_W,pBattery_W,energy_Wh";

    /**
     * Konstruktor mit Parametern aus vehicleType.
     *
     * @param debugCsvPath Pfad zur Debug-CSV-Datei; null = deaktiviert. Writer wird lazy geoeffnet.
     * @param vehicleId    Fahrzeug-ID fuer die Debug-Ausgabe.
     */
    public MpmDynamicBetDriveEnergyConsumption(
            double mass, double payload, double tractionEfficiency,
            double rollingC, double cdXA, double inertiaC,
            double recupEfficiency, double maxRecupPowerW, double maxGradeAbs,
            double maxMotorPowerW,
            double vehicleMaxSpeedMs,
            Path debugCsvPath, String vehicleId
    ) {
        this.mSum = mass + payload;
        this.mInertia = mass * inertiaC + payload;  // cb nur auf Fahrzeugmasse (rotierende Massen), Payload = 1.0
        this.tractionEfficiency = tractionEfficiency;
        this.ft = rollingC;
        this.fa = cdXA;

        this.recupEfficiency = recupEfficiency;
        this.maxRecupPowerW = maxRecupPowerW;
        this.maxGradeAbs = maxGradeAbs;
        this.maxMotorPowerW = maxMotorPowerW;

        this.vehicleMaxSpeedMs = vehicleMaxSpeedMs;

        this.debugCsvPath = debugCsvPath;
        this.vehicleId = vehicleId;
    }

    @Override
    public double calcEnergyConsumption(Link link, double travelTime, double linkEnterTime) {
        Objects.requireNonNull(link, "link must not be null");
        if (travelTime <= 0) return 0.0;

        double L = link.getLength();
        double grade = computeGrade(link);

        // Rollwiderstand wirkt mit der Normalkraft m*g*cos(theta). grade = sin(theta)
        // (Hoehendifferenz/Linklaenge), daher cos(theta) = sqrt(1 - grade^2). Auf
        // Steigungen reduziert das den Rollwiderstand leicht (flach: ~1).
        double cosGrade = Math.sqrt(1.0 - grade * grade);
        double ftEff = ft * cosGrade;

        // --- 0) Zielgeschwindigkeit: Freispeed des Links, begrenzt durch Dauerleistung bergauf ---
        // fa*v³ + mSum*G*totalResistC*v = maxMotorPowerW*η  (identisch zu PowerLimitedLinkSpeedCalculator)
        // Hinweis: travelTime (QSim-Vorgabe) wird NICHT verwendet – QSim diskretisiert Fahrzeiten
        // auf Vielfache von timeStepSize, was vQSim = L/travelTime verfaelscht (z.B. 49 km/h → 45 km/h).
        double vFreespeed = link.getFreespeed();
        double totalResistC = ftEff + grade;
        double vTarget;
        if (totalResistC > 0.0) {
            double pMechMax = maxMotorPowerW * tractionEfficiency;
            double vMax = solveMaxSpeed(fa, mSum * G * totalResistC, pMechMax);
            vTarget = Math.min(Math.min(vFreespeed, vehicleMaxSpeedMs), vMax);
        } else {
            // Netto-Bergab: keine Traktionsleistungsgrenze
            vTarget = Math.min(vFreespeed, vehicleMaxSpeedMs);
        }

        // --- 1) Einfahrtgeschwindigkeit (physikalisch aus letztem Link) ---
        double v0 = Math.max(vPrev, MIN_SPEED);

        // --- 2) Austrittsgeschwindigkeit ---
        double vExit;
        if (vTarget >= v0) {
            // Beschleunigung: v_exit durch verfuegbare Motorleistung begrenzt.
            // Leistungsanteil fuer Kinetik = Maximalleistung minus Widerstandsleistung.
            // Widerstand bei mittlerer Beschleunigungsgeschwindigkeit auswerten:
            // waehrend der Beschleunigung steigt der Widerstand (Aero ~ v^3); Bewertung bei
            // vRef ueberschaetzt den Widerstand leicht und damit konservativ vExit.
            double vRef = 0.5 * (v0 + Math.min(vTarget, vehicleMaxSpeedMs));
            double pResistRef = ftEff * mSum * G * vRef
                              + fa * vRef * vRef * vRef
                              + mSum * G * grade * vRef;
            double pKinBudget = maxMotorPowerW * tractionEfficiency - pResistRef;

            if (pKinBudget > 0.0) {
                // Konstante-Leistungs-Beschleunigung ueber die Strecke korrekt integriert:
                //   m*v² dv = P_kin ds  =>  vExit³ = v0³ + 3*P_kin*L/m
                // Die fruehere Zeitschaetzung t≈L/v0 ueberschaetzte das Budget bei kleinem v0
                // massiv (1 m bei 1 m/s = 1 s Vollgas) -> unphysikalischer Sprung aus dem
                // Stillstand auf ~20-30 km/h in einem einzigen 1m-Link.
                double vExitCubed = v0 * v0 * v0 + 3.0 * pKinBudget * L / mInertia;
                vExit = Math.min(vTarget, Math.cbrt(vExitCubed));
            } else {
                // Motor ueberwindet kaum die Widerstaende -> keine Beschleunigung moeglich
                vExit = v0;
            }
        } else {
            // Verzoegerung: immer auf Zielgeschwindigkeit moeglich (Reibungsbremse als Fallback)
            vExit = vTarget;
        }

        // --- 3) Physikalische Fahrzeit und mittlere Geschwindigkeit ---
        double vAvg = 0.5 * (v0 + vExit);
        if (vAvg < MIN_SPEED) vAvg = MIN_SPEED;
        double tPhysical = L / vAvg;

        // --- 4) Leistungskomponenten bei mittlerer Geschwindigkeit ---
        // Aero: exakte Integration ueber den Link fuer konstante Beschleunigung.
        // (1/L) * integral v(s)^2 ds = (v0^2 + vExit^2)/2 >= vAvg^2
        // => pAero * tPhysical = fa * (v0^2 + vExit^2)/2 * L (Jensen-korrekt)
        double vSqMean = 0.5 * (v0 * v0 + vExit * vExit);
        double pRoll   = ftEff * mSum * G * vAvg;
        double pAero   = fa * vSqMean * vAvg;
        double pGrav   = mSum * G * grade * vAvg;   // positiv = bergauf, negativ = bergab
        double pKin    = 0.5 * mInertia * (vExit * vExit - v0 * v0) / tPhysical; // nur Debug

        double pMechTotal = pRoll + pAero + pGrav + pKin;                          // nur Debug

        // --- 5) Batterieenergie: gekoppelte Effizienz-Entscheidung ---
        //
        // Die mechanische Energie pendelt pro Link zwischen Widerstand, Hub (Grav) und
        // Tempo (Kinetik). Wuerde die Lade-/Rekup-Effizienz GETRENNT auf den Widerstands-
        // und den kinetischen Anteil angewendet, wird derselbe physikalische Fluss (z.B.
        // Schwerkraft -> Beschleunigung bergab) doppelt besteuert: einmal als Rekuperation
        // (eta_recup) und gleichzeitig als Antrieb (1/eta_traction). Dieser kuenstliche
        // Rundlauf-Verlust skaliert mit der Masse und ueberzeichnet den Steigungseinfluss
        // auf welligem Terrain. Korrekt: EINE Effizienz fuer die NETTO-Mechanikenergie des
        // Links, entschieden ueber deren Vorzeichen (>=0 Antrieb, <0 Rekuperation).
        double pResist = pRoll + pAero + pGrav;
        double deltaKE = 0.5 * mInertia * (vExit * vExit - v0 * v0);

        double eMech = pResist * tPhysical + deltaKE;   // Netto-Mechanikenergie [J]
        double eta = (eMech >= 0.0) ? (1.0 / tractionEfficiency) : recupEfficiency;

        // Widerstandsanteil: zeitbasiert, MIT Leistungsgrenzen (Motor- bzw. Rekup-Cap).
        double pBattResist = pResist * eta;
        if (pBattResist > maxMotorPowerW) pBattResist = maxMotorPowerW;
        else if (pBattResist < -maxRecupPowerW) pBattResist = -maxRecupPowerW;
        double energyResist = pBattResist * tPhysical;

        // Kinetischer Anteil: energiebasiert, OHNE Power-Cap. Bei kurzen Links ist
        // tPhysical (~0.04 s) viel kuerzer als die physikalische Bremszeit (~5-10 s);
        // ein zeitbasierter Cap wuerde Bremsenergie faelschlich als Reibungswaerme werten.
        // Gleiche Effizienz (eta) wie der Widerstandsanteil -> Kopplung ueber eMech.
        double energyKin = deltaKE * eta;

        double energyJ = energyResist + energyKin;
        double pBattery = energyJ / tPhysical;  // nur Debug

        // --- 6) Zustand aktualisieren ---
        vPrev = vExit;

        if (debugCsvPath != null) {
            if (!debugWriterInitialized) {
                debugWriterInitialized = true;
                try {
                    boolean writeHeader = !Files.exists(debugCsvPath);
                    debugWriter = new PrintWriter(Files.newBufferedWriter(debugCsvPath,
                            StandardOpenOption.CREATE, StandardOpenOption.APPEND), true);
                    if (writeHeader) {
                        debugWriter.println(DEBUG_CSV_HEADER);
                    }
                    System.out.println("[MpmDynamic] Debug-CSV geoeffnet: " + debugCsvPath.toAbsolutePath());
                } catch (IOException e) {
                    System.err.println("[MpmDynamic] Konnte Debug-Datei nicht oeffnen: " + e.getMessage());
                }
            }
        }
        if (debugWriter != null) {
            debugWriter.printf(Locale.US, "%s,%s,%.1f,%.2f,%.2f,%.2f,%.3f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.4f%n",
                    vehicleId,
                    link.getId(),
                    L,
                    grade * 100.0,      // Steigung [%]
                    v0 * 3.6,           // Einfahrtgeschwindigkeit [km/h]
                    vExit * 3.6,        // Austrittsgeschwindigkeit [km/h]
                    tPhysical,          // Physikalische Fahrzeit [s]
                    pRoll,              // Rollwiderstandsleistung [W]
                    pAero,              // Luftwiderstandsleistung [W]
                    pGrav,              // Steigungsleistung [W]
                    pKin,               // Kinetische Leistung [W]
                    pMechTotal,         // Gesamtmech. Leistung [W]
                    pBattery,           // Batterieleistung [W]
                    energyJ / 3600.0    // Energie [Wh]
            );
        }

        return energyJ; // [J]
    }

    /**
     * Loest fa*v³ + b*v − p = 0 nach v > 0 mittels Newton-Raphson.
     * Identische Implementierung wie in PowerLimitedLinkSpeedCalculator.
     * Konvergiert typisch in 3–5 Iterationen.
     *
     * @param fa  aerodynamischer Koeffizient [kg/m]
     * @param b   linearer Widerstandsterm = mSum * G * totalResistC [N/(m/s)]
     * @param p   verfuegbare mechanische Leistung [W]
     * @return    maximale Gleichgewichtsgeschwindigkeit [m/s]
     */
    private static double solveMaxSpeed(double fa, double b, double p) {
        double v = Math.cbrt(p / fa);          // Startwert: kubische Naeherung
        for (int i = 0; i < 50; i++) {
            double f  = fa * v * v * v + b * v - p;
            double df = 3.0 * fa * v * v + b;
            double dv = f / df;
            v -= dv;
            if (Math.abs(dv) < 1e-4) break;   // Konvergenz: < 0.1 mm/s
        }
        return Math.max(v, 0.0);
    }

    /**
     * Lastabhaengiger effektiver Rollwiderstandsbeiwert (VECTO-Form).
     *
     * VECTO korrigiert den deklarierten RRC je Achse fuer die tatsaechliche
     * Radlast relativ zur ISO-Pruflast:
     *   RRC_eff = RRC_decl * (Fz / Fz_ISO)^(beta - 1),  beta = 0.9
     * (Reg. (EU) 2017/2400 Annex X / VECTO-Doku Gl. 7; Fz_ISO nach ISO 28580 =
     * 85 % der maximalen Reifentragfaehigkeit; verifiziert ueber KTH-Thesis
     * diva2:1218050, S. 23 f., die die VECTO-Gleichung samt beta=0.9 zitiert.
     * Physikbasis: Michelin-Patent US 9,481,415 (Crr ~ z^-0.1); SAE J2452
     * (F_RR = P^alpha * Z^beta * (a + bV + cV^2)).)
     *
     * Hier die Fahrzeug-Ebene mit gebuendelter Gesamtlast (eine "Achse"):
     * die Last geht als Gesamtmasse ein, der Exponent wirkt auf m/m_ref
     * (g kuerzt sich; VECTO wendet die Korrektur auf m*g an, NICHT auf die
     * Normalkraft m*g*cos(theta) — cos kommt erst in der Kraftgleichung dazu).
     * Dadurch ist das Ergebnis je Fahrzeug konstant und wird einmal im
     * Factory-Aufbau berechnet: kein Mehraufwand im Link-Hot-Path.
     * Fuer eine spaetere achsaufgeloeste Erweiterung dieselbe Funktion je
     * Achse mit deren Last/Referenzlast aufrufen und lastgewichtet summieren.
     *
     * @param rollingCRef  Beiwert bei Referenzmasse [-]
     * @param totalMassKg  aktuelle Gesamtmasse (Leermasse + Zuladung) [kg]
     * @param refMassKg    Masse, bei der rollingCRef gilt [kg]
     * @param loadExponent beta; 1.0 reproduziert konstantes c_r bitidentisch
     */
    public static double effectiveRollingC(double rollingCRef, double totalMassKg,
                                           double refMassKg, double loadExponent) {
        if (loadExponent == 1.0 || !(refMassKg > 0.0) || !(totalMassKg > 0.0)) {
            return rollingCRef;
        }
        return rollingCRef * Math.pow(totalMassKg / refMassKg, loadExponent - 1.0);
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
