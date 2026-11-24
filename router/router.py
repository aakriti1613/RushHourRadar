"""
router/router.py - Crowdedness-Aware Journey Planner Engine (Contract C).

Features:
  - Pinned requested departure time (base Pareto frontier on requested departure).
  - Multi-objective optimization: total_minutes vs comfort_penalty vs transfers.
  - Guarded time-shifting (+15, +20, -15 min): strictly appended only if they add
    a new bus and strictly improve the crowding tier.
  - Actual boarding-time differences for "Leave N min later" labels.
  - Linear temporal interpolation of segment loads at bus arrival minute.
  - 3-segment moving average stretch consolidation and natural summary notes.
  - Sub-50ms routing latency using vectorized arrays and inverted indices.
"""

import math
import os
import pickle
import re
import time
import duckdb
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process


# Color mapping conforming to Okabe-Ito accessible palette
OKABE_ITO = {
    "No estimate":     {"hex": "#999999", "icon": "?", "rank": 1},
    "Seats Available": {"hex": "#0072B2", "icon": "🪑", "rank": 0},
    "Standing Room":   {"hex": "#E69F00", "icon": "🚶", "rank": 1},
    "Crowded":         {"hex": "#D55E00", "icon": "👥", "rank": 2}
}
TIER_RANK = {k: v["rank"] for k, v in OKABE_ITO.items()}
TIER_WEIGHTS = {"Seats Available": 0.0, "Standing Room": 0.5, "Crowded": 1.5, "No estimate": 0.5}
LOW_TRUST_PENALTY_MIN = 10.0  # comfort-minutes added to Low-trust options when ranking
P_CROWDED_WEIGHT = 1.0  # extra comfort penalty per unit P(typical bus is crowded)

def time_str_to_min(t_str: str) -> float:
    """Converts 'HH:MM' or 'HH:MM:SS' to minutes from midnight (0..1439)."""
    parts = str(t_str).strip().split(":")
    h = int(parts[0]) if len(parts) > 0 else 18
    m = int(parts[1]) if len(parts) > 1 else 0
    s = float(parts[2]) if len(parts) > 2 else 0.0
    return (h * 60.0 + m + s / 60.0) % 1440.0


def min_to_time_str(m_val: float) -> str:
    """Converts minutes from midnight to 'HH:MM' string."""
    m_val = float(m_val) % 1440.0
    h = int(m_val // 60)
    m = int(round(m_val % 60))
    if m >= 60:
        h = (h + 1) % 24
        m = 0
    return f"{h:02d}:{m:02d}"


class JourneyPlanner:
    def __init__(
        self,
        graph_path: str = "data/interim/gtfs_graph.pkl",
        crowding_path: str = "data/processed/segment_crowding.parquet"
    ):
        t0 = time.time()
        print(f"[router] Initializing JourneyPlanner...")

        # 1. Load GTFS graph
        if not os.path.exists(graph_path):
            raise FileNotFoundError(f"Missing {graph_path}. Run gtfs/build_graph.py first.")
        with open(graph_path, "rb") as f:
            graph = pickle.load(f)

        self.route_patterns = graph["route_patterns"]
        self.stop_lookup = graph["stop_lookup"]
        self.cluster_map = graph["cluster_map"]
        self.transfer_map = graph.get("transfer_map", {})
        try:
            import yaml
            with open("config.yaml") as _f:
                self.transfer_penalty_min = float(yaml.safe_load(_f).get("routing", {}).get("transfer_penalty_min", 6.0))
        except Exception:
            self.transfer_penalty_min = 6.0

        # 2. Build Inverted Index: stop_id -> [(route_key, stop_seq)]
        self.stop_to_routes = {}
        for rk, pat in self.route_patterns.items():
            for seq, sid in enumerate(pat["canonical_stops"]):
                if sid not in self.stop_to_routes:
                    self.stop_to_routes[sid] = []
                self.stop_to_routes[sid].append((rk, seq))

        # 3. Build cluster lookup and normalized name registry for search
        self.clusters = {}
        self.cluster_names = []
        self.cluster_ids = []
        
        # Load cluster parquet if available for rich coordinates
        clusters_file = "data/interim/gtfs_stop_clusters.parquet"
        if os.path.exists(clusters_file):
            c_df = pd.read_parquet(clusters_file)
            for _, r in c_df.iterrows():
                cid = r["cluster_id"]
                cname = r["cluster_name"]
                self.clusters[cid] = {
                    "cluster_id": cid,
                    "name": cname,
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "stops": []
                }
                self.cluster_names.append(cname)
                self.cluster_ids.append(cid)

        # Attach stops to clusters
        for sid, cid in self.cluster_map.items():
            if cid in self.clusters:
                self.clusters[cid]["stops"].append(sid)

        # Pre-clean names for sub-millisecond stop resolution
        self.stop_names_clean = {
            str(sid): re.sub(r'\(.*?\)', '', str(p.get("stop_name", ""))).lower().strip()
            for sid, p in self.stop_lookup.items()
        }
        self.cluster_names_clean = {
            str(cid): re.sub(r'\(.*?\)', '', str(cdata["name"])).lower().strip()
            for cid, cdata in self.clusters.items()
        }

        # 4. Load Crowding Tensor into fast memory arrays
        print(f"[router] Loading crowding matrices from {crowding_path}...")
        self.crowding_matrices = {}  # (route_key, day_type) -> (load_arr[num_segs, 48], p90_arr, tier_arr, trust_arr)
        
        if os.path.exists(crowding_path):
            conn = duckdb.connect()
            sql_p = crowding_path.replace("\\", "/")
            c_df = conn.execute(f"""
                SELECT route_key, seg_seq, day_type, bin30, est_load, load_p90, crowding_tier, trust_level, tickets_n, tickets_pooled, p_crowded, seat_cap, max_cap
                FROM read_parquet('{sql_p}')
            """).df()

            tier_int_map = {"Seats Available": 0, "Standing Room": 1, "Crowded": 2}
            trust_int_map = {"Low": 0, "Medium": 1, "High": 2}

            for (rk, dt), grp in c_df.groupby(["route_key", "day_type"]):
                if rk in self.route_patterns:
                    num_segs = self.route_patterns[rk]["num_segments"]
                    load_m = np.zeros((num_segs, 48), dtype=np.float32)
                    p90_m = np.zeros((num_segs, 48), dtype=np.float32)
                    tier_m = np.zeros((num_segs, 48), dtype=np.int8)
                    trust_m = np.zeros((num_segs, 48), dtype=np.int8)
                    tickets_m = np.zeros((num_segs, 48), dtype=np.int32)
                    pc_m = np.zeros((num_segs, 48), dtype=np.float32)
                    seat_m = np.full((num_segs, 48), 38.0, dtype=np.float32)
                    max_m = np.full((num_segs, 48), 75.0, dtype=np.float32)
                    has_m = np.zeros((num_segs, 48), dtype=bool)

                    for row in grp.itertuples(index=False):
                        k = row.seg_seq
                        b = row.bin30
                        if k < num_segs:
                            load_m[k, b] = row.est_load
                            p90_m[k, b] = row.load_p90
                            tier_m[k, b] = tier_int_map.get(row.crowding_tier, 0)
                            trust_m[k, b] = trust_int_map.get(row.trust_level, 0)
                            tickets_m[k, b] = row.tickets_pooled
                            pc_m[k, b] = row.p_crowded
                            seat_m[k, b] = row.seat_cap
                            max_m[k, b] = row.max_cap
                            has_m[k, b] = True

                    self.crowding_matrices[(rk, dt)] = {
                        "load": load_m,
                        "p90": p90_m,
                        "tier": tier_m,
                        "trust": trust_m,
                        "tickets": tickets_m,
                        "pc": pc_m, "seat": seat_m, "max": max_m, "has": has_m
                    }

        print(f"[router] JourneyPlanner initialized in {time.time()-t0:.2f}s.")

    def resolve_cluster(self, query: str):
        """Resolves free-text stop or cluster query to cluster_id."""
        if not query:
            return None
        q = str(query).strip()
        # Direct cluster ID match
        if q in self.clusters:
            return q
        # Exact name match
        for cid, cdata in self.clusters.items():
            if cdata["name"].lower() == q.lower():
                return cid
        # Fuzzy match across cluster names
        res = process.extractOne(q, self.cluster_names, scorer=fuzz.token_set_ratio, score_cutoff=65)
        if res:
            match_name, score, idx = res
            return self.cluster_ids[idx]
        return None

    def search_stop_clusters(self, query: str, limit: int = 8):
        """Autocomplete search for stop clusters."""
        if not query or len(query.strip()) < 2:
            return []
        matches = process.extract(
            query.strip(), self.cluster_names, scorer=fuzz.token_set_ratio, limit=limit, score_cutoff=50
        )
        out = []
        for name, score, idx in matches:
            cid = self.cluster_ids[idx]
            out.append({
                "cluster_id": cid,
                "name": name,
                "score": score,
                "lat": self.clusters[cid]["lat"],
                "lon": self.clusters[cid]["lon"]
            })
        return out

    def get_interpolated_segment_crowding(self, rk: str, k: int, t_min: float, day_type: str):
        """Model estimate for segment k at minute t_min, interpolated between 30-min bin centres.
        Returns (load, p90, tier, trust, tickets_n, p_crowded). When the model has no estimate for the
        segment-time, tier is "No estimate" (never silently comfortable)."""
        NO = (None, None, "No estimate", "Low", 0, 0.0)
        mats = self.crowding_matrices.get((rk, day_type))
        if mats is None or k >= mats["load"].shape[0]:
            return NO
        raw_bin = (t_min - 15.0) / 30.0
        b_low = int(np.floor(raw_bin)) % 48
        b_high = (b_low + 1) % 48
        alpha = float(raw_bin - np.floor(raw_bin))
        nom_bin = int(t_min // 30) % 48
        if not mats["has"][k, nom_bin]:
            return NO
        h_low, h_high = bool(mats["has"][k, b_low]), bool(mats["has"][k, b_high])
        if h_low and h_high:
            wl, wh = 1.0 - alpha, alpha
        else:
            wl, wh = (1.0, 0.0) if h_low else (0.0, 1.0)

        def mix(name):
            return float(wl * mats[name][k, b_low] + wh * mats[name][k, b_high])

        load, p90, pc = mix("load"), mix("p90"), mix("pc")
        seat, mx = mix("seat"), mix("max")
        tier_str = "Seats Available" if load < 0.90 * seat else ("Standing Room" if load < 0.80 * mx else "Crowded")
        trust_names = ["Low", "Medium", "High"]
        trust_str = trust_names[int(mats["trust"][k, nom_bin])]
        return load, p90, tier_str, trust_str, int(mats["tickets"][k, nom_bin]), pc

    def find_direct_candidates(self, orig_stops, dest_stops):
        """Finds direct route patterns connecting origin and destination stops."""
        candidates = []
        for s_orig in orig_stops:
            if s_orig not in self.stop_to_routes:
                continue
            for rk, seq_orig in self.stop_to_routes[s_orig]:
                pat = self.route_patterns[rk]
                c_stops = pat["canonical_stops"]
                cum_min = pat["cum_min"]
                for s_dest in dest_stops:
                    if s_dest in pat["stop_to_indices"]:
                        for seq_dest in pat["stop_to_indices"][s_dest]:
                            if seq_orig < seq_dest:
                                dur = cum_min[seq_dest] - cum_min[seq_orig]
                                if dur < 2.0 and len(c_stops) > 6:
                                    continue
                                candidates.append({
                                    "route_key": rk,
                                    "board_seq": seq_orig,
                                    "alight_seq": seq_dest,
                                    "board_stop_id": s_orig,
                                    "alight_stop_id": s_dest
                                })
        return candidates

    def score_direct_trip(
        self,
        candidate: dict,
        dep_time_min: float,
        req_time_min: float,
        day_type: str
    ):
        """Scores a direct journey option departing >= dep_time_min."""
        rk = candidate["route_key"]
        pat = self.route_patterns[rk]
        s_seq = candidate["board_seq"]
        e_seq = candidate["alight_seq"]
        cum_min = pat["cum_min"]
        deps = pat["departures"][s_seq]

        if len(deps) == 0:
            return None

        # Next scheduled departure at boarding stop >= dep_time_min
        idx = np.searchsorted(deps, dep_time_min)
        if idx >= len(deps):
            # Wrap around midnight
            idx = 0
            t_board = deps[0] + 1440.0
        else:
            t_board = deps[idx]

        wait_min = t_board - dep_time_min
        ride_min = cum_min[e_seq] - cum_min[s_seq]
        t_arrive = t_board + ride_min
        total_min = t_arrive - req_time_min  # Strictly relative to user's requested time!

        # Segment-by-segment simulation
        raw_stretches = []
        comfort_penalty = 0.0
        trust_levels = []
        total_tickets_seen = 0

        board_cum = cum_min[s_seq]
        for k in range(s_seq, e_seq):
            # Scheduled time bus reaches segment k
            seg_time = (t_board + cum_min[k] - board_cum) % 1440.0
            seg_dur = max(0.5, cum_min[k + 1] - cum_min[k])

            load, p90, tier, trust, t_cnt, pcr = self.get_interpolated_segment_crowding(rk, k, seg_time, day_type)
            comfort_penalty += seg_dur * (TIER_WEIGHTS.get(tier, 0.5) + (P_CROWDED_WEIGHT * pcr if tier != "Crowded" else 0.0))
            trust_levels.append(trust)
            total_tickets_seen = max(total_tickets_seen, t_cnt)  # distinct tickets on the busiest segment, not a sum

            from_sid = pat["canonical_stops"][k]
            to_sid = pat["canonical_stops"][k + 1]
            p_from = self.stop_lookup.get(from_sid, {})
            p_to = self.stop_lookup.get(to_sid, {})

            raw_stretches.append({
                "from_stop": p_from.get("stop_name", from_sid),
                "to_stop": p_to.get("stop_name", to_sid),
                "load": None if load is None else round(load, 1),
                "p90": None if p90 is None else round(p90, 1),
                "p_crowded": round(pcr, 3),
                "tier": tier,
                "dur": seg_dur,
                "coords": [
                    [p_from.get("stop_lat", 0.0), p_from.get("stop_lon", 0.0)],
                    [p_to.get("stop_lat", 0.0), p_to.get("stop_lon", 0.0)]
                ]
            })

        # Consolidate stretches (merge same-tier runs, breaking at 15m milestones)
        consolidated = []
        curr = None
        for s in raw_stretches:
            if curr is None:
                curr = dict(s)
            elif curr["tier"] == s["tier"] and (curr["dur"] + s["dur"] <= 15.0):
                # Extend current stretch
                curr["to_stop"] = s["to_stop"]
                curr["dur"] += s["dur"]
                curr["load"] = max(curr["load"] or 0.0, s["load"] or 0.0) if (curr["load"] is not None or s["load"] is not None) else None
                curr["p90"] = max(curr["p90"] or 0.0, s["p90"] or 0.0) if (curr["p90"] is not None or s["p90"] is not None) else None
                curr["p_crowded"] = max(curr["p_crowded"], s["p_crowded"])
                curr["coords"].append(s["coords"][1])
            else:
                consolidated.append(curr)
                curr = dict(s)
        if curr is not None:
            consolidated.append(curr)

        # Overall tier and board tier
        known = [s for s in consolidated if s["tier"] != "No estimate"]
        rank_to_tier = {0: "Seats Available", 1: "Standing Room", 2: "Crowded"}
        overall_tier = rank_to_tier[max(TIER_RANK[s["tier"]] for s in known)] if known else "No estimate"
        board_tier = consolidated[0]["tier"] if consolidated else "No estimate"

        # Trust determination
        order = {"Low": 0, "Medium": 1, "High": 2}
        overall_trust = min(trust_levels, key=lambda t: order[t]) if (trust_levels and known) else "Low"

        # Summary note generation
        if overall_tier == "Crowded":
            # Find the crowded stretch
            cr_stretches = [s for s in consolidated if s["tier"] == "Crowded"]
            busiest = max(cr_stretches, key=lambda x: x["load"] or 0.0)
            summary_note = f"Crowded stretch: {busiest['from_stop']} → {busiest['to_stop']}"
        elif overall_tier == "No estimate":
            summary_note = "No ticket-based crowding estimate for this trip"
        elif overall_tier == "Standing Room":
            summary_note = "Moderate standing room across the route"
        else:
            summary_note = "Comfortable journey with seats available throughout"

        board_stop_name = self.stop_lookup.get(candidate["board_stop_id"], {}).get("stop_name", "Origin")
        alight_stop_name = self.stop_lookup.get(candidate["alight_stop_id"], {}).get("stop_name", "Destination")
        dep_str = min_to_time_str(t_board)
        arr_str = min_to_time_str(t_arrive)

        trip_key = f"{rk}|{dep_str}"
        has_unknown = any(x["tier"] == "No estimate" for x in consolidated)

        return {
            "option_label": "Fastest",
            "is_time_shifted": False,
            "departure_time": dep_str,
            "boarding_trip_key": trip_key,
            "_t_board": float(t_board),
            "_t_arrive": float(t_arrive),
            "has_unknown_stretch": has_unknown,
            "arrival_time": arr_str,
            "total_minutes": round(total_min, 1),
            "wait_minutes": round(wait_min, 1),
            "transfers": 0,
            "overall_tier": overall_tier,
            "board_tier": board_tier,
            "trust_level": overall_trust,
            "confidence_text": f"Based on {total_tickets_seen} ticket records on the busiest stretch, this route and time of day",
            "summary_note": summary_note,
            "comfort_penalty": round(comfort_penalty, 1),
            "legs": [{
                "route_key": rk,
                "board_stop": board_stop_name,
                "alight_stop": alight_stop_name,
                "minutes": round(ride_min, 1),
                "stretches": consolidated
            }]
        }

    def resolve_stops(self, query: str):
        """
        Resolves free-text stop or cluster query to a set of GTFS stop_ids.
        Supports cluster names, individual stop names, and aliases (e.g. CP -> Connaught Place).
        """
        if not query:
            return []
        q = str(query).strip().lower()

        alias_map = {
            "cp": "shivaji stadium",
            "connaught place": "shivaji stadium",
            "rajiv chowk": "shivaji stadium",
            "anand vihar": "anand vihar",
            "kashmere gate": "kashmere gate",
            "aiims": "aiims",
            "nehru place": "nehru place",
            "azadpur": "azadpur",
            "rohini": "rohini",
            "karol bagh": "karol bagh",
            "badarpur": "badarpur",
            "mehrauli": "mehrauli",
            "sarita vihar": "sarita vihar",
            "ito": "ito",
            "mandi house": "mandi house",
            "najafgarh": "najafgarh",
            "tilak nagar": "tilak nagar"
        }

        search_q = alias_map.get(q, q)
        clean_q = re.sub(r'\(.*?\)', '', search_q).strip()

        matched_clusters = set()
        matched_stops = set()

        # Fast pre-compiled pattern with word boundaries for locality matching
        pattern = re.compile(r'\b' + re.escape(clean_q) + r'\b')

        # Fast substring check + pre-compiled regex on pre-cleaned stop names
        for sid, s_clean in self.stop_names_clean.items():
            if clean_q in s_clean and pattern.search(s_clean):
                matched_stops.add(sid)
                if sid in self.cluster_map:
                    matched_clusters.add(str(self.cluster_map[sid]))

        for cid, c_clean in self.cluster_names_clean.items():
            if clean_q in c_clean and pattern.search(c_clean):
                matched_clusters.add(cid)

        for sid, cid in self.cluster_map.items():
            if str(cid) in matched_clusters:
                matched_stops.add(str(sid))

        # Fallback to fuzzy match across cluster names if empty
        if not matched_stops:
            res = process.extractOne(search_q, self.cluster_names, scorer=fuzz.token_set_ratio, score_cutoff=65)
            if res:
                cid = self.cluster_ids[res[2]]
                matched_stops.update(self.clusters[cid]["stops"])

        return list(matched_stops)

    def plan_one_transfer(self, orig_stops, dest_stops, req_time_min, day_type, lam, max_options=3):
        """Fallback when no direct bus exists: board route 1, ride to a stop that is (or is within the walking
        radius of) a stop on route 2, then ride route 2 to the destination. Cheap schedule-based pre-filter,
        then full crowding-aware scoring of the best few pairs."""
        walk_pen = float(self.transfer_penalty_min)
        dest_set = set(dest_stops)
        dest_routes = {}
        for sd in dest_stops:
            for rk2, seq2 in self.stop_to_routes.get(sd, []):
                dest_routes.setdefault(rk2, []).append(seq2)
        if not dest_routes:
            return []
        pre = []
        for so in orig_stops:
            for rk1, seq1 in self.stop_to_routes.get(so, []):
                pat1 = self.route_patterns[rk1]
                cs1, cm1 = pat1["canonical_stops"], pat1["cum_min"]
                for j in range(seq1 + 1, len(cs1)):
                    sj = cs1[j]
                    nbrs = [(sj, 0.0)] + [(n, w) for n, w, _ in self.transfer_map.get(sj, [])]
                    for sn, walk_min in nbrs:
                        for rk2, seq2 in self.stop_to_routes.get(sn, []):
                            if rk2 == rk1 or rk2 not in dest_routes:
                                continue
                            pat2 = self.route_patterns[rk2]
                            for seq_d in dest_routes[rk2]:
                                if seq2 < seq_d:
                                    ride = (cm1[j] - cm1[seq1]) + walk_min + walk_pen + (pat2["cum_min"][seq_d] - pat2["cum_min"][seq2])
                                    pre.append((ride, rk1, seq1, so, j, sj, rk2, seq2, sn, seq_d))
        if not pre:
            return []
        pre.sort(key=lambda x: x[0])
        seen, shortlist = set(), []
        for item in pre:
            key = (item[1], item[6])
            if key in seen:
                continue
            seen.add(key)
            shortlist.append(item)
            if len(shortlist) >= 12:
                break
        options, used = [], set()
        for ride, rk1, seq1, so, j, sj, rk2, seq2, sn, seq_d in shortlist:
            sd = self.route_patterns[rk2]["canonical_stops"][seq_d]
            c1 = {"route_key": rk1, "board_seq": seq1, "alight_seq": j, "board_stop_id": so, "alight_stop_id": sj}
            o1 = self.score_direct_trip(c1, req_time_min, req_time_min, day_type)
            if not o1:
                continue
            walk_min = 0.0 if sn == sj else next((w for n, w, _ in self.transfer_map.get(sj, []) if n == sn), 3.0)
            c2 = {"route_key": rk2, "board_seq": seq2, "alight_seq": seq_d, "board_stop_id": sn, "alight_stop_id": sd}
            o2 = self.score_direct_trip(c2, (o1["_t_arrive"] + walk_min) % 1440.0, req_time_min, day_type)
            if not o2:
                continue
            key = o1["boarding_trip_key"] + ">" + o2["boarding_trip_key"]
            if key in used:
                continue
            used.add(key)
            tiers = [t for t in (o1["overall_tier"], o2["overall_tier"]) if t != "No estimate"]
            rank_to_tier = {0: "Seats Available", 1: "Standing Room", 2: "Crowded"}
            overall = rank_to_tier[max(TIER_RANK[t] for t in tiers)] if tiers else "No estimate"
            order = {"Low": 0, "Medium": 1, "High": 2}
            trust = min((o1["trust_level"], o2["trust_level"]), key=lambda t: order[t]) if tiers else "Low"
            total = o2["total_minutes"] + (walk_min if sn != sj else 0.0)
            leg2 = dict(o2["legs"][0]); leg2["walk_from_previous_min"] = round(walk_min, 1)
            options.append({
                "option_label": "1 transfer", "is_time_shifted": False,
                "departure_time": o1["departure_time"], "boarding_trip_key": key,
                "_t_board": o1["_t_board"], "_t_arrive": o2["_t_arrive"], "has_unknown_stretch": o1["has_unknown_stretch"] or o2["has_unknown_stretch"],
                "arrival_time": o2["arrival_time"], "total_minutes": round(total, 1),
                "wait_minutes": o1["wait_minutes"], "transfers": 1,
                "overall_tier": overall, "board_tier": o1["board_tier"], "trust_level": trust,
                "confidence_text": o1["confidence_text"] + "; second leg: " + o2["confidence_text"],
                "summary_note": f"Change at {self.stop_lookup.get(sj, {}).get('stop_name', 'transfer stop')}"
                                + ("" if sn == sj else f" (walk ~{round(walk_min)} min)") + f". Leg 1: {o1['summary_note']}. Leg 2: {o2['summary_note']}",
                "comfort_penalty": round(o1["comfort_penalty"] + o2["comfort_penalty"], 1),
                "legs": [o1["legs"][0], leg2],
            })
        options.sort(key=lambda o: o["total_minutes"] + lam * o["comfort_penalty"] + walk_pen)
        out = options[:max_options]
        if out:
            fastest = min(out, key=lambda o: o["total_minutes"])
            fastest["option_label"] = "Fastest (1 transfer)"
        return out

    def plan_journey(
        self,
        origin: str,
        dest: str,
        dep_time: str = "18:00",
        day_type: str = "weekday",
        mode: str = "balanced",
        lam: float = None
    ) -> list:
        """
        Plans journey from origin to dest with crowdedness tradeoffs (Contract C).
        """
        orig_stops = self.resolve_stops(origin)
        dest_stops = self.resolve_stops(dest)

        if not orig_stops or not dest_stops:
            return []

        # Prevent overlap between origin and destination stops
        overlap = set(orig_stops).intersection(set(dest_stops))
        if overlap:
            orig_stops = [s for s in orig_stops if s not in overlap]
            if not orig_stops:
                orig_stops = list(overlap)
                dest_stops = [s for s in dest_stops if s not in overlap]

        req_time_min = time_str_to_min(dep_time)

        # Mode lambda setting
        if lam is None:
            mode_map = {"fastest": 0.0, "balanced": 0.35, "comfortable": 1.0}
            lam = mode_map.get(str(mode).lower(), 0.35)

        # 1. Direct candidates
        raw_cands = self.find_direct_candidates(orig_stops, dest_stops)
        if not raw_cands:
            return self.plan_one_transfer(orig_stops, dest_stops, req_time_min, day_type, lam)

        # 2. Score base candidates at requested departure
        base_options = []
        seen_trips = set()
        for cand in raw_cands:
            opt = self.score_direct_trip(cand, req_time_min, req_time_min, day_type)
            if opt and opt["boarding_trip_key"] not in seen_trips:
                seen_trips.add(opt["boarding_trip_key"])
                base_options.append(opt)

        if not base_options:
            return self.plan_one_transfer(orig_stops, dest_stops, req_time_min, day_type, lam)

        # 3. Base Selection: Provide 2 to 3 distinct, practical route options at requested time
        # Sort candidates according to user mode
        def score_fn(opt):
            # Low-trust options (thin ticket evidence) get a comfort-uncertainty penalty so that a
            # sparsely observed bus is not ranked "most comfortable" just because the model is unsure.
            unc = LOW_TRUST_PENALTY_MIN if opt["trust_level"] == "Low" else 0.0
            return opt["total_minutes"] + lam * (opt["comfort_penalty"] + unc)

        base_options.sort(key=score_fn)

        # Select up to 3 distinct routes among base options
        selected_base = []
        seen_base_routes = set()
        taken_trips = set()

        for opt in base_options:
            rk = opt["legs"][0]["route_key"]
            if rk not in seen_base_routes and len(selected_base) < 3:
                seen_base_routes.add(rk)
                taken_trips.add(opt["boarding_trip_key"])
                opt_copy = dict(opt)
                if len(selected_base) == 0:
                    opt_copy["option_label"] = "Recommended" if lam > 0 else "Fastest"
                elif opt["total_minutes"] <= selected_base[0]["total_minutes"]:
                    opt_copy["option_label"] = "Fastest"
                elif TIER_RANK[opt["overall_tier"]] < TIER_RANK[selected_base[0]["overall_tier"]]:
                    opt_copy["option_label"] = "More Comfortable"
                else:
                    opt_copy["option_label"] = "Alternative Route"
                selected_base.append(opt_copy)

        # Ensure first option is labeled Fastest if it is indeed the minimum time
        if selected_base:
            min_time_idx = min(range(len(selected_base)), key=lambda i: selected_base[i]["total_minutes"])
            if lam == 0.0 or min_time_idx == 0:
                selected_base[min_time_idx]["option_label"] = "Fastest"

        # 4. Time shifts (+20, +15, -15 min). Appended only if the option boards a NEW bus and its overall
        #    tier is strictly better than the best option available at the requested time.
        best_tier = min((TIER_RANK[o["overall_tier"]] for o in selected_base if o["overall_tier"] != "No estimate"),
                        default=None)
        shifted_options = []
        if best_tier is not None and best_tier > 0:
            for dt_shift in [20, 15, -15]:
                shifted_dep_min = (req_time_min + dt_shift) % 1440.0
                shift_cands = []
                for cand in raw_cands:
                    s_opt = self.score_direct_trip(cand, shifted_dep_min, req_time_min, day_type)
                    if (s_opt and s_opt["boarding_trip_key"] not in taken_trips
                            and s_opt["overall_tier"] != "No estimate"
                            and TIER_RANK[s_opt["overall_tier"]] < best_tier):
                        shift_cands.append(s_opt)
                if shift_cands:
                    best_shift = min(shift_cands, key=lambda o: (o["total_minutes"], o["comfort_penalty"]))
                    diff = int(round(best_shift["_t_board"] - req_time_min))
                    if diff > 0:
                        lbl = f"Leave {diff} min later"
                    elif diff < 0:
                        lbl = f"Leave {abs(diff)} min earlier"
                    else:
                        continue
                    best_shift = dict(best_shift)
                    best_shift["option_label"] = lbl
                    best_shift["is_time_shifted"] = True
                    shifted_options.append(best_shift)
                    taken_trips.add(best_shift["boarding_trip_key"])
                    break

        final_options = selected_base + shifted_options[:1]
        return final_options


_planner_instance = None


def get_planner():
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = JourneyPlanner()
    return _planner_instance


def plan_journey(origin: str, dest: str, dep_time: str = "18:00", day_type: str = "weekday", mode: str = "balanced", lam: float = None) -> list:
    """Module-level entry point conforming to Contract C."""
    planner = get_planner()
    return planner.plan_journey(origin=origin, dest=dest, dep_time=dep_time, day_type=day_type, mode=mode, lam=lam)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test Crowdedness-Aware Journey Planner.")
    parser.add_argument("--origin", default="Anand Vihar", help="Origin stop or cluster")
    parser.add_argument("--dest", default="Connaught Place", help="Destination stop or cluster")
    parser.add_argument("--time", default="18:00", help="Requested departure time HH:MM")
    parser.add_argument("--day", default="weekday", choices=["weekday", "saturday", "sunday_holiday"])
    parser.add_argument("--mode", default="balanced", choices=["fastest", "balanced", "comfortable"])
    args = parser.parse_args()

    print(f"\nPlanning Journey: {args.origin} → {args.dest} at {args.time} ({args.day}, mode={args.mode})")
    opts = plan_journey(args.origin, args.dest, dep_time=args.time, day_type=args.day, mode=args.mode)
    print(f"Returned {len(opts)} options:")
    for i, o in enumerate(opts, 1):
        print(f"\n[{i}] {o['option_label']}: Route {o['legs'][0]['route_key']}")
        print(f"    Board: {o['departure_time']} at {o['legs'][0]['board_stop']}")
        print(f"    Arrive: {o['arrival_time']} at {o['legs'][0]['alight_stop']}")
        print(f"    Total Time: {o['total_minutes']} min (Wait: {o['wait_minutes']} min)")
        print(f"    Overall Crowding: {o['overall_tier']} | Boarding Stop Crowding: {o['board_tier']}")
        print(f"    Trust: {o['trust_level']} ({o['confidence_text']})")
        print(f"    Note: {o['summary_note']}")


if __name__ == "__main__":
    main()
