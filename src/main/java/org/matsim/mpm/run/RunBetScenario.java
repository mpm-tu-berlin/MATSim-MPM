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

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.TransportMode;
import org.matsim.api.core.v01.population.Activity;
import org.matsim.api.core.v01.population.Person;
import org.matsim.api.core.v01.population.Plan;
import org.matsim.api.core.v01.population.PlanElement;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.AbstractModule;
import org.matsim.core.controler.Controler;
import org.matsim.core.controler.OutputDirectoryHierarchy.OverwriteFileSetting;
import org.matsim.core.router.StageActivityTypeIdentifier;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.core.utils.io.IOUtils;
import org.matsim.core.utils.misc.Time;
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

import javax.xml.stream.XMLInputFactory;
import javax.xml.stream.XMLStreamConstants;
import javax.xml.stream.XMLStreamReader;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * @author nagel
 *
 */
public class RunBetScenario {

	private static final Logger log = LogManager.getLogger(RunBetScenario.class);

	public static void main(String[] args) {

		Config config;
		if ( args==null || args.length==0 || args[0]==null ){
			//config = ConfigUtils.loadConfig( "scenarios/BETs/1pct_BETs_unlimited_deutschlandnetz/config.xml" );
			config = ConfigUtils.loadConfig( "scenarios/BETs/tests/10_BETs_test/config.xml" );
		} else {
			config = ConfigUtils.loadConfig( args );
		}

		config.controller().setOverwriteFileSetting( OverwriteFileSetting.deleteDirectoryIfExists );

		// possibly modify config here
		config.addModule(new org.matsim.contrib.ev.EvConfigGroup());
		config.addModule(new org.matsim.mpm.routing.MpmRoutingConfigGroup());


		Scenario scenario = ScenarioUtils.loadScenario(config) ;

		// Fix activity durations lost during XML round-trip (MATSim core bug in PopulationReaderMatsimV6:
		// stage activities with max_dur > 0 are converted from InteractionActivity to ActivityImpl,
		// but the duration is copied from InteractionActivity which hardcodes it to 0).
		repairActivityDurations(scenario);

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

	/**
	 * Fixes activity durations lost during XML deserialization.
	 *
	 * <p>MATSim's PopulationReaderMatsimV6 has a bug: stage activities (types ending in
	 * "interaction") with {@code max_dur > 0} are converted from InteractionActivity to
	 * ActivityImpl, but the duration is copied from the InteractionActivity (which hardcodes
	 * it to 0) instead of from the XML attributes.  This method re-parses the plans XML to
	 * recover the correct durations and applies them to the loaded population.
	 */
	private static void repairActivityDurations(Scenario scenario) {
		// Step 1: try attribute-based repair (for plans produced with mpm_duration attribute)
		int repairedFromAttr = 0;
		for (Person person : scenario.getPopulation().getPersons().values()) {
			for (Plan plan : person.getPlans()) {
				for (PlanElement pe : plan.getPlanElements()) {
					if (pe instanceof Activity act) {
						Object attr = act.getAttributes().getAttribute("mpm_duration");
						if (attr instanceof Number dur) {
							double seconds = dur.doubleValue();
							if (seconds > 0 && (!act.getMaximumDuration().isDefined()
									|| act.getMaximumDuration().seconds() < 1.0)) {
								act.setMaximumDuration(seconds);
								repairedFromAttr++;
							}
						}
					}
				}
			}
		}
		if (repairedFromAttr > 0) {
			log.info("Repaired {} activity durations from 'mpm_duration' attribute.", repairedFromAttr);
		}

		// Step 2: check if any stage activities need repair (maxDuration lost = 0 or undefined)
		boolean needsXmlRepair = false;
		for (Person person : scenario.getPopulation().getPersons().values()) {
			for (Plan plan : person.getPlans()) {
				for (PlanElement pe : plan.getPlanElements()) {
					if (pe instanceof Activity act
							&& StageActivityTypeIdentifier.isStageActivity(act.getType())
							&& (!act.getMaximumDuration().isDefined()
								|| act.getMaximumDuration().seconds() < 1.0)) {
						needsXmlRepair = true;
						break;
					}
				}
				if (needsXmlRepair) break;
			}
			if (needsXmlRepair) break;
		}
		if (!needsXmlRepair) return;

		// Step 3: re-parse the plans XML to extract correct max_dur values
		String plansFile = scenario.getConfig().plans().getInputFile();
		if (plansFile == null) return;

		// Resolve relative path using config context (same as MATSim's ScenarioLoaderImpl)
		java.net.URL plansUrl;
		try {
			plansUrl = IOUtils.resolveFileOrResource(plansFile);
		} catch (Exception e1) {
			// Relative path not found from CWD — resolve relative to config file
			java.net.URL configContext = scenario.getConfig().getContext();
			if (configContext != null) {
				try {
					plansUrl = new java.net.URL(configContext, plansFile);
				} catch (java.net.MalformedURLException e2) {
					log.warn("Could not resolve plans file path: {}", plansFile);
					return;
				}
			} else {
				log.warn("Could not resolve plans file path: {}", plansFile);
				return;
			}
		}
		log.info("Re-parsing plans XML to recover lost stage activity durations: {}", plansUrl);

		// Map: personId -> list of (activityIndex, durationSeconds) per plan
		// Key: personId + "#" + planIndex; Value: list of (activityIndexInPlan, duration)
		Map<String, List<double[]>> durationsFromXml = new HashMap<>();
		try (InputStream is = IOUtils.getInputStream(plansUrl)) {
			XMLInputFactory factory = XMLInputFactory.newInstance();
			factory.setProperty(XMLInputFactory.SUPPORT_DTD, false);
			XMLStreamReader reader = factory.createXMLStreamReader(is);

			String currentPersonId = null;
			int planIndex = -1;
			int actIndexInPlan = -1;

			while (reader.hasNext()) {
				int event = reader.next();
				if (event == XMLStreamConstants.START_ELEMENT) {
					String name = reader.getLocalName();
					if ("person".equals(name)) {
						currentPersonId = reader.getAttributeValue(null, "id");
						planIndex = -1;
					} else if ("plan".equals(name)) {
						planIndex++;
						actIndexInPlan = -1;
					} else if ("activity".equals(name)) {
						actIndexInPlan++;
						String actType = reader.getAttributeValue(null, "type");
						String maxDur = reader.getAttributeValue(null, "max_dur");
						if (actType != null && maxDur != null
								&& StageActivityTypeIdentifier.isStageActivity(actType)) {
							double dur = Time.parseTime(maxDur);
							if (dur > 0) {
								String key = currentPersonId + "#" + planIndex;
								durationsFromXml.computeIfAbsent(key, k -> new ArrayList<>())
										.add(new double[]{actIndexInPlan, dur});
							}
						}
					}
				}
			}
			reader.close();
		} catch (Exception e) {
			log.warn("Could not re-parse plans XML for duration repair: {}", e.getMessage());
			return;
		}

		// Step 4: apply recovered durations to loaded population
		int repaired = 0;
		for (Person person : scenario.getPopulation().getPersons().values()) {
			List<? extends Plan> plans = person.getPlans();
			for (int pi = 0; pi < plans.size(); pi++) {
				String key = person.getId().toString() + "#" + pi;
				List<double[]> entries = durationsFromXml.get(key);
				if (entries == null) continue;

				List<? extends PlanElement> elements = plans.get(pi).getPlanElements();
				// Build activity-only index
				List<Activity> activities = new ArrayList<>();
				for (PlanElement pe : elements) {
					if (pe instanceof Activity act) activities.add(act);
				}
				for (double[] entry : entries) {
					int actIdx = (int) entry[0];
					double dur = entry[1];
					if (actIdx < activities.size()) {
						Activity act = activities.get(actIdx);
						if (!act.getMaximumDuration().isDefined()
								|| act.getMaximumDuration().seconds() < 1.0) {
							act.setMaximumDuration(dur);
							repaired++;
						}
					}
				}
			}
		}
		log.info("Repaired {} activity durations from XML re-parse.", repaired);
	}
}
