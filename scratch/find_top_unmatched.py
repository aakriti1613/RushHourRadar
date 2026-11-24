"""
scratch/find_top_unmatched.py - Identify top 50 unmatched routes & stops across tickets.
"""

import pickle
import duckdb
import pandas as pd
from rapidfuzz import fuzz, process
import re

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
            parts = str(raw_name).split("/")
            if len(parts) > 1:
                for part in parts:
                    norm_part = normalize_stop(part)
                    if len(norm_part) >= 4 and norm_part not in name_to_idx:
                        name_to_idx[norm_part] = idx
                        names_list.append(norm_part)
                        
    route_stop_maps[rk] = {
        "name_to_idx": name_to_idx,
        "names_list": names_list
    }

conn = duckdb.connect()
print("Extracting top 50,000 tickets to find unmatched patterns...")
df = conn.execute("""
    SELECT route, start_stop_name, end_stop_name, count(*) as cnt
    FROM read_parquet('data/interim/tickets.parquet')
    GROUP BY route, start_stop_name, end_stop_name
    ORDER BY cnt DESC
    LIMIT 1000
""").df()

unmatched_records = []
for _, row in df.iterrows():
    rk = str(row["route"]).upper().strip()
    st_start = row["start_stop_name"]
    st_end = row["end_stop_name"]
    
    opp_rk = None
    if "UP" in rk: opp_rk = rk.replace("UP", "DOWN")
    elif "DOWN" in rk: opp_rk = rk.replace("DOWN", "UP")
    
    candidates = [rk]
    if opp_rk and opp_rk in route_stop_maps:
        candidates.append(opp_rk)
        
    matched = False
    for c_rk in candidates:
        if c_rk in route_stop_maps:
            d = route_stop_maps[c_rk]
            norm1 = normalize_stop(st_start)
            norm2 = normalize_stop(st_end)
            m1 = process.extractOne(norm1, d["names_list"], scorer=fuzz.token_set_ratio, score_cutoff=80)
            m2 = process.extractOne(norm2, d["names_list"], scorer=fuzz.token_set_ratio, score_cutoff=80)
            if m1 and m2:
                idx1 = d["name_to_idx"][m1[0]]
                idx2 = d["name_to_idx"][m2[0]]
                if idx1 < idx2:
                    matched = True
                    break
    if not matched:
        unmatched_records.append({
            "route": rk,
            "start": st_start,
            "end": st_end,
            "ticket_cnt": int(row["cnt"])
        })

print(f"Top unmatched count: {len(unmatched_records)}")
unmatched_df = pd.DataFrame(unmatched_records)
print(unmatched_df.head(20).to_string())
