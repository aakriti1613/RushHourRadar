"""
gtfs/build_graph.py - Compile static GTFS schedules into an efficient transit graph.

Processes:
  - Canonical stop sequences per route-direction (longest pattern, supporting short turns & loops).
  - Cumulative scheduled minutes and segment run times.
  - Scheduled trips T per (route_key, segment, day_type, bin30).
  - Next-departure timetable arrays per stop for O(log N) router lookups.
  - Spatial stop clusters (same normalized name or <= 400m).
  - cKDTree walking transfer map (<= 300m).
  - Cumulative haversine distances for fare vs distance validation.

Emits:
  - data/interim/gtfs_graph.pkl
  - data/interim/gtfs_canonical_segments.parquet
  - data/interim/gtfs_routes.parquet
  - data/interim/gtfs_stop_clusters.parquet
"""

import argparse
import math
import os
import pathlib
import pickle
import re
import time
import duckdb
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree


def haversine_km(lat1, lon1, lat2, lon2):
    """Computes great-circle distance between coordinates in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def time_str_to_minutes(t_str: str) -> float:
    """Converts 'HH:MM:SS' string to minutes from midnight."""
    if not t_str or pd.isna(t_str):
        return 0.0
    parts = str(t_str).strip().split(":")
    if len(parts) >= 2:
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2]) if len(parts) > 2 else 0.0
        return (h * 60.0 + m + s / 60.0) % 1440.0
    return 0.0


def normalize_stop_name(name: str) -> str:
    """Normalizes bus stop names for clustering and fuzzy matching."""
    if not name or pd.isna(name):
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    
    # Common Delhi transit abbreviation expansions
    expansions = {
        r"\bbdr\b": "border",
        r"\bmkt\b": "market",
        r"\bstn\b": "station",
        r"\bsec\b": "sector",
        r"\bterm\b": "terminal",
        r"\bint\b": "interchange",
        r"\brly\b": "railway",
        r"\bapt\b": "apartment",
        r"\bpkt\b": "pocket",
        r"\bcl\b": "cluster",
        r"\bisbt\b": "isbt"
    }
    for pat, rep in expansions.items():
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_stop_clusters(stops_df: pd.DataFrame, cluster_radius_m: float = 400.0):
    """
    Groups stops into spatial and lexical clusters.
    Returns:
      cluster_map: dict stop_id -> cluster_id
      clusters_df: DataFrame of cluster metadata
    """
    print(f"[graph] Building stop clusters (radius <= {cluster_radius_m}m)...")
    stops_df = stops_df.copy()
    stops_df["norm_name"] = stops_df["stop_name"].apply(normalize_stop_name)
    
    # Fast Union-Find for clustering
    parent = {s: s for s in stops_df["stop_id"]}
    
    def find(i):
        if parent[i] == i:
            return i
        parent[i] = find(parent[i])
        return parent[i]
    
    def union(i, j):
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    # 1. Union by exact normalized name
    name_groups = stops_df.groupby("norm_name")["stop_id"].apply(list)
    for s_ids in name_groups:
        if len(s_ids) > 1:
            for s in s_ids[1:]:
                union(s_ids[0], s)

    # 2. Union by physical proximity (<= cluster_radius_m)
    coords_rad = np.radians(stops_df[["stop_lat", "stop_lon"]].values)
    tree = cKDTree(coords_rad)
    r_rad = (cluster_radius_m / 6371000.0)
    pairs = tree.query_pairs(r_rad)
    
    id_list = stops_df["stop_id"].values
    for idx1, idx2 in pairs:
        # Connect if same or very close name prefix to avoid over-merging distinct hubs
        name1 = stops_df["norm_name"].iloc[idx1]
        name2 = stops_df["norm_name"].iloc[idx2]
        if name1 == name2 or name1 in name2 or name2 in name1:
            union(id_list[idx1], id_list[idx2])

    # Assign cluster IDs
    cluster_map = {s: find(s) for s in stops_df["stop_id"]}
    stops_df["cluster_id"] = stops_df["stop_id"].map(cluster_map)

    # Compile cluster summary
    cluster_records = []
    for cid, group in stops_df.groupby("cluster_id"):
        centroid_lat = float(group["stop_lat"].mean())
        centroid_lon = float(group["stop_lon"].mean())
        # Pick shortest clean name
        rep_name = min(group["stop_name"].tolist(), key=len)
        cluster_records.append({
            "cluster_id": cid,
            "cluster_name": rep_name,
            "norm_name": normalize_stop_name(rep_name),
            "lat": centroid_lat,
            "lon": centroid_lon,
            "stop_ids": group["stop_id"].tolist()
        })
    clusters_df = pd.DataFrame(cluster_records)
    print(f"[graph] Clustered {len(stops_df):,} stops into {len(clusters_df):,} clusters.")
    return cluster_map, clusters_df


def build_transfer_graph(stops_df: pd.DataFrame, max_walk_m: float = 300.0, walk_speed_m_per_s: float = 1.1, penalty_min: float = 6.0):
    """Builds pairwise walking transfer links between nearby stops."""
    print(f"[graph] Building transfer walking graph (walk <= {max_walk_m}m)...")
    coords_rad = np.radians(stops_df[["stop_lat", "stop_lon"]].values)
    tree = cKDTree(coords_rad)
    r_rad = (max_walk_m / 6371000.0)
    pairs = tree.query_pairs(r_rad)
    
    stop_ids = stops_df["stop_id"].values
    lats = stops_df["stop_lat"].values
    lons = stops_df["stop_lon"].values
    
    transfer_map = {}
    for idx1, idx2 in pairs:
        u, v = stop_ids[idx1], stop_ids[idx2]
        d_km = haversine_km(lats[idx1], lons[idx1], lats[idx2], lons[idx2])
        d_m = d_km * 1000.0
        walk_min = (d_m / (walk_speed_m_per_s * 60.0)) + penalty_min
        
        if u not in transfer_map: transfer_map[u] = []
        if v not in transfer_map: transfer_map[v] = []
        transfer_map[u].append((v, round(walk_min, 2), round(d_m, 1)))
        transfer_map[v].append((u, round(walk_min, 2), round(d_m, 1)))
        
    print(f"[graph] Generated transfer links for {len(transfer_map):,} stops.")
    return transfer_map


def build_graph(
    gtfs_dir: str = "data/raw/gtfs",
    out_dir: str = "data/interim"
):
    t0 = time.time()
    print("=" * 70)
    print("DELHI TRANSIT GRAPH COMPILER (GTFS)")
    print("=" * 70)
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)

    # 1. Load GTFS tables via DuckDB
    conn = duckdb.connect()
    gtfs_sql_dir = gtfs_dir.replace("\\", "/")
    
    print("[graph] Loading GTFS files...")
    stops_df = conn.execute(f"SELECT stop_id, stop_code, stop_name, stop_lat, stop_lon FROM read_csv('{gtfs_sql_dir}/stops.txt', header=true)").df()
    # Ensure stop_id is string
    stops_df["stop_id"] = stops_df["stop_id"].astype(str)
    stop_lookup = stops_df.set_index("stop_id").to_dict(orient="index")

    # 2. Build Clusters and Transfer graph
    cluster_map, clusters_df = build_stop_clusters(stops_df)
    transfer_map = build_transfer_graph(stops_df)

    # 3. Aggregate Trips and Patterns
    print("[graph] Aggregating route trip patterns and timetable sequences...")
    trips_query = f"""
        WITH trip_routes AS (
            SELECT 
                upper(trim(r.route_short_name)) AS route_key,
                t.trip_id,
                t.service_id
            FROM read_csv('{gtfs_sql_dir}/routes.txt', header=true) r
            JOIN read_csv('{gtfs_sql_dir}/trips.txt', header=true) t
              ON r.route_id = t.route_id
        ),
        trip_patterns AS (
            SELECT 
                st.trip_id,
                count(*) AS num_stops,
                list(st.stop_id::VARCHAR ORDER BY st.stop_sequence) AS stops,
                list(st.departure_time ORDER BY st.stop_sequence) AS dep_times,
                list(st.arrival_time ORDER BY st.stop_sequence) AS arr_times
            FROM read_csv('{gtfs_sql_dir}/stop_times.txt', header=true) st
            GROUP BY st.trip_id
        )
        SELECT 
            tr.route_key,
            tp.trip_id,
            tp.num_stops,
            tp.stops,
            tp.dep_times,
            tp.arr_times
        FROM trip_routes tr
        JOIN trip_patterns tp ON tr.trip_id = tp.trip_id
    """
    trip_data_df = conn.execute(trips_query).df()
    print(f"[graph] Loaded {len(trip_data_df):,} trips across {trip_data_df['route_key'].nunique():,} unique routes.")

    # 4. Construct canonical patterns per route
    print("[graph] Compiling canonical route patterns and scheduled trips T...")
    route_patterns = {}
    canonical_segments_records = []
    routes_summary = []

    # Pre-parse route UP/DOWN
    up_down_re = re.compile(r"^(.*?)(UP|DOWN)$", re.IGNORECASE)

    for route_key, group in trip_data_df.groupby("route_key"):
        m = up_down_re.match(route_key)
        if m:
            route_base, direction = m.group(1).upper(), m.group(2).upper()
        else:
            route_base, direction = route_key, "UP"

        # Find the trip with the maximum number of stops (canonical pattern)
        best_trip = group.loc[group["num_stops"].idxmax()]
        canonical_stops = [str(s) for s in best_trip["stops"]]
        L = len(canonical_stops)
        if L < 2:
            continue

        # Canonical stop index mapping (list for each stop_id to handle loops)
        stop_to_indices = {}
        for idx, sid in enumerate(canonical_stops):
            if sid not in stop_to_indices:
                stop_to_indices[sid] = []
            stop_to_indices[sid].append(idx)

        # Segments: k from 0 to L-2
        num_segments = L - 1

        # Calculate segment run times across all trips matching this route
        seg_times = [[] for _ in range(num_segments)]
        for _, trow in group.iterrows():
            t_stops = [str(s) for s in trow["stops"]]
            t_deps = trow["dep_times"]
            
            # Map trip stops onto canonical sequence
            # Match the first stop to canonical position
            first_sid = t_stops[0]
            if first_sid not in stop_to_indices:
                continue
            
            # Try to align sequence
            for start_idx in stop_to_indices[first_sid]:
                if start_idx + len(t_stops) <= L:
                    # Check if subsequence matches
                    if canonical_stops[start_idx : start_idx + len(t_stops)] == t_stops:
                        # Extract segment run times
                        for i in range(len(t_stops) - 1):
                            k = start_idx + i
                            t_dep1 = time_str_to_minutes(t_deps[i])
                            t_dep2 = time_str_to_minutes(t_deps[i + 1])
                            dur = (t_dep2 - t_dep1) % 1440.0
                            if 0.2 <= dur <= 60.0:  # sanity bounds on segment travel time
                                seg_times[k].append(dur)
                        break

        # Compute median segment minutes and cumulative minutes
        seg_min = []
        for k in range(num_segments):
            if seg_times[k]:
                med = float(np.median(seg_times[k]))
            else:
                # Fallback: estimate from distance at 18 km/h
                s1, s2 = canonical_stops[k], canonical_stops[k + 1]
                p1 = stop_lookup.get(s1, {"stop_lat": 28.6, "stop_lon": 77.2})
                p2 = stop_lookup.get(s2, {"stop_lat": 28.6, "stop_lon": 77.2})
                d = haversine_km(p1["stop_lat"], p1["stop_lon"], p2["stop_lat"], p2["stop_lon"])
                med = max(1.0, (d / 18.0) * 60.0)
            seg_min.append(round(med, 2))

        cum_min = [0.0]
        for m_val in seg_min:
            cum_min.append(round(cum_min[-1] + m_val, 2))

        # Calculate cumulative haversine distance
        cum_dist_km = [0.0]
        for k in range(num_segments):
            s1, s2 = canonical_stops[k], canonical_stops[k + 1]
            p1 = stop_lookup.get(s1, {"stop_lat": 28.6, "stop_lon": 77.2})
            p2 = stop_lookup.get(s2, {"stop_lat": 28.6, "stop_lon": 77.2})
            d = haversine_km(p1["stop_lat"], p1["stop_lon"], p2["stop_lat"], p2["stop_lon"])
            cum_dist_km.append(round(cum_dist_km[-1] + d, 3))

        # -------------------------------------------------------------
        # Compute Scheduled Trips T per (segment, day_type, bin30)
        # and Departure Arrays for Router
        # -------------------------------------------------------------
        # Shape: [num_segments, 48] (30-min bins: 0..47)
        # Delhi calendar runs service_id=1 on all days
        trips_T_bins = np.zeros((num_segments, 48), dtype=np.int32)
        stop_departures = [[] for _ in range(L)]

        for _, trow in group.iterrows():
            t_stops = [str(s) for s in trow["stops"]]
            t_deps = trow["dep_times"]
            
            first_sid = t_stops[0]
            if first_sid not in stop_to_indices:
                continue

            for start_idx in stop_to_indices[first_sid]:
                if start_idx + len(t_stops) <= L:
                    if canonical_stops[start_idx : start_idx + len(t_stops)] == t_stops:
                        # Record departure at stops
                        for i, dep_str in enumerate(t_deps):
                            stop_idx = start_idx + i
                            t_min = time_str_to_minutes(dep_str)
                            stop_departures[stop_idx].append(round(t_min, 1))

                        # Record trips covering segments
                        t_start_min = time_str_to_minutes(t_deps[0])
                        for i in range(len(t_stops) - 1):
                            k = start_idx + i
                            # Time when bus reaches segment k
                            seg_time = (t_start_min + cum_min[k]) % 1440.0
                            bin30 = int(seg_time // 30) % 48
                            trips_T_bins[k, bin30] += 1
                        break

        # Sort departures at each stop
        sorted_deps = [np.array(sorted(deps), dtype=np.float32) for deps in stop_departures]

        # Store in pattern dict
        route_patterns[route_key] = {
            "route_key": route_key,
            "route_base": route_base,
            "direction": direction,
            "canonical_stops": canonical_stops,
            "stop_to_indices": stop_to_indices,
            "num_stops": L,
            "num_segments": num_segments,
            "seg_min": seg_min,
            "cum_min": cum_min,
            "cum_dist_km": cum_dist_km,
            "trips_T_bins": trips_T_bins,  # [num_segments, 48]
            "departures": sorted_deps,     # list of length L
            "total_trips": len(group)
        }

        routes_summary.append({
            "route_key": route_key,
            "route_base": route_base,
            "direction": direction,
            "num_stops": L,
            "num_segments": num_segments,
            "total_dist_km": cum_dist_km[-1],
            "total_minutes": cum_min[-1],
            "total_trips": len(group)
        })

        # Save segment records
        for k in range(num_segments):
            from_sid = canonical_stops[k]
            to_sid = canonical_stops[k + 1]
            p_from = stop_lookup.get(from_sid, {})
            p_to = stop_lookup.get(to_sid, {})
            canonical_segments_records.append({
                "route_key": route_key,
                "route_base": route_base,
                "direction": direction,
                "seg_seq": k,
                "from_stop_id": from_sid,
                "to_stop_id": to_sid,
                "from_name": p_from.get("stop_name", from_sid),
                "to_name": p_to.get("stop_name", to_sid),
                "from_lat": p_from.get("stop_lat", 0.0),
                "from_lon": p_from.get("stop_lon", 0.0),
                "to_lat": p_to.get("stop_lat", 0.0),
                "to_lon": p_to.get("stop_lon", 0.0),
                "cum_min": cum_min[k],
                "seg_min": seg_min[k],
                "seg_dist_km": round(cum_dist_km[k + 1] - cum_dist_km[k], 3)
            })

    # 5. Export compiled datasets
    print("[graph] Saving compiled outputs to disk...")
    # Pickle graph
    graph_pkl_path = os.path.join(out_dir, "gtfs_graph.pkl")
    compiled_graph = {
        "route_patterns": route_patterns,
        "stop_lookup": stop_lookup,
        "cluster_map": cluster_map,
        "transfer_map": transfer_map
    }
    with open(graph_pkl_path, "wb") as f:
        pickle.dump(compiled_graph, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"[graph] Saved GTFS Graph to {graph_pkl_path} ({os.path.getsize(graph_pkl_path)/(1024*1024):.1f} MB)")

    # Parquet tables
    seg_df = pd.DataFrame(canonical_segments_records)
    seg_parquet_path = os.path.join(out_dir, "gtfs_canonical_segments.parquet")
    seg_df.to_parquet(seg_parquet_path, index=False)
    print(f"[graph] Saved {len(seg_df):,} canonical segments to {seg_parquet_path}")

    routes_summary_df = pd.DataFrame(routes_summary)
    routes_parquet_path = os.path.join(out_dir, "gtfs_routes.parquet")
    routes_summary_df.to_parquet(routes_parquet_path, index=False)
    print(f"[graph] Saved {len(routes_summary_df):,} routes summary to {routes_parquet_path}")

    clusters_parquet_path = os.path.join(out_dir, "gtfs_stop_clusters.parquet")
    # Drop stop_ids list for parquet compatibility or convert to string
    clusters_export_df = clusters_df.copy()
    clusters_export_df["num_stops"] = clusters_export_df["stop_ids"].apply(len)
    clusters_export_df = clusters_export_df.drop(columns=["stop_ids"])
    clusters_export_df.to_parquet(clusters_parquet_path, index=False)
    print(f"[graph] Saved {len(clusters_df):,} stop clusters to {clusters_parquet_path}")

    elapsed = time.time() - t0
    print(f"[graph] GTFS Graph compiled successfully in {elapsed:.1f}s.")
    return compiled_graph


def main():
    parser = argparse.ArgumentParser(description="Compile GTFS into graph structures.")
    parser.add_argument("--gtfs-dir", default="data/raw/gtfs", help="Path to raw GTFS directory")
    parser.add_argument("--out-dir", default="data/interim", help="Path to output interim directory")
    args = parser.parse_args()
    build_graph(gtfs_dir=args.gtfs_dir, out_dir=args.out_dir)


if __name__ == "__main__":
    main()
