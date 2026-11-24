"""
validation/eval_holdout.py - Leave-one-week-out evaluation harness (v8).

Answers, with held-out data only:
  * Does the v8 model predict next week's per-bus tagged load better than
    simple baselines and than the v7 model?           (point accuracy, tiers)
  * Which prior strength (shape a) should be used?      (selected by deviance)
  * Are the 10-90% ranges calibrated, per sample size?  (alpha by bucket)
  * How reliable are estimates at each sample size?     (split-half, justifies trust badges)
  * Which components matter?                            (ablations)

Protocol: weekday cells; test weeks 1-4 (Mon-Sun weeks of Oct 2026; week 0 has a
single weekday and is used only for training). Every model is fit on the other
weeks (prior included), then scored against the held-out week's observed rate
C_w / T_w on cells scheduled in both. Weekend day types are reported separately
(one day per week, so folds are low-power).

Outputs (outputs/eval/):
  metrics_long.csv       model x bucket x fold metrics
  summary.json           selected hyper-parameters + headline tables (read by 04_posterior)
"""

from __future__ import annotations

import argparse
import re
import json
import pathlib
import sys
import time

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import nbinom, spearmanr, gamma as gamma_dist

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model import crowd_model as cm  # noqa: E402

A_GRID = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]
ALPHA_GRID = [0.0, 0.02, 0.05, 0.08, 0.12, 0.16, 0.2, 0.3, 0.45, 0.7, 1.0]
KAPPA_EVAL = 39.9
SEAT, MAXC = 38.0, 75.0
EVAL_BUCKETS = [("all", 0, 10**9), ("<10", 0, 10), ("10-29", 10, 30), ("30-99", 30, 100), (">=100", 100, 10**9)]


def poisson_dev(y, mu, w):
    mu = np.maximum(mu, 1e-9)
    t = np.where(y > 0, y * np.log(np.where(y > 0, y, 1) / mu), 0.0) - (y - mu)
    return float(2 * np.sum(w * t) / np.sum(w))


def macro_f1(a, b):
    f = []
    for t in (0, 1, 2):
        tp = np.sum((a == t) & (b == t)); fp = np.sum((a == t) & (b != t)); fn = np.sum((a != t) & (b == t))
        if tp + fp + fn == 0:
            continue
        f.append(2 * tp / (2 * tp + fp + fn))
    return float(np.mean(f)) if f else float("nan")


def score(name, fold, pred, y_rate, Tw, Nw, out):
    for bname, lo, hi in EVAL_BUCKETS:
        s = (Nw >= lo) & (Nw < hi)
        if s.sum() < 200:
            continue
        p, y, w = pred[s], y_rate[s], Tw[s]
        tp = cm.tier_of(KAPPA_EVAL * p, SEAT, MAXC)
        ty = cm.tier_of(KAPPA_EVAL * y, SEAT, MAXC)
        out.append(dict(model=name, fold=fold, bucket=bname, n=int(s.sum()),
                        spearman=float(spearmanr(p, y).correlation),
                        wmae=float(np.sum(w * np.abs(p - y)) / np.sum(w)),
                        deviance=poisson_dev(y, p, w),
                        tier_agree=float(np.mean(tp == ty)),
                        tier_f1=macro_f1(tp, ty)))


def coverage_nb(Cw, Tw, mean, k):
    """Share of held-out counts inside the 10-90% predictive interval of
    C_w ~ NB(n=k, mean=T_w*mean) (Poisson-Gamma)."""
    mu = np.maximum(Tw * mean, 1e-9)
    p = k / (k + mu)
    y = np.rint(Cw)
    lo_ok = nbinom.cdf(y, k, p) >= 0.10
    hi_ok = nbinom.cdf(y - 1, k, p) < 0.90
    return lo_ok & hi_ok


def coverage_v7(Cw, Tw, shape, rate, phi=1.0):
    """v7 predictive: C_new = phi * NB(n=shape/phi, p=(rate/phi)/((rate/phi)+T_new/phi))."""
    n = shape / phi
    r = rate / phi
    p = r / (r + Tw / phi)
    y = np.rint(Cw / phi)
    return (nbinom.cdf(y, n, p) >= 0.10) & (nbinom.cdf(y - 1, n, p) < 0.90)


def run(credits_path, out_dir, day_types=("weekday", "saturday", "sunday_holiday")):
    t0 = time.time()
    out_dir = pathlib.Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    rows = []
    summary = {"protocol": "leave-one-week-out, test weeks 1-4 (Mon-Sun), target = held-out tagged riders per scheduled bus",
               "kappa_for_tiers": KAPPA_EVAL, "capacity_for_tiers": {"seats": SEAT, "max_cap": MAXC}}

    # ------------------------------------------------------------------ weekday
    g = cm.load_grid(con, credits_path, "weekday")
    print(f"[eval] weekday grid: {g.C.shape[0]:,} route-segments x 48 bins ({time.time()-t0:.1f}s)")
    folds = [1, 2, 3, 4]
    cov_rows = []
    v7_cov = []
    for w in folds:
        tr = [x for x in range(cm.N_WEEKS) if x != w]
        Ctr, Ttr, Ntr = g.Cw[tr].sum(0), g.Tw[tr].sum(0), g.Nw[tr].sum(0)
        act = Ttr > 0
        Cw, Tw, Nw = g.Cw[w], g.Tw[w], g.Nw[w]
        m = act & (Tw > 0)
        y = Cw[m] / Tw[m]
        # baselines
        Ca, Ta = np.where(act, Ctr, 0), np.where(act, Ttr, 0)
        rbC = np.zeros((g.n_routes, cm.N_BINS)); rbT = np.zeros((g.n_routes, cm.N_BINS))
        np.add.at(rbC, g.route_codes, Ca); np.add.at(rbT, g.route_codes, Ta)
        b0 = np.where(rbT > 0, rbC / np.where(rbT > 0, rbT, 1), 0)[g.route_codes]
        score("B0 route x hour mean", w, b0[m], y, Tw[m], Nw[m], rows)
        b1 = np.where(Ta > 0, Ca / np.where(Ta > 0, Ta, 1), 0)
        score("B1 raw cell rate", w, b1[m], y, Tw[m], Nw[m], rows)
        # v7
        v7m, v7s, _ = cm.fit_predict_v7(Ctr, Ttr, act, g.route_codes, g.n_routes)
        score("v7 posterior (b0=20)", w, v7m[m], y, Tw[m], Nw[m], rows)
        v7_rate = (np.where(act, Ttr, 0) + 20.0)
        hit7 = coverage_v7(Cw[m], Tw[m], v7s[m], v7_rate[m], phi=1.0)
        v7_cov.append(pd.DataFrame({"bucket": cm.bucket_index(Ntr[m]), "hit": hit7}))
        # v8 over prior-shape grid
        for sm in (False, True):
            for a in A_GRID:
                mean, shape, _ = cm.fit_predict(Ctr, Ttr, act, g.route_codes, g.n_routes, "weekday", a, smooth=sm)
                score(f"v8 a={a} smooth={int(sm)}", w, mean[m], y, Tw[m], Nw[m], rows)
        print(f"[eval] fold {w} done ({time.time()-t0:.1f}s)")

    df = pd.DataFrame(rows)
    # choose a: lowest mean deviance over all weekday cells
    dev = df[(df.bucket == "all") & df.model.str.startswith("v8 a=")].groupby("model")["deviance"].mean()
    best_model = dev.idxmin()
    a_best = float(re.search(r"a=([0-9.]+)", best_model).group(1))
    smooth_best = bool(int(re.search(r"smooth=(\d)", best_model).group(1)))
    other_model = f"v8 a={a_best} smooth={int(not smooth_best)}"
    print(f"[eval] selected prior shape a = {a_best}, smoothing = {smooth_best}")

    # ablations + coverage at a_best
    for w in folds:
        tr = [x for x in range(cm.N_WEEKS) if x != w]
        Ctr, Ttr, Ntr = g.Cw[tr].sum(0), g.Tw[tr].sum(0), g.Nw[tr].sum(0)
        act = Ttr > 0
        Cw, Tw, Nw = g.Cw[w], g.Tw[w], g.Nw[w]
        m = act & (Tw > 0)
        y = Cw[m] / Tw[m]
        mean_fp, _, _ = cm.fit_predict(Ctr, Ttr, act, g.route_codes, g.n_routes, "weekday", a_best,
                                      smooth=smooth_best, prior_kw=dict(m_level=1e12, m_route_bin=1e12, m_seg=1e12))
        score("v8 - hierarchy (flat city prior)", w, mean_fp[m], y, Tw[m], Nw[m], rows)
        mean, shape, _ = cm.fit_predict(Ctr, Ttr, act, g.route_codes, g.n_routes, "weekday", a_best, smooth=smooth_best)
        bidx = cm.bucket_index(Ntr[m])
        for al in ALPHA_GRID:
            k = 1.0 / (al + 1.0 / shape[m])
            hit = coverage_nb(Cw[m], Tw[m], mean[m], k)
            cov_rows.append(pd.DataFrame({"alpha": al, "bucket": bidx, "hit": hit, "fold": w}))
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "metrics_long.csv", index=False)

    cov = pd.concat(cov_rows)
    cov_tab = cov.groupby(["alpha", "bucket"])["hit"].agg(["mean", "size"]).reset_index()
    alpha_by_bucket, cov_selected = [], {}
    for bi, (_, _, bname) in enumerate(cm.TIX_BUCKETS):
        t = cov_tab[cov_tab.bucket == bi]
        if t.empty:
            alpha_by_bucket.append(0.1); continue
        best = t.iloc[(t["mean"] - 0.80).abs().argmin()]
        alpha_by_bucket.append(float(best["alpha"]))
        cov_selected[bname] = {"alpha": float(best["alpha"]), "coverage": round(float(best["mean"]), 3),
                               "cells": int(best["size"] // len(folds))}
    v7c = pd.concat(v7_cov).groupby("bucket")["hit"].mean()
    v7_cov_named = {cm.TIX_BUCKETS[int(b)][2]: round(float(v), 3) for b, v in v7c.items()}
    v7_overall = round(float(pd.concat(v7_cov)["hit"].mean()), 3)
    # overall v8 coverage with the selected alphas
    sel = cov.merge(pd.DataFrame({"bucket": range(4), "alpha": alpha_by_bucket}), on=["bucket", "alpha"])
    v8_overall = round(float(sel["hit"].mean()), 3)
    print(f"[eval] alpha by bucket {alpha_by_bucket}; coverage {cov_selected}; v7 {v7_cov_named}")

    # ------------------------------------------------ split-half reliability
    rel = {}
    A, B = [1, 3], [2, 4]
    ests = []
    for half in (A, B):
        Ch, Th = g.Cw[half].sum(0), g.Tw[half].sum(0)
        act = Th > 0
        mean, _, _ = cm.fit_predict(Ch, Th, act, g.route_codes, g.n_routes, "weekday", a_best, smooth=smooth_best)
        ests.append((mean, act))
    both = ests[0][1] & ests[1][1]
    Ntot = g.N
    for bname, lo, hi in EVAL_BUCKETS:
        s = both & (Ntot >= lo) & (Ntot < hi)
        if s.sum() < 200:
            continue
        r = spearmanr(ests[0][0][s], ests[1][0][s]).correlation
        rel[bname] = {"cells": int(s.sum()), "split_half_spearman": round(float(r), 3),
                      "spearman_brown_full": round(float(2 * r / (1 + r)), 3)}
    print(f"[eval] split-half reliability {rel}")

    # ------------------------------------------------ weekend (low power)
    weekend = {}
    for dt in ("saturday", "sunday_holiday"):
        if dt not in day_types:
            continue
        gw = cm.load_grid(con, credits_path, dt)
        wrows = []
        for w in folds:
            tr = [x for x in range(cm.N_WEEKS) if x != w]
            Ctr, Ttr = gw.Cw[tr].sum(0), gw.Tw[tr].sum(0)
            act = Ttr > 0
            Cw, Tw, Nw = gw.Cw[w], gw.Tw[w], gw.Nw[w]
            m = act & (Tw > 0)
            if m.sum() < 1000:
                continue
            y = Cw[m] / Tw[m]
            Ca, Ta = np.where(act, Ctr, 0), np.where(act, Ttr, 0)
            b1 = np.where(Ta > 0, Ca / np.where(Ta > 0, Ta, 1), 0)
            score("B1 raw cell rate", w, b1[m], y, Tw[m], Nw[m], wrows)
            v7m, _, _ = cm.fit_predict_v7(Ctr, Ttr, act, gw.route_codes, gw.n_routes)
            score("v7 posterior (b0=20)", w, v7m[m], y, Tw[m], Nw[m], wrows)
            mean, _, _ = cm.fit_predict(Ctr, Ttr, act, gw.route_codes, gw.n_routes, dt, a_best, smooth=smooth_best)
            score("v8 selected", w, mean[m], y, Tw[m], Nw[m], wrows)
        if wrows:
            wd = pd.DataFrame(wrows)
            weekend[dt] = (wd[wd.bucket == "all"].groupby("model")[["spearman", "wmae", "deviance"]].mean()
                           .round(4).reset_index().to_dict(orient="records"))
    print(f"[eval] weekend {json.dumps(weekend)[:300]}")

    # ------------------------------------------------ headline tables
    def table(models):
        t = df[df.model.isin(models)].groupby(["model", "bucket"]).agg(
            n=("n", "mean"), spearman=("spearman", "mean"), spearman_sd=("spearman", "std"),
            wmae=("wmae", "mean"), deviance=("deviance", "mean"),
            tier_agree=("tier_agree", "mean"), tier_f1=("tier_f1", "mean")).round(4).reset_index()
        t["n"] = t["n"].round().astype(int)
        return t.to_dict(orient="records")

    main_models = ["B0 route x hour mean", "B1 raw cell rate", "v7 posterior (b0=20)", best_model]
    abl_models = [best_model, other_model, "v8 - hierarchy (flat city prior)", "B1 raw cell rate"]
    summary.update({
        "selected": {"model": best_model, "prior_shape_a": a_best, "smoothing": smooth_best, "alpha_by_bucket": alpha_by_bucket,
                     "bucket_edges": [b[:2] for b in cm.TIX_BUCKETS], "bucket_names": [b[2] for b in cm.TIX_BUCKETS]},
        "prior_shape_grid": {k: round(float(v), 5) for k, v in dev.items()},
        "comparison_weekday": table(main_models),
        "ablation_weekday": table(abl_models),
        "coverage": {"v8_selected_by_bucket": cov_selected, "v8_overall": v8_overall,
                     "v7_phi1_by_bucket": v7_cov_named, "v7_phi1_overall": v7_overall,
                     "bucketing": "distinct tickets of the cell in the training weeks",
                     "grid": cov_tab.assign(bucket=cov_tab.bucket.map(lambda b: cm.TIX_BUCKETS[int(b)][2]))
                                   .rename(columns={"mean": "coverage", "size": "cells"}).round(3).to_dict(orient="records")},
        "split_half_reliability": rel,
        "weekend": weekend,
        "runtime_s": round(time.time() - t0, 1),
    })
    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[eval] wrote {out_dir/'summary.json'} in {time.time()-t0:.1f}s")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Leave-one-week-out evaluation of the crowding model.")
    ap.add_argument("--credits-path", default=str(ROOT / "data/interim/segment_credits.parquet"))
    ap.add_argument("--out-dir", default=str(ROOT / "outputs/eval"))
    a = ap.parse_args()
    run(a.credits_path, a.out_dir)


if __name__ == "__main__":
    main()
