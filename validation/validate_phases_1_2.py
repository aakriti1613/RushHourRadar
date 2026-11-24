"""
validation/validate_phases_1_2.py - Comprehensive audit and validation of Phase 1 & 2 deliverables.

Checks:
  1. File integrity & artifact existence.
  2. Parquet schema and data types against Coding Spec contracts.
  3. Logical invariants (board_seq < alight_seq, index bounds, zero nulls in keys).
  4. Calendar & holiday tagging correctness (2026-10-02, 2026-10-20 tagged as sunday_holiday).
  5. GTFS graph integrity (strictly increasing cum_min, valid clusters, transfer links, departure arrays).
  6. Match rate verification (passenger volume target >= 93.0%).
  7. Spot-check on golden corridor routes (8UP, 433UP, Anand Vihar & Connaught Place stops).
"""

import json
import os
import pickle
import time
import duckdb
import numpy as np
import pandas as pd
import yaml


def run_validation():
    print("=" * 75)
    print("SYSTEM INTEGRITY AUDIT: PHASE 1 & PHASE 2 DELIVERABLES")
    print("=" * 75)
    t0 = time.time()
    all_passed = True
    checks_passed = 0
    checks_total = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal all_passed, checks_passed, checks_total
        checks_total += 1
        status = "PASS" if condition else "FAIL"
        if condition:
            checks_passed += 1
            print(f"  [✓] {name}: {detail}")
        else:
            all_passed = False
            print(f"  [✗] {name}: {detail}")

    # -------------------------------------------------------------------------
    # 1. FILE EXISTENCE & SIZE CHECKS
    # -------------------------------------------------------------------------
    print("\n--- 1. File Artifact Existence & Size ---")
    required_files = {
        "Raw Ticketing Zip": ("data/raw/October-Delhi.csv.zip", 50 * 1024 * 1024),
        "Raw GTFS Routes": ("data/raw/gtfs/routes.txt", 100 * 1024),
        "Raw GTFS Stops": ("data/raw/gtfs/stops.txt", 200 * 1024),
        "Raw GTFS Trips": ("data/raw/gtfs/trips.txt", 1024 * 1024),
        "Raw GTFS Stop Times": ("data/raw/gtfs/stop_times.txt", 50 * 1024 * 1024),
        "Config YAML": ("config.yaml", 500),
        "EDA Report JSON": ("outputs/eda_report.json", 1000),
        "Loaded Tickets Parquet": ("data/interim/tickets.parquet", 30 * 1024 * 1024),
        "GTFS Graph Pickle": ("data/interim/gtfs_graph.pkl", 20 * 1024 * 1024),
        "Canonical Segments Parquet": ("data/interim/gtfs_canonical_segments.parquet", 1024 * 1024),
        "Stop Clusters Parquet": ("data/interim/gtfs_stop_clusters.parquet", 50 * 1024),
        "Matched Tickets Parquet": ("data/interim/matched_tickets.parquet", 35 * 1024 * 1024),
        "Match Report JSON": ("outputs/match_report.json", 500),
        "Unmatched Top 50 CSV": ("outputs/unmatched_top50.csv", 500)
    }

    for label, (fpath, min_bytes) in required_files.items():
        exists = os.path.exists(fpath)
        sz = os.path.getsize(fpath) if exists else 0
        cond = exists and (sz >= min_bytes)
        check(f"Artifact '{label}'", cond, f"Path={fpath}, Size={sz/(1024*1024):.2f} MB")

    # -------------------------------------------------------------------------
    # 2. SCHEMA & TYPE CHECKS: tickets.parquet
    # -------------------------------------------------------------------------
    print("\n--- 2. Schema Integrity: tickets.parquet ---")
    conn = duckdb.connect()
    t_df = conn.execute("SELECT * FROM read_parquet('data/interim/tickets.parquet') LIMIT 5").df()
    expected_t_cols = ["ticket_id", "pax", "fare", "vehicle_number", "ticket_type_id", "route", "start_stop_name", "end_stop_name", "ts", "is_ac"]
    missing_t = [c for c in expected_t_cols if c not in t_df.columns]
    check("tickets.parquet columns", len(missing_t) == 0, f"Expected {expected_t_cols}, missing: {missing_t}")
    
    total_raw = conn.execute("SELECT count(*) FROM read_parquet('data/interim/tickets.parquet')").fetchone()[0]
    check("tickets.parquet row count", total_raw > 2500000, f"Total rows = {total_raw:,} (> 2.5M expected)")

    # -------------------------------------------------------------------------
    # 3. SCHEMA & INVARIANTS: matched_tickets.parquet
    # -------------------------------------------------------------------------
    print("\n--- 3. Logic & Contract Verification: matched_tickets.parquet ---")
    m_info = conn.execute("""
        SELECT 
            count(*) as total_matched,
            sum(pax) as total_matched_pax,
            count(CASE WHEN board_seq >= alight_seq THEN 1 END) as backwards_count,
            count(CASE WHEN board_seq < 0 OR alight_seq < 0 THEN 1 END) as negative_seq,
            count(CASE WHEN route_key IS NULL OR board_seq IS NULL OR alight_seq IS NULL THEN 1 END) as null_keys,
            count(CASE WHEN day_type NOT IN ('weekday', 'saturday', 'sunday_holiday') THEN 1 END) as invalid_day_types
        FROM read_parquet('data/interim/matched_tickets.parquet')
    """).df().iloc[0]

    check("Strict sequence ordering (board_seq < alight_seq)", m_info["backwards_count"] == 0, 
          f"Backwards rows = {m_info['backwards_count']} (must be 0)")
    check("Non-negative sequence indices", m_info["negative_seq"] == 0, 
          f"Negative indices = {m_info['negative_seq']} (must be 0)")
    check("Zero nulls in primary routing keys", m_info["null_keys"] == 0, 
          f"Null keys = {m_info['null_keys']} (must be 0)")
    check("Valid day_type categories", m_info["invalid_day_types"] == 0, 
          f"Invalid day_types = {m_info['invalid_day_types']} (must be 0)")

    # -------------------------------------------------------------------------
    # 4. CALENDAR & HOLIDAY TAGGING AUDIT
    # -------------------------------------------------------------------------
    print("\n--- 4. Calendar & Holiday Tagging Audit ---")
    holidays_audit = conn.execute("""
        SELECT 
            strftime(ts, '%Y-%m-%d') as dt,
            dayname(ts) as dow,
            day_type,
            count(*) as cnt
        FROM read_parquet('data/interim/matched_tickets.parquet')
        WHERE strftime(ts, '%Y-%m-%d') IN ('2026-10-02', '2026-10-20', '2026-10-04', '2026-10-05', '2026-10-03')
        GROUP BY dt, dow, day_type
        ORDER BY dt
    """).df()

    # Oct 02 (Friday) must be sunday_holiday
    oct02_type = holidays_audit[holidays_audit["dt"] == "2026-10-02"]["day_type"].values[0]
    check("Gandhi Jayanti (2026-10-02) tagged as sunday_holiday", oct02_type == "sunday_holiday", f"Tag = '{oct02_type}'")

    # Oct 20 (Tuesday) must be sunday_holiday
    oct20_type = holidays_audit[holidays_audit["dt"] == "2026-10-20"]["day_type"].values[0]
    check("Dussehra (2026-10-20) tagged as sunday_holiday", oct20_type == "sunday_holiday", f"Tag = '{oct20_type}'")

    # Oct 03 (Saturday) must be saturday
    oct03_type = holidays_audit[holidays_audit["dt"] == "2026-10-03"]["day_type"].values[0]
    check("Saturday (2026-10-03) tagged as saturday", oct03_type == "saturday", f"Tag = '{oct03_type}'")

    # Oct 04 (Sunday) must be sunday_holiday
    oct04_type = holidays_audit[holidays_audit["dt"] == "2026-10-04"]["day_type"].values[0]
    check("Sunday (2026-10-04) tagged as sunday_holiday", oct04_type == "sunday_holiday", f"Tag = '{oct04_type}'")

    # Oct 05 (Monday) must be weekday
    oct05_type = holidays_audit[holidays_audit["dt"] == "2026-10-05"]["day_type"].values[0]
    check("Normal Monday (2026-10-05) tagged as weekday", oct05_type == "weekday", f"Tag = '{oct05_type}'")

    # -------------------------------------------------------------------------
    # 5. GTFS GRAPH INTEGRITY
    # -------------------------------------------------------------------------
    print("\n--- 5. GTFS Graph Data Structure & Invariants ---")
    with open("data/interim/gtfs_graph.pkl", "rb") as f:
        graph = pickle.load(f)

    r_pats = graph["route_patterns"]
    check("Total route patterns compiled", len(r_pats) >= 2500, f"Patterns count = {len(r_pats):,}")
    
    # Check stop lookup
    s_lookup = graph["stop_lookup"]
    check("Stops in lookup dictionary", len(s_lookup) >= 6800, f"Stop lookup count = {len(s_lookup):,}")

    # Check clusters
    c_map = graph["cluster_map"]
    check("Stops assigned to clusters", len(c_map) == len(s_lookup), f"Clustered stops = {len(c_map):,}")

    # Check walking transfers
    t_map = graph["transfer_map"]
    check("Transfer walk map populated", len(t_map) >= 5000, f"Stops with walk links = {len(t_map):,}")

    # Invariant: canonical segments strictly increasing cumulative minutes
    seg_df = pd.read_parquet("data/interim/gtfs_canonical_segments.parquet")
    check("Canonical segments count", len(seg_df) >= 80000, f"Segments = {len(seg_df):,}")

    # Verify cum_min is monotonic per route
    non_monotonic = 0
    for rk, g in seg_df.groupby("route_key"):
        cms = g.sort_values("seg_seq")["cum_min"].values
        if len(cms) > 1 and np.any(np.diff(cms) < -1e-5):
            non_monotonic += 1
    check("Monotonically increasing cum_min across all routes", non_monotonic == 0, 
          f"Routes with non-monotonic run times = {non_monotonic}")

    # -------------------------------------------------------------------------
    # 6. MATCH PERFORMANCE BENCHMARK
    # -------------------------------------------------------------------------
    print("\n--- 6. Match Performance Benchmark (Spec Requirement >= 93.0%) ---")
    with open("outputs/match_report.json", "r") as f:
        m_rep = json.load(f)

    pax_pct = m_rep["match_performance"]["matched_pax_pct"]
    ticket_pct = m_rep["match_performance"]["matched_tickets_pct"]
    target_met = m_rep["match_performance"]["target_achieved"]

    check("Passenger volume match >= 93.0%", target_met, 
          f"Achieved = {pax_pct:.2f}% (Target: >= 93.0%, Total matched pax: {m_rep['match_performance']['matched_pax']:,})")
    check("Ticket count match >= 90.0%", ticket_pct >= 90.0, 
          f"Achieved = {ticket_pct:.2f}% (Total matched tickets: {m_rep['match_performance']['matched_tickets']:,})")

    # -------------------------------------------------------------------------
    # 7. SPOT-CHECK: Golden corridor routes & stops
    # -------------------------------------------------------------------------
    print("\n--- 7. Golden Route & Stop Verification ---")
    test_routes = ["8UP", "8ADOWN", "433UP", "208DOWN"]
    for r in test_routes:
        in_pats = r in r_pats
        trips_T_sum = np.sum(r_pats[r]["trips_T_bins"]) if in_pats else 0
        tickets_on_route = conn.execute(f"SELECT count(*) FROM read_parquet('data/interim/matched_tickets.parquet') WHERE route_key='{r}'").fetchone()[0]
        check(f"Route '{r}' GTFS & Ticket verification", in_pats and trips_T_sum > 0 and tickets_on_route > 0,
              f"In GTFS: {in_pats}, Scheduled trip-segments: {trips_T_sum:,}, Matched tickets: {tickets_on_route:,}")

    # Verify Anand Vihar & Connaught Place stops in stop clusters
    clusters_df = pd.read_parquet("data/interim/gtfs_stop_clusters.parquet")
    av_clusters = clusters_df[clusters_df["cluster_name"].str.contains("Anand Vihar", case=False, na=False)]
    cp_clusters = clusters_df[clusters_df["cluster_name"].str.contains("Connaught Place|Shivaji Stadium", case=False, na=False)]
    check("Anand Vihar stop cluster exists", len(av_clusters) > 0, f"Found {len(av_clusters)} clusters: {av_clusters['cluster_name'].tolist()[:2]}")
    check("Connaught Place / Shivaji Stadium cluster exists", len(cp_clusters) > 0, f"Found {len(cp_clusters)} clusters: {cp_clusters['cluster_name'].tolist()[:2]}")

    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------
    elapsed = time.time() - t0
    print("\n" + "=" * 75)
    print(f"AUDIT SUMMARY: {checks_passed}/{checks_total} CHECKS PASSED ({100.0*checks_passed/checks_total:.1f}%) in {elapsed:.2f}s")
    print("=" * 75)
    if all_passed:
        print("ALL SPEC CONTRACTS & INTEGRITY INVARIANTS ARE FULLY SATISFIED.")
    else:
        print("WARNING: ONE OR MORE INTEGRITY CHECKS FAILED.")
    print("=" * 75)

    return all_passed


if __name__ == "__main__":
    run_validation()
