/* Port of fplkit/poisson.py — the parts the browser needs.
 *
 * Only the scoring side is here. The odds inversion (`lambdas_from_odds`) needs
 * a Nelder-Mead fit and runs once per fixture at projection time, on the
 * laptop; its output travels in the snapshot as lam_home/lam_away. What the
 * phone needs is the other direction: given a lambda, what does it pay.
 *
 * Verified against scipy by scripts/verify-js-port.mjs.
 */

// Same truncation as the Python. At the lambdas this model produces (clipped at
// 5.0 a side) the tail beyond 20 is far below the precision of the inputs.
export const MAX_GOALS = 20;

/* Poisson pmf without a factorial, which overflows a double at 171!. Stepping
   the pmf multiplicatively (p_k = p_{k-1} * lam / k) keeps every intermediate
   the size of a probability. */
function pmfTable(lambda, upTo) {
  const out = new Float64Array(upTo + 1);
  if (!(lambda >= 0)) return out;
  out[0] = Math.exp(-lambda);
  for (let k = 1; k <= upTo; k++) out[k] = (out[k - 1] * lambda) / k;
  return out;
}

/** P(concede zero) = pmf at 0. */
export function cleanSheetProb(lamAgainst) {
  return Math.exp(-lamAgainst);
}

/** E[floor(goals / 2)] — the -1-per-2-conceded deduction.
 *  Exact over the pmf, not lam/2: floor() is not linear and at the low lambdas
 *  a good defence produces, the difference is not a rounding error. */
export function expectedConcessionPenalty(lamAgainst) {
  const pmf = pmfTable(lamAgainst, MAX_GOALS);
  let total = 0;
  for (let g = 0; g <= MAX_GOALS; g++) total += Math.floor(g / 2) * pmf[g];
  return total;
}

/** E[floor(saves / perPoints)] with saves Poisson. */
export function expectedSavePoints(expectedSaves, perPoints = 3) {
  if (!(expectedSaves > 0)) return 0;
  const upTo = Math.max(MAX_GOALS * 3, Math.floor(expectedSaves * 4) + 12) - 1;
  const pmf = pmfTable(expectedSaves, upTo);
  let total = 0;
  for (let c = 0; c <= upTo; c++) total += Math.floor(c / perPoints) * pmf[c];
  return total;
}

/** P(X >= threshold), Poisson by default, negative binomial when over-dispersed.
 *
 *  Defensive contribution is the only threshold in the scoring rules, and a
 *  threshold pays the tail rather than the mean — which shrinkage flattens and
 *  Poisson understates. See poisson.prob_at_least in the Python for the full
 *  reasoning and where the 2.25 comes from. Var = dispersion x mean, so
 *  dispersion = 1 reduces exactly to Poisson. */
export function probAtLeast(threshold, mean, dispersion = 1) {
  if (!(mean > 0)) return 0;

  let below = 0;
  if (dispersion <= 1) {
    const pmf = pmfTable(mean, Math.max(0, threshold - 1));
    for (let k = 0; k <= threshold - 1; k++) below += pmf[k];
  } else {
    // The same recurrence the Python steps, so the two agree to the last bit
    // rather than to whatever scipy and a hand-rolled loop happen to share.
    const size = mean / (dispersion - 1);
    const p = 1 / dispersion;
    let term = Math.pow(p, size);   // P(X = 0)
    below = term;
    for (let k = 1; k < threshold; k++) {
      term *= ((k + size - 1) / k) * (1 - p);
      below += term;
    }
  }
  // Clamp: the complement of a sum of positives can land a hair below zero.
  return Math.min(1, Math.max(0, 1 - below));
}

/** Per-player dispersion for probAtLeast. Port of poisson.dc_dispersion --
 *  see there for why it interpolates between floor and ceiling rather than
 *  using one constant for every player. */
export function dcDispersion(evidenceWeight, floor, ceiling) {
  const w = Math.min(1, Math.max(0, evidenceWeight));
  return ceiling - (ceiling - floor) * w;
}
