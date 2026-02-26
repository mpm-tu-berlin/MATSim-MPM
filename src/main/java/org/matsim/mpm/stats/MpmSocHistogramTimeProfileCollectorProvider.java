package org.matsim.mpm.stats;

import static org.matsim.contrib.common.timeprofile.TimeProfileCollector.ProfileCalculator;

import java.awt.Color;

import org.matsim.contrib.common.histogram.UniformHistogram;
import org.matsim.contrib.common.timeprofile.TimeProfileCharts;
import org.matsim.contrib.common.timeprofile.TimeProfileCharts.ChartType;
import org.matsim.contrib.common.timeprofile.TimeProfileCollector;
import org.matsim.contrib.ev.fleet.ElectricFleet;
import org.matsim.contrib.ev.fleet.ElectricVehicle;
import org.matsim.core.controler.MatsimServices;
import org.matsim.core.mobsim.framework.listeners.MobsimListener;

import com.google.common.collect.ImmutableList;
import com.google.common.collect.ImmutableMap;
import com.google.inject.Inject;
import com.google.inject.Provider;

/**
 * Custom SoC histogram provider with 5% bin resolution (20 bins) instead of the
 * default MATSim EV contrib resolution of 10% (10 bins).
 */
public class MpmSocHistogramTimeProfileCollectorProvider implements Provider<MobsimListener> {

    private static final double BIN_SIZE = 0.05;
    private static final int BIN_COUNT = 20; // 0–5%, 5–10%, …, 95–100%

    private final ElectricFleet evFleet;
    private final MatsimServices matsimServices;

    @Inject
    public MpmSocHistogramTimeProfileCollectorProvider(ElectricFleet evFleet, MatsimServices matsimServices) {
        this.evFleet = evFleet;
        this.matsimServices = matsimServices;
    }

    @Override
    public MobsimListener get() {
        ImmutableList.Builder<String> headerBuilder = ImmutableList.builder();
        for (int b = 0; b < BIN_COUNT; b++) {
            int pct = (int) Math.round(b * BIN_SIZE * 100);
            headerBuilder.add(pct + "%+");
        }
        var header = headerBuilder.build();

        ProfileCalculator calculator = () -> {
            var histogram = new UniformHistogram(BIN_SIZE, BIN_COUNT);
            for (ElectricVehicle ev : evFleet.getElectricVehicles().values()) {
                histogram.addValue(ev.getBattery().getCharge() / ev.getBattery().getCapacity());
            }

            ImmutableMap.Builder<String, Double> builder = ImmutableMap.builder();
            for (int b = 0; b < BIN_COUNT; b++) {
                builder.put(header.get(b), (double) histogram.getCount(b));
            }
            return builder.build();
        };

        var collector = new TimeProfileCollector(header, calculator, 300, "soc_histogram_time_profiles", matsimServices);
        collector.setChartTypes(ChartType.StackedArea);
        collector.setChartCustomizer((chart, chartType) -> {
            // Interpolate colours from red (0% SoC) to green (100% SoC) across 20 bins
            Color[] colors = new Color[BIN_COUNT];
            for (int b = 0; b < BIN_COUNT; b++) {
                float t = (float) b / (BIN_COUNT - 1); // 0.0 … 1.0
                colors[b] = new Color(1f - t, t, 0f);
            }
            TimeProfileCharts.changeSeriesColors(chart, colors);
        });
        return collector;
    }
}
