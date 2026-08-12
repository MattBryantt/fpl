"""Turning market prices into expected goals, and expected goals into points.

The bridge between a bookmaker's 1X2 price and an FPL projection is a scoring
model. We use independent Poisson: goals for each side are Poisson with means
(lam_home, lam_away), which fixes every quantity we need downstream -- win/draw
probabilities, clean-sheet probability, and the distribution of goals conceded.

Independent Poisson slightly understates draws and 0-0s relative to reality
(the Dixon-Coles correction exists for exactly this). The effect on clean-sheet
probabilities is a percentage point or two, which is small next to the minutes
uncertainty that dominates any FPL projection.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson

# Where the goal distributions are truncated. Has to comfortably clear the
# largest lambda the odds inversion can explore (clipped at 5.0 per side): at 12
# a 5-5 fit loses 0.4% of its probability mass, which quietly distorts the
# least-squares fit against the bookmakers' 1X2. At 20 the residual is far below
# the precision of the prices themselves, and the joint matrix is still tiny.
MAX_GOALS = 20


def outcome_probs(lam_home: float, lam_away: float) -> tuple[float, float, float]:
    """P(home win), P(draw), P(away win) under independent Poisson."""
    goals = np.arange(MAX_GOALS + 1)
    home_pmf = poisson.pmf(goals, lam_home)
    away_pmf = poisson.pmf(goals, lam_away)
    joint = np.outer(home_pmf, away_pmf)
    home_win = np.tril(joint, -1).sum()  # home goals > away goals
    draw = np.trace(joint)
    away_win = np.triu(joint, 1).sum()
    return float(home_win), float(draw), float(away_win)


def prob_over(lam_home: float, lam_away: float, line: float) -> float:
    """P(total goals > line) under independent Poisson."""
    total_mean = lam_home + lam_away
    return float(1.0 - poisson.cdf(np.floor(line), total_mean))


def lambdas_from_odds(
    p_home: float,
    p_draw: float,
    p_away: float,
    p_over: float | None = None,
    totals_line: float | None = None,
) -> tuple[float, float]:
    """Invert de-vigged market probabilities into (lam_home, lam_away).

    Two free parameters against three (dependent) 1X2 probabilities, so the fit
    is over-determined and solved by least squares. When an over/under price is
    available it is added as a fourth target and pins down the total far better
    than 1X2 alone can.
    """
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
    """P(concede zero) = Poisson pmf at 0."""
    return float(poisson.pmf(0, lam_against))


def expected_concession_penalty(lam_against: float) -> float:
    """E[floor(goals_conceded / 2)] -- the -1-per-2-conceded deduction.

    Computed exactly over the Poisson pmf rather than approximated as lam/2,
    because floor() is not linear and the difference matters at low lambdas.
    """
    goals = np.arange(MAX_GOALS + 1)
    return float((np.floor(goals / 2) * poisson.pmf(goals, lam_against)).sum())


def expected_save_points(expected_saves: float, per_points: int = 3) -> float:
    """E[floor(saves / 3)] with saves modelled as Poisson."""
    if expected_saves <= 0:
        return 0.0
    counts = np.arange(0, max(MAX_GOALS * 3, int(expected_saves * 4) + 12))
    return float((np.floor(counts / per_points) * poisson.pmf(counts, expected_saves)).sum())


def prob_at_least(threshold: int, mean: float, dispersion: float = 1.0) -> float:
    """P(X >= threshold), Poisson by default, negative binomial when over-dispersed.

    Defensive contribution needs the second form. It is the only scoring term
    that is a *threshold* rather than a rate, and thresholds are unforgiving of
    two things this model does elsewhere for good reasons:

      * **Shrinkage.** Pulling every rate toward the positional average is right
        for an expected value and wrong for a step function -- E[f(X)] is not
        f(E[X]) when f is a step, and it is the tail that pays.
      * **Poisson.** Real per-match counts vary more than Poisson allows, because
        how many tackles a defender makes depends on the game state as much as on
        the defender. Measured within-player Var/Mean is 1.34, not 1.0.

    Both push the same way, and together they had the model paying 36 DC points
    a gameweek where the league actually paid 61. `dispersion` is calibrated
    against that measured total rather than against the count variance, so it
    absorbs the shrinkage compression too -- which is why it is 2.25 and not
    1.34. It is a correction, not a claim about the true distribution.

    Var = dispersion x mean, so dispersion = 1 reduces exactly to Poisson.
    """
    if mean <= 0:
        return 0.0
    if dispersion <= 1.0:
        return float(1.0 - poisson.cdf(threshold - 1, mean))

    # Stepped rather than taken from scipy so that the JS port can run the
    # identical recurrence and the two stay comparable to machine precision.
    size = mean / (dispersion - 1.0)
    prob = 1.0 / dispersion
    term = prob ** size          # P(X = 0)
    below = term
    for k in range(1, threshold):
        term *= (k + size - 1.0) / k * (1.0 - prob)
        below += term
    return float(min(1.0, max(0.0, 1.0 - below)))


def dc_dispersion(evidence_weight: float, floor: float, ceiling: float) -> float:
    """Per-player dispersion for `prob_at_least`, tightening as dc_per90 is trusted more.

    `ceiling` corrects for shrinkage compression as much as for real variance
    (see `prob_at_least`), and a player whose rate is mostly his own record --
    high `evidence_weight`, the same minutes-vs-prior weight shrinkage used to
    produce dc_per90 -- does not need the shrinkage half of that correction. A
    fresh mover or a thin sample, whose rate is mostly the positional prior,
    keeps the full `ceiling`; a nailed-on starter with a full season behind him
    moves toward `floor`, the dispersion raw counts actually show.
    """
    evidence_weight = min(1.0, max(0.0, evidence_weight))
    return ceiling - (ceiling - floor) * evidence_weight


def ratings_from_xg(
    attack_for: float,
    defence_against: float,
    league_mean: float,
) -> tuple[float, float]:
    """Convert a team's per-match xG for/against into attack/defence multipliers."""
    attack = attack_for / league_mean if league_mean > 0 else 1.0
    defence = defence_against / league_mean if league_mean > 0 else 1.0
    return float(np.clip(attack, 0.3, 3.0)), float(np.clip(defence, 0.3, 3.0))
