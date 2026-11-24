"""
etl/00_load.py - Ingest raw Delhi bus ticketing data via DuckDB into Parquet.

Extracts October-Delhi.csv from data/raw/October-Delhi.csv.zip if needed,
parses IST timestamps, applies passenger_count sanity bounds, and outputs
data/interim/tickets.parquet.
Supports --only START:END (inclusive IST dates) for smoke/demo runs.
"""

import argparse
import os
import pathlib
import time
import zipfile
import duckdb


def extract_csv_if_needed(zip_path: str, interim_csv: str) -> str:
    """Extracts CSV from zip if interim_csv doesn't exist."""
    if os.path.exists(interim_csv) and os.path.getsize(interim_csv) > 0:
        return interim_csv

    pathlib.Path(interim_csv).parent.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Source zip file not found at: {zip_path}")

    print(f"[load] Extracting {zip_path} -> {interim_csv}...")
    with zipfile.ZipFile(zip_path, "r") as z:
        # Find candidate csv entry (skip macOS metadata if present)
        csv_candidates = [
            n for n in z.namelist() 
            if n.lower().endswith(".csv") and not n.startswith("__MACOSX") and not os.path.basename(n).startswith("._")
        ]
        if not csv_candidates:
            raise ValueError(f"No valid .csv found in {zip_path}")
        
        target_name = csv_candidates[0]
        print(f"[load] Extracting archive member: {target_name}")
        with z.open(target_name) as source, open(interim_csv, "wb") as dest:
            # Stream extraction in chunks to be memory safe
            while chunk := source.read(1024 * 1024 * 8):
                dest.write(chunk)
    
    print(f"[load] Extracted to {interim_csv} ({os.path.getsize(interim_csv) / (1024 * 1024):.1f} MB)")
    return interim_csv


def load(
    zip_path: str = "data/raw/October-Delhi.csv.zip",
    interim_csv: str = "data/interim/October-Delhi.csv",
    out_path: str = "data/interim/tickets.parquet",
    only: str = None
) -> str:
    t0 = time.time()
    csv_file = extract_csv_if_needed(zip_path, interim_csv)
    
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    where_clauses = [
        "start_stop_name IS NOT NULL",
        "end_stop_name IS NOT NULL",
        "passenger_count BETWEEN 1 AND 20"
    ]

    if only:
        parts = only.split(":")
        if len(parts) == 2:
            start_date, end_date = parts[0].strip(), parts[1].strip()
            where_clauses.append(f"(TRY_CAST(created_at AS TIMESTAMPTZ) AT TIME ZONE 'Asia/Kolkata') >= '{start_date} 00:00:00'")
            where_clauses.append(f"(TRY_CAST(created_at AS TIMESTAMPTZ) AT TIME ZONE 'Asia/Kolkata') <= '{end_date} 23:59:59.999999'")
            print(f"[load] Filtering date range: {start_date} to {end_date}")
        else:
            print(f"[load] Warning: Invalid --only format '{only}'. Expected 'YYYY-MM-DD:YYYY-MM-DD'. Ignoring.")

    where_sql = " AND ".join(where_clauses)
    
    # Normalize paths for DuckDB SQL (use forward slashes)
    csv_sql_path = csv_file.replace("\\", "/")
    out_sql_path = out_path.replace("\\", "/")

    print(f"[load] Querying {csv_sql_path} into DuckDB -> {out_sql_path}...")
    conn = duckdb.connect()
    
    copy_query = f"""
    COPY (
      SELECT 
        row_number() OVER () AS ticket_id,
        passenger_count::INT AS pax,
        amount::DOUBLE AS fare,
        vehicle_number,
        ticket_type_id::INT AS ticket_type_id,
        route,
        start_stop_name,
        end_stop_name,
        (TRY_CAST(created_at AS TIMESTAMPTZ) AT TIME ZONE 'Asia/Kolkata') AS ts,
        is_ac::BOOLEAN AS is_ac
      FROM read_csv('{csv_sql_path}', header=true, nullstr='NULL')
      WHERE {where_sql}
    ) TO '{out_sql_path}' (FORMAT PARQUET, COMPRESSION SNAPPY)
    """
    
    conn.execute(copy_query)
    
    count_res = conn.execute(f"SELECT count(*) FROM read_parquet('{out_sql_path}')").fetchone()[0]
    out_size_mb = os.path.getsize(out_path) / (1024 * 1024)
    elapsed = time.time() - t0
    
    print(f"[load] Successfully written {count_res:,} tickets to {out_path} ({out_size_mb:.1f} MB) in {elapsed:.2f}s")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Ingest Delhi bus ticketing data into Parquet.")
    parser.add_argument("--zip-path", default="data/raw/October-Delhi.csv.zip", help="Path to raw zip file")
    parser.add_argument("--interim-csv", default="data/interim/October-Delhi.csv", help="Path to interim CSV file")
    parser.add_argument("--out-path", default="data/interim/tickets.parquet", help="Path to output Parquet file")
    parser.add_argument("--only", default=None, help="Filter date range inclusive, e.g. 2026-10-12:2026-10-18")
    
    args = parser.parse_args()
    load(zip_path=args.zip_path, interim_csv=args.interim_csv, out_path=args.out_path, only=args.only)


if __name__ == "__main__":
    main()
