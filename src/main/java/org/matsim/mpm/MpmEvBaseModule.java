package org.matsim.mpm;

import org.matsim.contrib.ev.EvModule;
import org.matsim.contrib.ev.charging.ChargingModule;
import org.matsim.contrib.ev.fleet.ElectricFleetModule;
import org.matsim.contrib.ev.infrastructure.ChargingInfrastructureModule;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.mobsim.qsim.components.QSimComponentsConfigGroup;
import org.matsim.mpm.discharging.MpmDischargingModule;

public class MpmEvBaseModule extends AbstractModule {
    public void install(){
        install(new ElectricFleetModule() );
        install(new ChargingInfrastructureModule() );
        install(new ChargingModule() );
        install(new MpmDischargingModule() );
        install(new MpmEvStatsModule() );
        {
            // this switches on all the QSimComponents that are registered at various places under EvModule.EV_Component.
            ConfigUtils.addOrGetModule( this.getConfig(), QSimComponentsConfigGroup.class ).addActiveComponent( EvModule.EV_COMPONENT );
        }
    }

}
