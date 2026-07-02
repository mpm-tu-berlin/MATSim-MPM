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

import org.matsim.api.core.v01.network.Link;
import org.matsim.api.core.v01.network.Network;
import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.discharging.*;
import org.matsim.contrib.ev.temperature.TemperatureService;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.mpm.PowerLimitedLinkSpeedCalculator;
import org.matsim.vehicles.Vehicle;

import com.google.inject.Provider;
import com.google.inject.Singleton;
import com.google.inject.name.Names;

import java.nio.file.Path;

/**
 * @author Michal Maciejewski (michalm)
 */
public final class MpmDischargingModule extends AbstractModule {

    /** Named-Binding fuer die Debug-freie Verbrauchs-Factory des EV-Routers (C1). */
    public static final String ROUTING_FACTORY = "mpmRoutingConsumption";

    @Override
    public void install() {
        // Kalibrierungsparameter einmalig laden (aus Datei oder Standardwerte)
        CalibrationParams calib = CalibrationParams.loadOrDefault();
        System.out.println("[MpmDischargingModule] " + calib);

        // Debug-CSV-Pfad im Output-Verzeichnis (wird lazy geoeffnet, da
        // OutputDirectoryHierarchy das Verzeichnis beim Start loescht/neu anlegt)
        String outputDir = getConfig().controller().getOutputDirectory();
        Path debugCsvPath = Path.of(outputDir).resolve("resistance_debug.csv");

        // QSim-Factory schreibt die Debug-CSV. Die Routing-Factory (Named-Binding,
        // injiziert in MpmEvNetworkRoutingProvider) ist physikalisch identisch,
        // aber OHNE CSV -> Router-Schaetzrows kontaminieren die Debug-Datei nicht
        // mehr mit identischer vehicleId (C1).
        bind(DriveEnergyConsumption.Factory.class)
                .toInstance(consumptionFactory(calib, debugCsvPath));
        bind(DriveEnergyConsumption.Factory.class)
                .annotatedWith(Names.named(ROUTING_FACTORY))
                .toInstance(consumptionFactory(calib, null));

        bind(TemperatureService.class).toInstance(linkId -> 15);// XXX fixed temperature 15 oC
        // Nebenverbrauch: kalibrierte Konstantleistung P_aux [W] fuer alle Fahrzeuge der Gruppe.
        // Entspricht dem VECTO-Modell: E_aux = P_aux * t (direkt von der Batterie).
        //
        // WICHTIG: Wir verwenden die *physikalische* Linkdurchfahrtzeit
        //   tPhysical = L / min(freespeed, vehicleMaxSpeed)
        // statt der von der QSim gemeldeten duration. Letztere ist auf
        // Vielfache von qsim.timeStepSize aufgerundet (1m-Link bei 23 m/s,
        // timestep=0.04s: 0.043s -> 0.08s, ~86 % Aux-Ueberschaetzung) und
        // wuerde den Aux-Anteil resolutions- und timestep-abhaengig machen.
        // Konsistent zu MpmDynamicBetDriveEnergyConsumption, das ebenfalls
        // tPhysical aus Linklaenge und Geschwindigkeit ableitet.
        //
        // Lambda-Signatur ist (beginTime, duration, linkId) -> Joule.
        // Network wird via Guice-Provider injiziert, damit der Link aus der
        // linkId rekonstruierbar ist.
        Provider<Network> networkProvider = binder().getProvider(Network.class);
        bind(AuxEnergyConsumption.Factory.class).toInstance(ev -> {
            final Network network = networkProvider.get();
            final double vehicleMaxSpeedMs =
                    ev.getVehicleSpecification().getMatsimVehicle().getType().getMaximumVelocity();
            return (beginTime, duration, linkId) -> {
                Link link = network.getLinks().get(linkId);
                if (link == null) {
                    // Fallback: ohne Linkinfo nehmen wir die QSim-duration.
                    return calib.auxPowerW * duration;
                }
                double v = Math.min(link.getFreespeed(), vehicleMaxSpeedMs);
                if (v <= 0.0) v = 1e-3; // numerische Absicherung
                double tPhysical = link.getLength() / v;
                return calib.auxPowerW * tPhysical; // Joule
            };
        });

        installQSimModule(new AbstractQSimModule() {
            @Override
            protected void configureQSim() {
                // Leistungsbegrenzte Hoechstgeschwindigkeit pro Link:
                // v = min(freespeed, v_power_limited). MUSS in der QSim per
                // Multibinder registriert werden — das fruehere Controler-Binding
                // (bind(LinkSpeedCalculator.class)) kam dort nie an (C2).
                this.addLinkSpeedCalculator().toInstance(new PowerLimitedLinkSpeedCalculator(calib));

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

    /**
     * Baut die Verbrauchs-Factory. debugCsvPath null = keine Debug-CSV und kein
     * Vehicle-Logging (Routing-Variante); sonst identische Physik.
     */
    private static DriveEnergyConsumption.Factory consumptionFactory(
            CalibrationParams calib, Path debugCsvPath) {
        return ev -> {
            // Fahrzeugspezifische Parameter aus Vehicle-Type-Attributen lesen
            Vehicle vehicle = ev.getVehicleSpecification().getMatsimVehicle();
            var attrs = vehicle.getType().getEngineInformation().getAttributes();

            double mass = attrOrDefault(attrs, "mass", 12_000.0);
            double payload = attrOrDefault(attrs, "payload", 0.0);
            // cdXA/rollingC: kalibrierter Wert hat Vorrang, sonst Fahrzeugwert aus vehicles.xml.
            double cdXA = Double.isFinite(calib.cdXA)
                    ? calib.cdXA : attrOrDefault(attrs, "cdXA", 5.5);  // Cd*A [m^2]
            double rollingC = Double.isFinite(calib.rollingC)
                    ? calib.rollingC : attrOrDefault(attrs, "rollingC", 0.01);
            double maxMotorPowerW = attrOrDefault(attrs, "maxMotorPowerW", 400_000.0);

            // CdxA (m^2) -> aerodynamischer Kraftbeiwert: F_aero = fa * v^2
            // fa = 0.5 * rho_Luft * CdxA   (rho = 1.225 kg/m^3 bei 15 C, 1013 hPa)
            double aeroFa = 0.5 * 1.225 * cdXA;

            // Max. Rekuperationsleistung: fahrzeugspezifische RatedPower * kalibrierter Anteil
            double maxRecupPowerW = calib.maxRecupPowerFraction * maxMotorPowerW;

            // Fahrzeug-Hoechstgeschwindigkeit aus VehicleType (identisch zu QSim-Limit)
            double vehicleMaxSpeedMs = vehicle.getType().getMaximumVelocity();
            if (debugCsvPath != null) {
                System.out.printf("[MpmDischargingModule] vehicleId=%s  vehicleTypeId=%s  maximumVelocity=%.4f m/s (%.2f km/h)%n",
                        ev.getId(), vehicle.getType().getId(), vehicleMaxSpeedMs, vehicleMaxSpeedMs * 3.6);
            }

            return new MpmDynamicBetDriveEnergyConsumption(
                    mass, payload, calib.tractionEfficiency, rollingC, aeroFa,
                    calib.inertiaC,
                    calib.recupEfficiency, maxRecupPowerW, 0.15,
                    maxMotorPowerW,
                    vehicleMaxSpeedMs,
                    debugCsvPath, ev.getId().toString()
            );
        };
    }

    /** Liest ein Double-Attribut oder gibt den Standardwert zurueck. */
    private static double attrOrDefault(org.matsim.utils.objectattributes.attributable.Attributes attrs,
                                        String name, double defaultValue) {
        Object val = attrs.getAttribute(name);
        if (val instanceof Number) return ((Number) val).doubleValue();
        return defaultValue;
    }

}
