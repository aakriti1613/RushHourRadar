"""
scratch/test_credits_perf.py - Benchmark credit tensor compilation speed.
"""

import pickle
import time
import duckdb
import numpy as np
import pandas as pd

t0 = time.time()
print("Loading GTFS graph and matched tickets...")
with open("data/interim/gtfs_graph.pkl", "rb") as f:
    graph = pickle.load(f)

route_patterns = graph["route_patterns"]

conn = duckdb.connect()
df = conn.execute("""
    SELECT 
        ticket_id,
        route_key,
        board_seq,
        alight_seq,
        pax,
        ts,
        day_type
    FROM read_parquet('data/interim/matched_tickets.parquet')
    LIMIT 100000
""").df()
print(f"Loaded {len(df):,} sample matched tickets in {time.time()-t0:.2f}s")

# Test credit function
def get_credits(s, e, L, kernel=(0.15, 0.70, 0.15)):
    cand = {e - 1: kernel[0], e: kernel[1], e + 1: kernel[2]}
    cand = {j: w for j, w in cand.items() if s < j <= L - 1}
    if not cand:
        return []
    tot = sum(cand.values())
    cand = {j: w / tot for j, w in cand.items()}
    return [(k, sum(w for j, w in cand.items() if j >= k + 1))
            for k in range(s, max(cand))]

# Precompute credits map for all possible (s, e, L)
credits_cache = {}
def cached_credits(s, e, L):
    key = (s, e, L)
    if key not in credits_cache:
        credits_cache[key] = get_credits(s, e, L)
    return credits_cache[key]

t1 = time.time()
sample_records = 0
for row in df.itertuples(index=False):
    s = row.board_seq
    e = row.alight_seq
    rk = row.route_key
    if rk in route_patterns:
        L = route_patterns[rk]["num_stops"]
        c_list = cached_credits(s, e, L)
        sample_records += len(c_list)

print(f"Processed {len(df):,} tickets -> {sample_records:,} segment credits in {time.time()-t1:.2f}s")
print(f"Cached paths: {len(credits_cache)}")
