import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from api.main import app
    with TestClient(app) as c:
        yield c


def test_health_and_static_page(client):
    assert client.get("/api/health").json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "rush hour radar" in r.text.lower()
    assert client.get("/assets/app.js").status_code == 200 and client.get("/assets/styles.css").status_code == 200


def test_places_prefix_first(client):
    r = client.get("/api/places", params={"q": "anand vihar"}).json()
    assert r and r[0]["name"].lower().startswith("anand vihar")
    assert client.get("/api/places", params={"q": "a"}).status_code == 422


def test_plan_returns_json_safe_options_and_timeline(client):
    r = client.get("/api/plan", params={"origin": "Anand Vihar", "dest": "Connaught Place", "time": "18:00", "day": "weekday"})
    assert r.status_code == 200
    j = r.json()
    assert j["options"] and len(j["timeline"]) == 11
    o = j["options"][0]
    assert set(["departure_time", "arrival_time", "overall_tier", "trust_level", "legs", "comfort_adjusted_minutes"]) <= set(o)
    assert not any(k.startswith("_") for k in o)


def test_plan_validation_errors(client):
    assert client.get("/api/plan", params={"origin": "a b", "dest": "c d", "day": "friday"}).status_code == 400
    assert client.get("/api/plan", params={"origin": "Anand Vihar", "dest": "Connaught Place", "time": "6pm"}).status_code == 400


def test_unknown_places_give_empty_options_not_error(client):
    j = client.get("/api/plan", params={"origin": "Zzzzqx", "dest": "Qqqqzz", "time": "09:00"}).json()
    assert j["options"] == []


def test_route_and_operator_endpoints(client):
    r = client.get("/api/route/73UP").json()
    assert r["segments"] and r["cells"] and r["max_cap"] > 0
    assert client.get("/api/route/NOPE").status_code == 404
    top = client.get("/api/operator/top", params={"limit": 5}).json()
    assert len(top) == 5 and top[0]["relief_rider_min"] >= top[-1]["relief_rider_min"]
    w = client.get("/api/operator/whatif", params={"route_key": top[0]["route_key"], "hour": top[0]["hour"], "extra_buses": 2}).json()
    assert w["peak_load_after"] < w["peak_load_now"]
    assert client.get("/api/operator/heat").json()["routes"]
    assert isinstance(client.get("/api/operator/underused").json(), list)


def test_sql_injection_in_route_key_is_inert(client):
    r = client.get("/api/route/73UP'%20OR%20'1'='1")
    assert r.status_code == 404


def test_evidence_payload(client):
    j = client.get("/api/evidence").json()
    assert j["eval"]["selected"] and j["model"]["model_version"] == "v8"
