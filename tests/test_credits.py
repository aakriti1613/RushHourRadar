import importlib.util
import pathlib
import pytest

file_path = pathlib.Path(__file__).resolve().parent.parent / "model" / "03_credits.py"
spec = importlib.util.spec_from_file_location("credits_module", str(file_path))
credits_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credits_mod)
compute_credits_weights = credits_mod.compute_credits_weights


def test_credits_interior():
    # Normal interior trip on a line of length 10 from stop 2 to stop 6
    L = 10
    s = 2
    e = 6
    cw = compute_credits_weights(s, e, L, kernel=(0.15, 0.70, 0.15))
    
    # Expected segments: cand has 5 (0.15), 6 (0.70), 7 (0.15)
    # Expected alighting stop = 0.15*5 + 0.70*6 + 0.15*7 = 6.0
    # Expected segments traveled = 6.0 - 2 = 4.0
    total_mass = sum(w for k, w in cw)
    assert abs(total_mass - 4.0) < 1e-6
    
    # Check segment indices
    segs = [k for k, w in cw]
    assert segs == [2, 3, 4, 5, 6]
    # Segments 2, 3, 4 must have weight 1.0 because all alighting candidates (5, 6, 7) are >= k+1
    assert abs(cw[0][1] - 1.0) < 1e-6
    assert abs(cw[1][1] - 1.0) < 1e-6
    assert abs(cw[2][1] - 1.0) < 1e-6
    # Segment 5 has candidates >= 6, so weight = 0.70 + 0.15 = 0.85
    assert abs(cw[3][1] - 0.85) < 1e-6
    # Segment 6 has candidates >= 7, so weight = 0.15
    assert abs(cw[4][1] - 0.15) < 1e-6


def test_credits_adjacent_first_stop():
    # Board at 0, alight at 1 on length 5 line
    L = 5
    s = 0
    e = 1
    cw = compute_credits_weights(s, e, L, kernel=(0.15, 0.70, 0.15))
    total_mass = sum(w for k, w in cw)
    expected_alight = (1 * 0.70 + 2 * 0.15) / 0.85
    assert abs(total_mass - expected_alight) < 1e-6


def test_credits_last_stop():
    # Board at 3, alight at last stop (L-1 = 4)
    L = 5
    s = 3
    e = 4
    cw = compute_credits_weights(s, e, L, kernel=(0.15, 0.70, 0.15))
    total_mass = sum(w for k, w in cw)
    assert abs(total_mass - 1.0) < 1e-6
    assert len(cw) == 1
    assert cw[0] == (3, 1.0)


def test_credits_empty_when_invalid():
    # Boarding and alighting at same stop or backwards
    assert compute_credits_weights(3, 3, 5) == []
    assert compute_credits_weights(4, 2, 5) == []
