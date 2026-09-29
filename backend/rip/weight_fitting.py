"""Offline, advisory weight-fitting from accumulated MatchFeedback.

Never called from the live query path. rip.nlq.WEIGHTS and
NAME_ONLY_WEIGHTS stay hand-set constants that a human edits by hand; this
module only helps decide whether and how to update them. MatchFeedback's
own docstring is explicit that it is "deliberately inert" and exists so a
downstream tool has "labelled training data" — this respects that boundary:
advisory only, human in the loop, nothing here writes back into ranking.

Deliberately dependency-free (no numpy/scikit-learn). The sample sizes this
will ever run against are small — tens to a few hundred votes, not the
scale that justifies pulling in a heavier stack — and a plain, readable
gradient-descent implementation is easier to read, trust, and unit-test
than a black-box import, for a script that exists specifically to keep
ranking auditable.
"""
import math

# Below these, a "suggestion" would be reporting noise as if it were signal.
# Both a floor on total votes and on the minority class: 40 votes that are
# 39 "good" and 1 "bad" say almost nothing about what separates the two.
MIN_TOTAL_VOTES = 30
MIN_PER_CLASS = 10


def sigmoid(z: float) -> float:
    """Logistic function, clipped to avoid float overflow on an extreme z
    (a bad fit or an outlier row could otherwise raise OverflowError deep
    inside a training loop instead of just saturating at 0 or 1)."""
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def fit_logistic_regression(
    X: list, y: list, *, l2: float = 0.1, lr: float = 0.5, iterations: int = 2000,
) -> list:
    """Batch gradient descent, L2-regularized. One coefficient per feature
    column in X — no intercept, since components are already 0..1 scaled
    and an intercept term would not map back onto the WEIGHTS convention
    (non-negative values that sum to 1).

    L2 regularization matters specifically here because sample sizes will
    often be small and components are correlated by construction (e.g.
    depth and breadth both rise together for well-documented people) — an
    unregularized fit on correlated features with few samples swings wildly
    and fits noise rather than signal. Kept deliberately simple: this is
    meant to be read and trusted, not to be a general-purpose ML library.
    """
    n = len(X)
    if n == 0:
        return []
    k = len(X[0])
    w = [0.0] * k
    for _ in range(iterations):
        grad = [0.0] * k
        for xi, yi in zip(X, y, strict=True):
            pred = sigmoid(sum(wj * xij for wj, xij in zip(w, xi, strict=True)))
            err = pred - yi
            for j in range(k):
                grad[j] += err * xi[j]
        for j in range(k):
            grad[j] = grad[j] / n + l2 * w[j]
            w[j] -= lr * grad[j]
    return w


def coefficients_to_weights(names: list, coefs: list) -> tuple:
    """Turn raw logistic-regression coefficients into a WEIGHTS-style dict:
    non-negative values that sum to 1, matching rip.nlq.WEIGHTS's own
    convention so a suggestion can be compared line-by-line against it.

    A negative coefficient says "more of this signal predicted WORSE
    feedback, not better" — a real, actionable finding. Folding it into a
    smaller positive weight would quietly hide that; it is reported
    separately (the second return value) instead, and excluded from the
    normalized dict rather than clipped to zero and forgotten.

    Returns ({component: suggested_weight}, [components with a negative
    coefficient]).
    """
    negative = [n for n, c in zip(names, coefs, strict=True) if c < 0]
    positive = {n: max(0.0, c) for n, c in zip(names, coefs, strict=True)}
    total = sum(positive.values())
    if total <= 0:
        return dict.fromkeys(names, 0.0), negative
    return {n: round(v / total, 4) for n, v in positive.items()}, negative
