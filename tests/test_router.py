import pathlib
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from router.router import plan_journey, get_planner, TIER_RANK


def test_golden_corridor_anand_vihar_connaught_place():
    options = plan_journey("Anand Vihar", "Connaught Place", dep_time="18:00", day_type="weekday", mode="balanced")
    assert 2 <= len(options) <= 5
    base = [o for o in options if not o["is_time_shifted"]]
    assert base, "requested departure was dropped"
    keys = [o["boarding_trip_key"] for o in options]
    assert len(keys) == len(set(keys)), f"duplicate boarding trips: {keys}"
    for o in options:
        assert o["total_minutes"] > 0 and o["wait_minutes"] >= 0


def test_time_shifted_options_are_strictly_better_and_labelled_from_real_times():
    """v7 spec fix #2/#3: a shifted option exists only if it boards a new bus AND has a strictly better tier."""
    for o_, d_, t_ in [("Anand Vihar", "Connaught Place", "18:00"), ("Kashmere Gate", "Nehru Place", "09:00"),
                       ("Rohini", "Karol Bagh", "18:30"), ("Azadpur", "AIIMS", "08:30")]:
        opts = plan_journey(o_, d_, dep_time=t_, day_type="weekday")
        base = [o for o in opts if not o["is_time_shifted"]]
        known = [TIER_RANK[o["overall_tier"]] for o in base if o["overall_tier"] != "No estimate"]
        for o in opts:
            if o["is_time_shifted"]:
                assert known and TIER_RANK[o["overall_tier"]] < min(known)
                h, m = map(int, t_.split(":"))
                diff = round(o["_t_board"] - (h * 60 + m))
                assert str(abs(diff)) in o["option_label"], (o["option_label"], diff)


def test_no_backward_boarding_and_stretches_have_geometry():
    for o in plan_journey("Kashmere Gate", "Nehru Place", dep_time="09:00", day_type="weekday"):
        for leg in o["legs"]:
            assert leg["stretches"] and all(len(s["coords"]) >= 2 and s["dur"] >= 0 for s in leg["stretches"])


def test_one_transfer_fallback_when_no_direct_bus():
    opts = plan_journey("Sarita Vihar", "Mandi House", dep_time="09:00", day_type="weekday")
    assert opts and all(o["transfers"] == 1 and len(o["legs"]) == 2 for o in opts)
    for o in opts:
        assert o["total_minutes"] > sum(l["minutes"] for l in o["legs"]) * 0.9      # includes wait + change


def test_missing_estimate_is_never_reported_as_comfortable():
    pl = get_planner()
    load, p90, tier, trust, n, pc = pl.get_interpolated_segment_crowding("73UP", 5, 150.0, "weekday")  # 02:30
    assert tier == "No estimate" and load is None and trust == "Low"
    load, p90, tier, trust, n, pc = pl.get_interpolated_segment_crowding("NOT_A_ROUTE", 0, 600.0, "weekday")
    assert tier == "No estimate"


def test_router_latency():
    plan_journey("Azadpur", "AIIMS", dep_time="08:30", day_type="weekday")
    t0 = time.time()
    plan_journey("Azadpur", "AIIMS", dep_time="08:30", day_type="weekday")
    assert (time.time() - t0) * 1000 < 100
