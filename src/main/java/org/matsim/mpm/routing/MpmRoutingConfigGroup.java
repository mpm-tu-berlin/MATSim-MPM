package org.matsim.mpm.routing;

import org.matsim.core.config.Config;
import org.matsim.core.config.ReflectiveConfigGroup;

/**
 * Config group for MPM-specific routing parameters.
 * Register in config.xml under module name "mpmRouting".
 *
 * <pre>{@code
 * <module name="mpmRouting">
 *   <param name="restAreasFile" value="rest_areas.xml" />
 * </module>
 * }</pre>
 *
 * If {@code restAreasFile} is not set, agents making non-charging regulatory breaks
 * will stop at an arbitrary route link (backward-compatible fallback).
 */
public final class MpmRoutingConfigGroup extends ReflectiveConfigGroup {

    public static final String GROUP_NAME = "mpmRouting";

    @Parameter
    @Comment("Path to the XML file containing highway rest areas (Rastplätze). " +
             "If not set, agents stopping for regulatory breaks stop at an arbitrary route link.")
    public String restAreasFile = null;

    public MpmRoutingConfigGroup() {
        super(GROUP_NAME);
    }

    public static MpmRoutingConfigGroup get(Config config) {
        return (MpmRoutingConfigGroup) config.getModule(GROUP_NAME);
    }
}
