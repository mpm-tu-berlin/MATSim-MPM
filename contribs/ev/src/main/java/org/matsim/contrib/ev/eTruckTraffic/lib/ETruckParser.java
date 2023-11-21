package org.matsim.contrib.ev.eTruckTraffic.lib;

import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.Charset;
import java.util.ArrayList;
import java.util.List;


public class ETruckParser {

	private String separator = ",";
	private Charset charset = Charset.forName("UTF-8");

	public List<ETruckEntry> readFile(String inFile)
	{
		List<ETruckEntry> entries = new ArrayList<ETruckEntry>();

		FileInputStream fis = null;
		InputStreamReader isr = null;
		BufferedReader br = null;
		// time from 1 to 24 in data but 0 - 23 needed -> hour-1
		// + offset
		int offset = -5;
		try
		{
			fis = new FileInputStream(inFile);
			isr = new InputStreamReader(fis, charset);
			br = new BufferedReader(isr);

			// skip first Line
			br.readLine();

			String line;
			while((line = br.readLine()) != null)
			{
				ETruckEntry fileEntry = new ETruckEntry();

				String[] cols = line.split(separator);

				fileEntry.id_person = parseInteger(cols[0]);
				fileEntry.origin = parseInteger(cols[1]);
				fileEntry.destiantion =  parseInteger(cols[2]);
				fileEntry.tripmode = cols[3];
				fileEntry.o_x = parseDouble(cols[4]);
				fileEntry.o_y = parseDouble(cols[5]);
				fileEntry.d_x = parseDouble(cols[6]);
				fileEntry.d_y = parseDouble(cols[7]);
				// day: 1 - 7 in data but 0 - 6 needed -> day-1
				fileEntry.day = parseInteger(cols[8]) - 1 ;

				fileEntry.starttime = parseDouble(cols[9]) + offset;
				if (fileEntry.starttime < 0){
					fileEntry.starttime += 24*7;
				}

				entries.add(fileEntry);
			}

			br.close();
			isr.close();
			fis.close();
		}
		catch (FileNotFoundException e)
		{
			e.printStackTrace();
		}
		catch (IOException e)
		{
			e.printStackTrace();
		}

		return entries;
	}

	private int parseInteger(String string)
	{
		if (string == null) return 0;
		else if (string.trim().isEmpty()) return 0;
		else return Integer.valueOf(string);
	}

	private double parseDouble(String string) {
		if (string == null) return 0.0;
		else if (string.trim().isEmpty()) return 0.0;
		else return Double.valueOf(string);
	}
}
