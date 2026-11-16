# Data notes: quality checks and calibration

## Data in the database
| Source | Coverage | Rows |
|---|---|---|
| Open-Meteo historical (ERA5 reanalysis), daily | 50 hubs, 2021-01-01 .. 2025-12-31 | 91,300 (1,826 days per hub) |
| OpenFEMA Disaster Declarations | 47 counties (50 hubs), 2006 .. 2026-10 | 669 rows (481 distinct disasters; one row per disaster and county) |

## Quality checks (all passed)
- No missing values in any weather variable; every hub has all 1,826 days.
- No impossible values: no day with min temperature above max, no negative precipitation, rain never exceeds total precipitation.
- Ranges are plausible: max temperature up to 48.2 C (desert hubs), min down to -36.5 C (Minneapolis), rain up to 238 mm/day, gusts up to 153 km/h.

## Findings that changed the model
1. **No freezing-rain codes.** The weather codes in this dataset only take the values 0-3, 51-55, 61-65 and 71-75. Codes 56/57/66/67 (freezing drizzle and rain) never appear, so an ice rule based on them can never fire. Ice is now detected as *rain >= 1 mm on a day whose max temperature stays <= 1 C*. This flags the right events: the North Texas ice storm of 30 Jan - 1 Feb 2023 (Dallas), Winter Storm Uri in Feb 2021 (Houston, Austin), and the Feb 2022 storm.
2. **Snowfall looks low.** The heaviest snow day in 5 years across 50 cities is 26.9 cm, and Denver has only 1.2 days/yr with >= 10 cm. Gridded reanalysis smooths peaks, and the snow-to-water ratio used is probably lower than real snow (not yet confirmed in Open-Meteo's documentation). Snow thresholds were therefore lowered to 7 / 17 cm (~10 / 25 cm real).
3. **Absolute heat and cold thresholds favour adapted cities.** At >= 38 C, Phoenix had 101 trigger days/yr and ranked #2 overall, Mesa #3 and Las Vegas #4, although their operations are built for that heat. Thresholds were moved to where even adapted operations are disrupted: heat >= 43 C and cold <= -25 C. Phoenix moved from #2 to #25.
4. **FEMA classification quirks.** Tropical Storm Erin (2007, Houston) is filed under incident type "Severe Storm", so it counts as a severe storm, not a tropical event. Inland Dallas and Fort Worth have 5 hurricane-related declarations each, 4 of them EM (support for evacuees). Only DR (major disaster) declarations are counted.

## Known limitation: coastal wind is likely overstated
Weather grid cells are about 25 km wide. For coastal hubs the cell partly covers open water, where gusts are stronger than on land. Affected hubs, high-wind disruption days/yr:

| Hub | High wind (days/yr) | Share of hub total |
|---|---|---|
| Virginia Beach | 6.4 | 73% of 8.8 |
| Oakland | 4.3 | 70% of 6.1 |
| Boston | 3.4 | 26% of 13.1 (mostly winter) |

This is **documented, not corrected**: the ranking places Virginia Beach #6, mostly because of wind. Ways to fix it are listed in `docs/IMPROVEMENTS.md`. Albuquerque's high wind (4.2 days/yr) is inland and likely real (mountain/plateau winds).

## Hurricanes: the average hides the tail
With 7 disruption days per counted hurricane, Houston gets about 1 day/yr from tropical events (3 hurricanes in 20 years). The average is correct, but one event like Harvey shuts a hub for a week or more. A "worst single event" view is in the improvement list.
