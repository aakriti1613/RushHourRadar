"""
api/main.py - FastAPI backend for the Delhi crowding planner.

Wraps (does not re-implement) the existing modules:
  router.router.JourneyPlanner   journey options (Contract C)
  data/processed/*.parquet       crowding estimates (Contracts A, B)
  outputs/**                     evaluation / validation evidence

Run:  uvicorn api.main:app --port 8000      (from the repository root)
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import sys
import time
from contextlib import asynccontextmanager

import duckdb
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.chdir(ROOT)  # the pipeline modules use repo-relative paths

from router.router import JourneyPlanner  # noqa: E402

CROWDING = ROOT / "data/processed/segment_crowding.parquet"
OPERATOR = ROOT / "data/processed/operator_overload.parquet"
OUT = ROOT / "outputs"
WEB = ROOT / "web"
DAY_TYPES = ("weekday", "saturday", "sunday_holiday")

state: dict = {}


def clean(o):
    """Recursively convert numpy / NaN values into JSON-safe Python values."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def read_json(rel: str):
    p = OUT / rel
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.time()
    state["planner"] = JourneyPlanner()
    state["con"] = duckdb.connect()
    op = pd.read_parquet(OPERATOR)
    state["op"] = op
    meta = state["con"].execute(
        f"SELECT route_key, any_value(route_base) b, any_value(direction) d FROM read_parquet('{CROWDING.as_posix()}') GROUP BY 1"
    ).df()
    state["routes"] = meta.sort_values("route_key").reset_index(drop=True)
    state["boot_s"] = round(time.time() - t0, 1)
    print(f"[api] ready in {state['boot_s']}s")
    yield


app = FastAPI(title="Delhi Crowding Planner API", version="8.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"status": "ok", "boot_s": state.get("boot_s"), "model": read_json("model_report.json") and "v8"}


# ----------------------------------------------------------------------------- planner
@app.get("/api/places")
def places(q: str = Query(..., min_length=2), limit: int = 8):
    pl: JourneyPlanner = state["planner"]
    ql = re.sub(r"\s+", " ", q.strip().lower())
    scored = []
    for cid, c in pl.clusters.items():
        n = c["name"].lower()
        if n.startswith(ql):
            sc = 0
        elif re.search(r"\b" + re.escape(ql), n):
            sc = 1
        elif ql in n:
            sc = 2
        else:
            continue
        scored.append((sc, len(n), c))
    scored.sort(key=lambda x: (x[0], x[1]))
    out = [{"cluster_id": c["cluster_id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"]} for _, _, c in scored[:limit]]
    if len(out) < limit:  # fuzzy fallback for typos
        seen = {o["cluster_id"] for o in out}
        for r in pl.search_stop_clusters(q, limit=limit):
            if r["cluster_id"] not in seen and r["score"] >= 70 and len(out) < limit:
                out.append({k: r[k] for k in ("cluster_id", "name", "lat", "lon")})
    return clean(out)


def hhmm_add(t: str, delta_min: int) -> str:
    h, m = [int(x) for x in t.split(":")[:2]]
    v = (h * 60 + m + delta_min) % 1440
    return f"{v // 60:02d}:{v % 60:02d}"


def decorate(opt: dict) -> dict:
    opt = dict(opt)
    opt["comfort_adjusted_minutes"] = round(opt["total_minutes"] + opt["comfort_penalty"], 1)
    return opt


@app.get("/api/plan")
def plan(origin: str, dest: str, time_: str = Query("18:00", alias="time"), day: str = "weekday",
         mode: str = "balanced", lam: float | None = None, timeline: bool = True):
    if day not in DAY_TYPES:
        raise HTTPException(400, "day must be one of " + ", ".join(DAY_TYPES))
    if not re.match(r"^\d{1,2}:\d{2}$", time_):
        raise HTTPException(400, "time must be HH:MM")
    pl: JourneyPlanner = state["planner"]
    t0 = time.time()
    options = [decorate(o) for o in pl.plan_journey(origin, dest, dep_time=time_, day_type=day, mode=mode, lam=lam)]
    resp = {"origin": origin, "dest": dest, "time": time_, "day": day, "mode": mode, "options": options}
    if timeline and options:
        tl = []
        for off in range(-60, 91, 15):
            t = hhmm_add(time_, off)
            r = pl.plan_journey(origin, dest, dep_time=t, day_type=day, mode="balanced")
            base = [o for o in r if not o["is_time_shifted"]]
            if not base:
                tl.append({"offset": off, "time": t, "tier": None})
                continue
            b = base[0]
            tl.append({"offset": off, "time": t, "tier": b["overall_tier"], "route": b["legs"][0]["route_key"],
                       "total_minutes": b["total_minutes"], "departure_time": b["departure_time"],
                       "trust": b["trust_level"], "transfers": b["transfers"]})
        resp["timeline"] = tl
    resp["latency_ms"] = round((time.time() - t0) * 1000)
    return clean(resp)


# ----------------------------------------------------------------------------- routes
@app.get("/api/routes")
def routes(q: str = "", limit: int = 30):
    r = state["routes"]
    if q:
        r = r[r.route_key.str.contains(re.escape(q), case=False, regex=True)]
    return clean(r.head(limit).to_dict(orient="records"))


@app.get("/api/route/{route_key}")
def route_detail(route_key: str, day: str = "weekday"):
    if day not in DAY_TYPES:
        raise HTTPException(400, "bad day")
    df = state["con"].execute(
        f"""SELECT seg_seq, bin30, from_name, to_name, from_lat, from_lon, to_lat, to_lon, est_load, load_p10, load_p90,
                   seat_cap, max_cap, crowding_tier, p_crowded, tickets_n, tickets_pooled, trust_level, kappa_r, kappa_capped
            FROM read_parquet('{CROWDING.as_posix()}') WHERE route_key = ? AND day_type = ?
            ORDER BY seg_seq, bin30""", [route_key, day]).df()
    if df.empty:
        raise HTTPException(404, "no estimates for this route/day type")
    segs = (df.groupby("seg_seq").agg(from_name=("from_name", "first"), to_name=("to_name", "first"),
                                      from_lat=("from_lat", "first"), from_lon=("from_lon", "first"),
                                      to_lat=("to_lat", "first"), to_lon=("to_lon", "first")).reset_index())
    tier_i = {"Seats Available": 0, "Standing Room": 1, "Crowded": 2}
    cells = {}
    for r in df.itertuples(index=False):
        cells[f"{r.seg_seq}:{r.bin30}"] = [round(r.est_load, 1), round(r.load_p10, 1), round(r.load_p90, 1),
                                           tier_i[r.crowding_tier], round(r.p_crowded, 2), int(r.tickets_pooled),
                                           {"High": 2, "Medium": 1, "Low": 0}[r.trust_level]]
    first = df.iloc[0]
    return clean({"route_key": route_key, "day": day, "segments": segs.to_dict(orient="records"), "cells": cells,
                  "seat_cap": first.seat_cap, "max_cap": first.max_cap, "kappa_r": first.kappa_r,
                  "kappa_capped": bool(first.kappa_capped),
                  "cols": ["load", "p10", "p90", "tier", "p_crowded", "tickets", "trust"]})


# ----------------------------------------------------------------------------- operator
@app.get("/api/operator/top")
def operator_top(day: str = "weekday", hour: int | None = None, metric: str = "relief_rider_min", limit: int = 20,
                 min_trust: str = "Low"):
    op: pd.DataFrame = state["op"]
    d = op[(op.day_type == day) & (op.overload_rider_min > 0)]
    if hour is not None:
        d = d[d.hour == hour]
    order = {"Low": 0, "Medium": 1, "High": 2}
    d = d[d.trust_level.map(order) >= order.get(min_trust, 0)]
    if metric not in ("relief_rider_min", "overload_rider_min", "peak_load"):
        raise HTTPException(400, "bad metric")
    d = d.sort_values(metric, ascending=False).head(limit)
    return clean(d.to_dict(orient="records"))


@app.get("/api/operator/heat")
def operator_heat(day: str = "weekday", n_routes: int = 25):
    op: pd.DataFrame = state["op"]
    d = op[op.day_type == day]
    top = d.groupby("route_key")["overload_rider_min"].sum().sort_values(ascending=False).head(n_routes).index.tolist()
    h = d[d.route_key.isin(top)]
    cells = [[r.route_key, int(r.hour), round(float(r.overload_rider_min), 1), round(float(r.peak_load), 1), r.tier_now]
             for r in h.itertuples(index=False)]
    return clean({"routes": top, "cells": cells})


@app.get("/api/operator/underused")
def operator_underused(day: str = "weekday", limit: int = 20):
    op: pd.DataFrame = state["op"]
    d = op[(op.day_type == day) & op.underused & (op.hour >= 6) & (op.hour <= 22) & (op.trips_hr >= 2)]
    d = d[d.trust_level != "Low"]
    d = d.sort_values(["trips_hr"], ascending=False).head(limit)
    return clean(d[["route_key", "hour", "trips_hr", "peak_load", "seat_cap", "trust_level", "tickets_n"]].to_dict(orient="records"))


@app.get("/api/operator/whatif")
def operator_whatif(route_key: str, day: str = "weekday", hour: int = 9, extra_buses: int = 1):
    op: pd.DataFrame = state["op"]
    r = op[(op.route_key == route_key) & (op.day_type == day) & (op.hour == hour)]
    if r.empty:
        raise HTTPException(404, "no data for that route-hour")
    r = r.iloc[0]
    n = max(1.0, float(r.trips_hr))
    load1 = float(r.peak_load) * n / (n + extra_buses)
    seat, mx = float(r.seat_cap), float(r.max_cap)
    tier = lambda l: "Seats Available" if l < 0.9 * seat else ("Standing Room" if l < 0.8 * mx else "Crowded")
    return clean({"route_key": route_key, "hour": hour, "trips_hr": n, "extra_buses": extra_buses,
                  "peak_load_now": r.peak_load, "peak_load_after": round(load1, 1), "tier_now": tier(float(r.peak_load)),
                  "tier_after": tier(load1), "peak_from": r.peak_from_name, "peak_to": r.peak_to_name,
                  "assumption": "Demand fixed; load scales with 1/(buses per hour). No induced demand, no bunching."})


# ----------------------------------------------------------------------------- evidence
@app.get("/api/evidence")
def evidence():
    return clean({
        "eval": read_json("eval/summary.json"),
        "model": read_json("model_report.json"),
        "validation": read_json("validation_v8.json"),
        "match": read_json("match_report.json"),
        "eda": read_json("eda_report.json"),
    })


@app.get("/api/meta")
def meta():
    m = read_json("model_report.json") or {}
    return clean({"model_version": m.get("model_version"), "tickets": 2559899, "period": "1-31 Oct 2026",
                  "routes": m.get("routes_total"), "kappa": m.get("kappa_global")})


# ----------------------------------------------------------------------------- static web UI
if WEB.exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")
