
export const MAX_GOALS = 20;

function pmfTable(lambda, upTo) {
  const out = new Float64Array(upTo + 1);
  if (!(lambda >= 0)) return out;
  out[0] = Math.exp(-lambda);
  for (let k = 1; k <= upTo; k++) out[k] = (out[k - 1] * lambda) / k;
  return out;
}

export function cleanSheetProb(lamAgainst) {
  return Math.exp(-lamAgainst);
}

export function expectedConcessionPenalty(lamAgainst) {
  const pmf = pmfTable(lamAgainst, MAX_GOALS);
  let total = 0;
  for (let g = 0; g <= MAX_GOALS; g++) total += Math.floor(g / 2) * pmf[g];
  return total;
}

export function expectedSavePoints(expectedSaves, perPoints = 3) {
  if (!(expectedSaves > 0)) return 0;
  const upTo = Math.max(MAX_GOALS * 3, Math.floor(expectedSaves * 4) + 12) - 1;
  const pmf = pmfTable(expectedSaves, upTo);
  let total = 0;
  for (let c = 0; c <= upTo; c++) total += Math.floor(c / perPoints) * pmf[c];
  return total;
}

export function probAtLeast(threshold, mean, dispersion = 1) {
  if (!(mean > 0)) return 0;

  let below = 0;
  if (dispersion <= 1) {
    const pmf = pmfTable(mean, Math.max(0, threshold - 1));
    for (let k = 0; k <= threshold - 1; k++) below += pmf[k];
  } else {
    const size = mean / (dispersion - 1);
    const p = 1 / dispersion;
    let term = Math.pow(p, size);
    below = term;
    for (let k = 1; k < threshold; k++) {
      term *= ((k + size - 1) / k) * (1 - p);
      below += term;
    }
  }
  return Math.min(1, Math.max(0, 1 - below));
}
