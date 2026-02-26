package org.matsim.mpm.routing;

import org.matsim.api.core.v01.Id;
import org.matsim.api.core.v01.network.Link;
import org.matsim.core.utils.io.MatsimXmlParser;
import org.xml.sax.Attributes;

import java.util.List;
import java.util.Stack;

/**
 * Reads rest area specifications from an XML file.
 *
 * <p>Expected format:
 * <pre>{@code
 * <restAreas>
 *   <restArea id="RAB_A1_Example" link="1234567890001f" />
 * </restAreas>
 * }</pre>
 */
public final class RestAreaReader extends MatsimXmlParser {

    private final List<RestAreaSpecification> restAreas;

    public RestAreaReader(List<RestAreaSpecification> restAreas) {
        super(ValidationType.NO_VALIDATION);
        this.restAreas = restAreas;
    }

    @Override
    public void startTag(String name, Attributes atts, Stack<String> context) {
        if ("restArea".equals(name)) {
            String ps = atts.getValue("parking_spaces");
            int parkingSpaces = (ps != null) ? Integer.parseInt(ps) : -1;
            restAreas.add(new RestAreaSpecification(
                    atts.getValue("id"),
                    Id.createLinkId(atts.getValue("link")),
                    parkingSpaces));
        }
    }

    @Override
    public void endTag(String name, String content, Stack<String> context) {
        // no content elements
    }
}
