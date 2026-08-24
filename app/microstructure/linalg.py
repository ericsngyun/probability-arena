"""Just enough linear algebra for a ridge fit, in pure Python.

Deliberately dependency-free. EVO is a shared production host and the tranche
is mid-flight; adding numpy to it now would be a deployment change during a
preregistered capture for no scientific gain. The frozen models are at most
~30 features, so the normal equations are a 30x30 solve and pure Python is
entirely adequate.
"""

from __future__ import annotations


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. `a` is destroyed."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError(f"singular system at column {col}")
        m[col], m[piv] = m[piv], m[col]
        p = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / p
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def ridge_fit(X: list[list[float]], y: list[float], lam: float = 1e-6) -> list[float]:
    """Ridge coefficients for `X` (rows already including an intercept column).

    The penalty exists for conditioning, not for model selection: it is a fixed
    constant, never tuned, so it cannot become a hidden degree of freedom.
    """
    if not X:
        raise ValueError("no rows to fit")
    p = len(X[0])
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for row, target in zip(X, y):
        for i in range(p):
            ri = row[i]
            if ri:
                xty[i] += ri * target
                xr = xtx[i]
                for j in range(p):
                    xr[j] += ri * row[j]
    for i in range(1, p):          # never penalise the intercept
        xtx[i][i] += lam
    return solve(xtx, xty)


def predict(X: list[list[float]], beta: list[float]) -> list[float]:
    return [sum(v * b for v, b in zip(row, beta)) for row in X]


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0
