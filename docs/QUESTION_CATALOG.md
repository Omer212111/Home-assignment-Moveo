# Question catalog: what an analyst will ask, and what the agent needs to answer it

The assignment gives four example questions. A logistics analyst choosing hubs for resilience
investment will ask many variations of them. This catalog groups the likely questions by the
*dimension* they need (which hubs, which hazard, which time period, what statistic), marks what the
agent can answer today, and proposes the smallest set of tools that covers the rest.

Status (updated after the time-dimension slice): ✅ answered · 🟡 partly · ❌ not yet.
The ❌ rows marked *(built)* below are now implemented: `period` on `rank_hubs` / `compare_hubs`,
`order` on `rank_hubs`, and the new `hub_breakdown` and `weather_stat` tools (`DESIGN_DECISIONS.md` section 14).
Still open: region totals, threshold counts, worst single event, a method tool.

## 1. Rankings (which hubs?)
| Example | Status | Tool |
|---|---|---|
| What are the 10 most dangerous hubs? | ✅ | `rank_hubs` |
| Which Midwest hubs are most exposed to winter disruption? *(assignment)* | ✅ | `rank_hubs` |
| Which are the 5 safest hubs? | ✅ *(built)* | `rank_hubs` needs an `order` (most / least exposed) |
| Which region is the riskiest overall? | ❌ | region totals (new `summary` in `rank_hubs` or a small tool) |
| How many hubs lose more than 8 days a year? | 🟡 | ranking + a threshold count (pre-computed) |
| Which hubs are most disrupted **in summer** / **in January**? | ✅ *(built)* | `rank_hubs` with a **period** filter |

## 2. One hub (why? what drives it?)
| Example | Status | Tool |
|---|---|---|
| Why is the Dallas hub's weather disruption risk high? *(assignment)* | ✅ | `explain_hub` (corrects the premise: rank 17 of 50) |
| What are the main hazards for Denver? | ✅ | `explain_hub` |
| **When in the year** is Chicago most disrupted? Which month is worst? | ✅ *(built)* | new: per-month / per-season breakdown |
| Was 2024 worse than 2021 for Denver? Which year was worst? | ✅ *(built)* | new: per-year breakdown |
| What was the worst single event in Dallas? | ❌ | events list (tail risk, `IMPROVEMENTS.md` section 2) |

## 3. Comparisons (X vs Y)
| Example | Status | Tool |
|---|---|---|
| Compare Miami and Houston in terms of hurricane and flood exposure. *(assignment)* | ✅ | `compare_hubs` |
| Compare Boston and Minneapolis **by season** / **month by month** | ✅ *(built)* | new breakdown, for 2-5 hubs |
| Is Houston riskier than Miami **during hurricane season**? | ✅ *(built)* | breakdown restricted to months (Jun-Nov) |
| Compare Midwest and South | ❌ | region totals |

## 4. Weather statistics (raw data, not disruption days)
| Example | Status | Tool |
|---|---|---|
| What percentage of days in Denver last year had snowfall? *(assignment)* | ✅ *(built)* | new `weather_stat` |
| How many days above 40 C did Phoenix have in 2024? | ✅ *(built)* | `weather_stat` |
| Total snowfall in Chicago in January 2024? Strongest gust in Boston? | ✅ *(built)* | `weather_stat` (count / percentage / sum / max) |
| Which month gets the most snow in Denver? | ✅ *(built)* | `weather_stat` by month |

## 5. Method and confidence
| Example | Status | Tool |
|---|---|---|
| How do you calculate the score? What counts as a disruption day? | 🟡 | `method` is in every tool result; full rules are in `config/disruption.yaml` |
| How confident are you? What could make this wrong? | ✅ | `confidence`, `assumptions`, `uncertainty_notes` in every answer |

## 6. Out of scope (must say so, not guess)
Weather forecasts, cities that are not hubs, cost in dollars (not modelled yet), live alerts
(planned bonus), hub-specific vulnerability (building standard, backup power).

---

## What the data shows (checked on the real data)

Weather-channel disruption days, average per year by month (Jan..Dec), 2021-2025:

| Hub | J | F | M | A | M | J | J | A | S | O | N | D |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Chicago | 3.23 | 2.34 | 1.26 | 0.22 | 0.10 | 0.10 | 0.60 | 0.10 | 0.20 | 0.10 | 0.96 | 0.64 |
| Denver | 1.33 | 1.78 | **2.52** | 1.26 | 0.72 | 0 | 0 | 0 | 0.10 | 0.52 | 0.70 | 1.41 |
| Dallas | 1.10 | 1.12 | 0.76 | 0.60 | 0.20 | 0.10 | 0.10 | 0.70 | 0 | 0.60 | 0.30 | 0 |

- Seasonality is real and not obvious: Denver's worst month is **March** (spring snow), not January.
- **Year-to-year variation is large**: Denver 5.8 (2025) to 14.4 (2024) days; Chicago 7.0 to 14.0.
  An answer per year must say that 5 years is a small sample and one year is not a trend.
- Definitions change answers: Denver 2025 had **43** days with any snowfall (> 0 cm) but **17** with
  at least 1 cm. "Snowfall day" must be an explicit, stated threshold.
- Hurricanes come from FEMA declarations, which have incident dates, so they can be placed in
  their month too (Miami: September-October), with the warning that this rests on very few events.

---

## Proposed design: one time dimension, used by every tool

**1. Engine:** keep each hub's **daily disruption series** (date, hazard, days) that the engine
already computes and then discards. Declarations are placed on their incident date. Every
breakdown is then a sum over this one series, so months add up to the annual total (testable).

**2. A shared `period` argument** (same JSON schema everywhere):
- `null`: the whole year (today's behaviour)
- a season: `winter` (Dec-Feb), `spring` (Mar-May), `summer` (Jun-Aug), `autumn` (Sep-Nov),
  meteorological seasons, stated as an assumption
- a list of months, e.g. `[6, 7, 8, 9, 10, 11]` for hurricane season

**3. Tools:**
| Tool | Change |
|---|---|
| `rank_hubs` | + `period` (rank by days in that season/months), + `order` (most / least exposed) |
| `compare_hubs` | + `period` |
| `hub_breakdown` (new) | 1-5 hubs, one hazard or total, `by`: `month` / `season` / `year`; per-period days, peak period, pre-computed differences between hubs, number of events behind each period (small-sample warning) |
| `weather_stat` (new) | 1-5 hubs, variable (snowfall, rain, precipitation, max/min temperature, gusts), condition (e.g. `>= 1` cm), statistic (count, percentage of days, sum, max), date range or year or month or season; returns the exact date range and threshold used |

**4. Language traps the prompt and evals must cover:**
- *"winter"* the hazard (snow and ice) vs *"winter"* the season (Dec-Feb, all hazards).
  "Which hubs have the worst winter storms?" -> hazard. "Which hubs are most disrupted in winter?" -> period.
- *"last year"* = 2025, the last full year in the data (stated in the answer).
- *"snowfall day"*: the threshold is stated; default > 0 cm, with the >= 1 cm count alongside.
- *"hurricane season"* = June-November.
- Per-year questions: always add the small-sample warning.

**5. New eval cases** (one per trap): season vs hazard "winter"; month-by-month comparison of two
hubs; worst month for Denver (March); year comparison with a sample warning; the Denver snowfall
percentage for 2025; "safest hubs" (order).
