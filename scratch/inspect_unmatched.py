"""
scratch/inspect_unmatched.py - Inspect unmatched stop names to find abbreviation rules.
"""

import pickle
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
    s = re.sub(r"\s+", " ", s).strip()
    return s

import duckdb
conn = duckdb.connect()
pairs_df = conn.execute("""
    WITH all_stops AS (
        SELECT route, start_stop_name AS stop_name FROM read_parquet('data/interim/tickets.parquet')
        UNION
        SELECT route, end_stop_name AS stop_name FROM read_parquet('data/interim/tickets.parquet')
    )
    SELECT route, stop_name, count(*) as freq 
    FROM (
        SELECT route, start_stop_name as stop_name FROM read_parquet('data/interim/tickets.parquet')
        UNION ALL
        SELECT route, end_stop_name as stop_name FROM read_parquet('data/interim/tickets.parquet')
    )
    GROUP BY route, stop_name
    ORDER BY freq DESC
    LIMIT 200
""").df()

route_canonical_names = {}
for rk, pat in route_patterns.items():
    c_stops = pat["canonical_stops"]
    names = [normalize_stop(stop_lookup[sid]["stop_name"]) for sid in c_stops if sid in stop_lookup]
    route_canonical_names[rk] = names

unmatched = []
for _, row in pairs_df.iterrows():
    rk = str(row["route"]).upper().strip()
    st = normalize_stop(row["stop_name"])
    if rk in route_canonical_names and route_canonical_names[rk]:
        res = process.extractOne(st, route_canonical_names[rk], scorer=fuzz.token_sort_ratio, score_cutoff=75)
        if not res:
            # Check opposite direction if UP/DOWN
            opp_rk = rk.replace("UP", "TEMP").replace("DOWN", "UP").replace("TEMP", "DOWN")
            opp_res = None
            if opp_rk in route_canonical_names and route_canonical_names[opp_rk]:
                opp_res = process.extractOne(st, route_canonical_names[opp_rk], scorer=fuzz.token_sort_ratio, score_cutoff=75)
            unmatched.append({
                "route": rk,
                "stop_name": row["stop_name"],
                "freq": row["freq"],
                "opp_match": opp_res[0] if opp_res else None,
                "opp_score": opp_res[1] if opp_res else None,
                "route_stops_sample": route_canonical_names[rk][:5]
            })

print(f"Top high-frequency unmatched ({len(unmatched)}):")
for u in unmatched[:15]:
    print(u)
