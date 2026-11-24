"""Tests for the v8 crowding model (model/crowd_model.py) and its published output."""
import pathlib
import sys

import duckdb
import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model import crowd_model as cm

OUT = ROOT / "data/processed/segment_crowding.parquet"


def toy(n_routes=3, segs=4, seed=0):
    rng = np.random.default_rng(seed)
    rows = n_routes * segs
    codes = np.repeat(np.arange(n_routes), segs)
    T = np.zeros((rows, 48)); T[:, 12:40] = 20.0           # service 06:00-20:00 only
    rate = rng.uniform(0.2, 2.0, size=(rows, 1)) * np.ones((1, 48))
    C = rng.poisson(T * rate).astype(float)
    return C, T, T > 0, codes, n_routes


def test_masked_smoothing_never_leaks_into_unserved_bins():
    C, T, act, codes, n = toy()
    Cs, Ts = cm.preprocess(C, T, act, "weekday", smooth=True)
    assert np.all(Cs[~act] == 0) and np.all(Ts[~act] == 0)


def test_smoothing_conserves_edge_scale():
    x = np.array([[0, 0, 4.0, 4.0, 4.0, 0, 0]]); act = x > 0
    y = cm.smooth_masked(x, act)
    assert np.allclose(y[act], 4.0)       # weights renormalised over active neighbours only


def test_posterior_between_prior_and_data_and_shrinks_less_with_evidence():
    mu0 = np.array([1.0, 1.0]); a = 2.0
    C = np.array([10.0, 1000.0]); T = np.array([2.0, 200.0])   # both observed rate 5.0
    shape, rate = cm.posterior(C, T, mu0, a)
    mean = shape / rate
    assert 1.0 < mean[0] < 5.0 and 1.0 < mean[1] < 5.0
    assert mean[1] > mean[0]                                    # more evidence -> closer to the data


def test_hierarchical_prior_pulls_sparse_route_toward_city_not_zero():
    C, T, act, codes, n = toy()
    C[0:4] = 0                                                  # route 0 has no tickets at all
    mean, shape, mu0 = cm.fit_predict(C, T, act, codes, n, "weekday", 2.0, smooth=False)
    assert np.all(mean[0:4][act[0:4]] > 0)                      # not zero: city prior fills in


def test_prediction_beats_chance_on_held_out_poisson_data():
    rng = np.random.default_rng(3)
    rows = 400
    true = rng.gamma(2.0, 0.5, size=(rows, 1)) * np.ones((1, 48))
    codes = np.arange(rows) // 4
    T = np.full((rows, 48), 15.0)
    Ctr, Cte = rng.poisson(T * true), rng.poisson(T * true)
    mean, _, _ = cm.fit_predict(Ctr.astype(float), T, T > 0, codes, 100, "weekday", 2.0, smooth=False)
    from scipy.stats import spearmanr
    assert spearmanr(mean.ravel(), (Cte / T).ravel()).correlation > 0.5


def test_pool_hourly_broadcasts_pairs():
    x = np.zeros((1, 48)); x[0, 0] = 1; x[0, 1] = 3
    y = cm.pool_hourly(x)
    assert y[0, 0] == y[0, 1] == 4


def test_week_k_widens_range_with_alpha_and_depends_on_bucket():
    shape = np.array([50.0, 50.0]); tix = np.array([5, 500])
    k = cm.week_k(shape, [0.0, 0.1, 0.1, 0.1], tix)
    assert k[0] == pytest.approx(50.0) and k[1] < 10.0


def test_route_kappa_caps_only_overloaded_routes():
    rate = np.zeros((4, 48)); rate[0] = 2.0; rate[2] = 0.2     # route 0 implies 2*39.9 = 80 riders
    tix = np.full((4, 48), 100)
    codes = np.array([0, 0, 1, 1])
    k, peak = cm.route_kappa(rate, tix, codes, 2, 39.9, np.array([75.0, 75.0]), ceiling_factor=1.0, min_tickets=30, quantile=1.0)
    assert k[0] == pytest.approx(75 / 2.0) and k[1] == pytest.approx(39.9)


@pytest.mark.skipif(not OUT.exists(), reason="pipeline output not built")
def test_published_output_has_no_unserved_cells_and_valid_ranges():
    con = duckdb.connect()
    r = con.execute(f"""SELECT count(*) n, sum((trips_sched_per_day <= 0)::int) unserved,
        sum((load_p10 > est_load * 1.0001 + 0.1)::int) p10_above, sum((load_p90 < est_load * 0.9999 - 0.1)::int) p90_below,
        sum((p_crowded < 0 OR p_crowded > 1)::int) bad_p, min(est_load) lo,
        count(DISTINCT seat_cap) seat_values FROM read_parquet('{OUT.as_posix()}')""").df().iloc[0]
    assert r.n > 4_000_000
    assert r.unserved == 0, "a cell with no scheduled bus was emitted (ghost crowding)"
    assert r.p10_above == 0 and r.bad_p == 0 and r.lo >= 0
    # mean sits inside the interval for the overwhelming majority of cells (median-vs-mean skew allowed)
    assert r.p90_below / r.n < 0.001


@pytest.mark.skipif(not OUT.exists(), reason="pipeline output not built")
def test_tier_follows_load_and_capacity():
    """est_load is stored rounded to 0.1, tiers come from the unrounded value, so cells within 0.06 of a
    boundary are excluded from the comparison (they can legitimately differ by rounding)."""
    con = duckdb.connect()
    bad = con.execute(f"""SELECT count(*) FROM read_parquet('{OUT.as_posix()}') WHERE
        abs(est_load - 0.9*seat_cap) > 0.06 AND abs(est_load - 0.8*max_cap) > 0.06 AND
        crowding_tier <> CASE WHEN est_load < 0.9*seat_cap THEN 'Seats Available'
                              WHEN est_load < 0.8*max_cap THEN 'Standing Room' ELSE 'Crowded' END""").fetchone()[0]
    assert bad == 0


@pytest.mark.skipif(not OUT.exists(), reason="pipeline output not built")
def test_night_hours_have_no_estimates_where_no_bus_runs():
    con = duckdb.connect()
    n = con.execute(f"""SELECT count(*) FROM read_parquet('{OUT.as_posix()}') WHERE day_type='weekday' AND bin30 = 5 AND trips_sched_per_day = 0""").fetchone()[0]
    assert n == 0
