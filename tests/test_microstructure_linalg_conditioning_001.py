"""Adversarial conditioning for the frozen ridge solver.

Normal equations square the condition number, so `(X'X + lam I)b = X'y` is the
formulation most exposed to collinearity. The fixed ridge penalty exists for
conditioning, not model selection, and is NEVER tuned here: these tests ask
only whether the frozen solver stays *defined* under hostile input, not whether
its answers improve.

The contract under adversarial input is: **finite, deterministic output, or an
explicit numerical refusal.** Silent NaN or a value that changes between runs
would be far worse than a raised error.
"""

from __future__ import annotations

import math
import random

import pytest

from app.microstructure.evaluate import RIDGE_LAMBDA
from app.microstructure.linalg import predict, ridge_fit, solve


def _finite(vals) -> bool:
    return all(math.isfinite(v) for v in vals)


def _fit_or_refuse(X, y, lam=RIDGE_LAMBDA):
    """The frozen contract: finite deterministic coefficients, or a raise."""
    try:
        beta = ridge_fit(X, y, lam)
    except ValueError:
        return None                      # explicit numerical refusal is allowed
    assert _finite(beta), f"solver produced non-finite coefficients: {beta}"
    assert _finite(predict(X, beta)), "solver produced non-finite predictions"
    return beta


def test_duplicate_feature_stays_defined():
    """Two literally identical columns -- X'X is singular without the penalty."""
    rng = random.Random(1)
    X, y = [], []
    for _ in range(200):
        a = rng.gauss(0, 1)
        X.append([1.0, a, a])            # column 2 == column 1
        y.append(2 * a + rng.gauss(0, 0.01))
    beta = _fit_or_refuse(X, y)
    assert beta is not None, "the ridge penalty should keep this solvable"
    assert abs(beta[1] + beta[2] - 2.0) < 0.1, "combined effect recovered"


def test_near_duplicate_feature_stays_defined():
    rng = random.Random(2)
    X, y = [], []
    for _ in range(300):
        a = rng.gauss(0, 1)
        X.append([1.0, a, a + rng.gauss(0, 1e-9)])
        y.append(3 * a + rng.gauss(0, 0.01))
    assert _fit_or_refuse(X, y) is not None


def test_wildly_different_feature_scales_stay_defined():
    """Realistic here: depth is in contracts, mid is a probability in [0,1]."""
    rng = random.Random(3)
    X, y = [], []
    for _ in range(300):
        tiny, huge = rng.gauss(0, 1e-6), rng.gauss(0, 1e6)
        X.append([1.0, tiny, huge, rng.gauss(0, 1)])
        y.append(0.5 * tiny + 1e-7 * huge + rng.gauss(0, 0.01))
    assert _fit_or_refuse(X, y) is not None


def test_constant_column_stays_defined():
    """A feature with zero variance -- e.g. a market whose spread never moves."""
    rng = random.Random(4)
    X = [[1.0, rng.gauss(0, 1), 7.0] for _ in range(200)]
    y = [rng.gauss(0, 1) for _ in range(200)]
    assert _fit_or_refuse(X, y) is not None


def test_perfectly_collinear_block_is_finite_or_refused():
    """Three columns summing to a fourth -- the worst realistic case."""
    rng = random.Random(5)
    X, y = [], []
    for _ in range(300):
        a, b, c = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
        X.append([1.0, a, b, c, a + b + c])
        y.append(a - b + rng.gauss(0, 0.01))
    _fit_or_refuse(X, y)                 # finite, or an explicit refusal


def test_more_features_than_rows_is_finite_or_refused():
    rng = random.Random(6)
    X = [[1.0] + [rng.gauss(0, 1) for _ in range(30)] for _ in range(10)]
    y = [rng.gauss(0, 1) for _ in range(10)]
    _fit_or_refuse(X, y)


def test_output_is_deterministic_across_repeated_fits():
    """A hostile fixture must not give a different answer on the second run."""
    rng = random.Random(7)
    X, y = [], []
    for _ in range(200):
        a = rng.gauss(0, 1)
        X.append([1.0, a, a + 1e-12, a * 1e6])
        y.append(a + rng.gauss(0, 0.01))
    first = _fit_or_refuse(X, y)
    for _ in range(5):
        assert _fit_or_refuse(X, y) == first, "solver is not deterministic"


def test_a_truly_singular_system_raises_rather_than_returning_nonsense():
    with pytest.raises(ValueError, match="singular"):
        solve([[0.0, 0.0], [0.0, 0.0]], [1.0, 1.0])


def test_the_penalty_is_a_fixed_constant_not_a_tuned_one():
    """If lambda ever becomes data-dependent it is a hidden degree of freedom."""
    import inspect
    from app.microstructure import evaluate as E
    assert isinstance(RIDGE_LAMBDA, float) and RIDGE_LAMBDA > 0
    src = inspect.getsource(E)
    for banned in ("lambda_grid", "tune", "cross_validate", "best_lam",
                   "select_lambda"):
        assert banned not in src
    # every call site passes the frozen constant
    assert src.count("ridge_fit(") == src.count("RIDGE_LAMBDA)")


def test_intercept_is_never_penalised():
    """Penalising the intercept biases the level, which is not conditioning."""
    X = [[1.0, 0.0]] * 50
    y = [5.0] * 50
    beta = ridge_fit(X, y, lam=1e3)
    assert abs(beta[0] - 5.0) < 1e-6, "intercept was shrunk toward zero"
