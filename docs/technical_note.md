# Technical note: crowding estimates for Delhi buses (v8)

Every number here is read from `outputs/eval/summary.json`, `outputs/model_report.json` or `outputs/validation_v8.json`, and can be regenerated with `make all`.

## 1. What the model is, and is not

**Purpose.** Estimate how many people are typically on a bus, by route, direction, stretch between two stops, half-hour of the day and day type (weekday, Saturday, Sunday/holiday), and show it as *seats available / standing room / crowded* beside each journey option.

**Inputs.** 2,559,899 e-tickets (1–31 Oct 2026, `ticket_type_id` = 1 for all of them) and the static GTFS feed (2,554 routes, 65,322 scheduled trips).

**Not.** It is not live, does not see buses that ran late, bunched or never ran, and has no ground-truth on-board counts to be checked against.

## 2. Data findings and what each changed

| Finding | Consequence |
|---|---|
| About 1 rider in 40 appears in the data (102,303 ticketed passengers per weekday vs 4.08 million published daily riders) | Counts must be scaled; thin cells need statistical stabilising |
| 100% of tickets are one type; fares are exactly 90% of the standard slabs | Looks like discounted digital tickets, so ticket capture may differ by route and rider type |
| 48.5% of tickets have no `vehicle_number`; median 5 tickets per vehicle-day | A per-trip running count is far too sparse to be the method; kept as a diagnostic (J6) |
| 2 Oct and 20 Oct run at 43% and 52% of a normal weekday | Treated as Sunday/holiday |
| 93.6% of passengers matched to a route stop order; 6.4% of tickets dropped (unmatched or alighting not after boarding) | Reported in J1 |
| The GTFS calendar has one service for all seven days | Weekend frequency equals weekday frequency; a stated assumption |

## 3. Method

1. **Occupancy credits.** A ticket (boarded at stop *s*, destination stop *e*) puts the rider on every stretch from *s* to the alighting stop. Alighting is spread 15% / 70% / 15% over *e*−1, *e*, *e*+1, because riders may get off early or late. Each stretch is timed with GTFS run times.
2. **Rate per bus.** Credits are summed by route-direction, stretch, day type, half hour and week, and divided by the number of scheduled bus passes. Cells with no scheduled bus are never emitted.
3. **Stabilising thin cells (Gamma-Poisson).** The rate in each cell is combined with a prior built from the same route at other stretches and times (route level → route × half-hour → stretch factor, each shrunk toward the level above). The prior strength is a shape parameter `a`, chosen on held-out weeks (a = 2.0). Weekend half-hours are pooled to hours.
4. **Week-to-week range.** A negative-binomial layer widens the range as 1/k = α + 1/shape, with α fitted per amount of evidence so that about 80% of held-out weeks fall in the stated 10–90% range.
5. **Scaling to people.** `load = κ_route × rate`. κ = 39.9 (published 40.8 lakh riders/day ÷ ticketed passengers/day). Where tickets would imply a typical peak (99th percentile of cells with 30+ tickets) above 1.5× crush capacity, κ for that route is reduced to that physical limit.
6. **Tiers.** Seats available below 90% of seats; crowded at 80% of maximum capacity or more; standing room between. Defaults: 38 seats / 75 maximum (12 m bus).
7. **Evidence label.** High: 30 or more distinct tickets behind the cell; Medium: 10–29; Low: fewer.

## 4. Results on weeks the model had not seen

Leave-one-week-out on weekdays; prior, prior strength and range calibration all re-fit without the test week; 4 folds; target = held-out tagged riders per scheduled bus.

| Model | Deviance, all cells (lower better) | Spearman, 30+ tickets | Tier agreement, all cells |
|---|---|---|---|
| B0 route × hour average | 0.424 | 0.454 | 0.859 |
| B1 raw cell rate | 0.401 | 0.818 | 0.899 |
| v7 (fixed prior, previous model) | 0.229 | 0.755 | 0.897 |
| **v8 (this model)** | **0.212** | 0.815 | 0.901 |

**Read honestly.** Against the previous model, point accuracy improves modestly (deviance −7%; rank agreement on busy cells 0.755 → 0.815; ≥100 tickets 0.797 → 0.846), and tier agreement is essentially unchanged. v8 matches the raw rate where data are plentiful and is 47% better in deviance where they are thin. Two ideas that did not help were removed: temporal smoothing (deviance 0.212 → 0.226) and a single city-wide prior instead of the route hierarchy (0.212 → 0.242).

**Calibrated ranges.** Share of held-out weeks inside the stated 10–90% range (target ≈ 80%):

| Tickets behind the cell | v7 | v8 |
|---|---|---|
| < 10 | 87.5% | 92.1% |
| 10–29 | 71.7% | 80.0% |
| 30–99 | 62.3% | 80.6% |
| ≥ 100 | **51.8%** | 80.8% |

v7's headline of "85.9% coverage" was an average dominated by thin cells; on busy cells its ranges were too narrow. (Cells under 10 tickets are conservative at 92%.)

**Reliability by evidence** (split-half, alternate weeks, Spearman–Brown to full data): < 10 tickets 0.91; 10–29 **0.63**; 30–99 0.83; ≥ 100 0.93. The 10–29 band is the weakest, which is why Medium is not shown as firm.

**Weekends** (one day per week, so low-power): Saturday deviance 0.597 (v7 0.632, raw 1.595); Sunday/holiday 0.438 (v7 0.467, raw 1.318).

## 5. Checks against known patterns (J1–J6)

| | Result |
|---|---|
| J1 match | 93.58% of passengers matched; fare vs GTFS distance Spearman 0.79 (AC), 0.83 (non-AC) |
| J2 held-out | Section 4 |
| J3 patterns | Morning peak (08:30–10:00) 5.9× midday load; evening peak 3.9×; Sunday/holiday = 0.49 and Saturday = 0.79 of weekday volume. Busiest boarding areas include Nehru Place (6th), Anand Vihar ISBT (13th), Dhaula Kuan (18th); Kashmere Gate, Azadpur and Connaught Place are not in the top 60 (see limits) |
| J4 plausibility | 1.8% of ticketed daytime weekday cells exceed crush capacity, 0.22% exceed 1.5×. Tier unchanged for 90–95% of cells if κ is 23.9, 31.9 or 49.9. Not run: kernel on/off, bin width, ±5 min timestamp |
| J5 router | 9 of 9 origin–destination pairs pass (distinct buses; requested departure kept; shifted options only if strictly better crowding; one with a change); warm query median 4.9 ms |
| J6 blind spots | 69% of weekday cells have Low evidence; 453 routes have no tickets at all; per-trip running count vs model: Spearman 0.30 on 29,948 trips (weak; the diagnostic is noisy at ~5 tickets per vehicle-day) |

## 6. Assumptions and where estimates are least reliable

- **Ticket capture is not uniform.** After capping, 678 routes (84% of ticketed passengers) are scaled below the city κ, and implied city ridership is only 54% of the published figure. If tickets were captured evenly the published total would be reproduced. So **rankings and tiers are more trustworthy than exact headcounts**, especially on the 678 routes. A route-level capture calibration needs ridership by route, which we do not have.
- **κ is press-sourced** (published ridership, FY2024–25) and should be confirmed against a primary source.
- **No ground truth.** No automatic passenger counts or conductor manifests were available, so absolute loads are not validated.
- **Schedules, not operations.** Load per scheduled bus ignores cancelled and bunched buses.
- **Weekends.** 4–5 days per type; GTFS lists weekday trips every day.
- **Bus size.** All routes use the 12 m default. The vehicle-prefix → 9 m mapping needs the operator's fleet list (hook in `config.yaml`: `bus_capacities.prefix_class`).
- **Hubs.** Kashmere Gate, Azadpur and Connaught Place appear low in ticket boardings; this may reflect where tickets are sold (adoption) rather than true demand.
- **Time stamp.** `created_at` is assumed to be boarding time.
- **October only.** No seasons, festivals beyond 2 and 20 Oct, or monsoon.
- **Thin evidence.** Late night, feeder (DS-*) and low-frequency routes; the app shows "Too few tickets for a reliable headcount" and ranks such options with an uncertainty penalty.

## 7. Claims → evidence

| Claim in the app | Where it comes from |
|---|---|
| "Typical, not live" | Method §1 |
| Ranges are calibrated | §4, `outputs/eval/summary.json` → `coverage` |
| Evidence High/Medium/Low | §4 reliability, `trust_thresholds` in `config.yaml` |
| Time-shift options are strictly better | J5; `tests/test_router.py` |
| "Where the next bus helps most" | `relief_rider_min` (Contract B); assumption shown beside it |

## 8. Re-running

`make verify && make all`, `make test` (32 tests), `make run`. Stages were run individually during this work (held-out evaluation ≈ 2.5 min, model ≈ 25 s, validation ≈ 2.5 min); the whole chain from raw data was not re-run end to end after the v8 changes.
