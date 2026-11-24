import duckdb
import json

print("="*60)
print("VERIFYING CONTRACT A: segment_crowding.parquet")
print("="*60)

path_a = "data/processed/segment_crowding.parquet"
df_a = duckdb.query(f"SELECT * FROM '{path_a}' LIMIT 2").df()
cols_a = df_a.columns.tolist()
print("Columns count:", len(cols_a))
print("Columns:", cols_a)
count_a = duckdb.query(f"SELECT COUNT(*) FROM '{path_a}'").fetchone()[0]
print("Total rows:", count_a)

req_cols_a = [
    "route_key", "route_base", "direction", "seg_seq",
    "from_stop_id", "to_stop_id", "from_name", "to_name",
    "from_lat", "from_lon", "to_lat", "to_lon",
    "day_type", "bin30", "est_load", "load_p10", "load_p90",
    "seat_cap", "max_cap", "crowding_tier", "p_crowded",
    "rel_rank", "credits_n", "tickets_n", "trust_level"
]
missing_a = [c for c in req_cols_a if c not in cols_a]
print("Missing columns in Contract A:", missing_a)

# Check nulls and types
null_check_a = duckdb.query(f"""
    SELECT 
        COUNT(CASE WHEN route_key IS NULL THEN 1 END) as null_route,
        COUNT(CASE WHEN est_load IS NULL THEN 1 END) as null_est_load,
        COUNT(CASE WHEN crowding_tier IS NULL THEN 1 END) as null_tier,
        COUNT(CASE WHEN trust_level IS NULL THEN 1 END) as null_trust,
        MIN(est_load) as min_load,
        MAX(est_load) as max_load,
        MIN(bin30) as min_bin,
        MAX(bin30) as max_bin
    FROM '{path_a}'
""").df()
print("Null & Range Check Contract A:\n", null_check_a.to_dict(orient='records'))

print("\n" + "="*60)
print("VERIFYING CONTRACT B: operator_overload.parquet")
print("="*60)

path_b = "data/processed/operator_overload.parquet"
df_b = duckdb.query(f"SELECT * FROM '{path_b}' LIMIT 2").df()
cols_b = df_b.columns.tolist()
print("Columns count:", len(cols_b))
print("Columns:", cols_b)
count_b = duckdb.query(f"SELECT COUNT(*) FROM '{path_b}'").fetchone()[0]
print("Total rows:", count_b)

req_cols_b = [
    "route_key", "day_type", "hour", "trips_hr",
    "peak_load", "peak_seg_seq", "peak_from_name", "peak_to_name",
    "overload_rider_min", "relief_rider_min",
    "tier_now", "tier_plus1", "peak_window_hours",
    "underused", "plus1_assumption", "trust_level"
]
missing_b = [c for c in req_cols_b if c not in cols_b]
print("Missing columns in Contract B:", missing_b)

null_check_b = duckdb.query(f"""
    SELECT 
        COUNT(CASE WHEN route_key IS NULL THEN 1 END) as null_route,
        COUNT(CASE WHEN overload_rider_min IS NULL THEN 1 END) as null_overload,
        COUNT(CASE WHEN relief_rider_min IS NULL THEN 1 END) as null_relief,
        MIN(overload_rider_min) as min_overload,
        MAX(overload_rider_min) as max_overload,
        MIN(relief_rider_min) as min_relief,
        MAX(relief_rider_min) as max_relief
    FROM '{path_b}'
""").df()
print("Null & Range Check Contract B:\n", null_check_b.to_dict(orient='records'))

# Check app/data parity
print("\n" + "="*60)
print("VERIFYING APP/DATA PARITY")
print("="*60)
count_app_a = duckdb.query("SELECT COUNT(*) FROM 'app/data/segment_crowding.parquet'").fetchone()[0]
count_app_b = duckdb.query("SELECT COUNT(*) FROM 'app/data/operator_overload.parquet'").fetchone()[0]
print(f"app/data/segment_crowding.parquet rows: {count_app_a} (matches processed: {count_app_a == count_a})")
print(f"app/data/operator_overload.parquet rows: {count_app_b} (matches processed: {count_app_b == count_b})")
