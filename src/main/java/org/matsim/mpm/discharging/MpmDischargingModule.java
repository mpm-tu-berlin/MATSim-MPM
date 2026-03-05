/*
 * *********************************************************************** *
 * project: org.matsim.*
 * *********************************************************************** *
 *                                                                         *
 * copyright       : (C) 2019 by the members listed in the COPYING,        *
 *                   LICENSE and WARRANTY file.                            *
 * email           : info at matsim dot org                                *
 *                                                                         *
 * *********************************************************************** *
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *   See also COPYING, LICENSE and WARRANTY file                           *
 *                                                                         *
 * *********************************************************************** *
 */

package org.matsim.mpm.discharging;

import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.discharging.*;
import org.matsim.contrib.ev.temperature.TemperatureService;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.core.mobsim.qsim.qnetsimengine.linkspeedcalculator.LinkSpeedCalculator;
import org.matsim.mpm.PowerLimitedLinkSpeedCalculator;
import org.matsim.vehicles.Vehicle;

import com.google.inject.Singleton;

import java.nio.file.Path;

/**
 * @author Michal Maciejewski (michalm)
 */
public final class MpmDischargingModule extends AbstractModule {

    @Override
    public void install() {
        // Kalibrierungsparameter einmalig laden (aus Datei oder Standardwerte)
        CalibrationParams calib = CalibrationParams.loadOrDefault();
        System.out.println("[MpmDischargingModule] " + calib);

        // Debug-CSV-Pfad im Output-Verzeichnis (wird lazy geoeffnet, da
        // OutputDirectoryHierarchy das Verzeichnis beim Start loescht/neu anlegt)
        String outputDir = getConfig().controller().getOutputDirectory();
        Path debugCsvPath = Path.of(outputDir).resolve("resistance_debug.csv");

        bind(DriveEnergyConsumption.Factory.class).toInstance(ev -> {
            // Fahrzeugspezifische Parameter aus Vehicle-Type-Attributen lesen
            Vehicle vehicle = ev.getVehicleSpecification().getMatsimVehicle();
            var attrs = vehicle.getType().getEngineInformation().getAttributes();

            double mass = attrOrDefault(attrs, "mass", 12_000.0);
            double payload = attrOrDefault(attrs, "payload", 0.0);
            double cdXA = attrOrDefault(attrs, "cdXA", 5.5);  // Cd*A [m^2] aus VECTO
            double rollingC = attrOrDefault(attrs, "rollingC", 0.01);
            double maxMotorPowerW = attrOrDefault(attrs, "maxMotorPowerW", 400_000.0);

            // CdxA (m^2) -> aerodynamischer Kraftbeiwert: F_aero = fa * v^2
            // fa = 0.5 * rho_Luft * CdxA   (rho = 1.225 kg/m^3 bei 15 C, 1013 hPa)
            double aeroFa = 0.5 * 1.225 * cdXA;

            // Max. Rekuperationsleistung: fahrzeugspezifische RatedPower * kalibrierter Anteil
            double maxRecupPowerW = calib.maxRecupPowerFraction * maxMotorPowerW;

            // Fahrzeug-Hoechstgeschwindigkeit aus VehicleType (identisch zu QSim-Limit)
            double vehicleMaxSpeedMs = vehicle.getType().getMaximumVelocity();
            System.out.printf("[MpmDischargingModule] vehicleId=%s  vehicleTypeId=%s  maximumVelocity=%.4f m/s (%.2f km/h)%n",
                    ev.getId(), vehicle.getType().getId(), vehicleMaxSpeedMs, vehicleMaxSpeedMs * 3.6);

            return new MpmDynamicBetDriveEnergyConsumption(
                    mass, payload, calib.tractionEfficiency, rollingC, aeroFa,
                    calib.inertiaC,
                    calib.recupEfficiency, maxRecupPowerW, 0.15,
                    maxMotorPowerW,
                    vehicleMaxSpeedMs,
                    debugCsvPath, ev.getId().toString()
            );
        });
        // Leistungsbegrenzte Hoechstgeschwindigkeit pro Link: v = min(freespeed, v_power_limited)
        bind(LinkSpeedCalculator.class).toInstance(new PowerLimitedLinkSpeedCalculator(calib));

        bind(TemperatureService.class).toInstance(linkId -> 15);// XXX fixed temperature 15 oC
        // Nebenverbrauch: kalibrierte Konstantleistung P_aux [W] fuer alle Fahrzeuge der Gruppe.
        // Entspricht dem VECTO-Modell: E_aux = P_aux * t (direkt von der Batterie).
        bind(AuxEnergyConsumption.Factory.class).toInstance(
                ev -> (link, travelTime, linkEnterTime) -> calib.auxPowerW * travelTime // Joule
        );

        installQSimModule(new AbstractQSimModule() {
            @Override
            protected void configureQSim() {
                this.bind(DriveDischargingHandler.class).in(Singleton.class);
                addMobsimScopeEventHandlerBinding().to(DriveDischargingHandler.class);
                this.addQSimComponentBinding(EvModule.EV_COMPONENT).to(DriveDischargingHandler.class);
                // event handlers are not qsim components

                this.bind(IdleDischargingHandler.class).in(Singleton.class);
                addMobsimScopeEventHandlerBinding().to(IdleDischargingHandler.class);
                this.addQSimComponentBinding(EvModule.EV_COMPONENT).to(IdleDischargingHandler.class);

                //by default, no vehicle will be AUX-discharged when not moving
                this.bind(IdleDischargingHandler.VehicleProvider.class).toInstance(event -> null);
            }
        });
    }

    /** Liest ein Double-Attribut oder gibt den Standardwert zurueck. */
    private static double attrOrDefault(org.matsim.utils.objectattributes.attributable.Attributes attrs,
                                        String name, double defaultValue) {
        Object val = attrs.getAttribute(name);
        if (val instanceof Number) return ((Number) val).doubleValue();
        return defaultValue;
    }

}
