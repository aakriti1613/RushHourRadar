"""
scratch/test_match_perf.py - Test route-scoped stop matching with RapidFuzz caching.
"""

import pickle
import time
import duckdb
import pandas as pd
from rapidfuzz import fuzz, process
import re

t0 = time.time()
print("Loading GTFS graph...")
with open("data/interim/gtfs_graph.pkl", "rb") as f:
    graph = pickle.load(f)

route_patterns = graph["route_patterns"]
stop_lookup = graph["stop_lookup"]
print(f"Loaded {len(route_patterns):,} route patterns in {time.time()-t0:.2f}s")

# Load unique route and stop pairs from tickets
conn = duckdb.connect()
print("Extracting unique (route, stop) pairs from tickets.parquet...")
pairs_df = conn.execute("""
    WITH all_stops AS (
        SELECT route, start_stop_name AS stop_name FROM read_parquet('data/interim/tickets.parquet')
        UNION
        SELECT route, end_stop_name AS stop_name FROM read_parquet('data/interim/tickets.parquet')
    )
    SELECT DISTINCT route, stop_name FROM all_stops WHERE stop_name IS NOT NULL
""").df()
print(f"Total distinct (route, stop) pairs: {len(pairs_df):,} in {time.time()-t0:.2f}s")

# Let's test matching first 1000 pairs
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

# Pre-build normalized names for canonical route stops
route_canonical_names = {}
for rk, pat in route_patterns.items():
    c_stops = pat["canonical_stops"]
    names = [normalize_stop(stop_lookup[sid]["stop_name"]) for sid in c_stops if sid in stop_lookup]
    route_canonical_names[rk] = names

# Match test
sample = pairs_df.head(2000)
matched = 0
t1 = time.time()
for _, row in sample.iterrows():
    rk = str(row["route"]).upper().strip()
    st = normalize_stop(row["stop_name"])
    if rk in route_canonical_names and route_canonical_names[rk]:
        res = process.extractOne(st, route_canonical_names[rk], scorer=fuzz.token_sort_ratio, score_cutoff=75)
        if res:
            matched += 1

print(f"Matched {matched}/{len(sample)} ({100.0*matched/len(sample):.1f}%) in {time.time()-t1:.2f}s")
