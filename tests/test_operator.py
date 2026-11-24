import duckdb
import pytest


def test_relief_metric_no_saturation():
    """
    Test that operator relief metric grows monotonically with passenger load
    and does not saturate at a constant value (verifying Section 0 item 7).
    """
    thr = 60.0  # 0.8 * max_cap
    dur = 3.0   # 3 minutes
    n_buses = 4.0

    loads = [80.0, 120.0, 160.0, 200.0]
    reliefs = []

    for l_curr in loads:
        d_seg = l_curr * n_buses
        ov = dur * d_seg * (l_curr / thr - 1.0)
        l_plus1 = l_curr * (n_buses / (n_buses + 1.0))
        ov_plus1 = dur * d_seg * (l_plus1 / thr - 1.0) if l_plus1 > thr else 0.0
        relief = ov - ov_plus1
        reliefs.append(relief)

    # Relief must strictly increase as load increases (no saturation)
    for i in range(len(reliefs) - 1):
        assert reliefs[i + 1] > reliefs[i], f"Relief saturated: {reliefs[i+1]} <= {reliefs[i]}"


def test_operator_table_contract_and_ranking():
    """
    Test Contract B dataset properties:
    - relief_rider_min >= 0
    - underused routes identified
    - ranked relief routes have positive overload
    """
    conn = duckdb.connect()
    res = conn.execute("""
        SELECT 
            COUNT(*) as total_rows,
            COUNT(CASE WHEN relief_rider_min < 0 THEN 1 END) as neg_relief,
            COUNT(CASE WHEN underused THEN 1 END) as underused_rows,
            MAX(relief_rider_min) as max_relief
        FROM 'data/processed/operator_overload.parquet'
    """).df()

    assert res["total_rows"][0] > 90000
    assert res["neg_relief"][0] == 0, "Found negative relief values!"
    assert res["underused_rows"][0] > 0, "Expected some underused route-hours!"
    assert res["max_relief"][0] > 1000.0, "Expected significant relief on bottleneck corridors!"


def test_build_operator_table_uses_rider_weighted_relief_without_saturation():
    """Runs the real model code (not a copy of the formula) on a toy segment table."""
    import importlib.util, pathlib, pandas as pd
    spec = importlib.util.spec_from_file_location("post", pathlib.Path(__file__).resolve().parent.parent / "model" / "04_posterior.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    def table(load):
        df = pd.DataFrame({"route_key": "X", "day_type": "weekday", "bin30": [18, 19], "seg_seq": 0, "est_load": load,
                           "seat_cap": 38.0, "max_cap": 75.0, "trips_sched_per_day": 2, "seg_min": 3.0,
                           "from_name": "A", "to_name": "B", "trust_level": "High", "tickets_n": 50})
        return mod.build_operator_table(df).iloc[0]

    reliefs = [table(l).relief_rider_min for l in (80.0, 120.0, 200.0, 400.0)]
    assert all(b > a for a, b in zip(reliefs, reliefs[1:])), reliefs          # grows with severity: no saturation
    assert table(30.0).relief_rider_min == 0 and bool(table(10.0).underused)
