import numpy as np


def generate_selectivity_matrix(affinities, target_indices=None, h=5):
    """
    Calculate a blended selectivity score matrix from pKd affinities.

    affinities: 2D NumPy array of pk_d values (may contain NaN). Shape: (num_compounds, num_targets)
    target_indices: List of target column indices to compute selectivity for. If None, computes for all.
    h: Number of nearest neighbors to use for local relative potency (default is 5)

    Returns a matrix of the same shape with selectivity scores.
    Entries not in target_indices will be NaN.
    """
    affinities = np.asarray(affinities, dtype=float)
    if affinities.ndim != 2:
        raise ValueError("affinities must be a 2D matrix")
    if not isinstance(h, (int, np.integer)) or isinstance(h, bool) or h < 1:
        raise ValueError("h must be a positive integer")
    num_compounds, num_targets = affinities.shape

    if num_targets < 2:
        raise ValueError(f"Need at least 2 targets to compute selectivity, got {num_targets}")
        
    if target_indices is None:
        target_indices = list(range(num_targets))

    # Pre-compute some sums and counts to speed things up
    measured_mask = ~np.isnan(affinities)
    measured_sum = np.nansum(affinities, axis=1)
    measured_count = np.sum(measured_mask, axis=1)

    global_matrix = np.full_like(affinities, np.nan)
    local_matrix = np.full_like(affinities, np.nan)

    for i in range(num_compounds):
        row = affinities[i]
        mask = measured_mask[i]
        sum_i = measured_sum[i]
        count_i = measured_count[i]

        for j in target_indices:
            if not mask[j]:
                continue  # no data → no score

            target_val = row[j]

            # 1. Global Potency
            other_count = count_i - 1
            if other_count < 1:
                global_matrix[i, j] = 0.0
            else:
                mean_other = (sum_i - target_val) / other_count
                global_matrix[i, j] = target_val - mean_other

            # 2. Local Potency
            # Vectorized difference computation
            diffs = np.abs(row - target_val)
            # Ignore self
            diffs[j] = np.inf
            
            # Find the h nearest measured neighbors using partition for performance
            effective_h = min(h, other_count)
            if effective_h == 0:
                local_matrix[i, j] = 0.0
                continue
                
            nearest_indices = np.argpartition(diffs, effective_h - 1)[:effective_h]
            mean_hnn = np.mean(row[nearest_indices])
            local_matrix[i, j] = target_val - mean_hnn

    # 3. Blend the Matrices (50/50 Split)
    alpha = 0.5
    final_scores_matrix = (alpha * local_matrix) + ((1 - alpha) * global_matrix)

    return final_scores_matrix
