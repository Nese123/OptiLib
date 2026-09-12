"""Shared selectivity calculation for uploaded affinities and ChEMBL builds."""

import numpy as np


SELECTIVITY_SCORING_VERSION = "blended_boundary_average_v2"


def _validate_h(h):
    if not isinstance(h, (int, np.integer)) or isinstance(h, bool) or h < 1:
        raise ValueError("h must be a positive integer")


def score_measured_affinities(values, h=5, target_positions=None):
    """Score one compound, averaging equally near neighbors at the cutoff.

    The tied group fills the remaining neighbor slots with equal fractional
    weights. This averages all valid choices of h nearest neighbors and is
    independent of target order. Unrequested positions are NaN.
    """
    _validate_h(h)
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("measured affinities must be a finite 1D array")
    positions = (np.arange(len(values)) if target_positions is None
                 else np.asarray(target_positions, dtype=int))
    scores = np.full(values.shape, np.nan)
    if len(values) <= 1:
        scores[positions] = 0.0
        return scores

    # Canonical summation order also prevents floating-point changes when
    # identical affinities arrive in a different target order.
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    inverse_order = np.empty(len(values), dtype=int)
    inverse_order[order] = np.arange(len(values))
    global_diffs = values - (sorted_values.sum() - values) / (len(values) - 1)
    if len(values) <= h + 1:
        scores[positions] = global_diffs[positions]
        return scores

    for position in positions:
        value = values[position]
        distances = np.abs(sorted_values - value)
        distances[inverse_order[position]] = np.inf
        cutoff = np.partition(distances, h - 1)[h - 1]
        closer = distances < cutoff
        tied = distances == cutoff
        remaining = h - np.count_nonzero(closer)
        local_mean = (sorted_values[closer].sum()
                      + remaining * sorted_values[tied].mean()) / h
        scores[position] = 0.5 * (global_diffs[position] + value - local_mean)
    return scores


def generate_selectivity_matrix(affinities, target_indices=None, h=5):
    """Return blended scores in the input shape, preserving missing values.

    Only requested target columns are populated. Local comparisons use measured
    affinities only, so sparse uploads do not partition full matrix rows.
    """
    affinities = np.asarray(affinities, dtype=float)
    if affinities.ndim != 2:
        raise ValueError("affinities must be a 2D matrix")
    _validate_h(h)
    num_targets = affinities.shape[1]
    if num_targets < 2:
        raise ValueError(f"Need at least 2 targets to compute selectivity, got {num_targets}")
    requested = np.ones(num_targets, dtype=bool)
    if target_indices is not None:
        requested[:] = False
        requested[list(target_indices)] = True

    scores = np.full_like(affinities, np.nan)
    for row_index, row in enumerate(affinities):
        measured = np.flatnonzero(~np.isnan(row))
        positions = np.flatnonzero(requested[measured])
        if len(positions):
            scores[row_index, measured] = score_measured_affinities(
                row[measured], h=h, target_positions=positions,
            )
    return scores
