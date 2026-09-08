import time
import numpy as np
import pandas as pd
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import ElementwiseProblem
from pymoo.operators.crossover.hux import HalfUniformCrossover
from pymoo.operators.mutation.bitflip import BitflipMutation
from pymoo.optimize import minimize
from pymoo.termination.default import DefaultMultiObjectiveTermination
import warnings


# ═══════════════════════════════════════════════════════════════
#  PROBLEM DEFINITION
# ═══════════════════════════════════════════════════════════════

class DrugLibraryProblem(ElementwiseProblem):
    """Multi-objective drug library optimization problem for NSGA-II.

    Objectives:
        1. Maximize biological selectivity (negated, normalized by pool baseline).
        2. Minimize total library cost (normalized by pool total cost).

    Constraints:
        - At most `allowed_miss_pct` fraction of targets may be uncovered.
    """

    def __init__(self, selectivity_matrix, price_array,
                 weight_mean=0.5, allowed_miss_pct=0.5):
        self.matrix = selectivity_matrix
        self.prices = price_array
        self.weight_mean = weight_mean
        self.weight_min = 1.0 - weight_mean
        self.allowed_miss_pct = allowed_miss_pct

        self.num_drugs, self.num_targets = self.matrix.shape
        self.max_allowed_misses = int(self.num_targets * self.allowed_miss_pct)

        # Compute pool-level baselines for normalization and reporting
        self.pool_total_cost = float(np.sum(self.prices))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            pool_max_scores = np.nanmax(self.matrix, axis=0)
        pool_max_scores = np.nan_to_num(pool_max_scores, nan=-1.0)

        positive_pool_scores = pool_max_scores[pool_max_scores > 0]
        
        self.pool_mean_sel = float(np.mean(positive_pool_scores)) if len(positive_pool_scores) > 0 else 0.0
        self.pool_min_sel = float(np.min(positive_pool_scores)) if len(positive_pool_scores) > 0 else 0.0
        self.pool_baseline_score = self.weight_mean * self.pool_mean_sel + self.weight_min * self.pool_min_sel
        self.pool_num_targets = self.num_targets
        self.pool_num_drugs = self.num_drugs

        super().__init__(
            n_var=self.num_drugs,
            n_obj=2,             # 2 Objectives: Selectivity and Cost
            n_ieq_constr=1,      # 1 inequality constraint: coverage
            xl=0,
            xu=1
        )

    def _evaluate(self, x, out, *args, **kwargs):
        mask = x > 0.5

        # Handle Empty Library
        if not np.any(mask):
            out["F"] = [9999, 9999]
            out["G"] = [self.num_targets]
            return

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            target_max_scores = np.nanmax(self.matrix[mask, :], axis=0)
        target_max_scores = np.nan_to_num(target_max_scores, nan=-1.0)

        # Constraint 1: The Allowance
        missed_targets = np.sum(target_max_scores <= 0)

        # pymoo passes constraints if G <= 0.
        # If we miss 2 targets, and 2 are allowed: 2 - 2 = 0 (Pass)
        # If we miss 3 targets, and 2 are allowed: 3 - 2 = 1 (Fail)
        coverage_violation = missed_targets - self.max_allowed_misses

        # Score all targets: covered targets get their max score, uncovered get 0.
        # This penalizes dropping targets even when the constraint allows it.
        all_scores = np.maximum(target_max_scores, 0)
        covered_scores = all_scores[all_scores > 0]

        out["G"] = [coverage_violation]

        # Calculate biological score across ALL targets (uncovered = 0)
        if len(covered_scores) > 0:
            biological_score = self.weight_mean * np.mean(all_scores) + self.weight_min * np.min(covered_scores)
        else:
            biological_score = 0.0  # Fallback if all targets are missed

        # Normalize selectivity: -1 = pool baseline; lower is better.
        obj_1 = -biological_score / (self.pool_baseline_score or 1.0)

        # Normalize cost: 0 = free, 1 = buying the entire pool
        raw_total_cost = self.prices[mask].sum()
        obj_2 = raw_total_cost / (self.pool_total_cost or 1.0)

        out["F"] = [obj_1, obj_2]


# ═══════════════════════════════════════════════════════════════
#  SMART INITIALIZATION
# ═══════════════════════════════════════════════════════════════

def build_smart_init(selectivities, prices, pop_size=100, seed=1):
    """Build the initial population with smart guesses seeded in.

    Smart Guess 1 ("Bargain Bin"):  cheapest drug covering each target.
    Smart Guess 2 ("Max Efficacy"): highest-selectivity drug for each target.
    Smart Guess 3 ("Cost-Effective"): highest selectivity per dollar.
    Smart Guess 4 ("The Union"): Bargain Bin + Max Efficacy combined.
    Smart Guess 5 ("Second Cheapest"): second cheapest drug for each target.

    Returns:
        X_init: 2D binary array of shape (pop_size, num_drugs).
    """
    if pop_size < 5:
        raise ValueError("pop_size must be at least 5 to include all smart seeds")
    rng = np.random.default_rng(seed)
    X_init = rng.integers(0, 2, size=(pop_size, selectivities.shape[0])).astype(bool)

    # Precompute coverage mask
    coverage_mask = selectivities > 0
    has_coverage = np.any(coverage_mask, axis=0)

    # -------------------------------------------------------------
    # Smart Guess 1: "The Bargain Bin" (Cheapest drug for each target)
    masked_prices = np.where(coverage_mask, prices[:, np.newaxis], np.inf)
    cheapest_per_target = np.argmin(masked_prices, axis=0)
    cheapest_drugs = np.unique(cheapest_per_target[has_coverage])

    X_init[0, :] = 0
    X_init[0, cheapest_drugs] = 1

    # -------------------------------------------------------------
    # Smart Guess 2: "Maximum Efficacy" (Highest selectivity drug for each target)
    best_per_target = np.argmax(np.where(coverage_mask, selectivities, -np.inf), axis=0)
    best_scores = selectivities[best_per_target, np.arange(selectivities.shape[1])]
    max_sel_drugs = np.unique(best_per_target[best_scores > 0])

    X_init[1, :] = 0
    X_init[1, max_sel_drugs] = 1

    # -------------------------------------------------------------
    # Smart Guess 3: "Cost-Effective" (Highest selectivity / price)
    masked_sel = np.where(coverage_mask, selectivities, 0)
    sel_per_dollar = masked_sel / np.maximum(prices[:, np.newaxis], 1e-6)
    del masked_sel  # Free memory
    cost_effective_per_target = np.argmax(sel_per_dollar, axis=0)
    del sel_per_dollar  # Free memory
    cost_effective_drugs = np.unique(cost_effective_per_target[has_coverage])

    X_init[2, :] = 0
    X_init[2, cost_effective_drugs] = 1

    # -------------------------------------------------------------
    # Smart Guess 4: "The Union" (Bargain Bin + Max Efficacy)
    union_drugs = np.unique(np.concatenate([cheapest_drugs, max_sel_drugs]))
    X_init[3, :] = 0
    X_init[3, union_drugs] = 1

    # -------------------------------------------------------------
    # Smart Guess 5: "Second Cheapest" (modify in-place, no copy needed)
    for t, d in enumerate(cheapest_per_target):
        if has_coverage[t]:
            masked_prices[d, t] = np.inf
    has_second_coverage = np.any(masked_prices != np.inf, axis=0)
    second_cheapest_per_target = np.argmin(masked_prices, axis=0)
    del masked_prices  # Free memory
    second_cheapest_drugs = np.unique(second_cheapest_per_target[has_second_coverage])

    X_init[4, :] = 0
    X_init[4, second_cheapest_drugs] = 1

    return X_init


# ═══════════════════════════════════════════════════════════════
#  OPTIMIZATION
# ═══════════════════════════════════════════════════════════════

def run_optimization(problem, X_init, pop_size=100, seed=1, max_gen=1000, ftol=0.0025, period=30, mutation_multiplier=1.0, callback=None):
    """Configure and run the NSGA-II optimizer with half-uniform crossover.

    Returns:
        (res, elapsed_time): pymoo Result and elapsed runtime in seconds.
    """
    if not np.isfinite(mutation_multiplier) or mutation_multiplier < 0:
        raise ValueError("mutation_multiplier must be finite and non-negative")

    algorithm = NSGA2(
        pop_size=pop_size,
        sampling=np.asarray(X_init, dtype=bool),
        crossover=HalfUniformCrossover(),
        mutation=BitflipMutation(
            prob=1.0,
            prob_var=min(1.0, mutation_multiplier / problem.num_drugs),
        ),
        eliminate_duplicates=True
    )

    # Stop the algorithm when the Pareto front stops significantly improving
    # over a given period (e.g. 30 generations).
    termination = DefaultMultiObjectiveTermination(
        xtol=1e-8,
        cvtol=1e-6,
        ftol=ftol,
        period=period,
        n_max_gen=max_gen,
        n_max_evals=max(1_000_000, (max_gen + 1) * pop_size)
    )

    print("Starting NSGA-II Optimization...")
    start_time = time.time()
    minimize_kwargs = {
        "seed": seed,
        "verbose": True,
        "save_history": False,
    }
    if callback is not None:
        minimize_kwargs["callback"] = callback

    res = minimize(
        problem,
        algorithm,
        termination,
        **minimize_kwargs
    )

    end_time = time.time()
    elapsed_time = end_time - start_time
    minutes = int(elapsed_time // 60)
    seconds = elapsed_time % 60
    if minutes > 0:
        print(f"Optimization Complete! (Run time: {minutes}m {seconds:.2f}s)")
    else:
        print(f"Optimization Complete! (Run time: {seconds:.2f}s)")

    return res, elapsed_time


def find_knee_point(front):
    """Find the knee point (elbow) on a 2D Pareto front using the chord method.

    Uses the Maximum Perpendicular Distance to the Secant Line connecting
    the two extreme points on the front, in normalized [0, 1] space.

    Args:
        front: 2D array of shape (N, 2) with real-world [selectivity, cost].

    Returns:
        best_idx: Index of the knee point in the front array.
    """
    if len(front) <= 1:
        return 0

    # Normalize to [0, 1]
    min_vals = np.min(front, axis=0)
    max_vals = np.max(front, axis=0)
    range_vals = max_vals - min_vals
    range_vals[range_vals == 0] = 1.0
    norm_front = (front - min_vals) / range_vals

    # Extreme endpoints
    idx_min_sel = np.argmin(norm_front[:, 0])
    idx_max_sel = np.argmax(norm_front[:, 0])
    p1 = norm_front[idx_min_sel]
    p2 = norm_front[idx_max_sel]

    line_vec = p2 - p1
    line_len = np.linalg.norm(line_vec)

    if line_len > 1e-9:
        cross_product = (line_vec[0] * (norm_front[:, 1] - p1[1])) - (line_vec[1] * (norm_front[:, 0] - p1[0]))
        distances = np.abs(cross_product) / line_len
        return int(np.argmax(distances))
    return 0


def reorder_meta_columns(df):
    """Move metadata columns to the front of a DataFrame, preserving the rest."""
    cols = df.columns.tolist()
    meta_cols = []
    for mc in ["Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"]:
        if mc in cols:
            cols.remove(mc)
            meta_cols.append(mc)
    return df[meta_cols + cols]


# ══════════════════════════════════════════════════════════════
#  BEST SOLUTION SELECTION (Knee Point / Secant Line Distance)
# ═══════════════════════════════════════════════════════════════

def select_best_solution(res, problem):
    """Pick the best-compromise knee point solution from the Pareto front.

    Converts the optimizer's internal objective values back to real-world units,
    and identifies the knee point (elbow) using the
    Maximum Perpendicular Distance to the Secant Line (Chord method) in normalized space.

    Returns:
        best_idx: Index of the winning solution in `res.X`.
        front: 2D array of real-world [selectivity, cost] for each Pareto solution.
    """
    # Convert optimizer's normalized values back to real-world units
    if res.F is None or len(res.F) == 0:
        raise ValueError("Optimization failed to find any feasible solutions. Try relaxing the constraints (e.g., increase the maximum amount of missed targets %).")
        
    front = res.F.copy()
    front[:, 0] *= -problem.pool_baseline_score   # Undo negation + normalization → real selectivity
    front[:, 1] *= problem.pool_total_cost  # Undo normalization → real cost (USD)

    best_idx = find_knee_point(front)

    return best_idx, front


# ═══════════════════════════════════════════════════════════════
#  RESULT EXTRACTION & SAVING
# ═══════════════════════════════════════════════════════════════

def save_results(res, best_idx, full_df, output_file='optimized_library.xlsx'):
    """Extract the winning library from the optimizer result and save it to Excel.

    Returns:
        winning_smiles: List of SMILES in the winning library.
        selected_drug_indices: NumPy array of row indices for winning compounds.
        winning_matrix_df: DataFrame of the winning library's selectivity sub-matrix.
    """
    # Extract the binary decision array for the winning library
    winning_binary_array = res.X[best_idx]
    selected_drug_indices = np.where(winning_binary_array > 0.5)[0]
    winning_smiles = full_df.index[selected_drug_indices].tolist()

    print("\nBuilding the isolated selectivity matrix for the winning library...")

    # Slice the rows: Keep only the drugs that won
    winning_matrix_df = full_df.iloc[selected_drug_indices].copy()

    # Drop any targets that do not have at least one selectivity measurement > 0
    target_cols = [c for c in winning_matrix_df.columns if c not in ['Compound_Name', 'Molecule_ChEMBL_ID', 'InChIKey', 'SMILES', 'Price_USD_per_mg']]
    missed_targets = [
        c for c in target_cols
        if not pd.to_numeric(winning_matrix_df[c], errors='coerce').gt(0).any()
    ]
    winning_matrix_df.drop(columns=missed_targets, inplace=True)

    # Reset the index so SMILES becomes a proper column
    winning_matrix_df.reset_index(inplace=True)

    winning_matrix_df = reorder_meta_columns(winning_matrix_df)

    winning_matrix_df.to_excel(output_file, index=False, engine='xlsxwriter')

    print(f"Success! The clean sub-matrix with pricing has been saved to: {output_file}")

    return winning_smiles, selected_drug_indices, winning_matrix_df
