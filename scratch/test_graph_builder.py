"""
scratch/test_graph_builder.py - Test pattern aggregation and canonical sequence extraction.
"""

import time
import duckdb
import pandas as pd

t0 = time.time()
conn = duckdb.connect()

print("Querying trip patterns via DuckDB...")
query = """
    WITH trip_routes AS (
        SELECT 
            upper(trim(r.route_short_name)) as route_key,
            t.trip_id
        FROM read_csv('data/raw/gtfs/routes.txt', header=true) r
        JOIN read_csv('data/raw/gtfs/trips.txt', header=true) t
          ON r.route_id = t.route_id
    ),
    trip_patterns AS (
        SELECT 
            st.trip_id,
            count(*) as num_stops,
            min(st.departure_time) as dep_start,
            list(st.stop_id ORDER BY st.stop_sequence) as stops,
            list(st.departure_time ORDER BY st.stop_sequence) as dep_times
        FROM read_csv('data/raw/gtfs/stop_times.txt', header=true) st
        GROUP BY st.trip_id
    )
    SELECT 
        tr.route_key,
        tp.trip_id,
        tp.num_stops,
        tp.dep_start,
        tp.stops,
        tp.dep_times
    FROM trip_routes tr
    JOIN trip_patterns tp ON tr.trip_id = tp.trip_id
"""
df = conn.execute(query).df()
print(f"Aggregated {len(df):,} trips with stop lists in {time.time()-t0:.2f}s")
print("Sample route:", df['route_key'].iloc[0])
print("Num stops:", df['num_stops'].iloc[0])
print("First 5 stops:", df['stops'].iloc[0][:5])
print("First 5 dep times:", df['dep_times'].iloc[0][:5])
