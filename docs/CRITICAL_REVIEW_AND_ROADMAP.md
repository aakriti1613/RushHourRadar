# Critical Review, Research & Roadmap — Delhi Crowdedness-Aware Journey Planner (PS 2)

*Prepared 24 Nov 2026. Every number below comes from the repository's own outputs or from a read-only probe run on the repository's data (`segment_credits.parquet`, weekday cells, leave-one-week-out, test weeks 2–5). Nothing here is estimated from memory unless marked "literature".*

---

## 1. Understanding of the problem

**What PS 2 asks for** (brief + hackathon page): turn ~2.56 M October-2026 e-ticket/ETM records plus static GTFS into an estimate of **people on board by route, direction, stretch and time of day**; map that to a commuter-readable level (seats / standing / crowded); show it beside each option in a planner; let the user trade speed for comfort; serve commuters *and* operators ("which routes and hours are overloaded and where extra buses would help most").

**How it is judged** (brief, verbatim): *"How well the crowding estimate is grounded in the data, and how clearly it is shown to commuters."* The page adds that estimates should *"hold up when spot-checked against known peak and off-peak patterns"*. The brief also says *"A simple planner with a sound crowding model is better than a polished planner with a weak one."*

**Phase structure** (hackathon page): Phase 1 = two weeks, "a proposal and a working prototype"; Phase 2 (shortlisted) = two more weeks with mentors; finale 11 Dec 2026. The page gives no Phase-1 deadline date, rubric weights or submission format. The full brief is a Google Doc; its text appears to match the local `Crowdedness-aware journey planner problem statement brief.txt`.

**The real difficulty** is not routing. It is that the ticket data is a small, *non-random* sample of riders:
- About 2.5% capture on average, and capture varies hugely by route (shown below).
- 100% of tickets are `ticket_type_id = 1`, with fares at exactly 90% of the standard slabs (4.5/9/13.5…). That suggests the file is mostly or entirely **discounted digital (app/QR) tickets**, not conductor ETM tickets, so "uniform capture" is the core assumption at risk.
- There is no on-board count to validate against, so absolute load is only partly identified. What *can* be validated is week-to-week predictive accuracy, ranking, and plausibility against capacity.

## 2. Understanding of the current project

| Layer | Implementation | Notes |
|---|---|---|
| ETL | `etl/00_load.py` (DuckDB → parquet), `01_eda_study.py` (5 categories), `02_match.py` (RapidFuzz route-scoped, direction vote) | 93.58% pax matched; 0 dupes; holidays 2 Oct and 20 Oct detected |
| Graph | `gtfs/build_graph.py`: canonical (longest) pattern per route, cum. minutes, `trips_T_bins[seg,48]`, departures, 400 m clusters, 300 m KD-tree transfers | GTFS has **one `service_id` for all 7 days**, so the weekend schedule equals the weekday schedule |
| Credits | `model/03_credits.py`: kernel 15/70/15 tail sums, segment time = ts + Δcum_min, 5 calendar weeks | weekend 60-min pooling is described but **not implemented** |
| Model | `model/04_posterior.py`: mask T=0, smooth [.25,.5,.25], separable prior R[bin]·S[seg] per route, Gamma-Poisson with b0=20, φ from LOWO on 200 routes, ×κ=39.9, tiers, trust, operator table | single capacity (38/75) for **every** route |
| Router | `router/router.py`: direct trips only, inverted index, interpolated load, time shifts | adds its own hard-coded shrinkage and tier logic on top of the model |
| App | Streamlit, 4 tabs (Planner, Explorer, Operator, Evidence), PyDeck, Okabe-Ito tiers | DTC logo hot-linked from Wikipedia; "Built with Google DeepMind Antigravity" caption |
| Validation | `validation/05_validate.py` J1–J6, 12 pytest tests (all pass, verified today) | most checks are threshold flags, not out-of-sample evaluation |

## 3. Available vs missing information

| Available | Missing / not accessible |
|---|---|
| Brief (txt), hackathon page, v7 spec, repo, raw data, GTFS, all outputs | Both shared chat links and the ChatGPT share (they render client-side or return 403). The Claude share needs a browser; paste the text if it matters |
| | Judging rubric weights, Phase-1 deadline date, submission format (repo? video? hosted URL? page limit?) |
| | Any ground truth for on-board load (APC counts, conductor manifests). Absolute level therefore rests on κ |
| | Vehicle-prefix → bus length (9 m / 12 m) mapping. Must come from DTC/DIMTS fleet lists or the data's README |
| | Whether GTFS trip counts match operated trips (the GPS feed would answer this; not downloaded) |
| | A working Python env on this machine (3.12 is broken, 3.13 has no deps). I used a scratch venv for analysis |
| | The project is **not a git repository**, so nothing can be rolled back |

---

## 4. Critical review (prioritised)

### 4.1 Model — findings from the held-out probe

Leave-one-week-out on weekday cells (weeks 2–5 as test; week 1 has one weekday). The prediction target is the held-out week's tagged-riders-per-scheduled-bus. Mean over 4 folds:

| Model | Spearman (all cells) | Spearman (≥10 test tickets) | Spearman (≥30) | wMAE (≥30) |
|---|---|---|---|---|
| B0: route × bin mean (no stretch detail) | 0.701 | 0.540 | 0.509 | 2.08 |
| B1: raw cell rate C/T, no prior | **0.791** | **0.793** | **0.833** | **1.08** |
| M: current Gamma-Poisson posterior (b0 = 20) | 0.778 | 0.761 | 0.784 | 1.21 |

**Observed dispersion** (held-out var ÷ Poisson var) by expected count: 2.3 for [0,1), 2.5 for [3,10), 3.6 for [10,30), 5.1 for [30,100), 7.1 for ≥100. NB2 α ≈ 0.05–0.15 for busy cells, i.e. 22–38% week-to-week CV.

**Capture heterogeneity:** route-peak tagged riders per scheduled bus has median 0.86, p95 9.5, p99 18.4, max 41.2. **768 of 2,554 routes** imply a peak above 100 riders at κ = 39.9. In `segment_crowding.parquet`, 32,282 of the 1.01 M ticketed weekday cells (3.2%) exceed 1.5× crush capacity, and the maximum is **955 riders on one bus** (D-7701AUP, 08:30).

| # | Area | Existing | Weakness | Impact | Recommendation |
|---|---|---|---|---|---|
| 1 | **Calibration claim** | "φ = 1.0, 85.9% coverage" | Coverage by bucket is 87.5% / 52.7% / 41.9% / **6.7%** (<10 / 10–29 / 30–99 / ≥100 tickets). The headline is an average dominated by sparse cells. Intervals are badly over-confident exactly on busy routes | The README/Evidence claim is misleading; a judge reading `calibration_report.json` will see it | Replace constant φ with an **NB2 (mean-dependent) predictive** model, α fitted out-of-sample; report coverage per bucket; claim only what holds per bucket |
| 2 | **Shrinkage strength** | fixed b0 = 20 pseudo-trips; prior clipped to [0.01, 5] × route mean | The posterior is *worse* than no prior on well-sampled cells (Spearman 0.78 vs 0.83, wMAE 1.21 vs 1.08). It over-shrinks real peaks | The "Bayesian" layer currently hurts where users care most | Estimate shrinkage from the data (EB method-of-moments per route/day-type, or b0 chosen by held-out deviance). It must beat B1 on busy cells **and** B0/B1 on sparse cells |
| 3 | **Single global κ** | κ = 39.9 everywhere | Capture varies more than 40× between routes. 768 routes give physically impossible loads | Crowded tiers on app-heavy routes are artefacts; tiers on low-capture routes are under-stated | **Capacity-constrained expansion**: κ_r = min(κ*, crush_r / q95(tagged/bus on route r)), with κ* re-solved so the city total still equals published ridership. Report it as a bound, show `rel_rank` beside the tier |
| 4 | **Bus size (brief §5)** | one capacity (38/75) for every cell | The brief explicitly asks for 9 m vs 12 m. Not implemented anywhere | A named requirement is missing | Route-level capacity mix from `vehicle_number` prefix + route family; NULL vehicles inherit the route mix; prefix map stated as an assumption |
| 5 | **Router overrides the model** | `CITY_HOUR_PROFILE` hard-coded in router; shrinkage w = N/(N+15) only in planner; tier re-computed with 38/75; missing data → "25 riders, Seats Available" | The planner shows numbers that differ from Contract A and from validation. The profile is not traceable to data. Missing data is shown as "comfortable" | Ungrounded numbers in the exact place judges look | All shrinkage lives in the model. Router reads tiers/capacity from the model. Missing → "No estimate" state |
| 6 | **Uncertainty not used in ranking** | Golden case ranks 976UP (10 tickets, Low trust, "Seats") first | Sparse routes look empty because the prior is low | Recommends the least-known bus as most comfortable | Risk-aware comfort: score by P(crowded) or the p75 load, not the mean; never label a Low-trust option "More comfortable" over a High-trust one without a caveat |
| 7 | **Time-shift guard** | appends best shifted option regardless | Golden case shows "Leave 26 min later" with the same tier and +26 min (spec says strictly better tier) | Violates the v7 fix #2/#3 the spec documents | Implement the guard; label from real boarding difference |
| 8 | **No transfers** | direct only | Brief example (55-min option) and many OD pairs (Sarita Vihar → Mandi House returns 0) need one change | Empty results for common trips | 1-transfer search via transfer map (≤ 300 m walk) with transfer penalty |
| 9 | **Weekend/day-type** | weekend 60-min pooling not implemented; GTFS same schedule all days | Saturday High-trust cells 1.2%, Sunday 0.5%. Weekend loads assume weekday frequency | Weekend output is mostly prior | Implement pooling (or pool weekend days), state the schedule caveat |
| 10 | **Validation is mostly tautological** | J2 reads its own calibration file; J4 "tier stability 99%" on `LIMIT 100000` rows (mostly empty night cells); "system mean load" is an unweighted cell mean; J6 always passes | No baseline, no ablation, no point-accuracy metric | Weak against "grounded in data" | New evaluation harness (section 6) |
| 11 | **Trust/confidence text** | `confidence_text` sums tickets over every segment (same ticket counted many times); trip trust = modal segment trust | "Based on 1,327 ticket records" is inflated | Over-states evidence | Distinct tickets on the route-hour; trip trust = worst stretch |
| 12 | **Per-trip running count (brief §5)** | Not implemented (not even as diagnostic) | Brief names it explicitly | Judges will ask | Vehicle-day sessionisation on the 51% of rows with `vehicle_number` → diagnostic correlation with the cell model (J6) |
| 13 | Operator relief | quadratic rider-weighted overload | Inherits κ artefacts (D-7701AUP dominates). `trips_hr` = 2 × per-30-min count | Rankings driven by capture, not crowding | Recompute after #3; show trust beside each row |
| 14 | Engineering | no git; Python env broken; 36 MB parquet in `app/data`; f-string SQL; README claims ("production-grade", "Grand Prize", "6/6 passed") | Hard to reproduce and over-claims | Credibility | git init; pinned `requirements.txt` + lock; parametrised SQL; honest README |
| 15 | Product/branding | Third-party DTC logo hot-linked; Streamlit default look; tables over visuals | Implies official endorsement; UI not differentiated | — | Own identity (section 8); remove third-party marks |

### 4.2 What is good and should be kept
Matching pipeline (93.6% pax, route-scoped, direction vote), kernel credits (mass-conserving, tested), zero-T masking, the leave-one-week-out idea, Okabe-Ito triple encoding, Contract A–D shapes, the operator relief concept, the overall pipeline/Makefile structure.

---

## 5. Research — approaches considered

*Literature items are from my training knowledge; verify the citations before quoting them in the note.*

| Approach | How it works | Pros | Cons | Fit for PS 2 |
|---|---|---|---|---|
| **Gamma-Poisson empirical Bayes with data-driven shrinkage** (Clayton & Kaldor 1987, disease mapping; Efron–Morris EB) | Cell rate shrunk toward a hierarchical prior; shrinkage estimated from between-cell variance | Closed form, explainable, fast, works on sparse cells | Needs a good prior and a dispersion model | **Keep, fix (#1, #2)** |
| **NB2 predictive layer** (Cameron & Trivedi) | Var = μ + αμ²; α fitted on held-out weeks | Matches the observed dispersion growth (2.3 → 7.1) | One more parameter | **Adopt** for "usual range" and P(crowded on a typical day) |
| **Poisson GBM with exposure offset** (LightGBM `objective=poisson`, `init_score=log T`) | Learns rate from features (route, stretch position, hour, stop degree, AC share, hub flags) | Borrows strength across similar routes; good for sparse cells | Less transparent; needs a new dependency; can over-fit 4 weeks | **Evaluate as challenger / prior source**; adopt only if it wins held-out |
| **Capacity-constrained expansion** (own proposal, a partial-identification bound) | Cap each route's κ so its peak can't exceed crush load; re-solve global κ* to keep city total | Removes impossible loads; uses physics, not guesswork | Still not ground truth; it is a bound | **Adopt**, stated as an assumption |
| **Trip chaining / destination inference** (Trépanier et al. 2007; Barry et al. 2002) | Infer alighting from the next boarding | Standard for AFC | Our tickets already carry destination; no card IDs | Not needed; the soft-alighting kernel covers early/late exits |
| **Per-trip running count** | Group tickets by vehicle-trip, cumulative on − off | Literal reading of brief §5 | ~5 tickets per vehicle-day → about 1 per trip | **Diagnostic only** (shape check) |
| **Deep spatio-temporal models** (GNN / LSTM passenger-flow forecasting) | Learn flows on a graph over time | State of the art for dense AFC/APC | 4 weeks, ~2.5% sample, no load labels. Would learn ticket noise; unexplainable | **Reject**, with the reason stated in the note |
| **Crowding valuation** (Wardman & Whelan 2011; Tirachini, Hensher & Rose 2013) | Standing/crowded in-vehicle minutes weighted ×1.2–2+ | Gives the comfort trade-off a literature basis | Values come from UK/Australia | **Adopt** to set tier weights → "feels like N min" |
| Interval scoring (Gneiting & Raftery 2007) | Proper scores (interval score / CRPS) | Judges interval quality, not just coverage | — | **Adopt** in evaluation |

**Selected approach:** a hierarchical Gamma-Poisson EB model (data-driven shrinkage, route→city prior), an NB2 predictive layer for day-to-day range, capacity-constrained route κ and route-level bus size. A Poisson-GBM challenger is kept only if it wins held-out. This stays explainable in one paragraph, and every component has a falsifiable held-out test.

---

## 6. Model evaluation plan (what will be *proved*)

All on held-out weeks (LOWO, weekday; weekend reported separately as low-power). Each metric is reported overall and by ticket bucket (<10, 10–29, 30–99, ≥100), as mean ± sd over folds.

1. **Point accuracy:** Spearman, Poisson deviance, exposure-weighted MAE of tagged/bus.
2. **Tier accuracy:** confusion matrix and macro-F1 of tier(pred) vs tier(held-out rate), under a fixed κ_r.
3. **Interval quality:** 10–90% coverage + interval score per bucket. Target 75–85% in *every* bucket with ≥ 1,000 cells.
4. **Baselines:** B0 route×bin, B1 raw cell, M_old (current), M_new, GBM challenger.
5. **Ablations:** −prior, −smoothing, −kernel (hard alighting), b0 fixed vs learned, κ global vs capped, φ vs NB2.
6. **Reliability:** split-half (alternate weeks) + Spearman–Brown, by bucket. This justifies the trust thresholds.
7. **Known patterns (J3), weighted properly:** passenger-km-weighted AM/PM/midday, weekday vs Sunday, top corridors/stops vs hubs.
8. **Plausibility (J4):** share of *ticketed* cells over 1.0× / 1.5× crush (before vs after the κ cap); bus-km-weighted mean load vs back-of-envelope; tier stability across κ grid **on daytime ticketed cells**.
9. **Router (J5):** golden OD set incl. a transfer case; guard invariants; latency.
10. **Trip-level diagnostic (J6):** running-count shape vs cell-model profile on dense vehicle-days.

Outputs: `outputs/eval/*.json|csv` + charts rendered in the Evidence page from those files (never hard-coded).

---

## 7. Features

| Class | Feature | Why | Priority |
|---|---|---|---|
| Essential | Route-level bus size + capacity-aware κ | Brief §5; removes impossible loads | P0 |
| Essential | Honest "No estimate" / Low-trust states; distinct-ticket confidence text | Grounding | P0 |
| Essential | 1-transfer journeys | Brief example; zero-result OD pairs | P1 |
| High value | **"Best time to leave" strip**: crowding of the best option for each departure across ±90 min | Turns "leave 20 min later" into one glance | P1 |
| High value | **"Feels like N min"** perceived time using crowding multipliers | Makes the speed/comfort trade-off concrete | P1 |
| High value | Route **heat-strip** (stops × time-of-day) in Explorer | The single clearest view of "where and when" for operators | P1 |
| High value | Operator **what-if**: +k buses/hr on a route-hour → tier/overload change, with assumption shown | "Where extra buses would help most" | P1 |
| Novel | Risk-aware ranking ("usually seats, sometimes standing" from the NB2 predictive) | Uses uncertainty instead of hiding it | P1 |
| Novel | "Why this estimate" drawer: tickets, weeks observed, κ_r, capacity, week-to-week range | Explainability for judges and users | P2 |
| Future | GTFS-RT operated-trip correction; live bunching; APC calibration | Needs data not in scope | Note only |

---

## 8. UI/UX direction

**Problem with today's UI:** Streamlit defaults, a sidebar of static numbers, a yellow warning banner as the first thing seen, tables where visuals should be, emoji icons, and no visual identity.

**Direction:** calm, map-first and editorial. One neutral ink/paper base (light and dark), one brand accent kept away from tier hues, and the three Okabe-Ito tier colours reserved **only** for crowding, so colour always means crowding. Inter/IBM Plex-style type, a tabular-figure numeric scale, 8-pt spacing, SVG icons with the tier word always present.

**Information architecture:**
1. **Plan** (home): search card (from/to with autocomplete, time, day chips, Fastest ↔ Comfortable segmented control) → result cards (depart → arrive, total, "feels like", tier pill, crowding ribbon per stretch, boarding-stop tier, trust chip) → "best time to leave" strip → map with the selected option coloured by tier.
2. **Routes**: route picker → stops × time heat-strip + map + hover "why".
3. **Operators**: overload map/heat, ranked "where the next bus helps most", what-if slider, underused hours.
4. **Method & evidence**: model card, held-out charts (baselines, coverage by bucket, reliability), known-pattern checks, least-reliable list. All rendered from `outputs/eval`.

**Stack decision (needs your call):** Streamlit caps how far the design can go (layout, interactions, mobile). The option I recommend is a **FastAPI backend (wraps the existing router/query modules unchanged) + a hand-built static front-end (HTML/CSS/vanilla JS + MapLibre GL + a light chart lib)**, served from the same process as one deployable (Render / HF Spaces / Railway). The alternative is staying on Streamlit with a full custom theme and components.

---

## 9. Roadmap

| Stage | Work | Files | Verify by | Depends on |
|---|---|---|---|---|
| 0 | git init + snapshot; working venv; pinned deps | repo root | `make test` green from clean venv | — |
| 1 | Evaluation harness with baselines (before touching the model) | `validation/eval_holdout.py`, `outputs/eval/` | Reproduces the probe numbers above | 0 |
| 2 | Model v8: learned shrinkage, hierarchical prior, NB2 predictive, capacity-constrained κ_r, route bus size, weekend pooling | `model/03_credits.py`, `model/04_posterior.py`, `config.yaml` | Beats B1 on busy cells **and** B0/B1 on sparse; coverage 75–85% per bucket; ≤ 0.5% ticketed cells over 1.5× crush | 1 |
| 3 | Validation J1–J6 rewritten on top of the harness; ablations; trip diagnostic | `validation/05_validate.py` | Report has no self-referential "passed" flags | 2 |
| 4 | Router: remove overrides, guard, risk-aware ranking, 1-transfer, distinct-ticket confidence, "no estimate" state | `router/router.py`, `tests/test_router.py` | New invariant tests; golden set incl. a transfer | 2 |
| 5 | API + new front-end (or Streamlit re-theme), all 4 pages, mobile-first | `api/`, `web/` (or `app/`) | Browser check of every page at 390 px / 768 px / 1440 px; no console errors | 4 |
| 6 | Technical note, README, claims → evidence table, demo script | `docs/technical_note.md`, `README.md` | Every number traced to `outputs/eval` | 3, 5 |
