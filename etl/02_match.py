"""
etl/02_match.py - Route-scoped fuzzy matching and direction voting.

Matches raw ticket records (start_stop_name, end_stop_name) to canonical GTFS stop sequences:
  1. Route parsing: parses route_base and direction (UP/DOWN).
  2. Delhi transit stop normalization and abbreviation expansion.
  3. Pre-indexed slash-component token matching with RapidFuzz and in-memory caching.
  4. Direction voting: checks primary direction and opposite direction; picks valid sequence order (start_seq < end_seq).
  5. Day tagging: weekday, saturday, sunday_holiday (accounting for Oct 2 and Oct 20).
  6. Outputs:
     - data/interim/matched_tickets.parquet
     - outputs/match_report.json
     - outputs/unmatched_top50.csv
"""

import argparse
import json
import os
import pathlib
import pickle
import re
import time
import duckdb
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    return {
        "calendar": {
            "holiday_dates": ["2026-10-02", "2026-10-20"],
            "pax_cap": 20
        }
    }


def normalize_stop(name: str, manual_aliases: dict = None) -> str:
    """Normalizes stop names with Delhi-specific transit lexicon."""
    if not name or pd.isna(name):
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    expansions = [
        (r"\bbdr\b", "border"),
        (r"\bmkt\b", "market"),
        (r"\bstn\b", "station"),
        (r"\bterm\b", "terminal"),
        (r"\bsec\b", "sector"),
        (r"\bint\b", "interchange"),
        (r"\bxing\b", "crossing"),
        (r"\bx\s+ing\b", "crossing"),
        (r"\bhosp\b", "hospital"),
        (r"\bshadi pur\b", "shadipur"),
        (r"\binder puri\b", "inderpuri"),
        (r"\bseelam pur\b", "seelampur"),
        (r"\bmaya puri\b", "mayapuri"),
        (r"\bsagar pur\b", "sagarpur"),
        (r"\bmangla puri\b", "manglapuri"),
        (r"\bbrar squre\b", "brar square"),
        (r"\breddison\b", "radisson"),
        (r"\bdevoli\b", "devli"),
        (r"\bsaid ul zeb\b", "said ul ajaib"),
        (r"\bsaidulajaib\b", "said ul ajaib"),
        (r"\bptc\b", "police training college"),
        (r"\bmundka\b", "mundka"),
        (r"\bcl\b", "cluster")
    ]
    for pat, rep in expansions:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    if manual_aliases and s in manual_aliases:
        s = manual_aliases[s]
    return s


def run_matching(
    tickets_path: str = "data/interim/tickets.parquet",
    graph_path: str = "data/interim/gtfs_graph.pkl",
    config_path: str = "config.yaml",
    out_parquet: str = "data/interim/matched_tickets.parquet",
    out_report: str = "outputs/match_report.json",
    out_unmatched: str = "outputs/unmatched_top50.csv"
):
    t0 = time.time()
    print("=" * 70)
    print("DELHI TICKETING STOP MATCHING & DIRECTION VOTING")
    print("=" * 70)

    cfg = load_config(config_path)
    holidays = set(cfg.get("calendar", {}).get("holiday_dates", ["2026-10-02", "2026-10-20"]))
    pax_cap = cfg.get("calendar", {}).get("pax_cap", 20)

    # 0. Load manual overrides if available
    manual_aliases = {}
    overrides_file = "etl/manual_overrides.json"
    if os.path.exists(overrides_file):
        with open(overrides_file, "r") as f:
            manual_aliases = json.load(f).get("stop_name_aliases", {})
        print(f"[match] Loaded {len(manual_aliases)} stop name overrides from {overrides_file}")

    # 1. Load compiled GTFS graph
    print(f"[match] Loading GTFS graph from {graph_path}...")
    with open(graph_path, "rb") as f:
        graph = pickle.load(f)

    route_patterns = graph["route_patterns"]
    stop_lookup = graph["stop_lookup"]
    print(f"[match] Loaded {len(route_patterns):,} route patterns.")

    # 2. Pre-index route stop candidates
    print("[match] Pre-indexing stop candidates and name aliases per route...")
    route_stop_maps = {}
    for rk, pat in route_patterns.items():
        c_stops = pat["canonical_stops"]
        name_to_idx = {}
        names_list = []
        for idx, sid in enumerate(c_stops):
            p = stop_lookup.get(sid, {})
            raw_name = p.get("stop_name", sid)
            norm_full = normalize_stop(raw_name, manual_aliases)
            if norm_full:
                if norm_full not in name_to_idx:
                    name_to_idx[norm_full] = idx
                    names_list.append(norm_full)
                # Add slash-split alias tokens
                parts = str(raw_name).split("/")
                if len(parts) > 1:
                    for part in parts:
                        norm_part = normalize_stop(part, manual_aliases)
                        if len(norm_part) >= 3 and norm_part not in name_to_idx:
                            name_to_idx[norm_part] = idx
                            names_list.append(norm_part)

        route_stop_maps[rk] = {
            "name_to_idx": name_to_idx,
            "names_list": names_list,
            "canonical_stops": c_stops,
            "L": len(c_stops)
        }

    # 3. Load all tickets via DuckDB
    print(f"[match] Reading tickets from {tickets_path}...")
    conn = duckdb.connect()
    tickets_df = conn.execute(f"""
        SELECT 
            ticket_id,
            pax,
            fare,
            vehicle_number,
            ticket_type_id,
            upper(trim(route)) AS route,
            start_stop_name,
            end_stop_name,
            ts,
            is_ac,
            strftime(ts, '%Y-%m-%d') AS dt,
            dayname(ts) AS day_of_week
        FROM read_parquet('{tickets_path}')
    """).df()

    total_tickets = len(tickets_df)
    total_pax = int(tickets_df["pax"].sum())
    print(f"[match] Loaded {total_tickets:,} tickets ({total_pax:,} passengers).")

    # 4. Route-scoped matching with in-memory memoization
    print("[match] Running route-scoped matching with direction voting...")
    match_cache = {}

    def match_stop_to_seq(rk, raw_name):
        key = (rk, raw_name)
        if key in match_cache:
            return match_cache[key]

        if rk not in route_stop_maps:
            match_cache[key] = None
            return None

        data = route_stop_maps[rk]
        norm_st = normalize_stop(raw_name, manual_aliases)
        if not norm_st:
            match_cache[key] = None
            return None

        # Exact match
        if norm_st in data["name_to_idx"]:
            match_cache[key] = (data["name_to_idx"][norm_st], 100.0)
            return match_cache[key]

        # Fuzzy extractOne
        res = process.extractOne(norm_st, data["names_list"], scorer=fuzz.token_set_ratio, score_cutoff=72)
        if res:
            best_name, score, _ = res
            idx = data["name_to_idx"][best_name]
            match_cache[key] = (idx, score)
            return match_cache[key]

        match_cache[key] = None
        return None

    matched_records = []
    unmatched_counts = {}
    direction_vote_stats = {}  # route -> {primary: count, flipped: count}
    dropped_same_stop = 0
    dropped_unmatched = 0

    up_down_re = re.compile(r"^(.*?)(UP|DOWN)$", re.IGNORECASE)

    t_start = time.time()
    for row in tickets_df.itertuples(index=False):
        t_id = row.ticket_id
        pax = min(row.pax, pax_cap)
        fare = row.fare
        v_num = row.vehicle_number
        is_ac = row.is_ac
        ts = row.ts
        dt_str = row.dt
        dow = row.day_of_week
        rk = row.route
        st_start = row.start_stop_name
        st_end = row.end_stop_name

        if not st_start or not st_end or st_start == st_end:
            dropped_same_stop += 1
            continue

        # Day typing
        if dt_str in holidays or dow == "Sunday":
            day_type = "sunday_holiday"
        elif dow == "Saturday":
            day_type = "saturday"
        else:
            day_type = "weekday"

        # Candidate routes
        m = up_down_re.match(rk)
        if m:
            base, direct = m.group(1), m.group(2)
            opp_direct = "DOWN" if direct == "UP" else "UP"
            opp_rk = f"{base}{opp_direct}"
            candidates = [rk]
            if opp_rk in route_stop_maps:
                candidates.append(opp_rk)
        else:
            base = rk
            candidates = [f"{rk}UP", f"{rk}DOWN"]
            candidates = [c for c in candidates if c in route_stop_maps]
            if not candidates and rk in route_stop_maps:
                candidates = [rk]

        if not candidates:
            dropped_unmatched += 1
            pair_key = (rk, st_start, st_end)
            unmatched_counts[pair_key] = unmatched_counts.get(pair_key, 0) + 1
            continue

        best_match = None
        # Test candidate directions
        for cand_idx, c_rk in enumerate(candidates):
            m1 = match_stop_to_seq(c_rk, st_start)
            m2 = match_stop_to_seq(c_rk, st_end)
            if m1 and m2:
                s_idx, s_score = m1
                e_idx, e_score = m2
                if s_idx < e_idx:
                    is_flipped = (cand_idx > 0)
                    best_match = (c_rk, s_idx, e_idx, is_flipped)
                    break

        if best_match:
            c_rk, s_idx, e_idx, is_flipped = best_match
            c_stops = route_stop_maps[c_rk]["canonical_stops"]
            from_sid = c_stops[s_idx]
            to_sid = c_stops[e_idx]

            m_dir = up_down_re.match(c_rk)
            r_base = m_dir.group(1) if m_dir else c_rk
            r_dir = m_dir.group(2) if m_dir else "UP"

            matched_records.append({
                "ticket_id": t_id,
                "route_key": c_rk,
                "route_base": r_base,
                "direction": r_dir,
                "board_seq": s_idx,
                "alight_seq": e_idx,
                "from_stop_id": from_sid,
                "to_stop_id": to_sid,
                "pax": pax,
                "fare": fare,
                "vehicle_number": v_num,
                "is_ac": is_ac,
                "ts": ts,
                "day_type": day_type
            })

            # Record direction vote
            if rk not in direction_vote_stats:
                direction_vote_stats[rk] = {"primary": 0, "flipped": 0}
            if is_flipped:
                direction_vote_stats[rk]["flipped"] += 1
            else:
                direction_vote_stats[rk]["primary"] += 1
        else:
            dropped_unmatched += 1
            pair_key = (rk, st_start, st_end)
            unmatched_counts[pair_key] = unmatched_counts.get(pair_key, 0) + 1

    match_time = time.time() - t_start
    print(f"[match] Matching completed in {match_time:.2f}s ({len(match_cache):,} cached lookups).")

    # 5. Compile outputs and save
    print(f"[match] Converting {len(matched_records):,} matched records to Parquet...")
    matched_df = pd.DataFrame(matched_records)
    pathlib.Path(out_parquet).parent.mkdir(parents=True, exist_ok=True)
    matched_df.to_parquet(out_parquet, index=False)
    out_size_mb = os.path.getsize(out_parquet) / (1024 * 1024)
    print(f"[match] Saved matched tickets to {out_parquet} ({out_size_mb:.1f} MB)")

    matched_tickets_count = len(matched_df)
    matched_pax_count = int(matched_df["pax"].sum())
    match_ticket_pct = round(100.0 * matched_tickets_count / total_tickets, 2)
    match_pax_pct = round(100.0 * matched_pax_count / total_pax, 2)

    # 6. Save top 50 unmatched for inspection
    unmatched_list = [
        {"route": k[0], "start_stop": k[1], "end_stop": k[2], "tickets": v}
        for k, v in unmatched_counts.items()
    ]
    unmatched_top_df = pd.DataFrame(unmatched_list).sort_values("tickets", ascending=False).head(50)
    pathlib.Path(out_unmatched).parent.mkdir(parents=True, exist_ok=True)
    unmatched_top_df.to_csv(out_unmatched, index=False)
    print(f"[match] Emitted top 50 unmatched pairs to {out_unmatched}")

    # 7. Direction vote summary
    flipped_routes = [
        {"route": r, "primary": d["primary"], "flipped": d["flipped"], 
         "flip_pct": round(100.0 * d["flipped"] / (d["primary"] + d["flipped"]), 2)}
        for r, d in direction_vote_stats.items()
        if (d["primary"] + d["flipped"]) >= 100 and d["flipped"] > 0.3 * (d["primary"] + d["flipped"])
    ]

    report = {
        "metadata": {
            "total_raw_tickets": total_tickets,
            "total_raw_pax": total_pax
        },
        "match_performance": {
            "matched_tickets": matched_tickets_count,
            "matched_tickets_pct": match_ticket_pct,
            "matched_pax": matched_pax_count,
            "matched_pax_pct": match_pax_pct,
            "target_pax_pct": 93.0,
            "target_achieved": bool(match_pax_pct >= 93.0)
        },
        "dropped_summary": {
            "dropped_same_stop": dropped_same_stop,
            "dropped_unmatched_or_reversed": dropped_unmatched,
            "total_dropped": dropped_same_stop + dropped_unmatched
        },
        "day_type_distribution": matched_df["day_type"].value_counts().to_dict(),
        "routes_with_high_direction_flips": flipped_routes[:10],
        "cached_lookups_count": len(match_cache),
        "execution_seconds": round(time.time() - t0, 2)
    }

    pathlib.Path(out_report).parent.mkdir(parents=True, exist_ok=True)
    with open(out_report, "w") as f:
        json.dump(report, f, indent=2)
    print(f"[match] Saved match report to {out_report}")

    print("\n" + "=" * 70)
    print("MATCHING SCORECARD")
    print("=" * 70)
    print(f"Total Tickets:             {total_tickets:,}")
    print(f"Matched Tickets:           {matched_tickets_count:,} ({match_ticket_pct}%)")
    print(f"Total Pax:                 {total_pax:,}")
    print(f"Matched Pax Volume:        {matched_pax_count:,} ({match_pax_pct}%)")
    print(f"Dropped Same Stop:         {dropped_same_stop:,}")
    print(f"Dropped Unmatched/Reverse: {dropped_unmatched:,}")
    print(f"Total Time:                {time.time()-t0:.1f}s")
    print("=" * 70)

    return report


def main():
    parser = argparse.ArgumentParser(description="Match tickets to GTFS stop sequences.")
    parser.add_argument("--tickets-path", default="data/interim/tickets.parquet", help="Path to input tickets Parquet")
    parser.add_argument("--graph-path", default="data/interim/gtfs_graph.pkl", help="Path to compiled GTFS graph pickle")
    parser.add_argument("--config-path", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--out-parquet", default="data/interim/matched_tickets.parquet", help="Output Parquet path")
    parser.add_argument("--out-report", default="outputs/match_report.json", help="Output JSON report path")
    parser.add_argument("--out-unmatched", default="outputs/unmatched_top50.csv", help="Output CSV path for unmatched pairs")

    args = parser.parse_args()
    run_matching(
        tickets_path=args.tickets_path,
        graph_path=args.graph_path,
        config_path=args.config_path,
        out_parquet=args.out_parquet,
        out_report=args.out_report,
        out_unmatched=args.out_unmatched
    )


if __name__ == "__main__":
    main()
