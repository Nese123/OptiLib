import argparse
import time
import numpy as np
import pandas as pd
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import ElementwiseProblem
from pymoo.operators.crossover.hux import HalfUniformCrossover
from pymoo.core.mutation import Mutation
from pymoo.operators.mutation.bitflip import BitflipMutation
from pymoo.optimize import minimize
from pymoo.termination.default import DefaultMultiObjectiveTermination
from pymoo.visualization.scatter import Scatter
from pymoo.core.callback import Callback
import matplotlib.pyplot as plt
from pymoo.operators.sampling.rnd import BinaryRandomSampling
from pymoo.operators.crossover.pntx import SinglePointCrossover
from pymoo.operators.crossover.pntx import TwoPointCrossover
from pymoo.operators.crossover.ux import UniformCrossover

# ═══════════════════════════════════════════════════════════════
#  CLI ARGUMENTS
# ═══════════════════════════════════════════════════════════════

def parse_args():
    """Parse command-line arguments for hyperparameters and file paths."""
    parser = argparse.ArgumentParser(
        description="NSGA-II Drug Library Optimization",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # File paths
    parser.add_argument("--input", default="final_selectivity_with_prices.xlsx",
                        help="Path to the input Excel file")
    parser.add_argument("--output", default="winning_library_matrix.xlsx",
                        help="Path for the output Excel file")
    parser.add_argument("--metrics", default="run_metrics.xlsx",
                        help="Path for the run metrics Excel file")

    # Problem hyperparameters
    parser.add_argument("--weight-mean", type=float, default=0.5,
                        help="Weight for mean selectivity in biological score. (weight_min will be 1 - weight_mean)")
    parser.add_argument("--allowed-miss-pct", type=float, default=0.04,
                        help="Fraction of targets allowed to be uncovered (0.0-1.0)")

    # Algorithm settings
    parser.add_argument("--mutation-multiplier", type=float, default=4.0,
                        help="Multiplier for the bitflip mutation rate (X / num_drugs)")
    parser.add_argument("--pop-size", type=int, default=100,
                        help="Population size for NSGA-II")
    parser.add_argument("--seed", type=int, default=1,
                        help="Random seed for reproducibility")
    parser.add_argument("--max-gen", type=int, default=1000,
                        help="Maximum number of generations")
    parser.add_argument("--crossover", type=str, default="ux",
                        choices=["spx", "tpx", "ux", "hux"],
                        help="Crossover operator: spx=SinglePoint, tpx=TwoPoint, ux=Uniform, hux=HalfUniform")
    parser.add_argument("--ftol", type=float, default=0.0025,
                        help="Convergence tolerance on the Pareto front")
    parser.add_argument("--use-median", action="store_true",
                        help="Use median selectivity instead of mean/min weighting")

    return parser.parse_args()


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
                 weight_mean=0.004, allowed_miss_pct=0.049, use_median=False):
        self.matrix = selectivity_matrix
        self.prices = price_array
        self.weight_mean = weight_mean
        self.weight_min = 1.0 - weight_mean
        self.allowed_miss_pct = allowed_miss_pct
        self.use_median = use_median

        self.num_drugs, self.num_targets = self.matrix.shape
        self.max_allowed_misses = int(self.num_targets * self.allowed_miss_pct)

        # Compute pool-level baselines for normalization and reporting
        self.pool_total_cost = float(np.sum(self.prices))
        pool_max_scores = np.max(self.matrix, axis=0)
        self.pool_mean_sel = float(np.mean(pool_max_scores[pool_max_scores > 0]))
        self.pool_min_sel = float(np.min(pool_max_scores[pool_max_scores > 0]))
        if self.use_median:
            self.pool_baseline_score = float(np.median(pool_max_scores[pool_max_scores > 0]))
        else:
            self.pool_baseline_score = self.weight_mean * self.pool_mean_sel + self.weight_min * self.pool_min_sel
        self.pool_num_targets = self.num_targets
        self.pool_num_drugs = self.num_drugs

        # Precompute cheapest drug per target for the smart repair mutation
        coverage_mask = self.matrix > 0
        masked_prices = np.where(coverage_mask, self.prices[:, np.newaxis], np.inf)
        self.cheapest_per_target = np.argmin(masked_prices, axis=0)
        self.has_coverage = np.any(coverage_mask, axis=0)

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

        target_max_scores = np.max(self.matrix[mask, :], axis=0)

        # Constraint 1: The Allowance
        missed_targets = np.sum(target_max_scores <= 0)

        # pymoo passes constraints if G <= 0.
        # If we miss 2 targets, and 2 are allowed: 2 - 2 = 0 (Pass)
        # If we miss 3 targets, and 2 are allowed: 3 - 2 = 1 (Fail)
        coverage_violation = missed_targets - self.max_allowed_misses

        # Filter the array to only look at targets with a score > 0
        covered_scores = target_max_scores[target_max_scores > 0]

        out["G"] = [coverage_violation]

        # Calculate the weakest link of the covered targets
        if len(covered_scores) > 0:
            if self.use_median:
                biological_score = np.median(covered_scores)
            else:
                biological_score = self.weight_mean * np.mean(covered_scores) + self.weight_min * np.min(covered_scores)
        else:
            biological_score = 0.0  # Fallback if all targets are missed

        # Normalize selectivity: 0 = pool baseline, negative = better than baseline
        obj_1 = -biological_score / self.pool_baseline_score

        # Normalize cost: 0 = free, 1 = buying the entire pool
        raw_total_cost = self.prices[mask].sum()
        obj_2 = raw_total_cost / self.pool_total_cost

        out["F"] = [obj_1, obj_2]


class SmartRepairMutation(Mutation):
    """A custom mutation operator that applies a standard bitflip mutation, 
    and then forcibly repairs libraries that violate the coverage constraint 
    by injecting the cheapest drug for the missing targets."""
    
    def __init__(self, prob, **kwargs):
        super().__init__(**kwargs)
        self.bitflip = BitflipMutation(prob=prob)

    def _do(self, problem, X, **kwargs):
        # 1. Standard Bitflip
        X_mut = self.bitflip._do(problem, X, **kwargs)
        
        # 2. Smart Repair (Vectorized)
        # Compute coverage mask for the whole problem matrix
        coverage_mask = problem.matrix > 0
        
        # Matrix multiplication to find how many drugs cover each target for each individual
        # Shape: (pop_size, num_drugs) @ (num_drugs, num_targets) -> (pop_size, num_targets)
        hits = (X_mut > 0.5).astype(int) @ coverage_mask.astype(int)
        
        # A target is missed if it has 0 hits
        missed = (hits == 0)
        num_misses = np.sum(missed, axis=1)
        
        # Find individuals that violate the constraint
        needs_repair = np.where(num_misses > problem.max_allowed_misses)[0]
        
        for i in needs_repair:
            missing_targets = np.where(missed[i])[0]
            
            # Randomly pick which missing targets to repair
            np.random.shuffle(missing_targets)
            num_to_repair = len(missing_targets) - problem.max_allowed_misses
            
            for t in missing_targets[:num_to_repair]:
                if problem.has_coverage[t]:
                    cheapest_drug = problem.cheapest_per_target[t]
                    X_mut[i, cheapest_drug] = 1
                        
        return X_mut


# ═══════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════

def load_data(filepath='final_selectivity_with_prices.xlsx'):
    """Load the master dataset and split it into selectivity matrix, prices, and SMILES.

    Returns:
        pure_selectivity_df: DataFrame with SMILES as index and targets as columns.
        selectivities: 2D NumPy array of selectivity scores (compounds × targets).
        prices: 1D NumPy array of per-compound prices.
        smiles: 1D NumPy array of SMILES strings.
    """
    import os
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Input file not found: '{filepath}'")

    print("Loading the final assembled dataset...")
    df = pd.read_excel(filepath)

    # Use SMILES as the row identifier
    df.set_index('SMILES', inplace=True)

    print("Extracting the price array...")
    prices = df['Price_USD_per_mg'].to_numpy()

    # SMILES are the index itself
    smiles = df.index.to_numpy()

    print("Isolating the pure selectivity matrix...")
    pure_selectivity_df = df.drop(columns=['Compound_Name', 'Molecule_ChEMBL_ID', 'Price_USD_per_mg', 'InChIKey'], errors='ignore')

    selectivities = pure_selectivity_df.to_numpy()

    # ---------------------------------------------------------
    # VERIFICATION
    # ---------------------------------------------------------
    print("\n--- Handoff Ready ---")
    print(f"Target Matrix Shape: {selectivities.shape} (Compounds x Targets)")
    print(f"Price Array Shape:   {prices.shape} (Prices,)")

    return df, pure_selectivity_df, selectivities, prices, smiles


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
    np.random.seed(seed)
    
    X_init = np.random.randint(0, 2, size=(pop_size, selectivities.shape[0]))

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
    best_per_target = np.argmax(selectivities, axis=0)
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

def run_optimization(problem, X_init, pop_size=100, seed=1, max_gen=1000, ftol=0.0025, mutation_multiplier=1.098, crossover_type="hux", callback=None):
    """Configure and run the NSGA-II optimizer.

    Returns:
        res: pymoo Result object containing the Pareto-optimal solutions.
    """
    crossover_map = {
        "spx": SinglePointCrossover(),
        "tpx": TwoPointCrossover(),
        "ux": UniformCrossover(),
        "hux": HalfUniformCrossover(),
    }
    crossover_op = crossover_map[crossover_type]
    print(f"Using crossover operator: {crossover_op.__class__.__name__}")

    algorithm = NSGA2(
        pop_size=pop_size,
        sampling=X_init,
        crossover=crossover_op,
        mutation=BitflipMutation(prob=1.0 / problem.num_drugs),
        eliminate_duplicates=True
    )

    # Stop the algorithm when the Pareto front stops significantly improving
    # over a given period (e.g. 30 generations).
    termination = DefaultMultiObjectiveTermination(
        xtol=1e-8,
        cvtol=1e-6,
        ftol=ftol,
        period=30,
        n_max_gen=max_gen,
        n_max_evals=900000
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


# ══════════════════════════════════════════════════════════════
#  BEST SOLUTION SELECTION (Utopia Distance)
# ═══════════════════════════════════════════════════════════════

def select_best_solution(res, problem):
    """Pick the best-compromise solution from the Pareto front using utopia distance.

    Converts the optimizer's internal objective values back to real-world units,
    plots the Pareto front, and selects the solution closest to the ideal
    (max selectivity, min cost) utopia point in normalized space.

    Returns:
        best_idx: Index of the winning solution in `res.X`.
        front: 2D array of real-world [selectivity, cost] for each Pareto solution.
    """
    # Convert optimizer's normalized values back to real-world units
    if res.F is None:
        raise ValueError("Optimization failed to find any feasible solutions. Try relaxing the constraints (e.g., increase Allowed Miss %).")
        
    front = res.F.copy()
    front[:, 0] *= -problem.pool_baseline_score   # Undo negation + normalization → real selectivity
    front[:, 1] *= problem.pool_total_cost  # Undo normalization → real cost (USD)

    front_for_plotting = front.copy()

    # Initialize the Scatter plot
    plot = Scatter(
        title="Pareto Front",
        labels=["Biological Score", "Total Library Cost"]
    )
    plot.add(front_for_plotting, color="green", facecolor="none", s=40)

    # Format the cost axis (Y) with comma separators for readability
    import matplotlib.ticker as mticker
    plot.do()  # Render first so plot.ax exists
    plot.ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))

    plot.fig.savefig("final_constrained_pareto_front.png", dpi=200, bbox_inches="tight")
    print("Plot successfully saved as 'final_constrained_pareto_front.png'!")

    # Normalize the PLOTTED values (biological score vs cost) to [0, 1]
    # so the knee-point calculation matches what is visually shown on the Pareto front.
    min_vals = np.min(front_for_plotting, axis=0)
    max_vals = np.max(front_for_plotting, axis=0)
    range_vals = max_vals - min_vals
    range_vals[range_vals == 0] = 1.0  # Avoid division by zero if all solutions share a value
    norm_front = (front_for_plotting - min_vals) / range_vals

    # Utopia-point selection: find the solution with the minimum Euclidean distance
    # to the theoretical "utopia" point. In our normalized space:
    # Selectivity (index 0) should be maximized, so ideal is 1.0.
    # Cost (index 1) should be minimized, so ideal is 0.0.
    utopia_point = np.array([1.0, 0.0])
    
    # Calculate Euclidean distance from each point to the utopia point
    distances = np.linalg.norm(norm_front - utopia_point, axis=1)
    
    # Choose the solution with the minimum distance
    best_idx = np.argmin(distances)

    return best_idx, front


# ═══════════════════════════════════════════════════════════════
#  RESULT EXTRACTION & SAVING
# ═══════════════════════════════════════════════════════════════

def save_results(res, best_idx, full_df, output_file='winning_library_matrix.xlsx'):
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
    winning_matrix_df = full_df.loc[winning_smiles].copy()

    # Drop any targets that this specific library completely missed
    target_cols = [c for c in winning_matrix_df.columns if c not in ['Compound_Name', 'Molecule_ChEMBL_ID', 'InChIKey', 'SMILES', 'Price_USD_per_mg']]
    missed_targets = [c for c in target_cols if winning_matrix_df[c].max() <= 0]
    winning_matrix_df.drop(columns=missed_targets, inplace=True)

    # Reset the index so SMILES becomes a proper column
    winning_matrix_df.reset_index(inplace=True)

    # Reorder exactly to match requested output
    cols = winning_matrix_df.columns.tolist()
    meta_cols = []
    for mc in ["Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"]:
        if mc in cols:
            cols.remove(mc)
            meta_cols.append(mc)
    winning_matrix_df = winning_matrix_df[meta_cols + cols]

    winning_matrix_df.to_excel(output_file, index=False, engine='xlsxwriter')

    print(f"Success! The clean sub-matrix with pricing has been saved to: {output_file}")

    return winning_smiles, selected_drug_indices, winning_matrix_df


# ═══════════════════════════════════════════════════════════════
#  LIBRARY EVALUATION
# ═══════════════════════════════════════════════════════════════

def print_comparison(winning_matrix_df, problem):
    """Print a side-by-side comparison of the original pool vs. the optimized library."""
    # Original library stats
    pool_total_cost = problem.pool_total_cost
    pool_mean_sel = problem.pool_mean_sel
    pool_min_sel = problem.pool_min_sel
    pool_num_targets = problem.pool_num_targets

    # Winning library stats
    lib_sel_cols = [c for c in winning_matrix_df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    lib_sel_matrix = winning_matrix_df[lib_sel_cols].to_numpy(dtype=float)
    lib_prices = winning_matrix_df['Price_USD_per_mg'].to_numpy(dtype=float)

    lib_total_cost = np.sum(lib_prices)
    lib_best_per_target = np.max(lib_sel_matrix, axis=0)
    lib_mean_sel = float(np.mean(lib_best_per_target[lib_best_per_target > 0]))
    lib_min_sel = float(np.min(lib_best_per_target[lib_best_per_target > 0]))
    lib_num_targets = lib_sel_matrix.shape[1]

    cost_pct = (lib_total_cost / pool_total_cost * 100) if pool_total_cost else 0
    sel_pct = (lib_mean_sel / pool_mean_sel * 100) if pool_mean_sel else 0
    min_sel_pct = (lib_min_sel / pool_min_sel * 100) if pool_min_sel else 0
    tgt_pct = (lib_num_targets / pool_num_targets * 100) if pool_num_targets else 0
    cmp_pct = (lib_sel_matrix.shape[0] / problem.pool_num_drugs * 100) if problem.pool_num_drugs else 0

    norm_cost = 1.0 - (lib_total_cost / pool_total_cost if pool_total_cost else 0)
    norm_sel = (lib_mean_sel / pool_mean_sel if pool_mean_sel else 0)
    norm_min_sel = (lib_min_sel / pool_min_sel if pool_min_sel else 0)
    norm_tgt = (lib_num_targets / pool_num_targets if pool_num_targets else 0)
    quality_score = (0.25 * norm_cost) + (0.25 * norm_sel) + (0.25 * norm_min_sel) + (0.25 * norm_tgt)

    DIVIDER = "═" * 60

    print(f"\n{DIVIDER}")
    print(f"  📊  LIBRARY COMPARISON")
    print(f"{DIVIDER}")
    print(f"  {'Metric':<25} {'Original':>15} {'Optimized':>15}")
    print(f"  {'─'*57}")
    print(f"  {'Total Cost (USD)':<25} {pool_total_cost:>15,.2f} {lib_total_cost:>15,.2f}")
    print(f"  {'':<25} {'':>15} {f'({cost_pct:.1f}%)':>15}")
    print(f"  {'Mean Selectivity':<25} {pool_mean_sel:>15.4f} {lib_mean_sel:>15.4f}")
    print(f"  {'':<25} {'':>15} {f'({sel_pct:.1f}%)':>15}")
    print(f"  {'Min Selectivity':<25} {pool_min_sel:>15.4f} {lib_min_sel:>15.4f}")
    print(f"  {'':<25} {'':>15} {f'({min_sel_pct:.1f}%)':>15}")
    print(f"  {'Targets':<25} {pool_num_targets:>15} {lib_num_targets:>15}")
    print(f"  {'':<25} {'':>15} {f'({tgt_pct:.1f}%)':>15}")
    print(f"  {'Compounds':<25} {problem.pool_num_drugs:>15} {lib_sel_matrix.shape[0]:>15}")
    print(f"  {'':<25} {'':>15} {f'({cmp_pct:.1f}%)':>15}")
    print(f"  {'─'*57}")
    print(f"  {'Quality Score (Scalar)':<25} {'':>15} {quality_score:>15.4f}")
    print(f"{DIVIDER}\n")


# ═══════════════════════════════════════════════════════════════
#  RUN METRICS
# ═══════════════════════════════════════════════════════════════

def save_run_metrics(winning_matrix_df, problem, elapsed_time, max_gen, metrics_file='run_metrics.xlsx', mutation_multiplier=3.0, algorithm_name='NSGA-II (2 Obj)'):
    """Append a summary row for this run to a cumulative metrics Excel file.

    Creates the file if it doesn't exist, otherwise appends a new row.
    Column widths are auto-adjusted for readability.
    """
    import os

    # Original pool stats
    pool_total_cost = problem.pool_total_cost
    pool_mean_sel = problem.pool_mean_sel
    pool_min_sel = problem.pool_min_sel
    pool_num_targets = problem.pool_num_targets

    # Winning library stats
    lib_sel_cols = [c for c in winning_matrix_df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    lib_sel_matrix = winning_matrix_df[lib_sel_cols].to_numpy(dtype=float)
    lib_prices = winning_matrix_df['Price_USD_per_mg'].to_numpy(dtype=float)

    lib_total_cost = float(np.sum(lib_prices))
    lib_best_per_target = np.max(lib_sel_matrix, axis=0)
    lib_mean_sel = float(np.mean(lib_best_per_target[lib_best_per_target > 0]))
    lib_min_sel = float(np.min(lib_best_per_target[lib_best_per_target > 0]))
    lib_num_targets = lib_sel_matrix.shape[1]

    cost_pct = (lib_total_cost / pool_total_cost * 100) if pool_total_cost else 0
    sel_pct = (lib_mean_sel / pool_mean_sel * 100) if pool_mean_sel else 0
    min_sel_pct = (lib_min_sel / pool_min_sel * 100) if pool_min_sel else 0
    tgt_pct = (lib_num_targets / pool_num_targets * 100) if pool_num_targets else 0
    cmp_pct = (lib_sel_matrix.shape[0] / problem.pool_num_drugs * 100) if problem.pool_num_drugs else 0

    norm_cost = 1.0 - (lib_total_cost / pool_total_cost if pool_total_cost else 0)
    norm_sel = (lib_mean_sel / pool_mean_sel if pool_mean_sel else 0)
    norm_min_sel = (lib_min_sel / pool_min_sel if pool_min_sel else 0)
    norm_tgt = (lib_num_targets / pool_num_targets if pool_num_targets else 0)
    quality_score = (0.25 * norm_cost) + (0.25 * norm_sel) + (0.25 * norm_min_sel) + (0.25 * norm_tgt)

    if problem.use_median:
        obj_str = 'Selectivity (Median), Cost'
    else:
        obj_str = f'Selectivity ({problem.weight_mean}*mean + {problem.weight_min}*min), Cost'

    new_data = pd.DataFrame([{
        'Algorithm': algorithm_name,
        'Objectives': obj_str,
        'Constraints': f'Coverage (≤{problem.allowed_miss_pct*100:.0f}% missed)',
        'Quality Score': round(quality_score, 4),
        'Mutation Rate': f'{mutation_multiplier} / num_drugs',
        'Generations': max_gen,
        'Run Time (s)': round(elapsed_time, 2),
        'Total Cost (USD)': f"{round(lib_total_cost, 2)} ({cost_pct:.1f}%)",
        'Mean Selectivity': f"{round(lib_mean_sel, 4)} ({sel_pct:.1f}%)",
        'Min Selectivity': f"{round(lib_min_sel, 4)} ({min_sel_pct:.1f}%)",
        'Amount of Drugs': f"{lib_sel_matrix.shape[0]} ({cmp_pct:.1f}%)",
        'Amount of Targets': f"{lib_num_targets} ({tgt_pct:.1f}%)"
    }])

    if os.path.isfile(metrics_file):
        df_existing = pd.read_excel(metrics_file)
        df_combined = pd.concat([df_existing, new_data], ignore_index=True)
        df_combined.to_excel(metrics_file, index=False)
    else:
        new_data.to_excel(metrics_file, index=False)

    # Auto-adjust column widths
    try:
        from openpyxl import load_workbook
        wb = load_workbook(metrics_file)
        ws = wb.active
        for col in ws.columns:
            max_length = 0
            column = col[0].column_letter
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            ws.column_dimensions[column].width = max_length + 2
        wb.save(metrics_file)
    except ImportError:
        pass

    print(f"Run metrics appended to {metrics_file}\n")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    # Guard against pop_size being smaller than the number of smart guesses
    num_smart_guesses = 5
    if args.pop_size < num_smart_guesses:
        print(f"Warning: pop_size ({args.pop_size}) is less than the number of smart guesses ({num_smart_guesses}). Forcing pop_size={num_smart_guesses}.")
        args.pop_size = num_smart_guesses

    # 1. Load data
    df, pure_selectivity_df, selectivities, prices, smiles = load_data(args.input)

    # 2. Define problem
    problem = DrugLibraryProblem(
        selectivities, prices,
        weight_mean=args.weight_mean,
        allowed_miss_pct=args.allowed_miss_pct,
        use_median=args.use_median
    )

    # 3. Build smart initial population
    X_init = build_smart_init(selectivities, prices, pop_size=args.pop_size, seed=args.seed)

    # 4. Run optimization
    res, elapsed_time = run_optimization(
        problem, X_init,
        pop_size=args.pop_size, seed=args.seed,
        max_gen=args.max_gen, ftol=args.ftol,
        mutation_multiplier=args.mutation_multiplier,
        crossover_type=args.crossover
    )

    # 5. Select best compromise from Pareto front
    best_idx, front = select_best_solution(res, problem)

    # 6. Save winning library
    winning_drug_names, selected_drug_indices, winning_matrix_df = save_results(
        res, best_idx, df,
        output_file=args.output
    )

    # 7. Print comparison
    print_comparison(winning_matrix_df, problem)

    # 8. Save run metrics
    save_run_metrics(winning_matrix_df, problem,
                     elapsed_time=elapsed_time, max_gen=args.max_gen,
                     metrics_file=args.metrics, mutation_multiplier=args.mutation_multiplier)


if __name__ == "__main__":
    main()