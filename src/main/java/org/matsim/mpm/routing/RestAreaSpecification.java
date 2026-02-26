package org.matsim.mpm.routing;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;

/**
 * Immutable specification of a highway rest area (Rastplatz).
 * {@code parkingSpaces} is -1 if not specified in the data file.
 */
public record RestAreaSpecification(String id, Id<Link> linkId, int parkingSpaces) {}
