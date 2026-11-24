"""
scratch/test_direction_vote.py - Test ticket matching with direction voting.
"""

import pickle
import time
import duckdb
import pandas as pd
from rapidfuzz import fuzz, process
import re

t0 = time.time()
with open("data/interim/gtfs_graph.pkl", "rb") as f:
    graph = pickle.load(f)

route_patterns = graph["route_patterns"]
stop_lookup = graph["stop_lookup"]

def normalize_stop(name):
    if not name: return ""
    s = str(name).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\bbdr\b", "border", s)
    s = re.sub(r"\bmkt\b", "market", s)
    s = re.sub(r"\bstn\b", "station", s)
    s = re.sub(r"\bterm\b", "terminal", s)
    s = re.sub(r"\bsec\b", "sector", s)
    s = re.sub(r"\bint\b", "interchange", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Pre-build normalized names and stop sequence indices
route_stop_info = {}
for rk, pat in route_patterns.items():
    c_stops = pat["canonical_stops"]
    names = []
    for sid in c_stops:
        p = stop_lookup.get(sid, {})
        names.append(normalize_stop(p.get("stop_name", sid)))
    route_stop_info[rk] = {
        "canonical_stops": c_stops,
        "norm_names": names,
        "L": len(c_stops)
    }

print(f"Pre-processed route stops in {time.time()-t0:.2f}s")

# Load 10,000 random tickets
conn = duckdb.connect()
sample_df = conn.execute("""
    SELECT ticket_id, route, start_stop_name, end_stop_name, pax, fare, is_ac
    FROM read_parquet('data/interim/tickets.parquet')
    LIMIT 20000
""").df()

# Cache for (route_key, norm_stop_name) -> best matching stop index in that route
match_cache = {}

def get_best_stop_idx(rk, raw_name):
    key = (rk, raw_name)
    if key in match_cache:
        return match_cache[key]
    
    if rk not in route_stop_info:
        match_cache[key] = None
        return None
    
    info = route_stop_info[rk]
    norm_st = normalize_stop(raw_name)
    
    # 1. Exact match
    try:
        idx = info["norm_names"].index(norm_st)
        match_cache[key] = (idx, 100.0)
        return match_cache[key]
    except ValueError:
        pass
        
    # 2. Fuzzy match
    res = process.extractOne(norm_st, info["norm_names"], scorer=fuzz.token_sort_ratio, score_cutoff=75)
    if res:
        match_name, score, idx = res
        match_cache[key] = (idx, score)
        return match_cache[key]
        
    match_cache[key] = None
    return None

t1 = time.time()
matched_count = 0
valid_order_count = 0

for _, row in sample_df.iterrows():
    rk = str(row["route"]).upper().strip()
    st_start = row["start_stop_name"]
    st_end = row["end_stop_name"]
    
    if not st_start or not st_end:
        continue
        
    # Candidate routes: rk, and opposite direction if UP/DOWN
    opp_rk = None
    if "UP" in rk: opp_rk = rk.replace("UP", "DOWN")
    elif "DOWN" in rk: opp_rk = rk.replace("DOWN", "UP")
    
    candidates = [rk]
    if opp_rk and opp_rk in route_stop_info:
        candidates.append(opp_rk)
        
    found_match = False
    for c_rk in candidates:
        m_start = get_best_stop_idx(c_rk, st_start)
        m_end = get_best_stop_idx(c_rk, st_end)
        if m_start and m_end:
            found_match = True
            idx_start, score_start = m_start
            idx_end, score_end = m_end
            if idx_start < idx_end:
                valid_order_count += 1
                matched_count += 1
                break
                
    if not found_match:
        # Check if matched in primary candidate but reversed order
        m1 = get_best_stop_idx(rk, st_start)
        m2 = get_best_stop_idx(rk, st_end)
        if m1 and m2:
            matched_count += 1

print(f"Processed {len(sample_df):,} sample tickets in {time.time()-t1:.2f}s")
print(f"Matched both stops: {matched_count}/{len(sample_df)} ({100.0*matched_count/len(sample_df):.2f}%)")
print(f"Valid sequence order (start < end): {valid_order_count}/{len(sample_df)} ({100.0*valid_order_count/len(sample_df):.2f}%)")
