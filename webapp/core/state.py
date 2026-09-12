"""Session defaults and synchronization shared by Flask endpoints."""

import time
import threading


class SessionState(dict):
    """Session data with synchronization kept out of its JSON-facing values."""

    def __init__(self, values):
        super().__init__(values)
        self.lock = threading.RLock()
        self.upload_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.dataset_revision = 0
        self.run_revision = 0
        self.selection_revision = 0


def make_session_state():
    """Create a fresh set of state dicts for a new user session."""
    return SessionState({
        "last_activity": time.time(),
        "pipeline_state": {
            "status": "idle",           # idle | running | complete | error
            "current_step": 0,
            "total_steps": 3,
            "step_label": "",
            "detail": "",
            "error": "",
            "step_summaries": {},
            "matched_targets": [],
            "unmatched_targets": [],
        },
        "dataset": {
            "selectivities": None,      # NumPy array (compounds × targets)
            "prices": None,             # NumPy array (prices)
            "num_drugs": 0,
            "num_targets": 0,
            "total_cost": 0.0,
            "matrix_metadata": None,
            "target_columns": [],
            "matrix_file": None,        # Path to saved CSV
            "ready": False,             # True once matrix is built
            "has_custom_affinity": False, # True if built from custom affinity data
        },
        "opt_state": {
            "status": "idle",           # idle | running | complete | error
            "generation": 0,
            "max_gen": 0,
            "error": "",
            "stop_requested": False,
            "history": [],
        },
        "opt_results": {
            "pareto_front": None,       # 2D array [[selectivity, cost], ...]
            "best_idx": None,
            "selected_idx": None,       # Currently selected solution index
            "comparison": None,         # dict with all comparison metrics
            "winning_matrix_df": None,
            "winning_file": None,       # Path to saved Excel
            "res_X": None,              # Solution matrix (pop × drugs) — no pymoo history
            "res_F": None,              # Objective values (pop × 2)
            "problem": None,            # DrugLibraryProblem instance
            "heatmap_cache": None,      # Cached JSON-ready heatmap dict
            "weight_mean": None,
            "weight_min": None,
        },
        "affinity_upload_state": {
            "files": {},                # filename -> { "df": df, "resolved_compounds": dict, "resolved_targets": dict, "formatted_compounds": list, "formatted_targets": list }
            "df": None,                 # Parsed DataFrame with columns [Compound_Raw, Target_Raw, Affinity]
            "resolved_compounds": {},   # raw_id -> dict(chembl_id, pref_name, inchi_key, smiles)
            "resolved_targets": {},     # raw_id -> dict(chembl_id, pref_name, gene_symbol, canonical_name)
            "num_compounds": 0,
            "num_targets": 0,
            "num_datapoints": 0,
            "unique_targets": [],
            "formatted_targets": [],
            "formatted_compounds": [],
        },
        "price_upload_state": {
            "files": {},                # filename -> { "df": df, "resolved_compounds": dict, "formatted_compounds": list }
            "df": None,
            "resolved_compounds": {},
            "formatted_compounds": [],
            "price_map": {},            # key (normalized identifier) -> price (float)
            "filename": "",
            "count": 0,
        },
    })
