"""Selected-row extraction and JSON-ready result metrics, independent of Flask."""

import warnings
import numpy as np
import pandas as pd
from .algorithm import extract_selected_library
from .records import clean_str, target_columns


def build_comparison(winning_matrix_df, problem, has_custom_affinity=False):
    """Build comparison metrics dict (mirroring print_comparison logic)."""
    pool_total_cost = problem.pool_total_cost
    pool_mean_sel = problem.pool_mean_sel
    pool_min_sel = problem.pool_min_sel
    pool_num_targets = problem.pool_num_targets

    lib_sel_cols = target_columns(winning_matrix_df)
    lib_sel_matrix = winning_matrix_df[lib_sel_cols].to_numpy(dtype=float)
    lib_prices = winning_matrix_df["Price_USD_per_mg"].to_numpy(dtype=float)

    lib_total_cost = float(np.sum(lib_prices))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        lib_best_per_target = np.nanmax(lib_sel_matrix, axis=0)
    lib_best_per_target = np.nan_to_num(lib_best_per_target, nan=-1.0)

    positive_lib_scores = lib_best_per_target[lib_best_per_target > 0]
    lib_mean_sel = float(np.mean(positive_lib_scores)) if len(positive_lib_scores) > 0 else 0.0
    lib_min_sel = float(np.min(positive_lib_scores)) if len(positive_lib_scores) > 0 else 0.0
    lib_num_targets = lib_sel_matrix.shape[1]
    lib_num_drugs = lib_sel_matrix.shape[0]

    # Rounded metrics matching the displayed table values
    pool_cost_val = int(round(pool_total_cost))
    lib_cost_val = int(round(lib_total_cost))
    pool_mean_val = round(pool_mean_sel, 2)
    lib_mean_val = round(lib_mean_sel, 2)
    pool_min_val = round(pool_min_sel, 2)
    lib_min_val = round(lib_min_sel, 2)
    pool_targets_val = pool_num_targets
    lib_targets_val = lib_num_targets
    pool_drugs_val = problem.pool_num_drugs
    lib_drugs_val = lib_num_drugs

    cost_pct = (lib_cost_val / pool_cost_val * 100) if pool_cost_val else 0
    sel_pct = (lib_mean_val / pool_mean_val * 100) if pool_mean_val else 0
    min_sel_pct = (lib_min_val / pool_min_val * 100) if pool_min_val else 0
    tgt_pct = (lib_targets_val / pool_targets_val * 100) if pool_targets_val else 0
    cmp_pct = (lib_drugs_val / pool_drugs_val * 100) if pool_drugs_val else 0

    compounds_list = []
    for idx, row in winning_matrix_df.iterrows():
        name_str = clean_str(row.get("Compound_Name", ""))
        inchikey_str = clean_str(row.get("InChIKey", ""))
        chembl_str = clean_str(row.get("Molecule_ChEMBL_ID", ""))
        price = row.get("Price_USD_per_mg", 0.0)

        compounds_list.append({
            "name": name_str,
            "inchikey": inchikey_str,
            "chembl_id": chembl_str,
            "price": float(price) if pd.notna(price) else 0.0
        })

    return {
        "has_custom_affinity": has_custom_affinity,
        "pool": {
            "total_cost": pool_cost_val,
            "mean_selectivity": pool_mean_val,
            "min_selectivity": pool_min_val,
            "num_targets": pool_targets_val,
            "num_drugs": pool_drugs_val,
        },
        "library": {
            "total_cost": lib_cost_val,
            "mean_selectivity": lib_mean_val,
            "min_selectivity": lib_min_val,
            "num_targets": lib_targets_val,
            "num_drugs": lib_drugs_val,
            "compounds": compounds_list,
        },
        "percentages": {
            "cost": round(cost_pct, 1),
            "mean_selectivity": round(sel_pct, 1),
            "min_selectivity": round(min_sel_pct, 1),
            "targets": round(tgt_pct, 1),
            "drugs": round(cmp_pct, 1),
        },

    }


def extract_compound_labels(df):
    """Extract preferred compound labels for heatmap (Molecule_ChEMBL_ID -> InChIKey -> Compound_Name -> Index)."""
    if "Molecule_ChEMBL_ID" in df.columns:
        labels = []
        for i, val in enumerate(df["Molecule_ChEMBL_ID"]):
            val_str = clean_str(val)
            if val_str:
                labels.append(val_str)
            elif "InChIKey" in df.columns and clean_str(df["InChIKey"].iloc[i]):
                labels.append(clean_str(df["InChIKey"].iloc[i]))
            elif "Compound_Name" in df.columns and clean_str(df["Compound_Name"].iloc[i]):
                labels.append(clean_str(df["Compound_Name"].iloc[i]))
            else:
                labels.append(f"Compound {i+1}")
        return labels
    elif "InChIKey" in df.columns:
        return [clean_str(x) or f"Compound {i+1}" for i, x in enumerate(df["InChIKey"])]
    elif "Compound_Name" in df.columns:
        return [clean_str(x) or f"Compound {i+1}" for i, x in enumerate(df["Compound_Name"])]
    else:
        return [str(x) for x in df.index.tolist()]


def build_heatmap_cache(df, resolve_target_info):
    """Pre-compute the heatmap JSON dict so /api/heatmap-data is instant."""
    sel_cols = target_columns(df)
    target_symbols, target_names = resolve_target_info(sel_cols)
    return {
        "matrix": df[sel_cols].astype(object).where(pd.notna(df[sel_cols]), None).values.tolist(),
        "compounds": extract_compound_labels(df),
        "targets": target_symbols,
        "target_names": target_names,
    }


def prepare_selected_library(selection, metadata, scores, targets, matrix_file):
    indices = np.flatnonzero(selection > 0.5)
    if metadata is not None:
        # Only selected rows are copied; metadata is stored once with the dataset.
        frame = metadata.iloc[indices].reset_index(drop=True)
        frame = pd.concat([frame, pd.DataFrame(scores[indices], columns=targets)], axis=1)
        frame = frame.set_index("SMILES")
        _, _, winning = extract_selected_library(frame, np.arange(len(frame)))
    else:
        # Compatibility for datasets loaded without retained metadata.
        frame = pd.read_csv(matrix_file).set_index("SMILES")
        _, _, winning = extract_selected_library(frame, indices)
    return winning
