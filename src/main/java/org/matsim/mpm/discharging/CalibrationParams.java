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
    private static final double DEF_DRIVETRAIN_EFF = 0.935;
    private static final double DEF_INERTIA_C = 1.05;
    private static final double DEF_RECUP_EFF = 0.6;
    private static final double DEF_MAX_RECUP_POWER_FRACTION = 0.4;

    public final double drivetrainEfficiency;
    public final double inertiaC;
    public final double recupEfficiency;
    /** Anteil der fahrzeugspezifischen RatedPower, der fuer Rekuperation genutzt werden darf [-]. */
    public final double maxRecupPowerFraction;

    public CalibrationParams(double drivetrainEfficiency, double inertiaC,
                             double recupEfficiency, double maxRecupPowerFraction) {
        this.drivetrainEfficiency = drivetrainEfficiency;
        this.inertiaC = inertiaC;
        this.recupEfficiency = recupEfficiency;
        this.maxRecupPowerFraction = maxRecupPowerFraction;
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
        return new CalibrationParams(DEF_DRIVETRAIN_EFF, DEF_INERTIA_C,
                DEF_RECUP_EFF, DEF_MAX_RECUP_POWER_FRACTION);
    }

    private static CalibrationParams fromProperties(Properties props) {
        return new CalibrationParams(
                parseOr(props, "drivetrainEfficiency", DEF_DRIVETRAIN_EFF),
                parseOr(props, "inertiaC", DEF_INERTIA_C),
                parseOr(props, "recupEfficiency", DEF_RECUP_EFF),
                parseOr(props, "maxRecupPowerFraction", DEF_MAX_RECUP_POWER_FRACTION)
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
                "CalibrationParams{drivetrainEff=%.4f, inertiaC=%.4f, recupEff=%.4f, maxRecupPowerFraction=%.3f}",
                drivetrainEfficiency, inertiaC, recupEfficiency, maxRecupPowerFraction);
    }
}
