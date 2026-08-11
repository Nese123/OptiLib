"""
Drug Library Optimization — Flask Web Application

Full pipeline: Upload targets → ChEMBL query → selectivity matrix → NSGA-II → results dashboard.
"""

import os
import sys
import json
import threading
import warnings
import tempfile
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from pymoo.core.callback import Callback

# ═══════════════════════════════════════════════════════════════
#  PATH SETUP — resolve project root so imports work
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = PROJECT_ROOT / "database"
MOLPRICE_DIR = PROJECT_ROOT / "MolPrice"

sys.path.insert(0, str(MOLPRICE_DIR))

from bin.numpy_predict import NumpyFingerprints
from core.selectivity import generate_selectivity_matrix
from core.algorithm import (
    DrugLibraryProblem,
    build_smart_init,
    run_optimization,
    select_best_solution,
    save_results,
)

# Suppress sklearn version mismatch warning from MolPrice's pickled scaler
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit

# ═══════════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════════

# Pipeline state
pipeline_state = {
    "status": "idle",           # idle | running | complete | error
    "current_step": 0,
    "total_steps": 5,
    "step_label": "",
    "detail": "",
    "error": "",
    "step_summaries": {},
    "matched_targets": [],
    "unmatched_targets": [],
}

class StopOptimization(Exception):
    pass

class WebappCallback(Callback):
    def notify(self, algorithm):
        with _lock:
            opt_state["generation"] = algorithm.n_gen
            if opt_state.get("stop_requested"):
                raise StopOptimization("Optimization stopped by user")

class _LightResult:
    """Minimal stand-in for pymoo Result so save_results(res, idx, ...) still works."""
    __slots__ = ("X", "F")
    def __init__(self, X, F):
        self.X = X
        self.F = F

# Dataset built by pipeline
dataset = {
    "selectivities": None,      # NumPy array (compounds × targets)
    "prices": None,             # NumPy array (prices)
    "smiles": None,             # NumPy array (SMILES)
    "num_drugs": 0,
    "num_targets": 0,
    "total_cost": 0.0,
    "matrix_file": None,        # Path to saved CSV
    "ready": False,             # True once matrix is built
}

# Optimization state
opt_state = {
    "status": "idle",           # idle | running | complete | error
    "generation": 0,
    "max_gen": 0,
    "error": "",
    "stop_requested": False,
}

# Optimization results
opt_results = {
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
}

# Thread lock for state access
_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Pages
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/optimize")
def tool():
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Target Upload & Validation
# ═══════════════════════════════════════════════════════════════

@app.route("/api/upload-targets", methods=["POST"])
def upload_targets():
    """Accept CSV/Excel with target names/IDs, validate against ChEMBL."""
    import sqlite3

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    all_targets = []
    for file in files:
        if file.filename == "":
            continue

        try:
            filename = file.filename.lower()
            if filename.endswith(".csv"):
                df = pd.read_csv(file)
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(file)
            else:
                return jsonify({"error": f"Unsupported file type for {file.filename}. Use CSV or Excel (.xlsx)."}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to read {file.filename}: {str(e)}"}), 400

        target_col = None
        for col in df.columns:
            if col.strip().lower() in ("target", "target_name", "targets", "target_names"):
                target_col = col
                break

        if target_col is None:
            if len(df.columns) == 1:
                target_col = df.columns[0]
            else:
                return jsonify({
                    "error": f"Could not find Target column in {file.filename}."
                }), 400

        file_targets = df[target_col].dropna().astype(str).str.strip().tolist()
        all_targets.extend(file_targets)

    # Get unique targets while preserving order
    input_targets = list(dict.fromkeys(all_targets))
    
    if not input_targets:
        return jsonify({"error": "No targets found in the files"}), 400

    db_path = str(DATABASE_DIR / "chembl_36.db")
    matched_chembl_ids = set()
    matched = []
    unmatched = []

    try:
        with sqlite3.connect(db_path) as conn:
            placeholders = ",".join(["?"] * len(input_targets))
            query = f"""
                SELECT DISTINCT td.chembl_id, td.pref_name, 
                       cs.accession, csy.component_synonym
                FROM target_dictionary td
                LEFT JOIN target_components tc ON td.tid = tc.tid
                LEFT JOIN component_sequences cs ON tc.component_id = cs.component_id
                LEFT JOIN component_synonyms csy ON cs.component_id = csy.component_id
                WHERE (
                    td.chembl_id COLLATE NOCASE IN ({placeholders}) OR
                    td.pref_name COLLATE NOCASE IN ({placeholders}) OR
                    cs.accession COLLATE NOCASE IN ({placeholders}) OR
                    (csy.component_synonym COLLATE NOCASE IN ({placeholders}) 
                     AND csy.syn_type IN ('GENE_SYMBOL', 'UNIPROT', 'EC_NUMBER'))
                )
                AND td.target_type = 'SINGLE PROTEIN'
                AND td.organism = 'Homo sapiens'
            """
            params = input_targets * 4
            rows = conn.execute(query, params).fetchall()
            
            # Map input to canonical ChEMBL IDs and Names
            for target_in in input_targets:
                target_in_lower = str(target_in).lower()
                found = False
                for r in rows:
                    cid, name, acc, syn = r
                    if (target_in_lower == str(cid).lower() or 
                        target_in_lower == str(name).lower() or
                        (acc and target_in_lower == str(acc).lower()) or
                        (syn and target_in_lower == str(syn).lower())):
                        found = True
                        if cid not in matched_chembl_ids:
                            matched_chembl_ids.add(cid)
                            matched.append(f"{target_in} -> {cid} ({name})")
                        break
                if not found:
                    unmatched.append(target_in)

    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    chembl_ids = list(matched_chembl_ids)

    with _lock:
        pipeline_state["matched_targets"] = matched
        pipeline_state["unmatched_targets"] = unmatched

    return jsonify({
        "total": len(input_targets),
        "matched": matched,
        "unmatched": unmatched,
        "chembl_ids": chembl_ids,
    })


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Pipeline (Build Matrix)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/build-matrix", methods=["POST"])
def build_matrix():
    """Launch the full pipeline in a background thread."""
    with _lock:
        if pipeline_state["status"] == "running":
            return jsonify({"error": "Pipeline is already running"}), 409

    data = request.get_json(force=True)
    chembl_ids = data.get("chembl_ids", [])
    selectivity_threshold = float(data.get("selectivity_threshold", 0.5))
    remove_targets = bool(data.get("remove_targets", True))
    matched_count = int(data.get("matched_count", len(chembl_ids)))

    if not chembl_ids:
        return jsonify({"error": "No matched targets provided"}), 400

    # Reset states
    with _lock:
        pipeline_state.update({
            "status": "running",
            "current_step": 0,
            "step_label": "Starting...",
            "detail": "",
            "error": "",
            "step_summaries": {},
        })
        opt_state.update({"status": "idle", "generation": 0, "error": ""})

    thread = threading.Thread(
        target=_run_pipeline,
        args=(chembl_ids, selectivity_threshold, remove_targets, matched_count),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/pipeline-status")
def pipeline_status():
    with _lock:
        return jsonify({**pipeline_state})


def _update_pipeline(step, label, detail="", summary=None):
    with _lock:
        pipeline_state["current_step"] = step
        pipeline_state["step_label"] = label
        pipeline_state["detail"] = detail
        if summary is not None:
            pipeline_state["step_summaries"][step] = summary


def _run_pipeline(chembl_ids, selectivity_threshold, remove_targets=True, matched_count=0):
    """Full pipeline: ChEMBL → pChEMBL rescue → selectivity → prices → save."""
    import sqlite3

    try:
        # ─────────────────────────────────────────────
        # Step 1: Searching for selective compounds
        # ─────────────────────────────────────────────
        _update_pipeline(1, "Searching for selective compounds...", f"Querying database for compounds active against {matched_count} targets... (this may take a few minutes)")

        db_path = str(DATABASE_DIR / "chembl_36.db")

        # Build WHERE clause
        id_ph = ",".join(["?"] * len(chembl_ids))
        where_targets = f"td.chembl_id IN ({id_ph})"
        params = [cid.upper() for cid in chembl_ids]

        # Get the actual names of the uploaded targets
        query0 = f"""
            SELECT DISTINCT COALESCE(td.pref_name, td.chembl_id) AS Target_Name
            FROM target_dictionary td
            WHERE ({where_targets})
        """
        with sqlite3.connect(db_path) as conn:
            uploaded_targets_df = pd.read_sql_query(query0, conn, params=params)
        uploaded_target_names = uploaded_targets_df['Target_Name'].tolist()

        # Query 1: Fetch selective and potent compounds in a single unified query
        query1 = f"""
            SELECT
                cts.molregno AS Clean_Molregno,
                md.chembl_id AS Molecule_ChEMBL_ID,
                md.pref_name AS Compound_Name,
                cs.canonical_smiles AS SMILES,
                cs.standard_inchi_key AS InChIKey,
                cp.full_mwt AS MW,
                td.chembl_id AS Target_ChEMBL_ID,
                COALESCE(td.pref_name, td.chembl_id) AS Target_Name,
                cts.selectivity_score AS Selectivity_Score
            FROM compound_target_selectivity cts
            JOIN target_dictionary td ON cts.tid = td.tid
            JOIN molecule_dictionary md ON cts.molregno = md.molregno
            LEFT JOIN compound_structures cs ON cts.molregno = cs.molregno
            LEFT JOIN compound_properties cp ON cts.molregno = cp.molregno
            WHERE ({where_targets})
              AND cts.molregno IN (
                  SELECT molregno 
                  FROM compound_target_selectivity 
                  WHERE selectivity_score > {selectivity_threshold}
              )
              AND cts.molregno IN (
                  SELECT DISTINCT COALESCE(mh.parent_molregno, md2.molregno)
                  FROM target_dictionary td2
                  JOIN assays ass ON td2.tid = ass.tid
                  JOIN activities act ON act.assay_id = ass.assay_id
                  JOIN molecule_dictionary md2 ON act.molregno = md2.molregno
                  LEFT JOIN molecule_hierarchy mh ON md2.molregno = mh.molregno
                  WHERE ({where_targets.replace('td.', 'td2.')})
                      AND td2.target_type = 'SINGLE PROTEIN'
                      AND td2.organism = 'Homo sapiens'
                      AND ass.confidence_score IN (8, 9)
                      AND act.pchembl_value > 6.0
              );
        """
        
        with sqlite3.connect(db_path) as conn:
            # Supply params twice: once for outer WHERE, once for subquery WHERE
            chunks = []
            compounds_so_far = set()
            for chunk in pd.read_sql_query(query1, conn, params=params + params, chunksize=1000):
                chunks.append(chunk)
                compounds_so_far.update(chunk['Clean_Molregno'])
                _update_pipeline(1, "Searching for selective compounds...",
                                 f"Found {len(compounds_so_far)} compounds so far...")
            
            if chunks:
                df_raw = pd.concat(chunks, ignore_index=True)
            else:
                df_raw = pd.DataFrame()

        compounds_found_initial = df_raw['Clean_Molregno'].nunique() if not df_raw.empty else 0
        if compounds_found_initial == 0:
            raise ValueError("No highly active and selective compounds found for the provided targets.")
        df_raw["Target_Name"] = df_raw["Target_Name"].fillna(df_raw["Target_ChEMBL_ID"])
        _update_pipeline(1, "Searching for selective compounds...",
                         f"Fetched selectivity scores for {len(df_raw)} records covering {compounds_found_initial} compounds")

        df_raw["SMILES"] = df_raw["SMILES"].astype(str).replace("nan", "Missing_SMILES")
        
        compounds_found_final = df_raw["SMILES"].nunique()

        smiles_mapping = (
            df_raw[["SMILES", "InChIKey", "MW", "Compound_Name", "Molecule_ChEMBL_ID"]]
            .dropna(subset=["SMILES"])
            .drop_duplicates(subset=["SMILES"])
        )

        # Pivot to Compound × Target selectivity matrix directly!
        selectivity_df = df_raw.pivot_table(
            index=["SMILES"],
            columns="Target_Name",
            values="Selectivity_Score",
            aggfunc="max"
        )
        del df_raw  # Free memory — no longer needed

        new_drugs, new_targets = selectivity_df.shape

        if remove_targets:
            clean_df = selectivity_df.loc[
                (selectivity_df.max(axis=1) >= selectivity_threshold),
                (selectivity_df.max(axis=0) >= selectivity_threshold),
            ]
        else:
            clean_df = selectivity_df.loc[
                (selectivity_df.max(axis=1) >= selectivity_threshold),
                :
            ]

        del selectivity_df  # Free memory — no longer needed
        final_drugs, final_targets = clean_df.shape
        pruned_low_sel = new_drugs - final_drugs
        _update_pipeline(1, "Searching for selective compounds...",
                         f"Pruned {pruned_low_sel} compounds with low selectivity: {final_drugs} compounds remaining",
                         summary=f"Found {final_drugs} compounds active against {matched_count} targets")

        if final_drugs == 0 or final_targets == 0:
            raise ValueError("No compounds/targets survived selectivity pruning. Try a lower threshold.")

        # Filter missing SMILES
        final_export_df = clean_df.reset_index()
        del clean_df  # Free memory — no longer needed
        final_export_df = final_export_df[final_export_df["SMILES"] != "Missing_SMILES"]
        final_export_df = final_export_df.dropna(subset=["SMILES"])

        if "Compound_Name" in final_export_df.columns:
            final_export_df = final_export_df.drop(columns=["Compound_Name"])
        if "Molecule_ChEMBL_ID" in final_export_df.columns:
            final_export_df = final_export_df.drop(columns=["Molecule_ChEMBL_ID"])

        final_export_df = final_export_df.merge(smiles_mapping, on="SMILES", how="left")

        # ─────────────────────────────────────────────
        # Step 2: Getting price data
        # ─────────────────────────────────────────────
        _update_pipeline(2, "Getting price data...", "Querying molport.db for 1mg prices")

        inchikeys = final_export_df["InChIKey"].dropna().unique().tolist()
        molport_dict = {}
        molport_source_dict = {}
        if inchikeys:
            molport_db = str(DATABASE_DIR / "molport.db")
            try:
                with sqlite3.connect(molport_db) as conn:
                    molport_dfs = []
                    mp_chunk_size = 30000
                    for i in range(0, len(inchikeys), mp_chunk_size):
                        chunk = inchikeys[i:i + mp_chunk_size]
                        placeholders = ",".join(["?"] * len(chunk))
                        molport_query = f"SELECT INCHIKEY, PRICE_1MG, MOLPORTID FROM compounds WHERE INCHIKEY IN ({placeholders})"
                        molport_dfs.append(pd.read_sql_query(molport_query, conn, params=chunk))
                    
                    if molport_dfs:
                        molport_df = pd.concat(molport_dfs, ignore_index=True)
                        
                        molport_source_df = molport_df.drop_duplicates(subset=["INCHIKEY"])
                        molport_source_dict = dict(zip(molport_source_df["INCHIKEY"], molport_source_df["MOLPORTID"]))

                        molport_df = molport_df.groupby("INCHIKEY")["PRICE_1MG"].median().reset_index()
                        molport_dict = dict(zip(molport_df["INCHIKEY"], molport_df["PRICE_1MG"]))
            except Exception as e:
                _update_pipeline(2, "Getting price data...", f"MolPort query warning: {e}")

        final_export_df["Molport_Price"] = final_export_df["InChIKey"].map(molport_dict)
        final_export_df["Molport_Source"] = final_export_df["InChIKey"].map(molport_source_dict)
        
        found_count = final_export_df["Molport_Price"].notna().sum()
        molprice_approx_count = (final_export_df["Molport_Source"] == "MolPrice").sum()
        molport_direct_count = found_count - molprice_approx_count
        
        _update_pipeline(2, "Getting price data...",
                         f"Found prices for {found_count}/{len(final_export_df)} compounds in database (MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})")

        # ─────────────────────────────────────────────
        # Handle missing prices
        # ─────────────────────────────────────────────
        missing_price_mask = final_export_df["Molport_Price"].isna()
        missing_count = int(missing_price_mask.sum())

        if missing_count > 0:
            _update_pipeline(2, "Getting price data...",
                             f"Predicting prices for {missing_count} compounds not found in database using MolPrice",
                             summary=f"Found prices. Predicting {missing_count} with MolPrice.")

            # Use MolPrice to predict prices from SMILES for missing compounds
            try:
                molprice_weights = str(MOLPRICE_DIR / "models" / "Numpy" / "MP_Morgan_hybrid.pkl")
                molprice_model = NumpyFingerprints(weights_path=molprice_weights)

                missing_smiles = final_export_df.loc[missing_price_mask, "SMILES"].tolist()
                predicted_prices = molprice_model.predict_batch_from_smiles(missing_smiles)
                predicted_prices = [p[0] if hasattr(p, '__len__') else float(p) for p in predicted_prices]

                # Assign predicted prices to missing entries
                final_prices = final_export_df["Molport_Price"].copy()
                final_prices.loc[missing_price_mask] = predicted_prices
                final_prices = final_prices.values

                _update_pipeline(2, "Getting price data...",
                                 f"MolPrice predicted prices for {missing_count} compounds. DB had {molport_direct_count} MolPort, {molprice_approx_count} MolPrice approx.",
                                 summary=f"Predicted {missing_count} prices. DB: {molport_direct_count} MolPort, {molprice_approx_count} MolPrice approx.")
            except Exception as e:
                _update_pipeline(2, "Getting price data...",
                                 f"MolPrice prediction failed ({e}), using median fallback. DB had {molport_direct_count} MolPort, {molprice_approx_count} MolPrice approx.",
                                 summary=f"Prediction failed, used fallback. DB: {molport_direct_count} MolPort, {molprice_approx_count} MolPrice approx.")
                fallback = final_export_df["Molport_Price"].median()
                if pd.isna(fallback):
                    fallback = 100.0
                final_prices = np.where(
                    missing_price_mask,
                    fallback,
                    final_export_df["Molport_Price"]
                )
        else:
            _update_pipeline(2, "Getting price data...", 
                             f"All prices found in database (MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})", 
                             summary=f"All prices found. MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count}")
            final_prices = final_export_df["Molport_Price"].values
        final_export_df["Price_USD_per_mg"] = final_prices
        final_export_df.drop(columns=["MW", "Molport_Price", "Molport_Source"], inplace=True, errors="ignore")

        # Drop rows with NaN prices
        final_export_df = final_export_df.dropna(subset=["Price_USD_per_mg"])

        # Reorder columns
        cols = final_export_df.columns.tolist()
        meta_cols = []
        for mc in ["Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"]:
            if mc in cols:
                cols.remove(mc)
                meta_cols.append(mc)
        final_export_df = final_export_df[meta_cols + cols]

        # Save matrix as CSV (fast) — Excel generated lazily on download
        _update_pipeline(3, "Saving matrix...", "Saving CSV matrix...")
        
        output_dir = PROJECT_ROOT / "webapp" / "output"
        output_dir.mkdir(exist_ok=True)
        matrix_file = str(output_dir / "selectivity_matrix.csv")
        final_export_df.to_csv(matrix_file, index=False)

        # Store in global dataset
        target_cols = [c for c in final_export_df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"}]
        with _lock:
            dataset["selectivities"] = final_export_df[target_cols].to_numpy(dtype=float)
            dataset["prices"] = final_export_df["Price_USD_per_mg"].to_numpy(dtype=float)
            dataset["smiles"] = final_export_df["SMILES"].to_numpy()
            dataset["num_drugs"] = len(final_export_df)
            dataset["num_targets"] = len(target_cols)
            dataset["total_cost"] = float(np.sum(dataset["prices"]))
            dataset["matrix_file"] = matrix_file
            dataset["ready"] = True

            pipeline_state["status"] = "complete"
            pipeline_state["detail"] = f"Matrix ready: {dataset['num_drugs']} compounds × {dataset['num_targets']} targets"
        del final_export_df  # Free DataFrame — numpy arrays and CSV are sufficient

    except Exception as e:
        with _lock:
            pipeline_state["status"] = "error"
            pipeline_state["error"] = str(e)
            pipeline_state["detail"] = ""


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Dataset Info
# ═══════════════════════════════════════════════════════════════

@app.route("/api/dataset-info")
def dataset_info():
    with _lock:
        return jsonify({
            "num_drugs": dataset["num_drugs"],
            "num_targets": dataset["num_targets"],
            "total_cost": round(dataset["total_cost"], 2),
            "ready": dataset["ready"],
        })


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Optimization
# ═══════════════════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
def run_optimization_route():
    """Launch NSGA-II optimization in a background thread."""
    with _lock:
        if opt_state["status"] == "running":
            return jsonify({"error": "Optimization is already running"}), 409
        if not dataset["ready"]:
            return jsonify({"error": "No dataset loaded. Build the matrix first."}), 400

    data = request.get_json(force=True)
    weight_mean = float(data.get("weight_mean", 0.5))
    allowed_miss_pct = float(data.get("allowed_miss_pct", 0.04))
    mutation_multiplier = float(data.get("mutation_multiplier", 4.0))
    pop_size = int(data.get("pop_size", 100))
    max_gen = int(data.get("max_gen", 300))

    # Clamp values
    pop_size = max(pop_size, 5)
    max_gen = max(max_gen, 10)

    with _lock:
        opt_state.update({
            "status": "running",
            "generation": 0,
            "max_gen": max_gen,
            "error": "",
            "stop_requested": False,
        })

    thread = threading.Thread(
        target=_run_nsga2,
        args=(weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/status")
def optimization_status():
    with _lock:
        return jsonify({**opt_state})


@app.route("/api/reset", methods=["POST"])
def reset_state():
    with _lock:
        pipeline_state.update({
            "status": "idle",
            "current_step": 0,
            "step_label": "",
            "detail": "",
            "error": "",
            "step_summaries": {},
            "matched_targets": [],
            "unmatched_targets": [],
        })
        dataset.update({
            "selectivities": None,
            "prices": None,
            "smiles": None,
            "num_drugs": 0,
            "num_targets": 0,
            "total_cost": 0.0,
            "matrix_file": None,
            "ready": False,
        })
        opt_state.update({
            "status": "idle",
            "generation": 0,
            "max_gen": 0,
            "error": "",
        })
        opt_results.update({
            "pareto_front": None,
            "best_idx": None,
            "selected_idx": None,
            "comparison": None,
            "winning_matrix_df": None,
            "winning_file": None,
            "res_X": None,
            "res_F": None,
            "problem": None,
            "heatmap_cache": None,
        })
    # Clean up cached Excel file so stale data isn't served
    xlsx_cache = str(PROJECT_ROOT / "webapp" / "output" / "selectivity_matrix.xlsx")
    if os.path.isfile(xlsx_cache):
        os.remove(xlsx_cache)

    return jsonify({"status": "reset"})


@app.route("/api/reset-opt", methods=["POST"])
def reset_opt_state():
    with _lock:
        opt_state.update({
            "status": "idle",
            "generation": 0,
            "error": "",
            "stop_requested": False,
        })
    return jsonify({"status": "reset"})

@app.route("/api/stop-opt", methods=["POST"])
def stop_opt_state():
    with _lock:
        if opt_state["status"] == "running":
            opt_state["stop_requested"] = True
    return jsonify({"status": "stop_requested"})


def _run_nsga2(weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen):
    """Run NSGA-II optimization using the loaded dataset."""
    try:
        with _lock:
            # Use direct references — these arrays are read-only during optimization
            selectivities = dataset["selectivities"]
            prices = dataset["prices"]

        # Create problem (stores its own reference to selectivities/prices)
        problem = DrugLibraryProblem(
            selectivities, prices,
            weight_mean=weight_mean,
            allowed_miss_pct=allowed_miss_pct,
        )

        # Build initial population
        X_init = build_smart_init(selectivities, prices, pop_size=pop_size, seed=1)

        # Drop local references — problem holds what it needs
        del selectivities, prices

        # Run optimization
        res, elapsed_time = run_optimization(
            problem, X_init,
            pop_size=pop_size, seed=1,
            max_gen=max_gen, ftol=0.0025,
            mutation_multiplier=mutation_multiplier,
            crossover_type="hux",
            callback=WebappCallback()
        )
        del X_init  # Free init population memory

        # Select best solution
        best_idx, front = select_best_solution(res, problem)

        # Extract lightweight data from res, then free pymoo's full result
        # (pymoo keeps algorithm state, history, deep copies, etc.)
        res_X = res.X.copy()
        res_F = res.F.copy()
        del res

        # Load matrix from CSV on demand (avoids keeping large DataFrame resident)
        with _lock:
            matrix_file = dataset["matrix_file"]
        matrix_df_indexed = pd.read_csv(matrix_file).set_index("SMILES")

        # Wrap in lightweight result for save_results compatibility
        res_light = _LightResult(res_X, res_F)

        # Save results
        output_dir = PROJECT_ROOT / "webapp" / "output"
        output_dir.mkdir(exist_ok=True)
        winning_file = str(output_dir / "winning_library.xlsx")

        winning_smiles, selected_drug_indices, winning_matrix_df = save_results(
            res_light, best_idx, matrix_df_indexed,
            output_file=winning_file,
        )
        del matrix_df_indexed  # Free immediately

        # Calculate comparison metrics
        comparison = _build_comparison(winning_matrix_df, problem)

        # Store results
        with _lock:
            opt_results["pareto_front"] = front.tolist()
            opt_results["best_idx"] = int(best_idx)
            opt_results["selected_idx"] = int(best_idx)
            opt_results["comparison"] = comparison
            opt_results["winning_matrix_df"] = winning_matrix_df
            opt_results["winning_file"] = winning_file
            opt_results["res_X"] = res_X
            opt_results["res_F"] = res_F
            opt_results["problem"] = problem
            opt_results["heatmap_cache"] = _build_heatmap_cache(winning_matrix_df)

            opt_state["status"] = "complete"

    except StopOptimization as e:
        with _lock:
            opt_state["status"] = "stopped"
            opt_state["error"] = str(e)
    except Exception as e:
        with _lock:
            opt_state["status"] = "error"
            opt_state["error"] = str(e)
        traceback.print_exc()


def _build_comparison(winning_matrix_df, problem):
    """Build comparison metrics dict (mirroring print_comparison logic)."""
    pool_total_cost = problem.pool_total_cost
    pool_mean_sel = problem.pool_mean_sel
    pool_min_sel = problem.pool_min_sel
    pool_num_targets = problem.pool_num_targets

    lib_sel_cols = [c for c in winning_matrix_df.columns
                    if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    lib_sel_matrix = winning_matrix_df[lib_sel_cols].to_numpy(dtype=float)
    lib_prices = winning_matrix_df["Price_USD_per_mg"].to_numpy(dtype=float)

    lib_total_cost = float(np.sum(lib_prices))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        lib_best_per_target = np.nanmax(lib_sel_matrix, axis=0)
    lib_best_per_target = np.nan_to_num(lib_best_per_target, nan=-1.0)
    
    positive_lib_scores = lib_best_per_target[lib_best_per_target > 0]
    lib_mean_sel = float(np.mean(positive_lib_scores)) if len(positive_lib_scores) > 0 else 0.0
    lib_min_sel = float(np.min(positive_lib_scores)) if len(positive_lib_scores) > 0 else 0.0
    lib_num_targets = lib_sel_matrix.shape[1]
    lib_num_drugs = lib_sel_matrix.shape[0]

    cost_pct = (lib_total_cost / pool_total_cost * 100) if pool_total_cost else 0
    sel_pct = (lib_mean_sel / pool_mean_sel * 100) if pool_mean_sel else 0
    min_sel_pct = (lib_min_sel / pool_min_sel * 100) if pool_min_sel else 0
    tgt_pct = (lib_num_targets / pool_num_targets * 100) if pool_num_targets else 0
    cmp_pct = (lib_num_drugs / problem.pool_num_drugs * 100) if problem.pool_num_drugs else 0



    compounds_list = []
    for idx, row in winning_matrix_df.iterrows():
        name = row.get("Compound_Name", "Unknown")
        inchikey = row.get("InChIKey", "Unknown")
        chembl_id = row.get("Molecule_ChEMBL_ID", "Unknown")
        price = row.get("Price_USD_per_mg", 0.0)
        
        compounds_list.append({
            "name": str(name) if not pd.isna(name) else "Unknown",
            "inchikey": str(inchikey) if not pd.isna(inchikey) else "Unknown",
            "chembl_id": str(chembl_id) if not pd.isna(chembl_id) else "Unknown",
            "price": float(price) if not pd.isna(price) else 0.0
        })

    return {
        "pool": {
            "total_cost": int(round(pool_total_cost)),
            "mean_selectivity": round(pool_mean_sel, 2),
            "min_selectivity": round(pool_min_sel, 2),
            "num_targets": pool_num_targets,
            "num_drugs": problem.pool_num_drugs,
        },
        "library": {
            "total_cost": int(round(lib_total_cost)),
            "mean_selectivity": round(lib_mean_sel, 2),
            "min_selectivity": round(lib_min_sel, 2),
            "num_targets": lib_num_targets,
            "num_drugs": lib_num_drugs,
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


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Results
# ═══════════════════════════════════════════════════════════════

@app.route("/api/results")
def get_results():
    with _lock:
        if opt_results["comparison"] is None:
            return jsonify({"error": "No results available yet"}), 404
        return jsonify({
            "comparison": opt_results["comparison"],
            "best_idx": opt_results["best_idx"],
        })


@app.route("/api/pareto-data")
def pareto_data():
    with _lock:
        if opt_results["pareto_front"] is None:
            return jsonify({"error": "No Pareto data available"}), 404
        front = opt_results["pareto_front"]
        best = opt_results["best_idx"]
        selected = opt_results.get("selected_idx", best)
    return jsonify({
        "points": front,
        "best_idx": best,
        "selected_idx": selected,
    })


@app.route("/api/select-solution", methods=["POST"])
def select_solution():
    """Switch the active solution to a different Pareto front point."""
    data = request.get_json()
    idx = data.get("index")
    if idx is None:
        return jsonify({"error": "Missing 'index' parameter"}), 400

    with _lock:
        res_X = opt_results.get("res_X")
        problem = opt_results.get("problem")
        matrix_file = dataset.get("matrix_file")

    if res_X is None or problem is None or matrix_file is None:
        return jsonify({"error": "No optimization results available"}), 404

    num_solutions = res_X.shape[0]
    if idx < 0 or idx >= num_solutions:
        return jsonify({"error": f"Index {idx} out of range (0-{num_solutions - 1})"}), 400

    try:
        # Load matrix from CSV on demand (avoids keeping large DataFrame resident)
        matrix_df_indexed = pd.read_csv(matrix_file).set_index("SMILES")
        res_light = _LightResult(res_X, opt_results.get("res_F"))

        output_dir = PROJECT_ROOT / "webapp" / "output"
        output_dir.mkdir(exist_ok=True)
        winning_file = str(output_dir / "winning_library.xlsx")

        winning_smiles, selected_drug_indices, winning_matrix_df = save_results(
            res_light, idx, matrix_df_indexed,
            output_file=winning_file,
        )
        del matrix_df_indexed  # Free immediately

        comparison = _build_comparison(winning_matrix_df, problem)

        with _lock:
            opt_results["selected_idx"] = int(idx)
            opt_results["comparison"] = comparison
            opt_results["winning_matrix_df"] = winning_matrix_df
            opt_results["winning_file"] = winning_file
            opt_results["heatmap_cache"] = _build_heatmap_cache(winning_matrix_df)

        return jsonify({"ok": True, "selected_idx": idx})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _build_heatmap_cache(df):
    """Pre-compute the heatmap JSON dict so /api/heatmap-data is instant."""
    sel_cols = [c for c in df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    return {
        "matrix": df[sel_cols].astype(object).where(pd.notna(df[sel_cols]), None).values.tolist(),
        "compounds": df["InChIKey"].tolist() if "InChIKey" in df.columns else df.index.tolist(),
        "targets": sel_cols,
    }


@app.route("/api/heatmap-data")
def heatmap_data():
    with _lock:
        if opt_results["winning_matrix_df"] is None:
            return jsonify({"error": "No heatmap data available"}), 404
        cache = opt_results.get("heatmap_cache")
        if cache:
            return jsonify(cache)
        df = opt_results["winning_matrix_df"]

    # Fallback: compute on the fly
    sel_cols = [c for c in df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    matrix = df[sel_cols].astype(object).where(pd.notna(df[sel_cols]), None).values.tolist()
    compounds = df["InChIKey"].tolist() if "InChIKey" in df.columns else df.index.tolist()
    targets = sel_cols

    return jsonify({
        "matrix": matrix,
        "compounds": compounds,
        "targets": targets,
    })


@app.route("/api/download/library")
def download_library():
    with _lock:
        path = opt_results.get("winning_file")
    if path and os.path.isfile(path):
        return send_file(path, as_attachment=True, download_name="winning_library.xlsx")
    return jsonify({"error": "No library file available"}), 404


@app.route("/api/download/matrix")
def download_matrix():
    with _lock:
        csv_path = dataset.get("matrix_file")

    if not (csv_path and os.path.isfile(csv_path)):
        return jsonify({"error": "No matrix file available"}), 404

    # Generate Excel lazily from CSV (cached after first call)
    output_dir = PROJECT_ROOT / "webapp" / "output"
    xlsx_path = str(output_dir / "selectivity_matrix.xlsx")

    if not os.path.isfile(xlsx_path):
        pd.read_csv(csv_path).to_excel(xlsx_path, index=False, engine='xlsxwriter')

    return send_file(xlsx_path, as_attachment=True, download_name="selectivity_matrix.xlsx")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Project root: {PROJECT_ROOT}")
    print(f"ChEMBL database: {DATABASE_DIR / 'chembl_36.db'}")
    print(f"MolPort database: {DATABASE_DIR / 'molport.db'}")
    app.run(debug=False, host="0.0.0.0", port=5000)
