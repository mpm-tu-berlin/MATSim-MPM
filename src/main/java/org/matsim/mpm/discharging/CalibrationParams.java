package org.matsim.mpm.discharging;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

/**
 * Kalibrierungsparameter, die von Optuna optimiert werden.
 * Werden aus einer .properties-Datei gelesen; Fallback auf Standardwerte.
 */
public final class CalibrationParams {

    private static final String DEFAULT_FILE = "calibration_params.properties";

    // Standardwerte
    private static final double DEF_TRACTION_EFF = 0.935;
    private static final double DEF_INERTIA_C = 1.05;
    private static final double DEF_RECUP_EFF = 0.6;
    // f_rec=1.0: User-Entscheid Option 1 (2026-08-18) — Kappung kostet auf den
    // Kalibrierstrecken nichts (Median 0 %, max 1,85 % @250 m) und macht
    // inertiaC ueber Beladungen konsistent.
    private static final double DEF_MAX_RECUP_POWER_FRACTION = 1.0;
    private static final double DEF_AUX_POWER_W = 4_000.0;
    // cdXA/rollingC: Sentinel NaN = "nicht kalibriert" -> Verbraucher fallen auf den
    // fahrzeugspezifischen Wert aus vehicles.xml zurueck. Erst wenn Optuna sie in die
    // Params-Datei schreibt, ueberschreiben sie den Fahrzeugwert (gruppenweit).
    private static final double DEF_CDXA = Double.NaN;
    private static final double DEF_ROLLING_C = Double.NaN;
    // Lastabhaengiger Rollwiderstand (VECTO-Form, s. MpmDynamicBetDriveEnergyConsumption
    // .effectiveRollingC): Default 0.9 = VECTO-Lastkorrektur (User-Entscheid
    // Option 1, 2026-08-18; Modellselektion RB/all 0.753 % vs. RA/all 0.857 %).
    // Exponent 1.0 stellt das alte konstante c_r bitidentisch wieder her.
    // Referenzmasse = Masse, bei der rollingC gilt; Default 35.5 t =
    // LH-repraesentativ-Kalibrieranker (nicht die ISO-28580-Reifenlast).
    private static final double DEF_ROLLING_LOAD_EXPONENT = 0.9;
    private static final double DEF_ROLLING_REF_MASS_KG = 35_500.0;
    // Luftdichte fuer F_aero = 0.5*rho*CdxA*v^2. Default 1.188 kg/m3 = VECTO-
    // Deklarationsbedingung (trockene Luft, 100 kPa, +20 C; Option 1 2026-08-18).
    // 1.225 (15 C, 1013 hPa) stellt das Verhalten vor der Umstellung wieder her;
    // damals absorbierte das gefittete cdXA den 3.1-%-Unterschied.
    private static final double DEF_AIR_DENSITY = 1.188;

    /** Gesamteffizienz Batterie → Rad (Umrichter + Motor + Getriebe) bei Traktion [-]. */
    public final double tractionEfficiency;
    /** Gesamteffizienz Rad → Batterie (Getriebe + Generator + Umrichter) bei Rekuperation [-]. */
    public final double recupEfficiency;
    public final double inertiaC;
    /** Anteil der fahrzeugspezifischen RatedPower, der fuer Rekuperation genutzt werden darf [-]. */
    public final double maxRecupPowerFraction;
    /** Konstante Nebenverbrauchsleistung [W] (gilt fuer alle Fahrzeuge der Gruppe). */
    public final double auxPowerW;
    /** Luftwiderstand CdxA [m²]; NaN = nicht kalibriert (Wert aus vehicles.xml verwenden). */
    public final double cdXA;
    /** Rollwiderstandsbeiwert [-]; NaN = nicht kalibriert (Wert aus vehicles.xml verwenden). */
    public final double rollingC;
    /** Last-Exponent beta der Rollwiderstands-Korrektur [-]; 1.0 = konstantes c_r. */
    public final double rollingLoadExponent;
    /** Gesamtmasse [kg], bei der rollingC gilt (Kalibrieranker). */
    public final double rollingRefMassKg;
    /** Luftdichte [kg/m3] fuer den Aero-Term (VECTO-Deklarationsbedingung: 1.188). */
    public final double airDensity;

    public CalibrationParams(double tractionEfficiency, double inertiaC,
                             double recupEfficiency, double maxRecupPowerFraction,
                             double auxPowerW, double cdXA, double rollingC,
                             double rollingLoadExponent, double rollingRefMassKg,
                             double airDensity) {
        this.tractionEfficiency = tractionEfficiency;
        this.inertiaC = inertiaC;
        this.recupEfficiency = recupEfficiency;
        this.maxRecupPowerFraction = maxRecupPowerFraction;
        this.auxPowerW = auxPowerW;
        this.cdXA = cdXA;
        this.rollingC = rollingC;
        this.rollingLoadExponent = rollingLoadExponent;
        this.rollingRefMassKg = rollingRefMassKg;
        this.airDensity = airDensity;
    }

    /**
     * Laedt Parameter aus einer .properties-Datei.
     * Fehlende Eintraege werden mit Standardwerten aufgefuellt.
     */
    public static CalibrationParams load(Path path) {
        Properties props = new Properties();
        try (Reader reader = Files.newBufferedReader(path)) {
            props.load(reader);
        } catch (IOException e) {
            throw new RuntimeException("Kalibrierungsparameter nicht lesbar: " + path, e);
        }
        return fromProperties(props);
    }

    /**
     * Laedt aus der System-Property "calibration.params.file" oder dem Standardpfad.
     * Wenn keine Datei gefunden wird, werden Standardwerte verwendet.
     */
    public static CalibrationParams loadOrDefault() {
        String fileProp = System.getProperty("calibration.params.file", DEFAULT_FILE);
        Path path = Path.of(fileProp);
        if (Files.exists(path)) {
            System.out.println("[CalibrationParams] Lade Kalibrierungsparameter aus: " + path.toAbsolutePath());
            return load(path);
        }
        System.out.println("[CalibrationParams] Keine Parameterdatei gefunden, nutze Standardwerte.");
        return defaults();
    }

    public static CalibrationParams defaults() {
        return new CalibrationParams(DEF_TRACTION_EFF, DEF_INERTIA_C,
                DEF_RECUP_EFF, DEF_MAX_RECUP_POWER_FRACTION, DEF_AUX_POWER_W,
                DEF_CDXA, DEF_ROLLING_C,
                DEF_ROLLING_LOAD_EXPONENT, DEF_ROLLING_REF_MASS_KG, DEF_AIR_DENSITY);
    }

    private static CalibrationParams fromProperties(Properties props) {
        return new CalibrationParams(
                parseOr(props, "tractionEfficiency", DEF_TRACTION_EFF),
                parseOr(props, "inertiaC", DEF_INERTIA_C),
                parseOr(props, "recupEfficiency", DEF_RECUP_EFF),
                parseOr(props, "maxRecupPowerFraction", DEF_MAX_RECUP_POWER_FRACTION),
                parseOr(props, "auxPowerW", DEF_AUX_POWER_W),
                parseOr(props, "cdXA", DEF_CDXA),
                parseOr(props, "rollingC", DEF_ROLLING_C),
                parseOr(props, "rollingLoadExponent", DEF_ROLLING_LOAD_EXPONENT),
                parseOr(props, "rollingRefMassKg", DEF_ROLLING_REF_MASS_KG),
                parseOr(props, "airDensity", DEF_AIR_DENSITY)
        );
    }

    private static double parseOr(Properties props, String key, double defaultValue) {
        String val = props.getProperty(key);
        if (val == null || val.isBlank()) return defaultValue;
        return Double.parseDouble(val.trim());
    }

    @Override
    public String toString() {
        return String.format(
                "CalibrationParams{tractionEff=%.4f, inertiaC=%.4f, recupEff=%.4f, maxRecupPowerFraction=%.3f, auxPowerW=%.0f, cdXA=%.4f, rollingC=%.5f, rollingLoadExp=%.2f, rollingRefMassKg=%.0f, airDensity=%.3f}",
                tractionEfficiency, inertiaC, recupEfficiency, maxRecupPowerFraction, auxPowerW, cdXA, rollingC,
                rollingLoadExponent, rollingRefMassKg, airDensity);
    }
}
