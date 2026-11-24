"""
scratch/test_anand_vihar_cp.py - Test finding routes between Anand Vihar and Connaught Place.
"""

import pickle
import pandas as pd

with open("data/interim/gtfs_graph.pkl", "rb") as f:
    graph = pickle.load(f)

route_patterns = graph["route_patterns"]
stop_lookup = graph["stop_lookup"]
clusters_df = pd.read_parquet("data/interim/gtfs_stop_clusters.parquet")

print("Searching stops for Anand Vihar...")
av_stops = [sid for sid, p in stop_lookup.items() if "anand vihar" in str(p.get("stop_name", "")).lower()]
print(f"Found {len(av_stops)} Anand Vihar stops: {[stop_lookup[s]['stop_name'] for s in av_stops[:3]]}")

print("\nSearching stops for Connaught Place / Shivaji Stadium...")
cp_stops = [sid for sid, p in stop_lookup.items() if any(k in str(p.get("stop_name", "")).lower() for k in ["connaught place", "shivaji stadium"])]
print(f"Found {len(cp_stops)} CP stops: {[stop_lookup[s]['stop_name'] for s in cp_stops[:3]]}")

# Find direct routes connecting any av_stop to any cp_stop
direct_routes = []
for rk, pat in route_patterns.items():
    c_stops = pat["canonical_stops"]
    av_indices = [i for i, sid in enumerate(c_stops) if sid in av_stops]
    cp_indices = [i for i, sid in enumerate(c_stops) if sid in cp_stops]
    for s_idx in av_indices:
        for e_idx in cp_indices:
            if s_idx < e_idx:
                direct_routes.append({
                    "route_key": rk,
                    "from_stop": stop_lookup[c_stops[s_idx]]["stop_name"],
                    "to_stop": stop_lookup[c_stops[e_idx]]["stop_name"],
                    "stops_count": e_idx - s_idx,
                    "scheduled_minutes": pat["cum_min"][e_idx] - pat["cum_min"][s_idx]
                })

print(f"\nDirect routes found from Anand Vihar to CP: {len(direct_routes)}")
for r in direct_routes[:10]:
    print(r)
