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
import org.matsim.mpm.MpmEvModule;
import org.matsim.mpm.routing.MpmEvNetworkRoutingProvider;

/**
 * @author nagel
 *
 */
public class RunBetScenario {

	public static void main(String[] args) {

		Config config;
		if ( args==null || args.length==0 || args[0]==null ){
			config = ConfigUtils.loadConfig( "scenarios/BETs/1.0pctBETs_1Iteration_unlimited/config.xml" );
		} else {
			config = ConfigUtils.loadConfig( args );
		}

		config.controller().setOverwriteFileSetting( OverwriteFileSetting.deleteDirectoryIfExists );

		// possibly modify config here
		config.addModule(new org.matsim.contrib.ev.EvConfigGroup());


		Scenario scenario = ScenarioUtils.loadScenario(config) ;

		// possibly modify scenario here
		// ---
		
		Controler controler = new Controler( scenario ) ;
		
		// possibly modify controler here
		controler.addOverridingModule(new AbstractModule(){

			@Override public void install(){
				install( new MpmEvModule() );
				addRoutingModuleBinding(TransportMode.car).toProvider(new MpmEvNetworkRoutingProvider(TransportMode.car));
			}
		} );
		
		controler.run();
	}
	
}
