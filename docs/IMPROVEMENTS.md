# Improvements after the baseline pipeline

To do once the baseline works end to end (data -> disruption days -> API -> agent -> UI -> evals).
Ordered roughly by value for the investment decision.

## 1. A cost metric instead of days only (research task)
The baseline KPI is expected disruption days per year. An investment committee thinks in money,
so the next step is **expected annual cost = downtime cost + property damage**. This needs
sources found and checked online. Candidates to evaluate (none verified yet unless marked):

| Need | Candidate sources to investigate |
|---|---|
| Property damage per hazard | **OpenFEMA Public Assistance Funded Projects** (damage repair $ per disaster per county; reachable from our environment, checked), FEMA National Risk Index Expected Annual Loss (modelled building loss per hazard and county), NOAA Storm Events Database (reported property damage per event and county), NOAA Billion-Dollar Weather and Climate Disasters (event costs by state), NFIP flood insurance claims (OpenFEMA; endpoint not found yet) |
| Cost of a lost or slowed operating day | Trucking cost-per-hour benchmarks (e.g. industry operational-cost studies), FHWA road weather statistics on weather-related delay, freight statistics from the Bureau of Transportation Statistics |
| Company-specific inputs (best source) | Revenue or throughput per hub-day, SLA penalties, the company's own downtime and claims history |

For each candidate, record: what it measures, geography and time coverage, licence, how often it updates, and how it would plug into the model. Then choose the cost formula with the analysts.

## 2. Tail risk next to the average
Add a **worst single event in 20 years** column (days lost in the worst event) next to the annual average, so hurricane hubs like Houston and Miami show their catastrophic exposure. Later: return periods (e.g. "1-in-10-year event costs X days").

## 3. Fix the coastal-wind overstatement
Coastal hubs (Virginia Beach, Oakland, Boston) get grid cells that partly cover open water. Options:
- check whether Open-Meteo can select a land-only grid cell for a coordinate;
- move the hub coordinate a few km inland, to where a distribution hub would realistically be;
- use gusts measured at the nearest airport weather station (NOAA station data) instead of the grid.

## 4. Calibrate rules with real downtime data
Severities, recovery days and the 7 days per hurricane are expert judgment. With the company's downtime logs, fit them (e.g. regression of actual lost hours on the trigger days). The config file is built so that this changes numbers, not code.

## 5. Uncertainty ranges
Run each hub with low / base / high parameter sets and report a range (e.g. "Minneapolis 9-14 days/yr"), and flag hubs whose rank changes between scenarios.

## 6. Longer and better weather history
- 10-20 years instead of 5 (needs a paid Open-Meteo key or several days of free quota) to reduce sampling noise.
- Check the snowfall bias against station data (NOAA daily station records) instead of assuming ~30%.

## 7. FEMA National Risk Index
Add modelled hurricane, flood and tornado frequency per county (rare events that 5 years of weather cannot capture). The download is blocked from our environment; it needs a manual CSV download.

## 8. Smaller items
- Group neighbouring hubs into metros (Dallas / Fort Worth / Arlington, the Bay Area, Phoenix / Mesa, LA / Long Beach).
- Statewide FEMA declarations (county code 000) are ignored today; decide whether to include them.
- Exposure is not risk: add hub vulnerability (building standard, backup power, alternative routes) when the company can provide it.
