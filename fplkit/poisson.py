
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson

MAX_GOALS = 20


def outcome_probs(lam_home: float, lam_away: float) -> tuple[float, float, float]:
    goals = np.arange(MAX_GOALS + 1)
    home_pmf = poisson.pmf(goals, lam_home)
    away_pmf = poisson.pmf(goals, lam_away)
    joint = np.outer(home_pmf, away_pmf)
    home_win = np.tril(joint, -1).sum()
    draw = np.trace(joint)
    away_win = np.triu(joint, 1).sum()
    return float(home_win), float(draw), float(away_win)


def prob_over(lam_home: float, lam_away: float, line: float) -> float:
    total_mean = lam_home + lam_away
    return float(1.0 - poisson.cdf(np.floor(line), total_mean))


def lambdas_from_odds(
    p_home: float,
    p_draw: float,
    p_away: float,
    p_over: float | None = None,
    totals_line: float | None = None,
) -> tuple[float, float]:
    targets = np.array([p_home, p_draw, p_away])
    use_totals = p_over is not None and totals_line is not None

    def loss(params: np.ndarray) -> float:
        lam_home, lam_away = np.exp(params)
        model = np.array(outcome_probs(lam_home, lam_away))
        error = float(((model - targets) ** 2).sum())
        if use_totals:
            error += 2.0 * (prob_over(lam_home, lam_away, totals_line) - p_over) ** 2
        return error

    best = minimize(loss, x0=np.log([1.5, 1.2]), method="Nelder-Mead",
                    options={"xatol": 1e-4, "fatol": 1e-8, "maxiter": 800})
    lam_home, lam_away = np.exp(best.x)
    return float(np.clip(lam_home, 0.15, 5.0)), float(np.clip(lam_away, 0.15, 5.0))


def clean_sheet_prob(lam_against: float) -> float:
    return float(poisson.pmf(0, lam_against))


def expected_concession_penalty(lam_against: float) -> float:
    goals = np.arange(MAX_GOALS + 1)
    return float((np.floor(goals / 2) * poisson.pmf(goals, lam_against)).sum())


def expected_save_points(expected_saves: float, per_points: int = 3) -> float:
    if expected_saves <= 0:
        return 0.0
    counts = np.arange(0, max(MAX_GOALS * 3, int(expected_saves * 4) + 12))
    return float((np.floor(counts / per_points) * poisson.pmf(counts, expected_saves)).sum())


def prob_at_least(threshold: int, mean: float, dispersion: float = 1.0) -> float:
    if mean <= 0:
        return 0.0
    if dispersion <= 1.0:
        return float(1.0 - poisson.cdf(threshold - 1, mean))

    size = mean / (dispersion - 1.0)
    prob = 1.0 / dispersion
    term = prob ** size
    below = term
    for k in range(1, threshold):
        term *= (k + size - 1.0) / k * (1.0 - prob)
        below += term
    return float(min(1.0, max(0.0, 1.0 - below)))
