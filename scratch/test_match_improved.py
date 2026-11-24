"""
scratch/test_match_improved.py - Test enhanced route-scoped matching.
"""

import pickle
import time
import duckdb
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

# Pre-build stop candidates per route:
# Map both full name and slash-split components to stop sequence index
route_stop_maps = {}
for rk, pat in route_patterns.items():
    c_stops = pat["canonical_stops"]
    name_to_idx = {}
    names_list = []
    for idx, sid in enumerate(c_stops):
        p = stop_lookup.get(sid, {})
        raw_name = p.get("stop_name", sid)
        norm_full = normalize_stop(raw_name)
        if norm_full:
            name_to_idx[norm_full] = idx
            names_list.append(norm_full)
            # Also add slash-separated parts
            parts = str(raw_name).split("/")
            if len(parts) > 1:
                for part in parts:
                    norm_part = normalize_stop(part)
                    if len(norm_part) >= 4 and norm_part not in name_to_idx:
                        name_to_idx[norm_part] = idx
                        names_list.append(norm_part)
                        
    route_stop_maps[rk] = {
        "name_to_idx": name_to_idx,
        "names_list": names_list,
        "L": len(c_stops)
    }

print(f"Pre-processed route stops in {time.time()-t0:.2f}s")

# Load 20,000 random tickets
conn = duckdb.connect()
sample_df = conn.execute("""
    SELECT ticket_id, route, start_stop_name, end_stop_name, pax, fare, is_ac
    FROM read_parquet('data/interim/tickets.parquet')
    LIMIT 20000
""").df()

match_cache = {}

def match_stop_to_seq(rk, raw_name):
    key = (rk, raw_name)
    if key in match_cache:
        return match_cache[key]
    
    if rk not in route_stop_maps:
        match_cache[key] = None
        return None
        
    data = route_stop_maps[rk]
    norm_st = normalize_stop(raw_name)
    if not norm_st:
        match_cache[key] = None
        return None
        
    # 1. Exact lookup
    if norm_st in data["name_to_idx"]:
        match_cache[key] = (data["name_to_idx"][norm_st], 100.0)
        return match_cache[key]
        
    # 2. Token set / sort ratio via extractOne
    res = process.extractOne(norm_st, data["names_list"], scorer=fuzz.token_set_ratio, score_cutoff=80)
    if res:
        best_name, score, _ = res
        idx = data["name_to_idx"][best_name]
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
    
    if not st_start or not st_end or st_start == st_end:
        continue

    opp_rk = None
    if "UP" in rk: opp_rk = rk.replace("UP", "DOWN")
    elif "DOWN" in rk: opp_rk = rk.replace("DOWN", "UP")
    
    candidates = [rk]
    if opp_rk and opp_rk in route_stop_maps:
        candidates.append(opp_rk)
        
    best_candidate = None
    for c_rk in candidates:
        m1 = match_stop_to_seq(c_rk, st_start)
        m2 = match_stop_to_seq(c_rk, st_end)
        if m1 and m2:
            s_idx, s_score = m1
            e_idx, e_score = m2
            if s_idx < e_idx:
                best_candidate = (c_rk, s_idx, e_idx)
                break
                
    if best_candidate:
        matched_count += 1
        valid_order_count += 1
    else:
        # Check if matched in primary candidate but reversed order
        m1 = match_stop_to_seq(rk, st_start)
        m2 = match_stop_to_seq(rk, st_end)
        if m1 and m2:
            matched_count += 1

print(f"Processed {len(sample_df):,} sample tickets in {time.time()-t1:.2f}s")
print(f"Matched both stops: {matched_count}/{len(sample_df)} ({100.0*matched_count/len(sample_df):.2f}%)")
print(f"Valid sequence order (start < end): {valid_order_count}/{len(sample_df)} ({100.0*valid_order_count/len(sample_df):.2f}%)")
