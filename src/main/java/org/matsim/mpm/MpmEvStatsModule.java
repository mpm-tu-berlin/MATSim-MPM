package org.matsim.mpm;

import com.google.inject.Inject;
import com.google.inject.Singleton;
import org.matsim.contrib.common.timeprofile.ProfileWriter;
import org.matsim.contrib.ev.EvConfigGroup;
import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.charging.ChargingEventSequenceCollector;
import org.matsim.contrib.ev.stats.*;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.MatsimServices;
import org.matsim.core.controler.listener.ControlerListener;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.mpm.stats.ChargerQueuingCollector;

public class MpmEvStatsModule extends AbstractModule {
    @Inject
    private EvConfigGroup evCfg;

    @Override
    public void install() {
        bind(ChargingEventSequenceCollector.class).asEagerSingleton();
        addEventHandlerBinding().to(ChargingEventSequenceCollector.class);
        addControlerListenerBinding().to(ChargingProceduresCSVWriter.class).in(Singleton.class);

        if (evCfg.timeProfiles) {
            installQSimModule(new AbstractQSimModule() {
                @Override
                protected void configureQSim() {
                    addQSimComponentBinding(EvModule.EV_COMPONENT)
                            .toProvider(SocHistogramTimeProfileCollectorProvider.class);
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
