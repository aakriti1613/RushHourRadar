"""
model/04_posterior.py - v8 crowding estimation (Contract A + Contract B).

Pipeline per day type (weekday, saturday, sunday_holiday):
  1. Load the credits tensor as dense [route-segment x 48 bin] arrays.
  2. Weekend: pool to 60-min bins. Optional masked smoothing (off by default:
     held-out evaluation showed it blurs real peaks).
  3. Hierarchical prior + Gamma-Poisson posterior with prior shape `a`
     (selected by validation/eval_holdout.py, read from outputs/eval/summary.json).
  4. Typical-week range from the NB2 layer (alpha per sample-size bucket, also
     selected on held-out weeks).
  5. Expansion: capacity-constrained kappa_r = min(kappa, ceiling x max_cap_r / peak_rate_r).
  6. Route capacity from the observed fleet mix (vehicle prefix -> bus class map
     in config.yaml; unknown -> default class).
  7. Tiers, p_crowded, rel_rank (kappa-free), trust (distinct tickets; thresholds
     justified by split-half reliability), operator overload/relief table.

Only cells with scheduled service (T > 0) are emitted.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import pickle
import re
import sys
import time

import duckdb
import numpy as np
import pandas as pd
import yaml
from scipy.stats import gamma

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model import crowd_model as cm  # noqa: E402


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_selected(cfg, summary_path):
    """Hyper-parameters chosen on held-out weeks; config values are the fallback."""
    bc = cfg.get("bayesian", {})
    sel = {"prior_shape_a": float(bc.get("prior_shape_a", 2.0)),
           "smoothing": bool(bc.get("smoothing", False)),
           "alpha_by_bucket": list(bc.get("alpha_by_bucket", [0.0, 0.08, 0.08, 0.05])),
           "source": "config.yaml"}
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            s = json.load(f).get("selected", {})
        if s:
            sel.update({k: s[k] for k in ("prior_shape_a", "smoothing", "alpha_by_bucket") if k in s})
            sel["source"] = str(summary_path)
    return sel


def route_capacity(con, matched_path, cfg, routes):
    """Seat / max capacity per route from the observed fleet mix.

    Each non-NULL vehicle_number is mapped to a bus class by prefix
    (config bus_capacities.prefix_class); unmapped or NULL -> default class.
    The route capacity is the ticket-weighted mix. With an empty prefix map every
    route is the default 12 m class: the 9 m mapping needs the operator's fleet
    list and is stated as an assumption."""
    bc = cfg["bus_capacities"]
    classes = bc["classes"]
    default = bc.get("default_class", "12m")
    pmap = bc.get("prefix_class", {}) or {}
    seat = {r: float(classes[default]["seats"]) for r in routes}
    mx = {r: float(classes[default]["max_cap"]) for r in routes}
    share9 = {r: 0.0 for r in routes}
    if pmap and os.path.exists(matched_path):
        path = str(matched_path).replace("\\", "/")
        v = con.execute(f"""SELECT route_key, left(vehicle_number, 6) AS pre, count(*) AS n
                            FROM read_parquet('{path}') WHERE vehicle_number IS NOT NULL
                            GROUP BY 1, 2""").df()
        v["cls"] = v["pre"].map(lambda p: next((c for pre, c in pmap.items() if str(p).startswith(pre)), default))
        v["seat"] = v["cls"].map(lambda c: classes[c]["seats"])
        v["max"] = v["cls"].map(lambda c: classes[c]["max_cap"])
        g = v.groupby("route_key").apply(lambda d: pd.Series({
            "seat": np.average(d["seat"], weights=d["n"]), "max": np.average(d["max"], weights=d["n"]),
            "s9": float((d["n"] * (d["cls"] != default)).sum() / d["n"].sum())}), include_groups=False)
        for r, row in g.iterrows():
            if r in seat:
                seat[r], mx[r], share9[r] = float(row["seat"]), float(row["max"]), float(row["s9"])
    return seat, mx, share9


def build_posterior(credits_path, graph_path, matched_path, config_path, summary_path,
                    out_crowding, out_operator, out_report):
    t0 = time.time()
    print("=" * 70)
    print("DELHI CROWDING ESTIMATION v8")
    print("=" * 70)
    cfg = load_config(config_path)
    sel = load_selected(cfg, summary_path)
    a = sel["prior_shape_a"]
    smooth = sel["smoothing"]
    alpha_b = sel["alpha_by_bucket"]
    kappa = float(cfg["expansion"]["kappa"])
    ceiling = float(cfg["expansion"].get("capacity_ceiling_factor", 2.0))
    cap_min_tix = int(cfg["expansion"].get("cap_min_tickets", 30))
    cap_q = float(cfg["expansion"].get("cap_quantile", 0.95))
    seat_f = float(cfg["crowding_tiers"]["seat_factor"])
    max_f = float(cfg["crowding_tiers"]["max_factor"])
    tt = cfg["trust_thresholds"]
    print(f"[v8] a={a}, smoothing={smooth}, alpha_by_bucket={alpha_b} (from {sel['source']}); kappa={kappa}")

    with open(graph_path, "rb") as f:
        graph = pickle.load(f)
    pats = graph["route_patterns"]
    stops = graph["stop_lookup"]
    con = duckdb.connect()

    grids = {dt: cm.load_grid(con, credits_path, dt) for dt in cm.DAY_TYPES}
    routes = sorted(set().union(*[set(g.route_names) for g in grids.values()]))
    seat_r, max_r, share9_r = route_capacity(con, matched_path, cfg, routes)

    # ---- posterior per day type
    est = {}
    for dt, g in grids.items():
        active = g.T > 0
        mean, shape, mu0 = cm.fit_predict(g.C, g.T, active, g.route_codes, g.n_routes, dt, a, smooth=smooth)
        N = cm.pool_hourly(np.where(active, g.N, 0)) if dt != "weekday" else g.N
        est[dt] = dict(mean=mean, shape=shape, active=active, N=N)
        print(f"[v8] {dt}: {active.sum():,} active cells")

    # ---- capacity-constrained kappa per route (from weekday surface)
    gw = grids["weekday"]
    max_cap_vec = np.array([max_r[r] for r in gw.route_names])
    k_r, peak_r = cm.route_kappa(est["weekday"]["mean"], gw.N, gw.route_codes, gw.n_routes, kappa,
                                 max_cap_vec, ceiling_factor=ceiling, min_tickets=cap_min_tix, quantile=cap_q)
    kappa_by_route = dict(zip(gw.route_names, k_r))
    peak_by_route = dict(zip(gw.route_names, peak_r))
    n_capped = int((k_r < kappa).sum())
    print(f"[v8] kappa capped on {n_capped:,} of {gw.n_routes:,} routes; median kappa_r={np.median(k_r):.1f}")

    # ---- emit Contract A
    up_down = re.compile(r"^(.*?)(UP|DOWN)$", re.IGNORECASE)
    frames = []
    for dt, g in grids.items():
        e = est[dt]
        rows, bins = np.nonzero(e["active"])
        rk = g.keys["route_key"].to_numpy()[rows]
        seg = g.keys["seg_seq"].to_numpy()[rows].astype(int)
        kap = np.array([kappa_by_route.get(r, kappa) for r in g.route_names])[g.route_codes][rows]
        seat = np.array([seat_r[r] for r in g.route_names])[g.route_codes][rows]
        mxc = np.array([max_r[r] for r in g.route_names])[g.route_codes][rows]
        mean = e["mean"][rows, bins]
        shp = e["shape"][rows, bins]
        N = e["N"][rows, bins]
        k = cm.week_k(shp, alpha_b, N)
        scale = mean / k
        load = kap * mean
        p10 = kap * gamma.ppf(0.10, a=k, scale=scale)
        p90 = kap * gamma.ppf(0.90, a=k, scale=scale)
        pcr = gamma.sf(max_f * mxc / kap, a=k, scale=scale)
        tier = cm.tier_of(load, seat, mxc, seat_f, max_f)
        ntix = g.N[rows, bins]
        trust = np.where(N >= tt["high_tickets"], "High", np.where(N >= tt["medium_tickets"], "Medium", "Low"))
        f = pd.DataFrame({
            "route_key": rk, "seg_seq": seg, "day_type": dt, "bin30": bins.astype(np.int16),
            "est_load": np.round(load, 1), "load_p10": np.round(p10, 1), "load_p90": np.round(p90, 1),
            "seat_cap": seat, "max_cap": mxc, "crowding_tier": cm.TIER_NAMES[tier],
            "p_crowded": np.round(pcr, 3), "rate_tagged": np.round(mean, 4),
            "credits_n": np.round(g.C[rows, bins], 2), "tickets_n": ntix.astype(np.int32),
            "tickets_pooled": N.astype(np.int32), "trust_level": trust,
            "trips_sched_per_day": g.trips[rows, bins].astype(np.int16),
            "kappa_r": np.round(kap, 2), "kappa_capped": kap < kappa - 1e-9,
        })
        frames.append(f)
    df = pd.concat(frames, ignore_index=True)
    del frames

    # route + stop attributes
    rk_u = df["route_key"].unique()
    base = {r: (up_down.match(r).group(1) if up_down.match(r) else r) for r in rk_u}
    dire = {r: (up_down.match(r).group(2).upper() if up_down.match(r) else "UP") for r in rk_u}
    df["route_base"] = df["route_key"].map(base)
    df["direction"] = df["route_key"].map(dire)
    seg_keys = df[["route_key", "seg_seq"]].drop_duplicates()
    fs, ts_, seg_min = [], [], []
    for r, k in seg_keys.itertuples(index=False):
        cs = pats[r]["canonical_stops"]
        fs.append(cs[k]); ts_.append(cs[k + 1])
        sm = pats[r].get("seg_min", [])
        seg_min.append(float(sm[k]) if k < len(sm) else 2.0)
    seg_keys = seg_keys.assign(from_stop_id=fs, to_stop_id=ts_, seg_min=seg_min)
    for side in ("from", "to"):
        ids = seg_keys[f"{side}_stop_id"]
        seg_keys[f"{side}_name"] = ids.map(lambda s: stops.get(s, {}).get("stop_name", s))
        seg_keys[f"{side}_lat"] = ids.map(lambda s: float(stops.get(s, {}).get("stop_lat", 0.0)))
        seg_keys[f"{side}_lon"] = ids.map(lambda s: float(stops.get(s, {}).get("stop_lon", 0.0)))
    df = df.merge(seg_keys, on=["route_key", "seg_seq"], how="left")
    # kappa-free relative rank within day type
    df["rel_rank"] = df.groupby("day_type")["rate_tagged"].rank(pct=True).round(3)

    cols = ["route_key", "route_base", "direction", "seg_seq", "from_stop_id", "to_stop_id", "from_name",
            "to_name", "from_lat", "from_lon", "to_lat", "to_lon", "day_type", "bin30", "est_load", "load_p10",
            "load_p90", "seat_cap", "max_cap", "crowding_tier", "p_crowded", "rel_rank", "credits_n",
            "tickets_n", "trust_level", "trips_sched_per_day", "rate_tagged", "tickets_pooled", "kappa_r",
            "kappa_capped", "seg_min"]
    df = df[cols].sort_values(["route_key", "day_type", "bin30", "seg_seq"]).reset_index(drop=True)
    pathlib.Path(out_crowding).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_crowding, index=False, compression="zstd")
    print(f"[v8] Contract A: {len(df):,} rows -> {out_crowding} ({os.path.getsize(out_crowding)/2**20:.1f} MB)")

    # ---- Contract B: operator table (route x day x hour)
    op = build_operator_table(df, seat_f, max_f)
    op.to_parquet(out_operator, index=False)
    print(f"[v8] Contract B: {len(op):,} rows -> {out_operator}")

    # ---- run report
    wk = df[df.day_type == "weekday"]
    tix = wk[wk.tickets_n > 0]
    report = {
        "model_version": "v8",
        "selected_hyperparameters": sel,
        "kappa_global": kappa,
        "capacity_ceiling_factor": ceiling,
        "cap_quantile": cap_q, "cap_min_tickets": cap_min_tix,
        "routes_total": int(gw.n_routes),
        "routes_kappa_capped": n_capped,
        "kappa_r_quantiles": {str(q): round(float(np.quantile(k_r, q)), 2) for q in (0.05, 0.25, 0.5, 0.75)},
        "share_tagged_pax_on_capped_routes": None,
        "routes_with_non_default_bus_class": int(sum(v > 0 for v in share9_r.values())),
        "weekday_tier_share": wk["crowding_tier"].value_counts(normalize=True).round(4).to_dict(),
        "weekday_trust_share": wk["trust_level"].value_counts(normalize=True).round(4).to_dict(),
        "ticketed_weekday_cells": int(len(tix)),
        "ticketed_cells_over_1x_max_cap_pct": round(float((tix.est_load > tix.max_cap).mean() * 100), 3),
        "ticketed_cells_over_1_5x_max_cap_pct": round(float((tix.est_load > 1.5 * tix.max_cap).mean() * 100), 3),
        "max_est_load": float(df.est_load.max()),
        "runtime_s": round(time.time() - t0, 1),
    }
    try:
        path = str(matched_path).replace("\\", "/")
        pax = con.execute(f"SELECT route_key, sum(pax) p FROM read_parquet('{path}') WHERE day_type='weekday' GROUP BY 1").df()
        pax["capped"] = pax.route_key.map(lambda r: kappa_by_route.get(r, kappa) < kappa - 1e-9)
        report["share_tagged_pax_on_capped_routes"] = round(float(pax.loc[pax.capped, "p"].sum() / pax.p.sum()), 4)
        pax["k"] = pax.route_key.map(lambda r: kappa_by_route.get(r, kappa))
        report["implied_daily_weekday_ridership_vs_published"] = round(float((pax.k * pax.p).sum() / (pax.p.sum() * kappa)), 4)
    except Exception as ex:  # pragma: no cover
        report["pax_note"] = str(ex)
    with open(out_report, "w") as f:
        json.dump(report, f, indent=2, default=float)
    print(json.dumps({k: report[k] for k in ("routes_kappa_capped", "weekday_tier_share",
                                             "ticketed_cells_over_1_5x_max_cap_pct", "max_est_load", "runtime_s")}, indent=1))
    return df, op


def build_operator_table(df, seat_f=0.9, max_f=0.8):
    """Route x day type x hour overload and one-more-bus relief (Contract B).

    overload_rider_min = SUM_seg seg_min x D x max(0, L/thr - 1), D = L x trips_hr, thr = max_f x max_cap
    relief_rider_min   = overload(n) - overload(n+1), L' = L x n/(n+1)   (fixed demand)"""
    d = df[["route_key", "day_type", "bin30", "seg_seq", "est_load", "seat_cap", "max_cap", "trips_sched_per_day",
            "seg_min", "from_name", "to_name", "trust_level", "tickets_n"]].copy()
    d["hour"] = (d["bin30"] // 2).astype(np.int16)
    d["w"] = d["trips_sched_per_day"].clip(lower=0).astype(float)
    d["lw"] = d["est_load"] * d["w"]
    h = d.groupby(["route_key", "day_type", "hour", "seg_seq"], sort=False).agg(
        lw=("lw", "sum"), trips_hr=("w", "sum"), load_mean=("est_load", "mean"), seat_cap=("seat_cap", "first"),
        max_cap=("max_cap", "first"), seg_min=("seg_min", "first"), from_name=("from_name", "first"),
        to_name=("to_name", "first"), trust=("trust_level", "first"), tickets=("tickets_n", "sum")).reset_index()
    h["load"] = np.where(h.trips_hr > 0, h.lw / h.trips_hr.where(h.trips_hr > 0, 1), h.load_mean)
    n = h.trips_hr.clip(lower=1.0)
    thr = max_f * h.max_cap
    D = h.load * n
    ov = h.seg_min * D * np.maximum(0, h.load / thr - 1)
    l1 = h.load * n / (n + 1)
    ov1 = h.seg_min * D * np.maximum(0, l1 / thr - 1)
    h["ov"], h["relief"], h["l1"] = ov, ov - ov1, l1
    idx = h.groupby(["route_key", "day_type", "hour"], sort=False)["load"].idxmax()
    pk = h.loc[idx].set_index(["route_key", "day_type", "hour"])
    agg = h.groupby(["route_key", "day_type", "hour"], sort=False).agg(
        overload_rider_min=("ov", "sum"), relief_rider_min=("relief", "sum"), trips_hr=("trips_hr", "max"),
        max_load=("load", "max"), tickets_n=("tickets", "sum"))

    def tier(l, s, m):
        return np.where(l < seat_f * s, "Seats Available", np.where(l < max_f * m, "Standing Room", "Crowded"))

    op = agg.join(pk[["load", "seg_seq", "from_name", "to_name", "seat_cap", "max_cap", "trust", "l1"]]).reset_index()
    op = op.rename(columns={"load": "peak_load", "seg_seq": "peak_seg_seq", "from_name": "peak_from_name",
                            "to_name": "peak_to_name", "trust": "trust_level"})
    op["tier_now"] = tier(op.peak_load, op.seat_cap, op.max_cap)
    op["tier_plus1"] = tier(op.l1, op.seat_cap, op.max_cap)
    op["underused"] = op.max_load < 0.5 * op.seat_cap
    op["plus1_assumption"] = "fixed demand; load ~ 1/frequency"
    crowded_hours = op[op.tier_now == "Crowded"].groupby(["route_key", "day_type"])["hour"].nunique()
    op["peak_window_hours"] = [int(crowded_hours.get((r, d_), 0)) for r, d_ in zip(op.route_key, op.day_type)]
    for c in ("overload_rider_min", "relief_rider_min", "peak_load", "trips_hr"):
        op[c] = op[c].round(1)
    return op[["route_key", "day_type", "hour", "trips_hr", "peak_load", "peak_seg_seq", "peak_from_name",
               "peak_to_name", "overload_rider_min", "relief_rider_min", "tier_now", "tier_plus1",
               "peak_window_hours", "underused", "plus1_assumption", "trust_level", "seat_cap", "max_cap",
               "tickets_n"]]


def main():
    ap = argparse.ArgumentParser(description="v8 crowding posterior + operator table")
    ap.add_argument("--credits-path", default="data/interim/segment_credits.parquet")
    ap.add_argument("--graph-path", default="data/interim/gtfs_graph.pkl")
    ap.add_argument("--matched-path", default="data/interim/matched_tickets.parquet")
    ap.add_argument("--config-path", default="config.yaml")
    ap.add_argument("--summary-path", default="outputs/eval/summary.json")
    ap.add_argument("--out-crowding", default="data/processed/segment_crowding.parquet")
    ap.add_argument("--out-operator", default="data/processed/operator_overload.parquet")
    ap.add_argument("--out-report", default="outputs/model_report.json")
    a = ap.parse_args()
    build_posterior(a.credits_path, a.graph_path, a.matched_path, a.config_path, a.summary_path,
                    a.out_crowding, a.out_operator, a.out_report)


if __name__ == "__main__":
    main()
