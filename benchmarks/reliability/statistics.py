"""Small, dependency-free one-sided binomial confidence calculations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt


@dataclass(frozen=True)
class BinomialBound:
    successes: int
    trials: int
    confidence: float
    lower: float
    upper: float


def _z_for_one_sided_confidence(confidence: float) -> float:
    # The stable release contract uses 95%; keep the supported table explicit so
    # a caller cannot silently change the statistical target.
    if confidence != 0.95:
        raise ValueError("only the frozen one-sided 95% confidence level is supported")
    return 1.6448536269514722


def wilson_bound(
    successes: int,
    trials: int,
    *,
    confidence: float = 0.95,
) -> BinomialBound:
    """Return the two-sided Wilson interval at the frozen 95% level.

    The lower and upper values are used as one-sided release bounds.  A zero
    denominator is deliberately rejected instead of being treated as success.
    """

    if type(successes) is not int or type(trials) is not int:
        raise TypeError("successes and trials must be integers")
    if trials < 1 or successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and a positive trial count")
    z = _z_for_one_sided_confidence(confidence)
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denominator
    radius = z * sqrt((p * (1 - p) + z2 / (4 * trials)) / trials) / denominator
    return BinomialBound(
        successes=successes,
        trials=trials,
        confidence=confidence,
        lower=max(0.0, center - radius),
        upper=min(1.0, center + radius),
    )


def release_metric(
    successes: int,
    trials: int,
    *,
    confidence: float = 0.95,
) -> dict[str, int | float]:
    """Serialize one metric with observed rate and its release confidence bounds."""

    bound = wilson_bound(successes, trials, confidence=confidence)
    observed = successes / trials
    if not all(isfinite(value) for value in (observed, bound.lower, bound.upper)):
        raise ValueError("binomial calculation produced a non-finite value")
    return {
        "numerator": successes,
        "denominator": trials,
        "value": observed,
        "one_sided_95_lower": bound.lower,
        "one_sided_95_upper": bound.upper,
    }
