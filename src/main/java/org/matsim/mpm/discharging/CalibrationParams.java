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
    private static final double DEF_MAX_RECUP_POWER_FRACTION = 0.9;
    private static final double DEF_AUX_POWER_W = 4_500.0;

    /** Gesamteffizienz Batterie → Rad (Umrichter + Motor + Getriebe) bei Traktion [-]. */
    public final double tractionEfficiency;
    /** Gesamteffizienz Rad → Batterie (Getriebe + Generator + Umrichter) bei Rekuperation [-]. */
    public final double recupEfficiency;
    public final double inertiaC;
    /** Anteil der fahrzeugspezifischen RatedPower, der fuer Rekuperation genutzt werden darf [-]. */
    public final double maxRecupPowerFraction;
    /** Konstante Nebenverbrauchsleistung [W] (gilt fuer alle Fahrzeuge der Gruppe). */
    public final double auxPowerW;

    public CalibrationParams(double tractionEfficiency, double inertiaC,
                             double recupEfficiency, double maxRecupPowerFraction,
                             double auxPowerW) {
        this.tractionEfficiency = tractionEfficiency;
        this.inertiaC = inertiaC;
        this.recupEfficiency = recupEfficiency;
        this.maxRecupPowerFraction = maxRecupPowerFraction;
        this.auxPowerW = auxPowerW;
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
                DEF_RECUP_EFF, DEF_MAX_RECUP_POWER_FRACTION, DEF_AUX_POWER_W);
    }

    private static CalibrationParams fromProperties(Properties props) {
        return new CalibrationParams(
                parseOr(props, "tractionEfficiency", DEF_TRACTION_EFF),
                parseOr(props, "inertiaC", DEF_INERTIA_C),
                parseOr(props, "recupEfficiency", DEF_RECUP_EFF),
                parseOr(props, "maxRecupPowerFraction", DEF_MAX_RECUP_POWER_FRACTION),
                parseOr(props, "auxPowerW", DEF_AUX_POWER_W)
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
                "CalibrationParams{tractionEff=%.4f, inertiaC=%.4f, recupEff=%.4f, maxRecupPowerFraction=%.3f, auxPowerW=%.0f}",
                tractionEfficiency, inertiaC, recupEfficiency, maxRecupPowerFraction, auxPowerW);
    }
}
