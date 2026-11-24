# Delhi Crowdedness-Aware Journey Planner — Build Plan & Coding Spec (v7)

**Problem:** Problem 2, Phase 1 (crowdedness model by route and time of day + a journey planner that shows it)
**Time:** 48 h (H0 = start) | **Team:** 3 engineers (solo/pair collapse in §9) | **Inputs:** `October-Delhi.csv.zip` (~2.56 M tickets, 1–31 Oct 2026) + static GTFS
**Principle:** the crowding model is the product; the planner only has to show it clearly. Every EDA finding maps to a config fix; every number shown to a user has a validation behind it.

v7 is standalone. It keeps v5's coding detail, takes v6's fixes and presentation, and adds the fixes marked **[new]** below.

---

## 0. What changed vs v5 / v6

| # | Change | Source | Why |
|---|---|---|---|
| 1 | Cells with no scheduled bus are masked before and after temporal smoothing; `bin30` only emitted where `trips_scheduled > 0` | v6 | No ghost crowding at 2:30 AM; no divide-by-zero |
| 2 | Requested departure is pinned: Pareto runs on the requested time only; shifted departures are appended only if their tier strictly improves | v6 | Pooling lets a later, emptier bus delete the user's requested bus |
| 3 | Time comparisons use arrival time measured from the requested time; shifted candidates are deduped against base options by boarding trip; the "Leave N min later" label is computed from actual boarding times | **new** | With a 25-min headway, "+15 min" can return the same bus; without arrival time, a later bus looks "free" |
| 4 | `phi` is **not** clamped. Grid 1–8, coverage reported per sample-size bucket; trust thresholds are tuned to the calibrated intervals, not the other way round | **new** (rejects v6 clamp) | Test run: `phi` 1→4 doubles p90/p10 on small cells (1.95→4.02). Clamping to look tidy would make the 80% claim false |
| 5 | Prior (`R`, `S`), `phi` and any smoothing are re-fit inside each leave-one-week-out fold | **new** | Otherwise held-out weeks leak into the prior and J2 is optimistic |
| 6 | Outage handling is by (day, hour) validity mask, with `n_valid_days[day_type, bin]`, not whole-day drops | **new** | Keeps good hours of partly broken days |
| 7 | Operator relief metric replaced by rider-weighted overload | **new** | The v5/v6 linear metric saturates: relief is a constant `0.8×max_cap` per overloaded segment-minute once still overloaded (test run: 60.0 for loads 100, 150, 200 and 400), so it cannot rank severity |
| 8 | Operated-trip correction promoted: a city-by-hour scalar is tried early (S1-lite); per-route version stays stretch | **new** | Load = C/T, so every unit of schedule-vs-reality error goes straight into the load; kappa does not absorb it |
| 9 | GTFS graph handles multiple stop patterns per route-direction and loops | **new** | Real GTFS has short-turns and variants; one stop list per route-direction silently miscounts T |
| 10 | Back-of-envelope load-factor plausibility check added to J4 | **new** | The only semi-independent check on kappa × T |
| 11 | Trust badge is based on `tickets_n` (distinct tickets), not `credits_n` | **new** | Credits are inflated by multi-segment spreading; the UI says "Based on N ticket records" |
| 12 | `p_crowded` documented as "probability the *typical* bus is crowded", not a per-bus probability | **new** | Posterior covers uncertainty in the mean, not bus-to-bus variation (bunching) |
| 13 | EDA as 5 categories (v6) over v5's 12 queries; validation as J1–J6 (v6) over v5's V1–V12 procedures | v6 + v5 | Presentation of v6, rigour of v5 |
| 14 | `make verify` is a separate target | v6 | A fresh GTFS folder never breaks `make all` |
| 15 | Posterior snippet takes `max_cap`, uses vectorised `ppf`/`sf` | **new** | v6 snippet referenced an undefined variable |

Checked numerically before writing: credit weights (364 combinations, closed form equals loop, mass equals expected segments), posterior mean invariant to `phi`, phi-scaled predictive mean and over-dispersion, and the relief saturation above.

---

## 1. Requirements -> where each is satisfied

| Brief requirement | Module / output |
|---|---|
| Tickets -> people on board by route, direction, stretch, time of day (§2.1) | `model/03_credits.py`, `model/04_posterior.py` -> `segment_crowding.parquet` |
| Crowding level a commuter understands (§2.1) | 3 seat-based tiers: Seats Available / Standing Room / Crowded |
| Crowding next to each journey option (§2.1) | `router/router.py` + App Tab 1 |
| Faster vs more comfortable (§2.1) | 3 mode pills (Fastest / Balanced / Comfortable) + fine `lam` slider |
| Commuters and operators can both read it (§2.3) | Tab 1 (commuter); Tab 3 (operator: overload, relief per added bus, chronic vs spike, underused) |
| Bus only, own planner on static GTFS (§2.4) | `gtfs/build_graph.py`, `router/router.py` |
| Re-runnable pipeline (§4) | `Makefile`, pinned `requirements.txt`, `config.yaml` |
| Queryable / on a map (§4) | `crowding/query.py` (function + CLI), Tab 2 |
| Planner / demo screen (§4) | `app/streamlit_app.py`, hosted link |
| Technical note incl. least-reliable areas (§4) | `docs/technical_note.md` |
| Checks vs known patterns (§4) | `validation/05_validate.py` J1–J6, Tab 4 |
| Group tickets by trip / running count (§5) | Soft-alighting credits aggregated by route-segment-time bin (a trip gets about 1–2 tickets at ~2% capture, so a per-trip count is statistically empty); per-trip running count kept as a dense-vehicle-day diagnostic (J6) |
| Riders absent from data, e.g. pass holders (§5) | Anchored expansion factor kappa; capture-heterogeneity check (J4) |
| Early/late alighting (§5) | Soft kernel 15/70/15, sensitivity on/off (J4) |
| Bus size 9 m vs 12 m (§5) | Capacity priors by vehicle-prefix map |
| Trust where data is thin (§5) | Calibrated 10–90% intervals + High/Medium/Low badge + plain ticket count |
| Simple display (§5) | 3 tiers; Okabe-Ito colour + icon + word; typical-day banner |

---

## 2. Architecture

```text
October-Delhi.csv.zip  +  Static GTFS (Transport Stack; fallback: Delhi OTD static GTFS)
        v
[etl/00_load.py]      unzip -> DuckDB -> tickets.parquet (IST, nullstr='NULL', sanity filters, --only START:END)
[etl/01_eda_study.py] 5-category diagnostic (§3) -> eda_report.json -> config.yaml
[etl/02_match.py]     route parse -> route-scoped RapidFuzz -> stop sequence; direction by vote;
                      seq(start) < seq(end) -> matched_tickets.parquet, match_report.json, unmatched_top50.csv
[gtfs/build_graph.py] per route-direction: canonical stop pattern, cumulative minutes, scheduled trips per
                      (day_type, bin, segment), next-departure arrays, stop clusters, transfer map, distances
        v
[model/03_credits.py]   ticket -> soft-alighting credits (15/70/15), timed by GTFS run times;
                        per-week C and T tensors; distinct ticket counts
[model/04_posterior.py] mask T==0 -> smooth -> rate = C/(T*operated_ratio) -> separable prior (in-fold) ->
                        Gamma-Poisson (phi from coverage, grid 1-8) -> x kappa -> load, p10, p90 ->
                        capacity -> tier, p_crowded, rel_rank, trust; operator table
        |  segment_crowding.parquet   operator_overload.parquet
        +--> [validation/05_validate.py]  J1-J6
        +--> [router/router.py]           pinned requested departure, then guarded time shifts
        +--> [crowding/query.py]          get_crowding(...) + CLI + GeoJSON
        +--> [calib/01_operated_trips.py] S1-lite (city x hour scalar), S1 (per route) stretch
        v
[app/streamlit_app.py] Tab 1 Planner | Tab 2 Route Explorer | Tab 3 Operator | Tab 4 Evidence
[docs/technical_note.md] [README.md] [Makefile]
```

**Model in one paragraph (for the note).** Each ticket becomes occupancy credits on every segment between boarding and alighting (15% one stop early, 70% at the stop, 15% one stop late), timed by GTFS run times. Credits are summed per route-direction × segment × day type × time bin and divided by the buses that ran there (scheduled trips, optionally corrected by an operated ratio) to give *tagged riders per bus*. Tickets are a small share of riders (~2–3.5%), so the rate is smoothed by a Gamma-Poisson posterior around a separable prior and scaled by kappa, anchored to published ridership. The on-board load comes with a calibrated 10–90% interval and is compared with bus-size-specific seats and maximum capacity to give three tiers.

---

## 3. EDA study and real-data fix playbook (H0–H2)

`etl/01_eda_study.py` runs the five categories below, prints a one-page scorecard to `eda_report.json`, and sets `config.yaml`. The numbered queries underneath are v5's original 12; keep them as the implementation list.

| Category (v5 query #) | Measures | Classification | Fix applied |
|---|---|---|---|
| **1. Volume & calendar** (#1, 2, 6, 8, 12) | Daily tickets and `SUM(pax)`; hour-by-day completeness; Mon–Fri homogeneity (Mon/Fri vs Tue–Thu, AM/PM shares); `pax` distribution; exact duplicates | Clean / Outage hours / Holiday dip (complete day < 0.75 × weekday median) | Build a (day, hour) validity mask; `n_valid_days[day_type, bin]` replaces a flat day count. Tag holiday weekdays `sunday_holiday` and print them for eye review (candidates to confirm against the calendar and the data's own dips: Fri 2 Oct, and Dussehra around Tue 20 Oct). Cap `pax` at the 99.9th percentile (default 20). Dedupe exact `(ts, route, start, end, vehicle_number)`. If Mon/Fri differ from Tue–Thu by > 10%, split or footnote |
| **2. Fleet scope & kappa anchor** (#9) | Ticket share on DTC vs cluster GTFS routes; mean daily `SUM(pax)`; share by `is_ac` and vehicle prefix | DTC-only / DTC + cluster / Skewed fleet (> 1.5× capture gap) | Set `kappa` and `kappa_grid` (0.6×–1.25×). If skewed, enable `kappa_by_group` (S2) and always show scale-free `rel_rank` |
| **3. Vehicle & bus size** (#3, 5, 11) | `% NULL vehicle_number`; NULL vs non-NULL by hour, route, `is_ac`; median tickets per non-NULL vehicle-day; top-30 prefixes | Representative / Biased NULLs; Sparse (< 40/veh-day) / Dense | Representative: NULL rows inherit the route's non-NULL size mix; biased: city-wide mix. Sparse: trip-level count stays a diagnostic |
| **4. Fare & ticket type** (#4, 10) | `ticket_type_id`; share of `amount == 0`; modal fares by `is_ac` (e.g. 9, 13.5, 22 look like a 10% discount on 10/15/25, which signals app/QR tickets) | Paid app/QR only / includes free-pass | Do **not** add a pass uplift: kappa anchors to total published ridership, which already includes free and pass riders. But note the cost: if free riders are captured differently from paying riders, uniform capture is violated. State that in the note and let J4 quantify it |
| **5. Route & stop matchability** (#7) | Parse rate of `^(.*?)(UP\|DOWN)$`; % routes in GTFS; exact stop-name match; `start == end` rows | High ≥ 90% / Moderate 75–90% / GTFS drift < 75% | Fill `manual_overrides.json` from `unmatched_top50.csv` until matched passenger share plateaus (target ≥ 93%, report the real number). Log and drop `start_seq >= end_seq`; report dropped volume of loop routes |

---

## 4. Data contracts (freeze at H2; adding columns is fine, renaming is not)

### Contract A: `segment_crowding.parquet`

| Column | Type | Notes |
|---|---|---|
| `route_key`, `route_base`, `direction` | str | `"8UP"`, `"8"`, `"UP"` |
| `seg_seq` | int | segment `k` between stop `k` and `k+1` |
| `from_stop_id`, `to_stop_id`, `from_name`, `to_name` | str | GTFS ids and display names |
| `from_lat`, `from_lon`, `to_lat`, `to_lon` | float | polyline |
| `day_type` | str | `weekday` / `saturday` / `sunday_holiday` |
| `bin30` | int | 0..47 (36 = 18:00–18:30 IST); emitted only where `trips_scheduled > 0` |
| `est_load`, `load_p10`, `load_p90` | float | posterior mean, calibrated 10–90% interval (sampling uncertainty only) |
| `seat_cap`, `max_cap` | float | route-mix capacity |
| `crowding_tier` | str | `Seats Available` / `Standing Room` / `Crowded` |
| `p_crowded` | float | P(**mean** load > 0.8 × `max_cap`); "typical bus", not a per-bus probability |
| `rel_rank` | float | 0..1 percentile within `day_type`; invariant to kappa |
| `credits_n`, `tickets_n` | float, int | raw credits; distinct ticket records |
| `trust_level` | str | `High` / `Medium` / `Low` |

### Contract B: `operator_overload.parquet`

| Column | Type | Notes |
|---|---|---|
| `route_key`, `day_type`, `hour` | str, str, int | key |
| `trips_hr` | float | scheduled trips/hr at the busiest segment |
| `peak_load`, `peak_seg_seq`, `peak_from_name`, `peak_to_name` | | worst stretch |
| `overload_rider_min` | float | `SUM_seg seg_min × D_seg × max(0, est_load/thr − 1)`, with `D_seg = est_load × trips_hr` (riders/hr) and `thr = 0.8 × max_cap` |
| `relief_rider_min` | float | `overload_rider_min(n) − overload_rider_min(n+1)` with `est_load' = est_load × n/(n+1)`. For fixed demand this equals `D²/thr × (1/n − 1/(n+1))` per segment-minute while both are overloaded, so it grows with the number of riders and severity |
| `tier_now`, `tier_plus1` | str | tier today vs. with one more bus/hr |
| `peak_window_hours` | int | hours per day this route is Crowded (spike vs chronic) |
| `underused` | bool | all segments `< 0.5 × seat_cap` for the hour |
| `plus1_assumption` | str | `"fixed demand; load ~ 1/frequency"` |
| `trust_level` | str | |

### Contract C: `plan_journey(origin, dest, dep_time, day_type, mode="balanced", lam=None) -> list[dict]`

```python
{
  "option_label": "Fastest" | "Balanced" | "More Comfortable" | "Leave 20 min later" | "Leave 15 min earlier",
  "is_time_shifted": False,
  "departure_time": "18:10",          # actual boarding time of the first leg
  "boarding_trip_key": "8UP|18:10",   # used for dedupe
  "arrival_time": "18:55",
  "total_minutes": 55,                # requested time -> arrival (wait + ride)
  "wait_minutes": 10,
  "transfers": 0,
  "overall_tier": "Crowded",          # worst-stretch tier
  "board_tier": "Standing Room",      # tier on the first stretch after boarding
  "trust_level": "High",
  "confidence_text": "Based on 120 ticket records on this route and hour",
  "summary_note": "Crowded on middle stretch: ITO -> Mandi House",
  "comfort_penalty": 18.5,
  "legs": [{
    "route_key": "...", "board_stop": "...", "alight_stop": "...", "minutes": 41,
    "stretches": [{"from_stop": "...", "to_stop": "...", "tier": "Crowded",
                   "load": 71, "p90": 84, "coords": [[lat, lon], [lat, lon]]}]
  }]
}
```

### Contract D: `crowding/query.py`
`get_crowding(route_key, from_stop, to_stop, day_type, time_str) -> DataFrame` (Contract A columns; loads linearly interpolated between 30-min bin centres) and `python -m crowding.query --route 8UP --time 18:20 --day weekday`.

---

## 5. Method (coding spec)

### 5.1 `etl/00_load.py`
```python
import duckdb, zipfile, pathlib

def load(zip_path="data/raw/October-Delhi.csv.zip"):
    pathlib.Path("data/interim").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:               # DuckDB does not read .zip directly
        name = z.namelist()[0]
        z.extract(name, "data/interim")
    duckdb.sql(f"""
      COPY (
        SELECT passenger_count::INT AS pax, amount::DOUBLE AS fare, vehicle_number,
               ticket_type_id, route, start_stop_name, end_stop_name,
               (created_at::TIMESTAMPTZ AT TIME ZONE 'Asia/Kolkata') AS ts, is_ac
        FROM read_csv('data/interim/{name}', header=true, nullstr='NULL')
        WHERE start_stop_name IS NOT NULL AND end_stop_name IS NOT NULL
          AND passenger_count BETWEEN 1 AND 20        -- confirm cap in EDA category 1
      ) TO 'data/interim/tickets.parquet' (FORMAT PARQUET)""")
```
`--only START:END` (inclusive IST dates) filters tickets; `make demo` uses it.

### 5.2 `etl/02_match.py`
1. Route parse: `re.match(r'^(.*?)(UP|DOWN)$', route)` -> `(route_base, direction)`.
2. Normalise stop names: lowercase, strip punctuation, expand abbreviations (`bdr`->`border`, `mkt`->`market`, `stn`->`station`; confirm each against the data first).
3. Route-scoped match: `rapidfuzz.process.extractOne(name, route_stop_names, scorer=fuzz.token_sort_ratio, score_cutoff=75)` against only that route's stops.
4. Direction by vote: for each ticket route string and GTFS direction, the share of matched tickets with `seq(start) < seq(end)`; pick the higher share; log both and the margin (< 0.2 flags the route).
5. Overrides from `unmatched_top50.csv`; re-run until the matched share stops improving.
6. Outputs: `matched_tickets.parquet` (`route_key, board_seq, alight_seq, pax, ts, fare, vehicle_number, is_ac, ticket_id`); `match_report.json`.
7. Day tagging per §3 category 1; print every tagged day.

**Timestamp assumption.** `created_at` is treated as the boarding time. ETM tickets may be issued after boarding and app tickets before it; J4 re-runs with the timestamp shifted ±5 min and reports tier stability.

### 5.3 `gtfs/build_graph.py`
- **Patterns:** for each route-direction, take the pattern with the most stops as canonical; map other patterns (short-turns, variants) onto it by `stop_id`. A segment's `trips_scheduled` counts every trip whose pattern covers that segment.
- **Loops/circular routes** (first stop = last stop): keep stop indices 0 and L−1 distinct; tickets with `start == end` are dropped and their volume reported.
- Cumulative scheduled minutes = median run time over trips covering the segment; scheduled trips per (`day_type`, bin, segment); next-departure arrays per stop; stop clusters (same normalised name or within ~400 m); `cKDTree` neighbour map (≤ 300 m); cumulative haversine distance per route (for the fare check). Save as pickle + parquet.

### 5.4 `model/03_credits.py`
Segment `k` lies between stop `k` and `k+1`. A rider boarded at `s` is on segment `k` iff they alight at a stop `>= k+1`. Kernel `(0.15, 0.70, 0.15)` on stops `e-1, e, e+1`, clipped to `s < j <= L-1` and renormalised.
```python
def credits(s, e, L, kernel=(0.15, 0.70, 0.15)):
    cand = {e-1: kernel[0], e: kernel[1], e+1: kernel[2]}
    cand = {j: w for j, w in cand.items() if s < j <= L-1}
    if not cand: return []
    tot = sum(cand.values())
    cand = {j: w/tot for j, w in cand.items()}
    return [(k, sum(w for j, w in cand.items() if j >= k+1))
            for k in range(s, max(cand))]
```
- Segment time for a ticket at `ts`: `ts + (cum_min[k] - cum_min[s])` -> `(day_type, bin)`. Scheduled run times are an assumption; real peak speeds are slower. The 30-min bins absorb part of it, and J4 reports the effect.
- `C[route_key, seg, day_type, bin, week] = SUM(pax × weight)`; `tickets_n` = distinct `ticket_id` with weight > 0.
- `T = n_valid_days[day_type, bin] × trips_scheduled[...]`, also stored per week.
- A plain Python/Numba loop over 2.56 M tickets × ~8 segments is fast enough; do not vectorise first.
- Weekday: 30-min bins. Saturday and Sunday/holiday (4–5 days each): 60-min bins broadcast to two 30-min bins.

### 5.5 `model/04_posterior.py`
**Rate:** `lam_raw = C / (T × operated_ratio)`; `operated_ratio` defaults to 1.0 (S1-lite may set it per city-hour).

**Zero-service guard and smoothing:** mask `T == 0` cells before and after convolving `C` and `T` with `[0.25, 0.5, 0.25]` across adjacent bins (renormalise edge weights over active bins only; never across day-type boundaries). Masked cells are not emitted.

**Separable prior (fit inside each fold):** `mu0[r,k,b] = R[r,b] × S[r,k]`; `R` = route-direction × bin mean rate over segments; `S` = segment rate relative to the route-direction daily mean (mean 1). Fallback when `R` has too few credits: `route_level[r] × city_hour_profile[day_type, b]`.

**Posterior:**
```python
from scipy.stats import gamma
def posterior(C, T, mu0, max_cap, b0=20.0, phi=2.0, kappa=48.0):
    # arrays over ACTIVE cells only (T > 0)
    a0 = mu0 * b0
    shape, rate = (C + a0) / phi, (T + b0) / phi
    scale = 1.0 / rate
    mean = shape * scale                                   # independent of phi
    p10 = gamma.ppf(0.10, a=shape, scale=scale)
    p90 = gamma.ppf(0.90, a=shape, scale=scale)
    p_crowded = gamma.sf(0.8 * max_cap / kappa, a=shape, scale=scale)
    return kappa * mean, kappa * p10, kappa * p90, p_crowded
```
**`phi` calibration (not clamped):** for each leave-one-week-out fold, fit on the other weeks; the held-out week's rate is `C_new / T_new`, with predictive `C_new = phi × NB(n=shape, p=rate/(rate + T_new/phi))` (this keeps the predictive mean at `T_new × mean` and inflates variance by `phi`). Choose `phi` on grid `(1, 1.5, 2, 3, 5, 8)` so 10–90% coverage is about 80% (accept 75–85%) on cells with `T_new` above a minimum. Report coverage by `tickets_n` bucket. If even `phi = 8` gives coverage < 70%, flag those buckets as misspecified and show only `rel_rank` and the tier with a Low badge for them.

**kappa:** `kappa = published_daily_ridership / mean_daily(SUM pax)`.
- DTC + cluster ~40.8 lakh/day -> kappa ~ 48; DTC only ~24–25.6 lakh/day -> kappa ~ 29–31. The EDA fleet-scope result chooses; report results over `kappa_grid` covering both.
- No separate pass-holder uplift (published ridership already includes them).
- **Capture heterogeneity (J4):** compare each fleet group's ticket share with its ridership share (electric/CNG, AC/non-AC, prefix). If any group differs by > ~1.5× from average, use `kappa_by_group` (S2); otherwise keep the global kappa and report it as a limitation.

**Bus size:** from the prefix map; route `seat_cap`/`max_cap` = weighted mix over observed vehicles (NULL rows per §3 category 3). Default priors (state as assumptions): 12 m = 38 seats / 75 max; 9 m = 28 / 50.

**Tiers:**
```python
import numpy as np
def tier(L, seats, maxcap):
    return np.where(L < 0.9*seats, "Seats Available",
           np.where(L < 0.8*maxcap, "Standing Room", "Crowded"))
```
Display "usually crowded" when `p_crowded >= 0.5`.

**Trust:** `High`: `tickets_n >= 30` and `p90/p10 <= 1.8`; `Medium`: `tickets_n >= 10` and `p90/p10 <= 3.0`; `Low` otherwise. **Starting values only:** after `phi` is fixed, review the badge distribution (Low should not swallow the busy routes) and adjust thresholds, never `phi`. Always show a text badge (not colour alone) and "Based on N ticket records…".

**Operator table:** per route × day type × hour per Contract B; rank "where the next bus helps most" by `relief_rider_min`.

**Trip-level diagnostic (J6 only):** on vehicle-days with high ticket density, build trip sessions (gap > 45 min or `board_seq` reset), compute running tagged load per trip, correlate with the model's cell estimates; report the correlation and how many trips qualified.

### 5.6 `router/router.py`
```python
def plan_journey(origin_cluster, dest_cluster, dep_time, day_type, lam=None):
    # Step 1: Pareto frontier on the requested departure only
    base = get_candidates(origin_cluster, dest_cluster, dep_time, day_type)   # arrival measured from dep_time
    front = pareto(base, keys=("total_minutes", "comfort_penalty", "transfers"))
    best_tier = min(TIER_RANK[c["overall_tier"]] for c in front)
    taken = {c["boarding_trip_key"] for c in front}

    # Step 2: time shifts, appended only if they add a new bus AND strictly improve the tier
    shifted = []
    for dt in (15, 20, -15):
        cands = get_candidates(origin_cluster, dest_cluster, dep_time + dt, day_type, arrival_ref=dep_time)
        better = [c for c in cands
                  if c["boarding_trip_key"] not in taken
                  and TIER_RANK[c["overall_tier"]] < best_tier]
        if better:
            shifted.append(min(better, key=lambda c: (c["total_minutes"], c["comfort_penalty"])))
    return label_and_merge_stretches(front, shifted[:2], lam)   # label N from actual boarding-time difference
```
- `score()`: wait = next scheduled departure at the boarding stop ≥ t (scheduled, not real-time); per segment, crowding looked up at the minute the bus reaches it, linearly interpolated between bin centres; `comfort_penalty = SUM(seg_min × tier_weight)`; trust = worst on any segment; `board_tier` = tier of first stretch.
- `total_minutes` for shifted options is measured from the **requested** time so a later bus is not shown as free.
- **Slider:** `lam in [0,1]` picks `argmin(total_minutes + lam × K × comfort_penalty)` over the base frontier (`K = 1`); pills map to fixed `lam` (config).
- **Stretches:** 3-segment moving average, assign tiers, merge same-tier runs, absorb stretches shorter than `min_stretch_segments` (2) into the neighbour with closer load.
- One-transfer routing is a late, cuttable addition (≤ 300 m walk, +6 min). Target < 100 ms per query after startup (precompute arrays per route-direction).

### 5.7 Stretch items
- **S1-lite (try at H10–H12):** from one weekday GPS day and one Sunday, count distinct `(vehicle_id, trip_id)` per hour for DTC routes against scheduled DTC trips in the same hour; use as a city × hour `operated_ratio`, clipped to [0.5, 1.2], only if the GPS `trip_id`/`route_id` joins to GTFS for ≥ 70% of DTC routes. Otherwise stop and keep 1.0.
- **S1 (per route):** the same by route if time allows; the GPS feed is raw (duplicates, gaps, noise), so report ratios in the note and use them only if the median ratio lies in [0.6, 1.2].
- **S2:** group-specific kappa if J4 shows capture heterogeneity > ~1.5×.

---

## 6. Validation J1–J6 (`validation/05_validate.py`)

| Check | Procedure | Output |
|---|---|---|
| **J1. Match & geometry** | % passengers matched (≥ 93%); % dropped by `seq(start) >= seq(end)`; direction-vote margins; fare vs cumulative GTFS distance, Spearman by `is_ac`; share of tickets with implausible fare/distance | Match table; flags bad stop matches |
| **J2. Out-of-sample** | Mon–Sun weeks (1 Oct is a Thursday, so first and last weeks are partial; weekend folds are very noisy). Leave-one-week-out with prior, `S`, `R`, `phi` re-fit in-fold: Spearman, 3-tier agreement, MAE on cells with `tickets_n >= 10`, mean ± sd over folds. Split-half reliability (alternate weeks) with Spearman–Brown. Interval coverage by `tickets_n` bucket | Coverage table (≈ 80%), chosen `phi`, reliability vs sample size (justifies badges) |
| **J3. Known patterns** | AM (08:30–10:00) and PM (18:00–20:00) load vs midday (13:00–14:30), city-wide and top-20 routes; weekday vs Sunday on top-20; top-20 stops by boardings and top-20 segments by load vs known hubs (Anand Vihar, Kashmere Gate ISBT, Nehru Place, Azadpur, Rajiv Chowk/Connaught Place, AIIMS) | Ratios and tables with commentary |
| **J4. Plausibility & sensitivity** | Share of route-hours with `est_load > 1.5 × max_cap`; **load-factor check**: the implied distance-and-time-weighted mean load across all operated segment-trips should be the same order as a back-of-envelope figure (about 630 boardings/bus-day × ~7 km ÷ ~210 bus-km ≈ 20 riders; every input is approximate: fleet size, trip length and bus speed must be verified before it is quoted). % cells with unchanged tier across `kappa_grid`, kernel on/off, 30 vs 60-min bins, `b0` ×0.5/×2, timestamp ±5 min. Capture-heterogeneity table (electric/CNG, AC/non-AC, tickets per scheduled trip by group) | Headline tier-stability number; unstable cells feed the trust badge; decides S2 |
| **J5. Router & golden case** | 10 direct OD pairs: in-vehicle minutes equal GTFS scheduled time; no boarding against stop order; **Anand Vihar -> Connaught Place, 6 PM weekday** returns 2–4 options including the requested-time bus; **no two options share a boarding trip**; the requested bus never disappears when a shifted option exists; latency < 100 ms | Pass/fail table in `tests/` |
| **J6. Blind spots** | Bottom-15 route-hours by trust; dense-vehicle-day trip diagnostic (correlation, trips qualified); list of known gaps (late night, sparse feeders, weekends, holidays, adoption bias, kappa anchor) | Goes straight into the technical note |

---

## 7. App (`app/`)
- **Banner:** "Typical crowding for this day type, based on October 2026 tickets — not live."
- **Palette (Okabe-Ito) + icon + word:** Seats Available blue `#0072B2` (seat icon); Standing Room amber `#E69F00` (standing person); Crowded vermillion `#D55E00` (crowd). Never colour alone. Design for 390 px width first.
- **Tab 1 Planner:** stop-cluster autocomplete; day type + time; preset *Anand Vihar -> Connaught Place, weekday, 6 PM*; mode pills + slider; 2–4 option cards, each with depart/arrive, total and wait, transfers, overall tier badge, "At your boarding stop: …" (`board_tier`), trust badge + `confidence_text`, stretch strip, `summary_note`; map of the selected option.
- **Tab 2 Route Explorer:** route-direction + day type; static 18:00 map first, time slider only if time allows; tier-coloured segments; tooltip with interval and ticket count.
- **Tab 3 Operator:** route × hour heatmap of `overload_rider_min`; top-20 overloaded stretches; "Where the next bus helps most" by `relief_rider_min` with the stated assumption; "Underused route-hours".
- **Tab 4 Evidence:** J1–J6 tables and charts; match report; kappa sensitivity; tier stability; least-reliable list.

---

## 8. Repo, config, Makefile

```text
repo/
  Makefile  README.md  requirements.txt  config.yaml  SHA256SUMS
  data/{raw,interim,processed,mock}/
  etl/00_load.py  01_eda_study.py  02_match.py  manual_overrides.json
  gtfs/build_graph.py
  model/03_credits.py  04_posterior.py
  calib/01_operated_trips.py
  validation/05_validate.py  outputs/
  router/router.py
  crowding/query.py
  app/streamlit_app.py  tabs/{planner,explorer,operator,evidence}.py  data/
  tests/  (test_credits.py, test_posterior.py, test_router.py, test_operator.py)
  docs/technical_note.md
```

`config.yaml` (single source of truth): `seed`, `kappa`, `kappa_grid`, `kappa_by_group` (optional), `kernel: [0.15, 0.70, 0.15]`, `bin_minutes: 30`, `weekend_bin_minutes: 60`, `prior_pseudo_trips: 20`, `phi_grid: [1, 1.5, 2, 3, 5, 8]`, `phi` (chosen), `operated_ratio_path` (optional), `bus_prior` (seats/max/prefix map), tier thresholds, `trust_thresholds`, `transfer_penalty_min: 6`, `tier_weights: {Seats: 0, Standing: 0.5, Crowded: 1.5}`, `min_stretch_segments: 2`, `mode_lam: {fastest: 0, balanced: 0.35, comfortable: 1.0}`.

**Tests:** `test_credits.py` (hand-made tickets incl. adjacent stops, last stop, first stop; mass equals expected segments); `test_posterior.py` (T = 0 never emitted; mean independent of `phi`); `test_router.py` (requested bus never dropped; no duplicate boarding trips; labels from real time differences; no backward boarding); `test_operator.py` (relief grows with load and riders, no saturation).

```make
verify:   ; shasum -a 256 -c SHA256SUMS
all: load eda match graph model validate app-data
load:     ; python etl/00_load.py
eda:      ; python etl/01_eda_study.py
match:    ; python etl/02_match.py
graph:    ; python gtfs/build_graph.py
model:    ; python model/03_credits.py && python model/04_posterior.py
validate: ; python validation/05_validate.py
demo:     ; python etl/00_load.py --only 2026-10-12:2026-10-18 && python etl/02_match.py && python gtfs/build_graph.py && python model/03_credits.py && python model/04_posterior.py   # one-week smoke run, target < 5 min
test:     ; pytest tests/
app-data: ; cp data/processed/*.parquet app/data/
run:      ; streamlit run app/streamlit_app.py
```
Pin `requirements.txt` and the Python version; seed everything from `config.yaml`; record per-stage wall-clock in `README.md`; commit the small processed Parquets (or attach as a release asset) so the hosted app does not need raw data. Use `make verify && make all` for a checked run.

---

## 9. 48-hour schedule

**Critical path (anything here slipping delays everything):** GTFS access -> stop matching -> graph with correct T -> credits -> posterior -> real Parquets. Everything else can run on mock data.

**Request GTFS access at H0** (Transport Stack + transportstack.hackathon@gmail.com); download Delhi OTD static GTFS as the fallback and start on it immediately.

| Hours | P1: Data & Validation | P2: Crowding model | P3: GTFS, router, app |
|---|---|---|---|
| **H0–H2** | Repo, requirements, `make verify`, load CSV, run the 5-category EDA, share the scorecard | `model/` skeleton, credits function + unit tests on hand-made tickets | Scaffold, 4-tab Streamlit shell with banner and palette, deploy hello-world |
| **H2 — Checkpoint 1** | Freeze Contracts A–D | **Generate 300-row mock Parquets** into `data/mock/` | Code against mocks |
| **H2–H10** | `02_match.py`; **hand-off v0 at H8** (top-100 routes by volume) | Credits on synthetic tickets, then on v0 from H8 | `build_graph.py` (done by H6, with pattern handling), router v1: 0-transfer, pinned requested departure, dedupe, arrival time |
| **H10 — Checkpoint 2** | Match rate, vote margins, unmatched volume | Confirm graph schema | |
| **H10–H12** | S1-lite gate: does GPS join to GTFS for ≥ 70% of DTC routes? If not, stop | | |
| **H10–H18** | Full match (report real %), fare-vs-distance QA, validity mask, holiday tagging | `04_posterior.py` incl. zero-T guard, in-fold prior, tiers, trust, operator table. **Real Parquets at H18** | Slider, time shifts, interpolated lookups; Tab 1 on mock data with preset, cards, badges, stretch strip, map |
| **H18 — Checkpoint 3** | Swap mock -> real Parquets everywhere | | |
| **H18–H24** | J1, J2 (coverage first) | **`phi` calibration**; trip diagnostic; first sensitivity table | Wire Tab 1 to real router and data |
| **H24–H26 — Checkpoint 4** | Live test: Anand Vihar -> Connaught Place, weekday 6 PM — 2–4 distinct, sensible options? Log defects | | |
| **H26–H34** | J3–J6; Tab 3, Tab 4; least-reliable table | Fix defects; J4 sensitivity; Tab 2; S2 if J4 says so; Method text | 10 real OD pairs (Rohini, Dwarka, Nehru Place, AIIMS, ISBT Kashmere Gate, Rajiv Chowk…); `summary_note` generator; 390 px readability pass |
| **H34–H36** | Validation and Reliability sections of the note | Method section final | Note formatting, claims -> evidence table, README |
| **H36** | **HARD FEATURE FREEZE** (bug fixes, reproducibility, pitch only) | | |
| **H36–H44** | Clean-venv `make all` and `make demo` on a second machine; record timings | End-to-end QA, latency check | Demo recording (2–3 min), final deploy |
| **H44–H48** | Buffer: public repo, links, submission dry-run; submit at ~H46 | | |

**Solo or pair:** do it in this order and stop adding at each gate: (1) load + EDA, (2) GTFS + match, (3) credits + posterior on one week (`make demo`), (4) router 0-transfer + Tab 1, (5) J1/J2 + `phi`, (6) full month + Tab 3, (7) J3–J6 + note, (8) Tab 2, Tab 4 polish. The pipeline and the note are the deliverables a judge scores first; Tab 2 and 1-transfer are the first things to drop.

**Build faster:** mock Parquets first (the app never waits on the model); top-100 routes first (hand-off v0); `make demo` one-week run as the inner loop; no vectorising before it is slow; 0-transfer only until Checkpoint 4; commit processed Parquets.

**Cut order if behind:** (1) S2, S1 per route; (2) Tab 4 polish (keep raw tables); (3) 1-transfer routing; (4) Tab 2 time slider; (5) S1-lite. **Never cut:** the EDA fixes, stop matching, the posterior with zero-T guard, `phi` calibration, J1–J6 numbers, technical note, hosted demo.

---

## 10. Technical note outline (2–3 pages, numbers first)

1. **Model card:** purpose, inputs, outputs, intended use (typical-day crowding from October 2026 tickets, not live), what it is not.
2. **Data and EDA findings** (from `eda_report.json`) and the config fix each caused; ticket source and representativeness (app/QR vs conductor tickets).
3. **Method:** credits -> masked rate per bus -> Gamma-Poisson -> kappa -> tiers; why a per-trip running count is only a diagnostic (about 1–2 tickets per trip at ~2% capture), and how the diagnostic compares.
4. **Assumptions and sensitivity:** kappa anchor and grid (press-sourced figures, confirm against a primary source), uniform capture across riders and routes, soft-alighting kernel, scheduled run times and trips, timestamp = boarding time, bus-size priors, free/pass riders inside kappa, `p_crowded` is about the typical bus.
5. **Claims -> evidence table:** every claim in the app or note mapped to a J1–J6 number.
6. **Where estimates are least reliable:** J6 bottom-15; late night; low-frequency feeders; sparse cells; mixed-size routes; weekends (few days); October holidays/festivals; non-uniform ticketing adoption across fleets and depots; GTFS fallback mismatch; absolute load levels rest on kappa (no independent on-board count available).
7. **How to rerun:** `make verify && make all`, `make demo`, wall-clock timings.

---

## 11. Demo script (2–3 min)
1. Tab 1: Anand Vihar -> Connaught Place, weekday 6 PM. Show fastest / balanced / leave-later with tier badges and the boarding-stop line; move the slider.
2. Tab 2: the crowded stretch on the map; hover for interval and ticket count.
3. Tab 3: where the next bus helps most (with its assumption) and underused route-hours.
4. Tab 4: reliability vs sample size, interval coverage, tier stability, peak ratios, corridor check, least-reliable list.

---

## 12. Sources for anchors (unverified figures: confirm before quoting)
- Daily DTC + cluster ridership ~40.8 lakh (FY2024-25, Economic Survey of Delhi, via press summary): https://www.newsbytesapp.com/news/auto/economic-survey-2025-26-finds-delhi-bus-ridership-down-nearly-20/story
- DTC average daily ridership 24.29 lakh (2025-26) and fleet mix: https://dailypioneer.com/news/delhis-vehicle-count-hits-87-6-lakh-8-rise-in-2025-26
- Electric buses ~40% of DTC daily ridership (rough check in J4 only): https://www.newsbytesapp.com/news/india/delhi-to-add-500-more-electric-busses-to-its-fleet/tldr
- Delhi Open Transit Data static GTFS (fallback; last updated June 2024): https://otd.delhi.gov.in/data/static
- Fleet and ridership figures differ between sources. The back-of-envelope load-factor inputs in J4 are approximate.
- Bus GPS (GTFS-RT archive, DTC buses only, raw) is used only for S1-lite/S1; the core model does not depend on it.
