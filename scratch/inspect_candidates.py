"""
scratch/inspect_candidates.py - Inspect all candidate routes for Anand Vihar to CP.
"""

import sys
sys.path.insert(0, ".")
from router.router import get_planner, time_str_to_min

p = get_planner()
orig_stops = p.resolve_stops("Anand Vihar")
dest_stops = p.resolve_stops("Connaught Place")
raw_cands = p.find_direct_candidates(orig_stops, dest_stops)
print("Total raw candidates:", len(raw_cands))
t_min = time_str_to_min("18:00")
scored = []
for c in raw_cands:
    opt = p.score_direct_trip(c, t_min, t_min, "weekday")
    if opt:
        scored.append(opt)

seen_routes = set()
print(f"Total scored options: {len(scored)}")
for s in scored:
    rk = s["legs"][0]["route_key"]
    if rk not in seen_routes:
        seen_routes.add(rk)
        print(f"Route {rk}: Depart {s['departure_time']}, Arrive {s['arrival_time']}, Total {s['total_minutes']}m, Wait {s['wait_minutes']}m, Tier: {s['overall_tier']}, Trust: {s['trust_level']}")
