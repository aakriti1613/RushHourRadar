"""
model/crowd_model.py - Vectorised core of the v8 crowding model.

Shared by the production pipeline (model/04_posterior.py) and the held-out
evaluation harness (validation/eval_holdout.py), so the code that is evaluated
is exactly the code that produces the published estimates.

Notation (per route-direction r, segment k, 30-min bin b, for one day type):
  C[k,b]  tagged occupancy credits (sum of pax x soft-alighting weight)
  T[k,b]  scheduled bus passes = valid days x scheduled trips
  rate    tagged riders per scheduled bus  = C / T
  load    people on board = kappa_r x rate

Model (v8):
  1. Weekend day types pool 30-min bins into 60-min bins (few days observed).
  2. Masked temporal smoothing [0.25, 0.5, 0.25] of C and T over active bins.
  3. Hierarchical prior mu0[k,b] = R[r,b] x S[r,k]
       level_r  route mean rate, shrunk to the city mean
       R[r,b]   route x bin rate, shrunk to level_r x city time profile
       S[r,k]   segment factor, shrunk to 1
  4. Gamma-Poisson posterior with a constant prior SHAPE a (chosen by held-out
     deviance):  rate ~ Gamma(C + a, T + a / mu0).
     Pseudo-trips a/mu0 fall as the prior rate rises, so busy, well-observed
     peaks are shrunk less than sparse cells (the v7 fixed b0 = 20 over-shrank
     peaks and lost to the raw rate on held-out weeks).
  5. Week-to-week variability (NB2): the "typical week" rate is
     Gamma(k, k / mean) with 1/k = alpha + 1/(C + a). alpha is fitted on
     held-out weeks per sample-size bucket, so the 10-90% range is calibrated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DAY_TYPES = ("weekday", "saturday", "sunday_holiday")
N_BINS = 48
N_WEEKS = 5
TIX_BUCKETS = ((0, 10, "<10"), (10, 30, "10-29"), (30, 100, "30-99"), (100, 10**9, ">=100"))


# --------------------------------------------------------------------------- #
# Grid loading
# --------------------------------------------------------------------------- #
class Grid:
    """Dense [rows x 48] arrays for one day type; one row per (route_key, seg_seq)."""

    def __init__(self, df: pd.DataFrame):
        keys = df[["route_key", "seg_seq"]].drop_duplicates().sort_values(["route_key", "seg_seq"])
        keys = keys.reset_index(drop=True)
        self.keys = keys
        row_of = pd.Series(np.arange(len(keys)), index=pd.MultiIndex.from_frame(keys))
        rows = row_of.reindex(pd.MultiIndex.from_frame(df[["route_key", "seg_seq"]])).to_numpy()
        bins = df["bin30"].to_numpy().astype(int)
        R = len(keys)

        def dense(col, dtype=np.float64):
            a = np.zeros((R, N_BINS), dtype=dtype)
            a[rows, bins] = df[col].to_numpy()
            return a

        self.C = dense("credits_all")
        self.T = dense("trips_T_all")
        self.N = dense("tickets_n")
        self.trips = dense("trips_scheduled_per_day")
        self.Cw = np.stack([dense(f"c_week_{w}") for w in range(N_WEEKS)])
        self.Tw = np.stack([dense(f"t_week_{w}") for w in range(N_WEEKS)])
        self.Nw = np.stack([dense(f"tickets_week_{w}") for w in range(N_WEEKS)])
        self.route_codes, self.route_names = pd.factorize(keys["route_key"], sort=True)
        self.n_routes = len(self.route_names)


def load_grid(con, credits_path: str, day_type: str) -> Grid:
    week_cols = ", ".join(
        [f"c_week_{w}" for w in range(N_WEEKS)]
        + [f"t_week_{w}" for w in range(N_WEEKS)]
        + [f"tickets_week_{w}" for w in range(N_WEEKS)]
    )
    path = str(credits_path).replace("\\", "/").replace("'", "''")
    df = con.execute(
        f"""SELECT route_key, seg_seq, bin30, credits_all, trips_T_all, tickets_n,
                   trips_scheduled_per_day, {week_cols}
            FROM read_parquet('{path}') WHERE day_type = ?""",
        [day_type],
    ).df()
    return Grid(df)


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def pool_hourly(X: np.ndarray) -> np.ndarray:
    """Sum adjacent 30-min bins into 60-min bins and broadcast back to 48 bins."""
    h = X[..., 0::2] + X[..., 1::2]
    return np.repeat(h, 2, axis=-1)


def smooth_masked(X: np.ndarray, active: np.ndarray, kernel=(0.25, 0.5, 0.25)) -> np.ndarray:
    """Masked 3-tap smoothing along the last axis; weights renormalised over active
    neighbours; inactive cells stay 0. No wrap across midnight."""
    a = active.astype(np.float64)
    Xa = np.where(active, X, 0.0)
    num = kernel[1] * Xa
    den = kernel[1] * a
    num[..., 1:] += kernel[0] * Xa[..., :-1]
    den[..., 1:] += kernel[0] * a[..., :-1]
    num[..., :-1] += kernel[2] * Xa[..., 1:]
    den[..., :-1] += kernel[2] * a[..., 1:]
    out = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
    return np.where(active, out, 0.0)


def preprocess(C, T, active, day_type, smooth=True):
    """Weekend pooling + masked smoothing. Returns (C_s, T_s)."""
    if day_type != "weekday":
        C = pool_hourly(np.where(active, C, 0.0))
        T = pool_hourly(np.where(active, T, 0.0))
    if smooth:
        C = smooth_masked(C, active)
        T = smooth_masked(T, active)
    return np.where(active, C, 0.0), np.where(active, T, 0.0)


def fit_prior(C, T, active, route_codes, n_routes, m_level=50.0, m_route_bin=20.0, m_seg=20.0,
              floor=1e-3):
    """Hierarchical separable prior mu0[row, bin] = R[route, bin] * S[row].
    All pseudo-counts are in scheduled-bus-pass units."""
    Ca = np.where(active, C, 0.0)
    Ta = np.where(active, T, 0.0)
    city_C, city_T = Ca.sum(), Ta.sum()
    city_mean = city_C / city_T if city_T > 0 else 0.05
    # city time-of-day profile (relative), lightly regularised
    bC, bT = Ca.sum(0), Ta.sum(0)
    city_prof = (bC + 10 * city_mean) / (bT + 10) / city_mean if city_mean > 0 else np.ones(N_BINS)
    # route level
    rC = np.bincount(route_codes, weights=Ca.sum(1), minlength=n_routes)
    rT = np.bincount(route_codes, weights=Ta.sum(1), minlength=n_routes)
    level = (rC + m_level * city_mean) / (rT + m_level)
    # route x bin
    rbC = np.zeros((n_routes, N_BINS))
    rbT = np.zeros((n_routes, N_BINS))
    np.add.at(rbC, route_codes, Ca)
    np.add.at(rbT, route_codes, Ta)
    R = (rbC + m_route_bin * level[:, None] * city_prof[None, :]) / (rbT + m_route_bin)
    # segment factor
    sC, sT = Ca.sum(1), Ta.sum(1)
    lv = level[route_codes]
    S = ((sC + m_seg * lv) / (sT + m_seg)) / np.maximum(lv, 1e-12)
    mu0 = R[route_codes] * S[:, None]
    return np.maximum(mu0, floor)


def posterior(C, T, mu0, a):
    """Gamma posterior (shape, rate) of the per-bus tagged rate."""
    shape = C + a
    rate = T + a / mu0
    return shape, rate


def bucket_index(n: np.ndarray) -> np.ndarray:
    idx = np.zeros(n.shape, dtype=np.int8)
    for i, (lo, hi, _) in enumerate(TIX_BUCKETS):
        idx[(n >= lo) & (n < hi)] = i
    return idx


def week_k(shape: np.ndarray, alpha_by_bucket, tickets: np.ndarray) -> np.ndarray:
    """Gamma shape of the typical-week rate: 1/k = alpha_bucket + 1/shape_post."""
    al = np.asarray(alpha_by_bucket, dtype=np.float64)[bucket_index(tickets)]
    return 1.0 / (al + 1.0 / np.maximum(shape, 1e-9))


def fit_predict(C, T, active, route_codes, n_routes, day_type, a, smooth=True, prior_kw=None):
    """Full v8 fit on training arrays -> (mean_rate, post_shape)."""
    Cs, Ts = preprocess(C, T, active, day_type, smooth=smooth)
    mu0 = fit_prior(Cs, Ts, active, route_codes, n_routes, **(prior_kw or {}))
    shape, rate = posterior(Cs, Ts, mu0, a)
    return shape / rate, shape, mu0


def fit_predict_v7(C, T, active, route_codes, n_routes, b0=20.0):
    """Reproduction of the v7 estimator (separable per-route prior clipped to
    [0.01, 5] x route mean, fixed b0 = 20 pseudo-trips, no smoothing) for comparison."""
    Ca, Ta = np.where(active, C, 0.0), np.where(active, T, 0.0)
    rC = np.bincount(route_codes, weights=Ca.sum(1), minlength=n_routes)
    rT = np.bincount(route_codes, weights=Ta.sum(1), minlength=n_routes)
    om = np.where(rT > 0, rC / np.where(rT > 0, rT, 1), 0.05)
    rbC = np.zeros((n_routes, N_BINS)); rbT = np.zeros((n_routes, N_BINS))
    np.add.at(rbC, route_codes, Ca); np.add.at(rbT, route_codes, Ta)
    R = np.where(rbT > 0, rbC / np.where(rbT > 0, rbT, 1), om[:, None])
    sC, sT = Ca.sum(1), Ta.sum(1)
    omr = om[route_codes]
    S = np.where((sT > 0) & (omr > 0), (sC / np.where(sT > 0, sT, 1)) / np.where(omr > 0, omr, 1), 1.0)
    mu0 = np.clip(R[route_codes] * S[:, None], 0.01 * omr[:, None], 5.0 * omr[:, None])
    return (Ca + b0 * mu0) / (Ta + b0), (Ca + b0 * mu0), mu0


# --------------------------------------------------------------------------- #
# Expansion (kappa) and capacity
# --------------------------------------------------------------------------- #
def route_kappa(mean_rate, tickets, route_codes, n_routes, kappa_global, max_cap_route,
                ceiling_factor=2.0, min_tickets=30, quantile=0.95):
    """Capacity-constrained expansion factor per route.

    kappa_r = min(kappa_global, ceiling_factor * max_cap_r / peak_rate_r), where
    peak_rate_r is the `quantile` of the route's posterior rate over cells with
    >= min_tickets distinct tickets. A route whose tickets would put a *typical*
    peak far beyond crush capacity must have a higher ticket capture than the
    city average, so the city-wide kappa over-states its load.
    This is a physical upper bound, not a measured capture rate: using it means
    total expanded ridership falls below the published figure (reported in
    outputs/model_report.json)."""
    peak = np.zeros(n_routes)
    m = np.where(tickets >= min_tickets, mean_rate, np.nan)
    order = np.argsort(route_codes, kind="stable")
    codes = route_codes[order]
    starts = np.searchsorted(codes, np.arange(n_routes))
    ends = np.searchsorted(codes, np.arange(n_routes), side="right")
    for r in range(n_routes):
        v = m[order[starts[r]:ends[r]]].ravel()
        v = v[~np.isnan(v)]
        if v.size:
            peak[r] = np.quantile(v, quantile)
    cap = np.where(peak > 0, ceiling_factor * max_cap_route / np.where(peak > 0, peak, 1), np.inf)
    return np.minimum(kappa_global, cap), peak


def tier_of(load, seat_cap, max_cap, seat_factor=0.9, max_factor=0.8):
    return np.where(load < seat_factor * seat_cap, 0, np.where(load < max_factor * max_cap, 1, 2)).astype(np.int8)


TIER_NAMES = np.array(["Seats Available", "Standing Room", "Crowded"])
