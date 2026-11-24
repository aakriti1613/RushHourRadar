"""
scratch/inspect_unmatched_details.py - Detailed inspection of unmatched tickets.
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
    s = re.sub(r"\s+", " ", s).strip()
    return s

conn = duckdb.connect()
df = conn.execute("""
    SELECT route, start_stop_name, end_stop_name, count(*) as cnt
    FROM read_parquet('data/interim/tickets.parquet')
    GROUP BY route, start_stop_name, end_stop_name
    ORDER BY cnt DESC
    LIMIT 200
""").df()

for _, row in df.head(30).iterrows():
    rk = str(row["route"]).upper().strip()
    st1 = normalize_stop(row["start_stop_name"])
    st2 = normalize_stop(row["end_stop_name"])
    in_gtfs = rk in route_patterns
    c_names = []
    if in_gtfs:
        c_names = [normalize_stop(stop_lookup[sid]["stop_name"]) for sid in route_patterns[rk]["canonical_stops"]]
    m1 = process.extractOne(st1, c_names, scorer=fuzz.token_sort_ratio, score_cutoff=75) if c_names else None
    m2 = process.extractOne(st2, c_names, scorer=fuzz.token_sort_ratio, score_cutoff=75) if c_names else None
    print(f"[{row['cnt']} tickets] Route: {rk} (in_gtfs={in_gtfs}, {len(c_names)} stops)")
    print(f"   Start: '{row['start_stop_name']}' -> '{st1}' | Matched: {m1}")
    print(f"   End:   '{row['end_stop_name']}' -> '{st2}' | Matched: {m2}")
