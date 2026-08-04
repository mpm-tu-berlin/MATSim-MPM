package org.matsim.mpm;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Node;
import org.matsim.core.mobsim.qsim.qnetsimengine.QVehicle;
import org.matsim.core.mobsim.qsim.qnetsimengine.linkspeedcalculator.LinkSpeedCalculator;
import org.matsim.mpm.discharging.CalibrationParams;
import org.matsim.utils.objectattributes.attributable.Attributes;

/**
 * Begrenzt die Fahrgeschwindigkeit auf einem Link auf die physikalisch maximal
 * erreichbare Leistungsgeschwindigkeit:
 *
 *   v_link = min(v_freespeed, v_power_limited)
 *
 * v_power_limited ist die stationaere Hoechstgeschwindigkeit, bei der die
 * verfuegbare mechanische Motorleistung (nach Antriebsstrangverlusten) gerade
 * ausreicht, um Roll-, Steigungswiderstand und Luftwiderstand zu ueberwinden.
 *
 * Gleichgewichtsbedingung bei konstanter Geschwindigkeit v auf einem Link
 * mit Laengsneigung g = (z_to - z_from) / L:
 *
 *   P_mech_max = fa * v^3  +  m * g * (cr * cos(theta) + grade) * v
 *
 * wobei:
 *   fa          = 0.5 * rho_Luft * CdA   [kg/m]
 *   P_mech_max  = maxMotorPowerW * tractionEfficiency   [W]
 *
 * Die kubische Gleichung   fa*v^3 + b*v - P = 0   wird mit Newton-Raphson
 * geloest (typisch 3-5 Iterationen).
 *
 * Fahrzeugparameter werden aus den VehicleType-Attributen gelesen (selbe
 * Attribute wie in MpmDischargingModule):
 *   mass, payload, cdXA, rollingC, maxMotorPowerW
 *
 * Der Kalibrierungsparameter tractionEfficiency aus CalibrationParams
 * skaliert die verfuegbare mechanische Leistung und ist damit direkt in
 * die leistungsbegrenzte Geschwindigkeit eingebunden.
 */
public final class PowerLimitedLinkSpeedCalculator implements LinkSpeedCalculator {

    private static final double G = 9.81;          // Erdbeschleunigung [m/s²]
    private static final double RHO_AIR = 1.225;   // Luftdichte bei 15 °C, 1013 hPa [kg/m³]
    private static final double MAX_GRADE = 0.15;  // Maximale beruecksichtigte Steigung [-]

    private final CalibrationParams calib;

    public PowerLimitedLinkSpeedCalculator(CalibrationParams calib) {
        this.calib = calib;
    }

    @Override
    public double getMaximumVelocity(QVehicle vehicle, Link link, double time) {
        double freespeed = link.getFreespeed(time);

        // --- Fahrzeugparameter aus VehicleType-Attributen ---
        Attributes attrs = vehicle.getVehicle().getType().getEngineInformation().getAttributes();
        double mass           = attrOrDefault(attrs, "mass",           12_000.0);
        double payload        = attrOrDefault(attrs, "payload",             0.0);
        // cdXA/rollingC: kalibrierter Wert hat Vorrang, sonst Fahrzeugwert aus vehicles.xml.
        double cdXA           = Double.isFinite(calib.cdXA)
                ? calib.cdXA : attrOrDefault(attrs, "cdXA", 5.5);
        double rollingC       = Double.isFinite(calib.rollingC)
                ? calib.rollingC : attrOrDefault(attrs, "rollingC", 0.01);
        double maxMotorPowerW = attrOrDefault(attrs, "maxMotorPowerW", 400_000.0);

        double mSum     = mass + payload;
        double fa       = 0.5 * RHO_AIR * cdXA;                         // aerodyn. Koeffizient [kg/m]
        double pMechMax = maxMotorPowerW * calib.tractionEfficiency;   // max. mech. Leistung [W]

        // --- Steigung: sin(α) ≈ Δh / L (Kleinwinkelnaeherung, wie im Verbrauchsmodell) ---
        double grade         = computeGrade(link);
        // Rollwiderstand wirkt mit der Normalkraft m*g*cos(theta); grade = sin(theta)
        // => cos(theta) = sqrt(1 - grade^2). Identisch zum Verbrauchsmodell
        // (MpmDynamicBetDriveEnergyConsumption), damit beide Klassen dieselbe
        // Gleichgewichtsgeschwindigkeit liefern.
        double cosGrade      = Math.sqrt(1.0 - grade * grade);
        double totalResistC  = rollingC * cosGrade + grade;  // effektiver Gesamtwiderstandsbeiwert

        // Bergab ohne positiven Kraftbedarf: Motorleistung ist nicht limitierend
        if (totalResistC <= 0.0) {
            return freespeed;
        }

        // --- v_power_limited loesen: fa*v³ + mSum*G*totalResistC*v - pMechMax = 0 ---
        double vPowerLimited = solveMaxSpeed(fa, mSum * G * totalResistC, pMechMax);
        return Math.min(freespeed, vPowerLimited);
    }

    /**
     * Loest fa*v³ + b*v − P = 0 nach v > 0 mittels Newton-Raphson.
     *
     * Startwert: aerodynamisch dominierter Term (kubische Wurzel), konvergiert
     * in 3–5 Iterationen fuer alle physikalisch sinnvollen Eingaben (fa, b, P > 0).
     *
     * @param fa  aerodynamischer Koeffizient [kg/m]
     * @param b   linearer Widerstandsterm = mSum * G * totalResistC [N/(m/s)]
     * @param p   verfuegbare mechanische Leistung [W]
     * @return maximale Gleichgewichtsgeschwindigkeit [m/s]
     */
    private static double solveMaxSpeed(double fa, double b, double p) {
        double v = Math.cbrt(p / fa);  // Startwert
        for (int i = 0; i < 50; i++) {
            double f  = fa * v * v * v + b * v - p;
            double df = 3.0 * fa * v * v + b;
            double dv = f / df;
            v -= dv;
            if (Math.abs(dv) < 1e-4) break;  // Konvergenz: < 0.1 mm/s
        }
        return Math.max(v, 0.0);
    }

    /** Berechnet die Laengsneigung des Links (sin α ≈ Δh / L), begrenzt auf ±MAX_GRADE. */
    private static double computeGrade(Link link) {
        double len = link.getLength();
        if (!(len > 0)) return 0.0;
        double zFrom = safeZ(link.getFromNode());
        double zTo   = safeZ(link.getToNode());
        if (!Double.isFinite(zFrom) || !Double.isFinite(zTo)) return 0.0;
        double grade = (zTo - zFrom) / len;
        if (grade >  MAX_GRADE) return  MAX_GRADE;
        if (grade < -MAX_GRADE) return -MAX_GRADE;
        return grade;
    }

    private static double safeZ(Node n) {
        if (n == null) return Double.NaN;
        Coord c = n.getCoord();
        if (c == null) return Double.NaN;
        double z = c.getZ();
        return Double.isFinite(z) ? z : Double.NaN;
    }

    private static double attrOrDefault(Attributes attrs, String name, double defaultValue) {
        Object val = attrs.getAttribute(name);
        if (val instanceof Number) return ((Number) val).doubleValue();
        return defaultValue;
    }
}
