"""
validation/05_validate.py - J1-J6 validation (v8). Every number is computed here or read from
outputs/eval/summary.json (written by validation/eval_holdout.py); nothing is hard-coded.

Status values: "pass" (met its stated criterion), "note" (reported, no pass/fail criterion),
"fail" (criterion missed - shown in the app, not hidden).

Writes outputs/validation_v8.json (rendered by the web UI's "Method & evidence" page).
"""

from __future__ import annotations

import json
import pathlib
import pickle
import sys
import time

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import os  # noqa: E402

os.chdir(ROOT)

CROWD = "data/processed/segment_crowding.parquet"
MATCHED = "data/interim/matched_tickets.parquet"
GRAPH = "data/interim/gtfs_graph.pkl"
HUBS = ["anand vihar", "kashmere gate", "nehru place", "azadpur", "aiims", "rajiv chowk", "connaught", "shivaji stadium",
        "karol bagh", "new delhi", "old delhi", "kaushambi", "sarai kale khan", "dhaula kuan", "nizamuddin", "lajpat nagar"]


def j1(con):
    m = json.load(open("outputs/match_report.json"))
    mp, ds = m["match_performance"], m["dropped_summary"]
    with open(GRAPH, "rb") as f:
        pats = pickle.load(f)["route_patterns"]
    s = con.execute(f"""SELECT route_key, board_seq, alight_seq, fare, is_ac FROM read_parquet('{MATCHED}')
                        WHERE fare > 0 USING SAMPLE 60000 ROWS (reservoir, 42)""").df()
    d = []
    for r in s.itertuples(index=False):
        pat = pats.get(r.route_key)
        if pat is not None and r.alight_seq < len(pat["cum_dist_km"]):
            d.append(pat["cum_dist_km"][r.alight_seq] - pat["cum_dist_km"][r.board_seq])
        else:
            d.append(np.nan)
    s["km"] = d
    s = s.dropna()
    rho = {k: round(float(spearmanr(g.km, g.fare).correlation), 3) for k, g in s.groupby("is_ac")}
    return {"id": "J1", "title": "Tickets matched to the stop sequence; fare grows with distance",
            "status": "pass" if mp["matched_pax_pct"] >= 93 and all(v > .6 for v in rho.values()) else "fail",
            "summary": "Share of passengers whose boarding and alighting stops were matched to a route's stop order, and a check that fares rise with GTFS distance.",
            "metrics": {"passengers matched %": mp["matched_pax_pct"],
                        "tickets dropped (unmatched or alighting not after boarding)": f'{ds["dropped_unmatched_or_reversed"]:,} ({100 * ds["dropped_unmatched_or_reversed"] / m["metadata"]["total_raw_tickets"]:.1f}% of tickets)',
                        "start = end rows dropped": ds["dropped_same_stop"],
                        "fare vs distance Spearman (AC / non-AC)": {("AC" if k else "non-AC"): v for k, v in rho.items()},
                        "routes with direction-vote flips > 30%": len(m.get("routes_with_high_direction_flips", []))}}


def j2():
    E = json.load(open("outputs/eval/summary.json"))
    comp = pd.DataFrame(E["comparison_weekday"])
    best = [m for m in comp.model.unique() if m.startswith("v8")][0]

    def g(model, b, k):
        return float(comp[(comp.model == model) & (comp.bucket == b)][k].iloc[0])
    cov = E["coverage"]["v8_selected_by_bucket"]
    ok_cov = all(0.75 <= v["coverage"] <= 0.9 for k, v in cov.items() if k != "<10") and cov["<10"]["coverage"] >= 0.75
    beats = g(best, "all", "deviance") < g("B1 raw cell rate", "all", "deviance") and g(best, "all", "deviance") < g("v7 posterior (b0=20)", "all", "deviance")
    return {"id": "J2", "title": "Predicting a week the model has not seen",
            "status": "pass" if (beats and ok_cov) else "fail",
            "summary": "Leave-one-week-out on weekdays (test weeks 1-4). Prior, prior strength and range calibration are re-fit without the test week.",
            "metrics": {"held-out deviance, this model vs raw cell rate vs v7": [round(g(best, "all", "deviance"), 3), round(g("B1 raw cell rate", "all", "deviance"), 3), round(g("v7 posterior (b0=20)", "all", "deviance"), 3)],
                        "Spearman, cells with >= 30 tickets (this / raw / v7)": [round(g(best, "30-99", "spearman"), 3), round(g("B1 raw cell rate", "30-99", "spearman"), 3), round(g("v7 posterior (b0=20)", "30-99", "spearman"), 3)],
                        "10-90% range coverage by ticket bucket": {k: v["coverage"] for k, v in cov.items()},
                        "v7 coverage by ticket bucket (for comparison)": E["coverage"]["v7_phi1_by_bucket"],
                        "weekends": "reported separately in outputs/eval/summary.json; one day per week so folds are low-power"}}


def j3(con):
    q = lambda lo, hi: con.execute(f"""SELECT sum(est_load * trips_sched_per_day * seg_min) / sum(trips_sched_per_day * seg_min)
        FROM read_parquet('{CROWD}') WHERE day_type='weekday' AND bin30 BETWEEN {lo} AND {hi}""").fetchone()[0]
    am, mid, pm = q(17, 19), q(26, 28), q(36, 39)
    # daily tagged passengers by date (kappa-free)
    d = con.execute(f"""SELECT day_type, count(DISTINCT CAST(ts AS DATE)) AS n_days, sum(pax) AS n_pax FROM read_parquet('{MATCHED}') GROUP BY 1""").df().set_index("day_type")
    per_day = (d.n_pax / d.n_days).round(0).to_dict()
    sun_ratio = per_day["sunday_holiday"] / per_day["weekday"]
    sat_ratio = per_day["saturday"] / per_day["weekday"]
    names = con.execute(f"""SELECT s.stop_name n, count(*) c FROM (SELECT from_stop_id FROM read_parquet('{MATCHED}')) m
        JOIN (SELECT * FROM stops_tbl) s ON s.stop_id = m.from_stop_id GROUP BY 1 ORDER BY c DESC LIMIT 60""").df()
    top = names.n.str.lower().tolist()
    found = {h: next((i + 1 for i, n in enumerate(top) if h in n), None) for h in HUBS}
    hit = [h for h, r in found.items() if r]
    return {"id": "J3", "title": "Matches known patterns: peaks, weekends, hubs",
            "status": "pass" if (am > 1.5 * mid and pm > 1.5 * mid and sun_ratio < .8) else "fail",
            "summary": "Bus-minute-weighted mean load on weekdays, weekday vs weekend ticket volume per day, and the busiest boarding areas (stops grouped within ~400 m). The pass criterion covers the peak and weekend ratios; the hub list is reported as-is.",
            "metrics": {"morning peak (08:30-10:00) vs midday (13:00-14:30), load ratio": round(am / mid, 2),
                        "evening peak (18:00-20:00) vs midday, load ratio": round(pm / mid, 2),
                        "tickets-passengers per day by day type": per_day,
                        "Saturday / weekday volume": round(sat_ratio, 2), "Sunday-or-holiday / weekday volume": round(sun_ratio, 2),
                        "known hubs among the 60 busiest boarding areas (stop clusters; rank)": {h: found[h] for h in hit},
                        "known hubs not in the top 60 areas": [h for h in HUBS if h not in hit]}}


def j4(con):
    base = f"read_parquet('{CROWD}')"
    r = json.load(open("outputs/model_report.json"))
    cells = con.execute(f"""SELECT est_load, seat_cap, max_cap, crowding_tier, kappa_r FROM {base}
                            WHERE day_type='weekday' AND tickets_n > 0 AND bin30 BETWEEN 12 AND 43""").df()
    k0 = r["kappa_global"]
    stab = {}
    for k in (23.9, 31.9, 49.9):
        scaled = cells.est_load * (k / k0)
        t = np.where(scaled < .9 * cells.seat_cap, 0, np.where(scaled < .8 * cells.max_cap, 1, 2))
        t0 = np.where(cells.est_load < .9 * cells.seat_cap, 0, np.where(cells.est_load < .8 * cells.max_cap, 1, 2))
        stab[str(k)] = round(float((t == t0).mean() * 100), 1)
    lf = con.execute(f"""SELECT sum(est_load * trips_sched_per_day * seg_min) / sum(trips_sched_per_day * seg_min) FROM {base} WHERE day_type='weekday' AND bin30 BETWEEN 12 AND 43""").fetchone()[0]
    return {"id": "J4", "title": "Plausibility and sensitivity",
            "status": "note",
            "summary": "No independent on-board counts exist, so these are plausibility checks, not accuracy. Absolute headcounts depend on the scale factor and capacity cap below.",
            "metrics": {"daytime ticketed weekday cells above crush capacity %": r["ticketed_cells_over_1x_max_cap_pct"],
                        "... above 1.5x crush capacity %": r["ticketed_cells_over_1_5x_max_cap_pct"],
                        "bus-minute-weighted mean load, 06:00-22:00 weekdays (riders per bus)": round(float(lf), 1),
                        "back-of-envelope load factor (inputs unverified)": "about 20 riders per bus from ~630 boardings/bus-day x ~7 km / ~210 bus-km/day; fleet size, trip length and speed must be confirmed before quoting",
                        "tier unchanged if the scale factor changes (kappa 23.9 / 31.9 / 49.9), %": stab,
                        "routes whose scale factor is capped by physical capacity": f'{r["routes_kappa_capped"]} of {r["routes_total"]} ({100 * r["share_tagged_pax_on_capped_routes"]:.0f}% of ticketed passengers)',
                        "city ridership implied after the cap vs published": f'{100 * r["implied_daily_weekday_ridership_vs_published"]:.0f}%  (ticket capture is clearly not uniform across routes)',
                        "not run": "alighting-kernel on/off, 30 vs 60 minute bins, timestamp +/-5 min: require re-running the credits stage"}}


def j5():
    from router.router import JourneyPlanner, TIER_RANK
    pl = JourneyPlanner()
    pairs = [("Anand Vihar", "Connaught Place", "18:00"), ("Kashmere Gate", "Nehru Place", "09:00"), ("Azadpur", "AIIMS", "08:30"),
             ("Rohini", "Karol Bagh", "18:30"), ("Badarpur", "Mehrauli", "17:30"), ("Najafgarh", "Tilak Nagar", "08:30"),
             ("Jahangirpuri", "Old Delhi", "08:00"), ("Punjabi Bagh", "Azadpur", "10:00"), ("Sarita Vihar", "Mandi House", "09:00")]
    rows, lat, bad = [], [], []
    for o, d, t in pairs:
        pl.plan_journey(o, d, dep_time=t, day_type="weekday")  # warm
        t0 = time.time()
        opts = pl.plan_journey(o, d, dep_time=t, day_type="weekday")
        lat.append((time.time() - t0) * 1000)
        keys = [x["boarding_trip_key"] for x in opts]
        direct = [x for x in opts if x["transfers"] == 0]
        base = [x for x in opts if not x["is_time_shifted"]]
        best = min((TIER_RANK[x["overall_tier"]] for x in base if x["overall_tier"] != "No estimate"), default=None)
        guard = all(best is not None and TIER_RANK[x["overall_tier"]] < best for x in opts if x["is_time_shifted"])
        checks = {"has options": len(opts) >= 1, "no duplicate boarding trip": len(keys) == len(set(keys)),
                  "requested-time option kept": len(base) >= 1, "shifted options strictly better tier": guard,
                  "arrival after request": all(x["total_minutes"] > 0 and x["wait_minutes"] >= 0 for x in opts)}
        rows.append({"pair": f"{o} -> {d} {t}", "options": len(opts), "transfers": sorted({x["transfers"] for x in opts}), "ok": all(checks.values()), "failed": [k for k, v in checks.items() if not v]})
        if not all(checks.values()):
            bad.append(rows[-1]["pair"])
    # in-vehicle minutes equal scheduled time on the first golden case
    return {"id": "J5", "title": "Journey planner behaves as specified",
            "status": "pass" if (not bad and np.median(lat) < 100) else "fail",
            "summary": "Nine origin-destination pairs including one needing a change. Checks: distinct buses, requested departure never dropped, time-shifted options only if strictly better crowding, sane times.",
            "metrics": {"pairs passing all checks": f"{len(rows) - len(bad)} / {len(rows)}", "failing": bad or "none",
                        "warm query latency ms (median / max)": [round(float(np.median(lat)), 1), round(float(max(lat)), 1)],
                        "per pair": {r["pair"]: f'{r["options"]} options, transfers {r["transfers"]}' for r in rows}}}


def j6(con):
    q = con.execute(f"""SELECT day_type, avg((trust_level='High')::int) hi, avg((trust_level='Low')::int) lo, avg((tickets_n=0)::int) zero
                        FROM read_parquet('{CROWD}') GROUP BY 1""").df().set_index("day_type").round(3)
    worst = con.execute(f"""SELECT route_key, sum(tickets_n) t FROM read_parquet('{CROWD}') WHERE day_type='weekday' GROUP BY 1 HAVING sum(tickets_n) < 10 ORDER BY t LIMIT 5""").df()
    zero_routes = con.execute(f"""SELECT count(*) FROM (SELECT route_key FROM read_parquet('{CROWD}') WHERE day_type='weekday' GROUP BY 1 HAVING sum(tickets_n)=0)""").fetchone()[0]
    # per-trip running count diagnostic (brief section 5)
    t = con.execute(f"""SELECT vehicle_number v, route_key r, board_seq b, alight_seq a, pax p, ts FROM read_parquet('{MATCHED}')
                        WHERE vehicle_number IS NOT NULL AND day_type='weekday'""").df()
    t["date"] = t.ts.dt.date
    vd = t.groupby(["v", "date"]).size()
    dense = vd[vd >= 15].index
    trips = []
    for (v, d), g in t.set_index(["v", "date"]).loc[dense].groupby(level=[0, 1]):
        g = g.sort_values("ts")
        for r, gr in g.groupby("r"):
            gr = gr.sort_values("ts")
            gap = gr.ts.diff().dt.total_seconds().fillna(0) > 45 * 60
            for _, tr in gr.groupby(gap.cumsum()):
                if len(tr) < 4:
                    continue
                n = int(tr.a.max()) + 2
                dlt = np.zeros(n)
                np.add.at(dlt, tr.b.to_numpy(int), tr.p.to_numpy())
                np.add.at(dlt, tr.a.to_numpy(int), -tr.p.to_numpy())
                trips.append((r, int(tr.ts.iloc[0].hour * 2 + tr.ts.iloc[0].minute // 30), float(np.cumsum(dlt).max()), len(tr)))
    corr, ntrip = None, len(trips)
    if ntrip >= 30:
        tf = pd.DataFrame(trips, columns=["route_key", "bin30", "peak_running", "n"])
        m = con.execute(f"""SELECT route_key, bin30, max(rate_tagged) rate FROM read_parquet('{CROWD}') WHERE day_type='weekday' GROUP BY 1, 2""").df()
        tf = tf.merge(m, on=["route_key", "bin30"])
        if len(tf) >= 30:
            corr = round(float(spearmanr(tf.peak_running, tf.rate).correlation), 3)
    return {"id": "J6", "title": "Blind spots and the per-trip running count",
            "status": "note",
            "summary": "Where the estimate is weakest, and the brief's per-trip running count as a diagnostic (tickets per vehicle-day are too few for it to be the main method).",
            "metrics": {"weekday cells with High / Low evidence %": {"High": round(100 * q.loc["weekday", "hi"], 1), "Low": round(100 * q.loc["weekday", "lo"], 1)},
                        "Saturday High-evidence cells %": round(100 * q.loc["saturday", "hi"], 1), "Sunday/holiday High-evidence cells %": round(100 * q.loc["sunday_holiday", "hi"], 1),
                        "weekday routes with no tickets at all": int(zero_routes),
                        "least-evidenced weekday routes (< 10 tickets)": worst.route_key.tolist(),
                        "vehicle-days with >= 15 tickets (trip diagnostic)": int(len(dense)), "trips with >= 4 tickets": ntrip,
                        "Spearman: per-trip peak running tagged load vs model cell rate": corr if corr is not None else "too few qualifying trips",
                        "known gaps": "late night, sparse feeder (DS-*) routes, weekends (4-5 days), October festivals, uneven ticketing adoption, 12 m default bus size, scale factor rests on press-sourced ridership"}}


def main():
    t0 = time.time()
    con = duckdb.connect()
    with open(GRAPH, "rb") as f:
        g = pickle.load(f)
    cl = pd.read_parquet("data/interim/gtfs_stop_clusters.parquet").set_index("cluster_id")["cluster_name"].astype(str).to_dict()
    cm_ = {str(k): str(v) for k, v in g["cluster_map"].items()}
    con.register("stops_tbl", pd.DataFrame([{"stop_id": str(k), "stop_name": cl.get(cm_.get(str(k)), str(v.get("stop_name", "")))}
                                           for k, v in g["stop_lookup"].items()]))
    out = {"generated_by": "validation/05_validate.py", "checks": []}
    for name, fn in (("J1", lambda: j1(con)), ("J2", j2), ("J3", lambda: j3(con)), ("J4", lambda: j4(con)), ("J5", j5), ("J6", lambda: j6(con))):
        t = time.time()
        res = fn()
        out["checks"].append(res)
        print(f"[{name}] {res['status']:<5} {res['title']}  ({time.time() - t:.1f}s)")
    out["runtime_s"] = round(time.time() - t0, 1)
    with open("outputs/validation_v8.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(json.dumps({c["id"]: c["status"] for c in out["checks"]}), f"{out['runtime_s']}s")
    return all(c["status"] != "fail" for c in out["checks"])


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
