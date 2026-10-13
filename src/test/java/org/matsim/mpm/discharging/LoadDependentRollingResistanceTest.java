package org.matsim.mpm.discharging;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.matsim.mpm.discharging.MpmDynamicBetDriveEnergyConsumption.effectiveRollingC;

/**
 * Abnahmekriterien der lastabhaengigen Rollwiderstands-Korrektur (VECTO-Form,
 * s. Doku von {@link MpmDynamicBetDriveEnergyConsumption#effectiveRollingC}).
 */
public class LoadDependentRollingResistanceTest {

    private static final double C_R_REF = 0.004574812564082316; // kalibrierter A(all)-Wert
    private static final double M_REF = 35_500.0;

    /** beta = 1.0 muss das bisherige konstante c_r BITIDENTISCH reproduzieren. */
    @Test
    public void beta1IsBitIdentical() {
        for (double m : new double[]{12_000, 18_000, 19_000, 35_500, 40_000, 43_000}) {
            assertEquals(C_R_REF, effectiveRollingC(C_R_REF, m, M_REF, 1.0), 0.0);
        }
        // Defaults = Option 1 (User 2026-08-18): VECTO-Lastkorrektur und
        // VECTO-Deklarationsluftdichte sind das neue Standardverhalten
        CalibrationParams def = CalibrationParams.defaults();
        assertEquals(0.9, def.rollingLoadExponent, 0.0);
        assertEquals(1.188, def.airDensity, 0.0);
        assertEquals(1.0, def.maxRecupPowerFraction, 0.0);
    }

    /** 18 t -> 40 t (Faktor ~2.2 Last) senkt c_r um 2.2^(-0.1) ~= -8 %. */
    @Test
    public void sanityLoadRatio() {
        double c18 = effectiveRollingC(C_R_REF, 18_000, M_REF, 0.9);
        double c40 = effectiveRollingC(C_R_REF, 40_000, M_REF, 0.9);
        double expected = Math.pow(40.0 / 18.0, -0.1);
        assertEquals(expected, c40 / c18, 1e-12);
        // ~ -8 % laut Task-Spezifikation (2.2^-0.1 = 0.924)
        assertEquals(0.924, c40 / c18, 0.005);
    }

    /** Leerfahrzeug unterhalb m_ref bekommt ein HOEHERES c_r als c_r_ref. */
    @Test
    public void emptyAboveReference() {
        assertTrue(effectiveRollingC(C_R_REF, 19_000, M_REF, 0.9) > C_R_REF);
        // ... und Volllast oberhalb m_ref ein niedrigeres
        assertTrue(effectiveRollingC(C_R_REF, 43_000, M_REF, 0.9) < C_R_REF);
        // an der Referenzmasse exakt der Referenzwert
        assertEquals(C_R_REF, effectiveRollingC(C_R_REF, M_REF, M_REF, 0.9), 1e-18);
    }

    /** Ungueltige Referenz-/Gesamtmasse faellt sicher auf den Referenzwert zurueck. */
    @Test
    public void degenerateInputsFallBack() {
        assertEquals(C_R_REF, effectiveRollingC(C_R_REF, 19_000, 0.0, 0.9), 0.0);
        assertEquals(C_R_REF, effectiveRollingC(C_R_REF, 0.0, M_REF, 0.9), 0.0);
        assertEquals(C_R_REF, effectiveRollingC(C_R_REF, 19_000, Double.NaN, 0.9), 0.0);
    }
}
