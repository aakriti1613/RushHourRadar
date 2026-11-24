import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from router.router import plan_journey

corridors = [
    ("Anand Vihar", "Connaught Place", "18:00", "weekday"),
    ("Kashmere Gate", "Nehru Place", "09:00", "weekday"),
    ("Azadpur", "AIIMS", "08:30", "weekday"),
    ("Rohini", "Karol Bagh", "18:30", "weekday"),
    ("Badarpur", "Mehrauli", "17:30", "weekday"),
    ("Shivaji Stadium Terminal", "Uttam Nagar", "18:00", "weekday"),
    ("Jahangirpuri", "Old Delhi Railway Station", "08:00", "weekday"),
    ("Sarita Vihar", "Mandi House", "09:00", "weekday"),
    ("Punjabi Bagh", "Azadpur", "10:00", "weekday"),
    ("Najafgarh", "Tilak Nagar", "08:30", "weekday")
]

for orig, dest, dtime, dtype in corridors:
    print("=" * 75)
    print(f"OD: {orig} -> {dest} at {dtime} ({dtype})")
    opts = plan_journey(orig, dest, dep_time=dtime, day_type=dtype)
    print(f"Found {len(opts)} options:")
    for i, o in enumerate(opts):
        r_key = o["legs"][0]["route_key"] if o["legs"] else "N/A"
        label = o.get("option_label", "")
        dep = o.get("departure_time", "")
        arr = o.get("arrival_time", "")
        dur = o.get("total_minutes", 0)
        wait = o.get("wait_minutes", 0)
        tier = o.get("overall_tier", "")
        board_tier = o.get("board_tier", "")
        note = o.get("summary_note", "")
        print(f"  Opt {i+1} [{label}]: Route {r_key} | Dep: {dep} | Arr: {arr} | {dur:.1f}m (wait: {wait:.1f}m) | Tier: {tier} | Board: {board_tier}")
        print(f"         Note: {note}")
        if o["legs"] and o["legs"][0]["stretches"]:
            stretches = o["legs"][0]["stretches"]
            loads = [s.get("load", 0) for s in stretches]
            p90s = [s.get("p90", 0) for s in stretches]
            tiers = [s.get("tier", "") for s in stretches]
            print(f"         Stretches count: {len(stretches)} | Load range: [{min(loads):.1f} - {max(loads):.1f}] | P90 range: [{min(p90s):.1f} - {max(p90s):.1f}] | Tiers: {set(tiers)}")
            for s_idx, s in enumerate(stretches[:3]):
                print(f"           - Stretch {s_idx+1}: {s['from_stop']} -> {s['to_stop']} ({s['tier']}, load={s['load']:.1f}, dur={s['dur']:.1f}m)")
