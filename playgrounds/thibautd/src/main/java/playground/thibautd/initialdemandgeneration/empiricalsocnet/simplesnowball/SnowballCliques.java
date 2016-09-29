/* *********************************************************************** *
 * project: org.matsim.*
 *                                                                         *
 * *********************************************************************** *
 *                                                                         *
 * copyright       : (C) 2013 by the members listed in the COPYING,        *
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
package playground.thibautd.initialdemandgeneration.empiricalsocnet.simplesnowball;

import org.matsim.api.core.v01.Coord;
import org.matsim.api.core.v01.Id;
import org.matsim.core.utils.geometry.CoordinateTransformation;
import org.matsim.core.utils.geometry.transformations.TransformationFactory;
import org.matsim.core.utils.io.UncheckedIOException;
import playground.thibautd.utils.CsvParser;

import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;


/**
 * @author thibautd
 */
public class SnowballCliques {
	public static Map<Id<Clique>, Clique> readCliques( final String file ) {
		final CoordinateTransformation transformation =
				TransformationFactory.getCoordinateTransformation(
						TransformationFactory.WGS84,
						TransformationFactory.CH1903_LV03_GT );

		final Map<Id<Clique>,Clique> cliques = new HashMap<>();
		try ( final CsvParser parser = new CsvParser( ',' , '\"' , file ) ) {
			while ( parser.nextLine() ) {
				final Id<Clique> cliqueId = parser.getIdField( "Clique_ID" , Clique.class );

				final Sex egoSex = parser.getEnumField( "E_sex" , Sex.class );
				final double egoLatitude = parser.getDoubleField( "E_latitude" );
				final double egoLongitude = parser.getDoubleField( "E_longitude" );
				final int egoAge = parser.getIntField( "E_age" );
				final int egoDegree = parser.getIntField( "E_degree" );

				final Sex alterSex = parser.getEnumField( "A_sex" , Sex.class );
				final double alterLatitude = parser.getDoubleField( "A_latitude" );
				final double alterLongitude = parser.getDoubleField( "A_longitude" );
				final int alterAge = parser.getIntField( "A_age" );

				final Coord egoCoord = transformation.transform( new Coord( egoLongitude , egoLatitude ) );
				final Coord alterCoord = transformation.transform( new Coord( alterLongitude , alterLatitude ) );

				Clique clique = cliques.get( cliqueId );
				if ( clique == null ) {
					clique = new Clique( cliqueId , new Member( egoSex , egoCoord , egoAge , egoDegree ) );
					cliques.put( cliqueId , clique );
				}
				clique.alters.add( new Member( alterSex , alterCoord , alterAge , -1 ) );
			}
		}
		catch ( IOException e ) {
			throw new UncheckedIOException( e );
		}

		return cliques;
	}

	public enum Sex { female, male }

	public static class Clique {
		final Id<Clique> cliqueId;
		final Member ego;
		final List<Member> alters = new ArrayList<>();

		public Clique( final Id<Clique> cliqueId, final Member ego ) {
			this.cliqueId = cliqueId;
			this.ego = ego;
		}
	}

	public static class Member {
		final Sex sex;
		final Coord coord;
		final int age;
		final int degree;

		public Member( final Sex sex, final Coord coord, final int age, final int degree ) {
			this.sex = sex;
			this.coord = coord;
			this.age = age;
			this.degree = degree;
		}

		public Sex getSex() {
			return sex;
		}

		public Coord getCoord() {
			return coord;
		}

		public int getAge() {
			return age;
		}

		public int getDegree() {
			if ( degree < 0 ) throw new IllegalStateException( "no degree known" );
			return degree;
		}
	}
}