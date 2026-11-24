import pathlib
import sys
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from router.router import plan_journey

print("="*60)
print("VERIFYING CONTRACT C: plan_journey return structure")
print("="*60)

options = plan_journey("Anand Vihar", "Connaught Place", dep_time="18:00", day_type="weekday", mode="balanced")

print(f"Retrieved {len(options)} options for Anand Vihar -> Connaught Place (18:00 weekday)")
assert len(options) > 0, "No options returned!"

req_keys = [
    "option_label", "is_time_shifted", "departure_time", "boarding_trip_key",
    "arrival_time", "total_minutes", "wait_minutes", "transfers",
    "overall_tier", "board_tier", "trust_level", "confidence_text",
    "summary_note", "comfort_penalty", "legs"
]

req_leg_keys = ["route_key", "board_stop", "alight_stop", "minutes", "stretches"]
req_stretch_keys = ["from_stop", "to_stop", "tier", "load", "p90", "coords"]

for i, opt in enumerate(options):
    missing_keys = [k for k in req_keys if k not in opt]
    print(f"Option {i+1} [{opt.get('option_label')}]: Missing keys: {missing_keys}")
    assert len(missing_keys) == 0, f"Option {i+1} missing keys: {missing_keys}"
    
    # Check legs
    assert len(opt["legs"]) > 0, f"Option {i+1} has no legs!"
    for leg in opt["legs"]:
        missing_leg = [k for k in req_leg_keys if k not in leg]
        assert len(missing_leg) == 0, f"Leg missing keys: {missing_leg}"
        for stretch in leg["stretches"]:
            missing_stretch = [k for k in req_stretch_keys if k not in stretch]
            assert len(missing_stretch) == 0, f"Stretch missing keys: {missing_stretch}"

print("All options strictly satisfy Contract C specifications!")
print("\nSample option preview:")
sample = options[0].copy()
sample_leg = sample["legs"][0].copy()
sample_leg["stretches"] = sample_leg["stretches"][:2]
sample["legs"] = [sample_leg]
import pprint
pprint.pprint(sample)
