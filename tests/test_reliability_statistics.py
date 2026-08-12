from __future__ import annotations

import pytest

from benchmarks.reliability.statistics import release_metric, wilson_bound


def test_zero_false_pass_bound_is_below_one_percent_at_300_runs() -> None:
    metric = release_metric(0, 300)

    assert metric["value"] == 0.0
    assert metric["one_sided_95_upper"] < 0.01


def test_known_true_acceptance_bound_is_conservative() -> None:
    metric = release_metric(95, 100)

    assert metric["value"] == 0.95
    assert metric["one_sided_95_lower"] >= 0.90


def test_statistics_reject_empty_or_invalid_populations() -> None:
    with pytest.raises(ValueError, match="positive trial"):
        wilson_bound(0, 0)
    with pytest.raises(ValueError, match="between zero"):
        wilson_bound(4, 3)
    with pytest.raises(ValueError, match="frozen"):
        wilson_bound(1, 2, confidence=0.9)
