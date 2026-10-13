package org.matsim.mpm;

import com.google.inject.Inject;
import com.google.inject.Singleton;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.charging.ChargingEventSequenceCollector;
import org.matsim.contrib.ev.stats.*;
import org.matsim.mpm.stats.MpmSocHistogramTimeProfileCollectorProvider;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.mpm.scoring.ChargingQueueWaitingScoringHandler;
import org.matsim.mpm.stats.ChargerQueuingCollector;
import org.matsim.mpm.stats.ChargerWaitingTimeTracker;
import org.matsim.mpm.stats.MpmChargingProceduresCSVWriter;
import org.matsim.mpm.stats.RouteDetourTracker;

public class MpmEvStatsModule extends AbstractModule {
    @Inject
    private EvConfigGroup evCfg;

    @Override
    public void install() {
        bind(ChargingEventSequenceCollector.class).asEagerSingleton();
        addEventHandlerBinding().to(ChargingEventSequenceCollector.class);
        bind(MpmChargingProceduresCSVWriter.class).asEagerSingleton();
        addEventHandlerBinding().to(MpmChargingProceduresCSVWriter.class);
        addControlerListenerBinding().to(MpmChargingProceduresCSVWriter.class);


        // NOTE: ScoringFunctionFactory is set directly in RunBetScenario via setScoringFunctionFactory()
        // to ensure it's not overridden by other modules

        // Add waiting time tracking handler for statistics (CSV export)
        bind(ChargingQueueWaitingScoringHandler.class).asEagerSingleton();
        addEventHandlerBinding().to(ChargingQueueWaitingScoringHandler.class);
        addControlerListenerBinding().to(ChargingQueueWaitingScoringHandler.class);

        // Per-charger waiting time tracker for congestion-aware routing
        bind(ChargerWaitingTimeTracker.class).asEagerSingleton();
        addEventHandlerBinding().to(ChargerWaitingTimeTracker.class);
        addControlerListenerBinding().to(ChargerWaitingTimeTracker.class);

        // Per-person route detour tracker (basic vs staged route + waiting time)
        bind(RouteDetourTracker.class).asEagerSingleton();
        addEventHandlerBinding().to(RouteDetourTracker.class);
        addControlerListenerBinding().to(RouteDetourTracker.class);


        if (evCfg.timeProfiles) {
            installQSimModule(new AbstractQSimModule() {
                @Override
                protected void configureQSim() {
                    addQSimComponentBinding(EvModule.EV_COMPONENT)
                            .toProvider(MpmSocHistogramTimeProfileCollectorProvider.class);
                    addQSimComponentBinding(EvModule.EV_COMPONENT)
                            .toProvider(IndividualChargeTimeProfileCollectorProvider.class);
                    addQSimComponentBinding(EvModule.EV_COMPONENT)
                            .toProvider(ChargerOccupancyTimeProfileCollectorProvider.class);
                    addQSimComponentBinding(EvModule.EV_COMPONENT).to(ChargerOccupancyXYDataCollector.class)
                            .asEagerSingleton();
                    addQSimComponentBinding(EvModule.EV_COMPONENT)
                            .toProvider(VehicleTypeAggregatedChargeTimeProfileCollectorProvider.class);

                    bind(EnergyConsumptionCollector.class).asEagerSingleton();
                    addMobsimScopeEventHandlerBinding().to(EnergyConsumptionCollector.class);
                    addQSimComponentBinding(EvModule.EV_COMPONENT).to(EnergyConsumptionCollector.class);

                    bind(ChargerQueuingCollector.class).asEagerSingleton();
                    addMobsimScopeEventHandlerBinding().to(ChargerQueuingCollector.class);
                    // add more time profiles or collectors if necessary
                }
            });
            /*bind(ChargerPowerTimeProfileCalculator.class).asEagerSingleton();
            addEventHandlerBinding().to(ChargerPowerTimeProfileCalculator.class);
            addControlerListenerBinding().toProvider(new Provider<>() {
                @Inject
                private ChargerPowerTimeProfileCalculator calculator;
                @Inject
                private MatsimServices matsimServices;

                @Override
                public ControlerListener get() {
                    var profileView = new ChargerPowerTimeProfileView(calculator);
                    return new ProfileWriter(matsimServices, "ev", profileView, "charger_power_time_profiles");

                }
            });*/
        }
    }
}
