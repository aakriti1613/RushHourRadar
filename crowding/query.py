"""
crowding/query.py - Segment and route crowdedness query API & CLI (Contract D).

Provides:
  - get_crowding(route_key, from_stop=None, to_stop=None, day_type="weekday", time_str="18:00") -> pd.DataFrame
  - Linear interpolation of loads between 30-minute bin centres.
  - GeoJSON export helper for web/map visualization.
  - Command-line interface: python -m crowding.query --route 8UP --time 18:20 --day weekday
"""

import argparse
import json
import os
import re
import duckdb
import numpy as np
import pandas as pd


_PARQUET_PATH = "data/processed/segment_crowding.parquet"
_cached_duckdb = None


def get_connection():
    global _cached_duckdb
    if _cached_duckdb is None:
        _cached_duckdb = duckdb.connect()
    return _cached_duckdb


def time_to_bin_and_alpha(time_str: str):
    """
    Computes lower bin, upper bin, and interpolation weight alpha.
    30-min bin b has centre at (b * 30 + 15) minutes from midnight.
    """
    parts = str(time_str).strip().split(":")
    h = int(parts[0]) if len(parts) > 0 else 18
    m = int(parts[1]) if len(parts) > 1 else 0
    t_min = (h * 60 + m) % 1440.0

    # Bin index [0..47]
    raw_bin = (t_min - 15.0) / 30.0
    b_low = int(np.floor(raw_bin)) % 48
    b_high = (b_low + 1) % 48
    alpha = float(raw_bin - np.floor(raw_bin))
    nominal_bin = int(t_min // 30) % 48
    return b_low, b_high, alpha, nominal_bin


def get_crowding(
    route_key: str,
    from_stop: str = None,
    to_stop: str = None,
    day_type: str = "weekday",
    time_str: str = "18:00",
    parquet_path: str = _PARQUET_PATH
) -> pd.DataFrame:
    """
    Retrieves segment-level crowding with linear interpolation between 30-min bin centres.
    Conforms to Contract A schema.
    """
    if not os.path.exists(parquet_path):
        # Fallback to app/data
        alt_path = os.path.join("app", "data", "segment_crowding.parquet")
        if os.path.exists(alt_path):
            parquet_path = alt_path
        else:
            raise FileNotFoundError(f"Missing {parquet_path}. Run model/04_posterior.py first.")

    rk = str(route_key).upper().strip()
    b_low, b_high, alpha, nominal_bin = time_to_bin_and_alpha(time_str)

    conn = get_connection()
    sql_path = parquet_path.replace("\\", "/")

    query = f"""
        SELECT *
        FROM read_parquet('{sql_path}')
        WHERE route_key = '{rk}'
          AND day_type = '{day_type}'
          AND bin30 IN ({b_low}, {b_high})
        ORDER BY seg_seq, bin30
    """
    raw_df = conn.execute(query).df()
    if raw_df.empty:
        return pd.DataFrame()

    # Interpolate loads between low and high bin for each segment
    segs = sorted(raw_df["seg_seq"].unique())
    interpolated_records = []

    for seg in segs:
        seg_rows = raw_df[raw_df["seg_seq"] == seg]
        row_low = seg_rows[seg_rows["bin30"] == b_low]
        row_high = seg_rows[seg_rows["bin30"] == b_high]

        # Use nominal row as metadata baseline
        base_row = seg_rows.iloc[0] if (seg_rows["bin30"] == nominal_bin).empty else seg_rows[seg_rows["bin30"] == nominal_bin].iloc[0]

        if not row_low.empty and not row_high.empty:
            l_low, l_high = row_low["est_load"].values[0], row_high["est_load"].values[0]
            p10_l, p10_h = row_low["load_p10"].values[0], row_high["load_p10"].values[0]
            p90_l, p90_h = row_low["load_p90"].values[0], row_high["load_p90"].values[0]
            pcr_l, pcr_h = row_low["p_crowded"].values[0], row_high["p_crowded"].values[0]

            est_load = round(float((1.0 - alpha) * l_low + alpha * l_high), 1)
            load_p10 = round(float((1.0 - alpha) * p10_l + alpha * p10_h), 1)
            load_p90 = round(float((1.0 - alpha) * p90_l + alpha * p90_h), 1)
            p_crowded = round(float((1.0 - alpha) * pcr_l + alpha * pcr_h), 3)
        else:
            est_load = float(base_row["est_load"])
            load_p10 = float(base_row["load_p10"])
            load_p90 = float(base_row["load_p90"])
            p_crowded = float(base_row["p_crowded"])

        seat_cap = float(base_row["seat_cap"])
        max_cap = float(base_row["max_cap"])

        # Re-evaluate tier on interpolated load
        if est_load < 0.90 * seat_cap:
            tier = "Seats Available"
        elif est_load < 0.80 * max_cap:
            tier = "Standing Room"
        else:
            tier = "Crowded"

        rec = base_row.to_dict()
        rec["bin30"] = nominal_bin
        rec["est_load"] = est_load
        rec["load_p10"] = load_p10
        rec["load_p90"] = load_p90
        rec["p_crowded"] = p_crowded
        rec["crowding_tier"] = tier
        interpolated_records.append(rec)

    res_df = pd.DataFrame(interpolated_records)

    # Optional slice by from_stop / to_stop
    if from_stop or to_stop:
        start_idx = 0
        end_idx = len(res_df)
        if from_stop:
            m_from = res_df[res_df["from_name"].str.contains(from_stop, case=False, na=False)]
            if not m_from.empty:
                start_idx = int(m_from["seg_seq"].min())
        if to_stop:
            m_to = res_df[res_df["to_name"].str.contains(to_stop, case=False, na=False)]
            if not m_to.empty:
                end_idx = int(m_to["seg_seq"].max()) + 1
        if start_idx < end_idx:
            res_df = res_df[(res_df["seg_seq"] >= start_idx) & (res_df["seg_seq"] < end_idx)]

    return res_df


def to_geojson(crowding_df: pd.DataFrame) -> dict:
    """Converts crowding DataFrame into a GeoJSON FeatureCollection."""
    features = []
    color_map = {
        "Seats Available": "#0072B2",
        "Standing Room": "#E69F00",
        "Crowded": "#D55E00"
    }

    for _, row in crowding_df.iterrows():
        coords = [
            [float(row["from_lon"]), float(row["from_lat"])],
            [float(row["to_lon"]), float(row["to_lat"])]
        ]
        tier = row["crowding_tier"]
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords
            },
            "properties": {
                "route_key": row["route_key"],
                "seg_seq": int(row["seg_seq"]),
                "from_name": row["from_name"],
                "to_name": row["to_name"],
                "est_load": float(row["est_load"]),
                "load_p10": float(row["load_p10"]),
                "load_p90": float(row["load_p90"]),
                "crowding_tier": tier,
                "color": color_map.get(tier, "#0072B2"),
                "trust_level": row["trust_level"],
                "tickets_n": int(row["tickets_n"])
            }
        })

    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description="Query Delhi bus crowding model.")
    parser.add_argument("--route", required=True, help="Route key e.g. 8UP or 433UP")
    parser.add_argument("--time", default="18:00", help="Time of day HH:MM e.g. 18:20")
    parser.add_argument("--day", default="weekday", choices=["weekday", "saturday", "sunday_holiday"])
    parser.add_argument("--from-stop", default=None, help="Filter from stop name")
    parser.add_argument("--to-stop", default=None, help="Filter to stop name")
    parser.add_argument("--geojson", action="store_true", help="Output GeoJSON instead of table")

    args = parser.parse_args()
    df = get_crowding(
        route_key=args.route,
        from_stop=args.from_stop,
        to_stop=args.to_stop,
        day_type=args.day,
        time_str=args.time
    )

    if df.empty:
        print(f"No crowding data found for route '{args.route}' on {args.day} around {args.time}.")
        return

    if args.geojson:
        print(json.dumps(to_geojson(df), indent=2))
    else:
        print(f"\nCrowding for Route {args.route} at {args.time} ({args.day}):")
        cols = ["seg_seq", "from_name", "to_name", "est_load", "load_p10", "load_p90", "crowding_tier", "trust_level"]
        print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
