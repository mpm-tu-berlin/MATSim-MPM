/* *********************************************************************** *
 * project: org.matsim.*												   *
 *                                                                         *
 * *********************************************************************** *
 *                                                                         *
 * copyright       : (C) 2008 by the members listed in the COPYING,        *
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
 * *********************************************************************** */
package org.matsim.mpm.run;

import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy.OverwriteFileSetting;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.contrib.ev.charging.ChargeUpToMaxSocStrategy;
import org.matsim.contrib.ev.charging.ChargingLogic;
import org.matsim.core.api.experimental.events.EventsManager;
import org.matsim.core.scoring.ScoringFunctionFactory;
import org.matsim.mpm.MpmEvModule;
import org.matsim.mpm.charging.HoldUntilLeaveChargingLogic;
import org.matsim.mpm.charging.RejectIfFullChargingLogic;
import org.matsim.mpm.routing.MpmEvNetworkRoutingProvider;
import org.matsim.mpm.scoring.ChargingWaitingScoringFunctionFactory;

import com.google.inject.Inject;
import com.google.inject.Provider;

/**
 * @author nagel
 *
 */
public class RunBetScenario {

	public static void main(String[] args) {

		Config config;
		if ( args==null || args.length==0 || args[0]==null ){
			config = ConfigUtils.loadConfig( "scenarios/BETs/1pct_BETs_unlimited_deutschlandnetz/config.xml" );
			//config = ConfigUtils.loadConfig( "scenarios/BETs/10_BETs_test/config.xml" );
		} else {
			config = ConfigUtils.loadConfig( args );
		}

		config.controller().setOverwriteFileSetting( OverwriteFileSetting.deleteDirectoryIfExists );

		// possibly modify config here
		config.addModule(new org.matsim.contrib.ev.EvConfigGroup());
		config.addModule(new org.matsim.mpm.routing.MpmRoutingConfigGroup());


		Scenario scenario = ScenarioUtils.loadScenario(config) ;

		// possibly modify scenario here
		// ---
		
		Controler controler = new Controler( scenario ) ;

		// possibly modify controler here
		controler.addOverridingModule(new AbstractModule(){

			@Override public void install(){
				install( new MpmEvModule() );
				addRoutingModuleBinding(TransportMode.car).toProvider(new MpmEvNetworkRoutingProvider(TransportMode.car));
				// Bind custom scoring factory (Guice injects the ChargingQueueWaitingScoringHandler singleton)
				bind(ScoringFunctionFactory.class).to(ChargingWaitingScoringFunctionFactory.class);
			}
		} );

		// Override ChargingLogic to keep charger occupied until vehicle leaves the charging activity
		controler.addOverridingModule(new AbstractModule(){
			@Override public void install(){
				bind(ChargingLogic.Factory.class).toProvider(new Provider<>() {
					@Inject private EventsManager eventsManager;
					@Override public ChargingLogic.Factory get() {
						return charger -> {
							if ("DC_slow".equals(charger.getChargerType())) {
								// REST-stop chargers: reject if full, no queuing
								return new RejectIfFullChargingLogic(charger,
										new ChargeUpToMaxSocStrategy(charger, 1.), eventsManager);
							} else {
								// All other chargers: hold until vehicle leaves (queuing enabled)
								return new HoldUntilLeaveChargingLogic(charger,
										new ChargeUpToMaxSocStrategy(charger, 1.), eventsManager);
							}
						};
					}
				});
			}
		} );

		controler.run();
	}

}
