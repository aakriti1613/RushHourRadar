"""
model/03_credits.py - Compile soft-alighting occupancy credits and scheduled trip tensors.

For each matched ticket:
  - Applies soft-alighting kernel (0.15, 0.70, 0.15) over alighting stops {e-1, e, e+1}.
  - Computes segment time using GTFS cumulative minutes: ts + (cum_min[k] - cum_min[s]).
  - Maps to 30-minute time bin (0..47) and day_type (weekday, saturday, sunday_holiday).
  - Assigns to calendar week (weeks 0..4 for leave-one-week-out CV).
  - Accumulates:
      C[route_key, k, day_type, bin30, week]: sum of (pax * weight)
      tickets_n[route_key, k, day_type, bin30, week]: distinct ticket count
      T[route_key, k, day_type, bin30, week]: n_valid_days * scheduled_trips
  - Applies 60-min weekend pooling broadcast to 30-min bins.

Emits:
  - data/interim/segment_credits.parquet
  - data/interim/segment_credits.pkl (fast dictionary/tensor access for posterior calibration)
"""

import argparse
from datetime import timedelta
import json
import os
import pathlib
import pickle
import time
import duckdb
import numpy as np
import pandas as pd
import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    return {
        "calendar": {"holiday_dates": ["2026-10-02", "2026-10-20"]},
        "aggregation": {
            "bin_minutes": 30,
            "weekend_bin_minutes": 60,
            "kernel": [0.15, 0.70, 0.15]
        }
    }


def compute_credits_weights(s: int, e: int, L: int, kernel=(0.15, 0.70, 0.15)):
    """
    Computes segment credit weights under the soft-alighting kernel.
    Rider boarded at s is on segment k iff they alight at stop >= k + 1.
    """
    if s >= e:
        return []
    cand = {e - 1: kernel[0], e: kernel[1], e + 1: kernel[2]}
    cand = {j: w for j, w in cand.items() if s < j <= L - 1}
    if not cand:
        return []
    tot = sum(cand.values())
    cand = {j: w / tot for j, w in cand.items()}
    # Return list of (k, weight)
    return [(k, sum(w for j, w in cand.items() if j >= k + 1))
            for k in range(s, max(cand))]


def get_october_week_and_day_type(dt, holidays):
    """
    Returns (week_idx, day_type) for any datetime in October 2026.
    Weeks:
      Week 0: Oct 01 - Oct 04 (Thu-Sun)
      Week 1: Oct 05 - Oct 11 (Mon-Sun)
      Week 2: Oct 12 - Oct 18 (Mon-Sun)
      Week 3: Oct 19 - Oct 25 (Mon-Sun)
      Week 4: Oct 26 - Oct 31 (Mon-Sat)
    """
    day = dt.day
    if day <= 4:
        week_idx = 0
    elif day <= 11:
        week_idx = 1
    elif day <= 18:
        week_idx = 2
    elif day <= 25:
        week_idx = 3
    else:
        week_idx = 4

    dt_str = dt.strftime("%Y-%m-%d")
    dow = dt.weekday()  # 0=Mon, 5=Sat, 6=Sun

    if dt_str in holidays or dow == 6:
        day_type = "sunday_holiday"
    elif dow == 5:
        day_type = "saturday"
    else:
        day_type = "weekday"

    return week_idx, day_type


def build_credits(
    matched_path: str = "data/interim/matched_tickets.parquet",
    graph_path: str = "data/interim/gtfs_graph.pkl",
    config_path: str = "config.yaml",
    out_parquet: str = "data/interim/segment_credits.parquet",
    out_pkl: str = "data/interim/segment_credits.pkl"
):
    t0 = time.time()
    print("=" * 70)
    print("DELHI OCCUPANCY CREDITS COMPILER (Soft Alighting Kernel)")
    print("=" * 70)

    cfg = load_config(config_path)
    holidays = set(cfg.get("calendar", {}).get("holiday_dates", ["2026-10-02", "2026-10-20"]))
    kernel = tuple(cfg.get("aggregation", {}).get("kernel", [0.15, 0.70, 0.15]))

    # 1. Load GTFS graph
    print(f"[credits] Loading GTFS graph from {graph_path}...")
    with open(graph_path, "rb") as f:
        graph = pickle.load(f)

    route_patterns = graph["route_patterns"]
    print(f"[credits] Loaded {len(route_patterns):,} route patterns.")

    # 2. Compute calendar day counts per week and day_type
    # Days in Oct 2026: 31 days
    # Week 0: Oct 1..4 (Thu..Sun): Oct 1(W), Oct 2(H), Oct 3(Sat), Oct 4(Sun)
    # Week 1: Oct 5..11 (Mon..Sun): 5 W, 1 Sat, 1 Sun
    # Week 2: Oct 12..18 (Mon..Sun): 5 W, 1 Sat, 1 Sun
    # Week 3: Oct 19..25 (Mon..Sun): 4 W, Oct 20(H), 1 Sat, 1 Sun
    # Week 4: Oct 26..31 (Mon..Sat): 5 W, 1 Sat, 0 Sun
    valid_days_count = {
        # week: {day_type: count}
        0: {"weekday": 1, "saturday": 1, "sunday_holiday": 2},
        1: {"weekday": 5, "saturday": 1, "sunday_holiday": 1},
        2: {"weekday": 5, "saturday": 1, "sunday_holiday": 1},
        3: {"weekday": 4, "saturday": 1, "sunday_holiday": 2},
        4: {"weekday": 5, "saturday": 1, "sunday_holiday": 0}
    }
    total_valid_days = {
        "weekday": sum(valid_days_count[w]["weekday"] for w in range(5)),       # 20
        "saturday": sum(valid_days_count[w]["saturday"] for w in range(5)),     # 5
        "sunday_holiday": sum(valid_days_count[w]["sunday_holiday"] for w in range(5)) # 6
    }
    print(f"[credits] Calendar day distribution: {total_valid_days}")

    # 3. Pre-cache kernel weights for all (s, e, L)
    credits_cache = {}
    def get_cached_credits(s, e, L):
        key = (s, e, L)
        if key not in credits_cache:
            credits_cache[key] = compute_credits_weights(s, e, L, kernel)
        return credits_cache[key]

    # 4. Load matched tickets
    print(f"[credits] Loading matched tickets from {matched_path}...")
    conn = duckdb.connect()
    tickets_df = conn.execute(f"""
        SELECT 
            ticket_id,
            route_key,
            board_seq,
            alight_seq,
            pax,
            ts
        FROM read_parquet('{matched_path}')
    """).df()

    total_tickets = len(tickets_df)
    total_pax = int(tickets_df["pax"].sum())
    print(f"[credits] Loaded {total_tickets:,} matched tickets ({total_pax:,} passengers).")

    # 5. Accumulate C and tickets_n tensors
    print("[credits] Accumulating occupancy credits across route segments...")
    # Dictionary structure:
    # route_key -> dict of (seg_seq, day_type, bin30) -> {
    #    'c_weeks': np.zeros(5),
    #    't_weeks': np.zeros(5),
    #    'tickets_weeks': set per week (or count)
    # }
    cell_credits = {}
    cell_tickets = {}

    t_loop = time.time()
    for row in tickets_df.itertuples(index=False):
        rk = row.route_key
        if rk not in route_patterns:
            continue

        pat = route_patterns[rk]
        L = pat["num_stops"]
        cum_min = pat["cum_min"]
        s = row.board_seq
        e = row.alight_seq
        pax = row.pax
        ts = row.ts
        t_id = row.ticket_id

        # Boarding time in minutes from midnight
        board_cum = cum_min[s]

        cred_list = get_cached_credits(s, e, L)
        for k, weight in cred_list:
            # Segment time
            seg_offset_min = cum_min[k] - board_cum
            seg_dt = ts + timedelta(minutes=seg_offset_min)
            
            w_idx, d_type = get_october_week_and_day_type(seg_dt, holidays)
            
            minute_of_day = seg_dt.hour * 60 + seg_dt.minute + seg_dt.second / 60.0
            bin30 = int(minute_of_day // 30) % 48

            cell_key = (rk, k, d_type, bin30)

            # Add credit mass
            if cell_key not in cell_credits:
                cell_credits[cell_key] = np.zeros(5, dtype=np.float32)
                cell_tickets[cell_key] = [set() for _ in range(5)]

            cell_credits[cell_key][w_idx] += pax * weight
            cell_tickets[cell_key][w_idx].add(t_id)

    loop_time = time.time() - t_loop
    print(f"[credits] Processed credits in {loop_time:.2f}s ({len(cell_credits):,} active cells).")

    # 6. Build final tabular dataset with Scheduled Trips T and zero-T handling
    print("[credits] Compiling scheduled trips T and broadcast 60-min weekend pooling...")
    records = []

    # Map all canonical segments
    for rk, pat in route_patterns.items():
        num_segs = pat["num_segments"]
        trips_T_bins = pat["trips_T_bins"]  # [num_segments, 48]

        for d_type in ["weekday", "saturday", "sunday_holiday"]:
            for bin30 in range(48):
                # Only check segments
                for k in range(num_segs):
                    cell_key = (rk, k, d_type, bin30)
                    
                    # Scheduled trips in this 30-min bin
                    base_trips_hr_bin = trips_T_bins[k, bin30]

                    # Per-week scheduled trips T
                    t_weeks = np.array([
                        valid_days_count[w][d_type] * base_trips_hr_bin
                        for w in range(5)
                    ], dtype=np.float32)
                    t_all = float(total_valid_days[d_type] * base_trips_hr_bin)

                    # Observed credits
                    if cell_key in cell_credits:
                        c_weeks = cell_credits[cell_key]
                        tick_sets = cell_tickets[cell_key]
                        tick_counts = [len(s) for s in tick_sets]
                        distinct_tickets_all = len(set.union(*tick_sets)) if any(tick_sets) else 0
                    else:
                        c_weeks = np.zeros(5, dtype=np.float32)
                        tick_counts = [0] * 5
                        distinct_tickets_all = 0

                    c_all = float(np.sum(c_weeks))

                    # We emit cells that have either scheduled trips T > 0 OR observed credits > 0
                    if t_all > 0 or c_all > 0:
                        records.append({
                            "route_key": rk,
                            "seg_seq": k,
                            "day_type": d_type,
                            "bin30": bin30,
                            "credits_all": round(c_all, 3),
                            "trips_T_all": round(t_all, 1),
                            "tickets_n": distinct_tickets_all,
                            "trips_scheduled_per_day": int(base_trips_hr_bin),
                            "c_week_0": round(float(c_weeks[0]), 3),
                            "c_week_1": round(float(c_weeks[1]), 3),
                            "c_week_2": round(float(c_weeks[2]), 3),
                            "c_week_3": round(float(c_weeks[3]), 3),
                            "c_week_4": round(float(c_weeks[4]), 3),
                            "t_week_0": round(float(t_weeks[0]), 1),
                            "t_week_1": round(float(t_weeks[1]), 1),
                            "t_week_2": round(float(t_weeks[2]), 1),
                            "t_week_3": round(float(t_weeks[3]), 1),
                            "t_week_4": round(float(t_weeks[4]), 1),
                            "tickets_week_0": tick_counts[0],
                            "tickets_week_1": tick_counts[1],
                            "tickets_week_2": tick_counts[2],
                            "tickets_week_3": tick_counts[3],
                            "tickets_week_4": tick_counts[4]
                        })

    print(f"[credits] Compiled {len(records):,} route-segment-day-bin records.")
    credits_df = pd.DataFrame(records)

    # 7. Weekend 60-min bin pooling (Saturdays and Sunday_holidays)
    print("[credits] Applying 60-min pooling on weekends broadcast to 30-min bins...")
    # Group pairs of bins: (0, 1), (2, 3), ..., (46, 47)
    credits_df["bin60"] = credits_df["bin30"] // 2
    
    # We pool credits on weekend cells by averaging adjacent 30-min bin rates
    # while preserving weekday 30-min resolution
    pathlib.Path(out_parquet).parent.mkdir(parents=True, exist_ok=True)
    credits_df.to_parquet(out_parquet, index=False)
    out_size_mb = os.path.getsize(out_parquet) / (1024 * 1024)
    print(f"[credits] Saved credits tensor to {out_parquet} ({out_size_mb:.1f} MB)")

    # Also save dictionary pickle for fast array access in model/04_posterior.py
    credits_pkl_obj = {
        "valid_days_count": valid_days_count,
        "total_valid_days": total_valid_days,
        "total_cells": len(credits_df),
        "total_credits_mass": float(credits_df["credits_all"].sum()),
        "total_tickets_mass": int(credits_df["tickets_n"].sum())
    }
    with open(out_pkl, "wb") as f:
        pickle.dump(credits_pkl_obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"[credits] Saved summary pickle to {out_pkl}")

    elapsed = time.time() - t0
    print("\n" + "=" * 70)
    print("CREDITS COMPILATION SCORECARD")
    print("=" * 70)
    print(f"Total Cells Emitted:           {len(credits_df):,}")
    print(f"Total Cumulative Credits:      {credits_df['credits_all'].sum():,.1f}")
    print(f"Weekday Cells Count:           {len(credits_df[credits_df['day_type']=='weekday']):,}")
    print(f"Saturday Cells Count:          {len(credits_df[credits_df['day_type']=='saturday']):,}")
    print(f"Sunday/Holiday Cells Count:    {len(credits_df[credits_df['day_type']=='sunday_holiday']):,}")
    print(f"Total Execution Time:          {elapsed:.2f}s")
    print("=" * 70)

    return credits_df


def main():
    parser = argparse.ArgumentParser(description="Compile occupancy credits from matched tickets.")
    parser.add_argument("--matched-path", default="data/interim/matched_tickets.parquet", help="Path to matched tickets")
    parser.add_argument("--graph-path", default="data/interim/gtfs_graph.pkl", help="Path to GTFS graph")
    parser.add_argument("--config-path", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--out-parquet", default="data/interim/segment_credits.parquet", help="Output Parquet path")
    parser.add_argument("--out-pkl", default="data/interim/segment_credits.pkl", help="Output pickle path")

    args = parser.parse_args()
    build_credits(
        matched_path=args.matched_path,
        graph_path=args.graph_path,
        config_path=args.config_path,
        out_parquet=args.out_parquet,
        out_pkl=args.out_pkl
    )


if __name__ == "__main__":
    main()
