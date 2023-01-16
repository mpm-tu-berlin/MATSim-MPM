package org.matsim.contrib.ev.eTruckTraffic.lib;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.Scenario;
import org.matsim.api.core.v01.population.Activity;
import org.matsim.api.core.v01.population.Leg;
import org.matsim.api.core.v01.population.Person;
import org.matsim.api.core.v01.population.Plan;
import org.matsim.api.core.v01.population.Population;
import org.matsim.api.core.v01.population.PopulationFactory;
import org.matsim.api.core.v01.population.PopulationWriter;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.scenario.ScenarioUtils;
public class RunETruckPoplationGenerator {

	public static void run(String rawPlansFile, String plansFile) {

		Scenario scenario = createPopulationFromFile(rawPlansFile);
		PopulationWriter populationWriter = new PopulationWriter(scenario.getPopulation(), scenario.getNetwork());
		populationWriter.write(plansFile);

	}

	private static Scenario createPopulationFromFile(String file)	{

		Scenario scenario = ScenarioUtils.createScenario(ConfigUtils.createConfig());
		 // Use Parser to read csv file.
		List<ETruckEntry> fileEntries = new ETruckParser().readFile(file);

		//Get Population and PopulationFactory objects.
		Population population = scenario.getPopulation();
		PopulationFactory populationFactory = population.getFactory();


		// Create a Map with the PersonIds as key and a list of Entry as values.

		Map<Integer, List<ETruckEntry>> personEntryMapping = new TreeMap<Integer, List<ETruckEntry>>();
		for (ETruckEntry fileEntry : fileEntries) {

			 // If the Map already contains an entry for the current person
			 // the list will not be null.

			List<ETruckEntry> entries = personEntryMapping.get(fileEntry.id_person);



			// If no mapping exists -> create a new one

			if (entries == null) {
				entries = new ArrayList<ETruckEntry>();
				personEntryMapping.put(fileEntry.id_person, entries);
			}

			//Add currently processed entry to the list
			entries.add(fileEntry);
		}


		// Now create a plan for each person - iterate over all entries in the map.

		for (List<ETruckEntry> personEntries : personEntryMapping.values()) {
			// Get the first entry from the list - it will never be null.
			ETruckEntry entry = personEntries.get(0);

			// Get id of the person from the Entry.
			int idPerson = entry.id_person;

			// Create new person and add it to the population.
			// Use scenario.createId(String id) to create the Person's Id.
			Person person = populationFactory.createPerson(Id.create(idPerson, Person.class));
			population.addPerson(person);

			// Create new plan and add it to the person.
			Plan plan = populationFactory.createPlan();
			person.addPlan(plan);

			Coord homeCoord =  new Coord(entry.o_x, entry.o_y);
			Activity homeActivity = populationFactory.createActivityFromCoord(entry.tripmode, homeCoord);
			homeActivity.setEndTime(entry.starttime * 60 * 60);
			plan.addActivity(homeActivity);

			// Create person's Trips and add them to the Plan.
			Coord endCoord = new Coord(entry.d_x, entry.d_y);

			// Create a new Leg using the PopulationFactory and set its parameters.
			Leg leg = populationFactory.createLeg("car");

			// Create a new Activity using the Population Factory and set its parameters.
			Activity activity = populationFactory.createActivityFromCoord(entry.tripmode, endCoord);

			// Add the Leg and the Activity to the plan.
			plan.addLeg(leg);
			plan.addActivity(activity);

		}
		return scenario;

	}
}
