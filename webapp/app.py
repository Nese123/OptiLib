"""
Drug Library Optimization — Flask Web Application

Full pipeline: Upload targets → ChEMBL query → selectivity matrix → NSGA-II → results dashboard.
"""

import os
import sys
import json
import sqlite3
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

sys.path.insert(0, str(PROJECT_ROOT / "webapp"))
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
    def __init__(self, problem):
        super().__init__()
        self.problem = problem
        # Snapshot of the latest algorithm state for early-stop result extraction
        self.last_pop_X = None
        self.last_pop_F = None
        self.last_pop_G = None

    def notify(self, algorithm):
        import numpy as np

        # Always snapshot the current population before any stop check,
        # so if we stop we have the latest state available.
        self.last_pop_X = algorithm.pop.get("X").copy()
        self.last_pop_F = algorithm.pop.get("F").copy()
        self.last_pop_G = algorithm.pop.get("G").copy()

        with _lock:
            opt_state["generation"] = algorithm.n_gen
            if opt_state.get("stop_requested"):
                raise StopOptimization("Optimization stopped by user")

            G = self.last_pop_G
            F = self.last_pop_F

            feasible_idx = np.where(G.ravel() <= 0)[0] if G.ndim == 1 else np.where(np.all(G <= 0, axis=1))[0]
            if len(feasible_idx) > 0:
                feasible_F = F[feasible_idx]
                min_obj1 = np.min(feasible_F[:, 0])
                min_obj2 = np.min(feasible_F[:, 1])
            else:
                min_obj1 = np.min(F[:, 0])
                min_obj2 = np.min(F[:, 1])

            best_sel = -min_obj1 * self.problem.pool_baseline_score
            best_cost = min_obj2 * self.problem.pool_total_cost

            opt_state["history"].append({
                "generation": algorithm.n_gen,
                "best_selectivity": float(best_sel),
                "best_cost": float(best_cost)
            })

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
    "history": [],
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
    "weight_mean": None,
    "weight_min": None,
}

# Custom uploaded data state
affinity_upload_state = {
    "df": None,                 # Parsed DataFrame with columns [Compound_Raw, Target_Raw, Affinity]
    "resolved_compounds": {},   # raw_id -> dict(chembl_id, pref_name, inchi_key, smiles)
    "resolved_targets": {},     # raw_id -> dict(chembl_id, pref_name, gene_symbol, canonical_name)
    "num_compounds": 0,
    "num_targets": 0,
    "num_datapoints": 0,
    "unique_targets": [],
    "formatted_targets": [],
    "formatted_compounds": [],
}

price_upload_state = {
    "price_map": {},            # key (normalized identifier) -> price (float)
    "filename": "",
    "count": 0,
}

# Thread lock for state access
_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  HELPERS — Identifier Resolution & Price Matching
# ═══════════════════════════════════════════════════════════════

def _resolve_compounds(compound_ids):
    """Resolve a list/set of compound identifiers against ChEMBL database.
    
    Accepts ChEMBL IDs, SMILES, InChIKeys, or compound names.
    Returns a dict mapping raw_id -> dict(chembl_id, pref_name, inchi_key, smiles).
    """
    if not compound_ids:
        return {}
        
    cleaned_ids = [str(cid).strip() for cid in compound_ids if str(cid).strip()]
    unique_ids = list(dict.fromkeys(cleaned_ids))
    if not unique_ids:
        return {}
        
    # Generate search variations
    search_set = set()
    for cid in unique_ids:
        search_set.add(cid)
        search_set.add(cid.upper())
        search_set.add(cid.title())
        search_set.add(cid.lower())
    search_list = list(search_set)
    
    db_path = str(DATABASE_DIR / "chembl_36.db")
    raw_matches = []
    try:
        with sqlite3.connect(db_path) as conn:
            chunk_size = 500
            for i in range(0, len(search_list), chunk_size):
                chunk = search_list[i:i + chunk_size]
                ph = ",".join(["?"] * len(chunk))
                
                # 1. By chembl_id
                q1 = f"SELECT md.chembl_id, md.pref_name, cs.standard_inchi_key, cs.canonical_smiles FROM molecule_dictionary md LEFT JOIN compound_structures cs ON md.molregno = cs.molregno WHERE md.chembl_id IN ({ph})"
                raw_matches.extend(conn.execute(q1, chunk).fetchall())
                
                # 2. By pref_name
                q2 = f"SELECT md.chembl_id, md.pref_name, cs.standard_inchi_key, cs.canonical_smiles FROM molecule_dictionary md LEFT JOIN compound_structures cs ON md.molregno = cs.molregno WHERE md.pref_name IN ({ph})"
                raw_matches.extend(conn.execute(q2, chunk).fetchall())
                
                # 3. By inchi_key
                q3 = f"SELECT md.chembl_id, md.pref_name, cs.standard_inchi_key, cs.canonical_smiles FROM compound_structures cs JOIN molecule_dictionary md ON cs.molregno = md.molregno WHERE cs.standard_inchi_key IN ({ph})"
                raw_matches.extend(conn.execute(q3, chunk).fetchall())
                
                # 4. By smiles
                q4 = f"SELECT md.chembl_id, md.pref_name, cs.standard_inchi_key, cs.canonical_smiles FROM compound_structures cs JOIN molecule_dictionary md ON cs.molregno = md.molregno WHERE cs.canonical_smiles IN ({ph})"
                raw_matches.extend(conn.execute(q4, chunk).fetchall())
    except Exception as e:
        print("Warning during compound resolution:", e)
        
    # Build fast lookup indexes
    by_chembl_id = {}
    by_pref_name = {}
    by_inchikey = {}
    by_smiles = {}
    
    for row in raw_matches:
        cid, name, ik, smi = row
        item = {
            "chembl_id": cid or "",
            "pref_name": name or "",
            "inchi_key": ik or "",
            "smiles": smi or "",
        }
        if cid:
            by_chembl_id[str(cid).strip().upper()] = item
        if name:
            by_pref_name[str(name).strip().lower()] = item
        if ik:
            by_inchikey[str(ik).strip().upper()] = item
        if smi:
            by_smiles[str(smi).strip()] = item
            
    resolved = {}
    for raw_id in unique_ids:
        raw_str = str(raw_id).strip()
        raw_upper = raw_str.upper()
        raw_lower = raw_str.lower()
        
        match = None
        if raw_upper in by_chembl_id:
            match = by_chembl_id[raw_upper]
        elif raw_lower in by_pref_name:
            match = by_pref_name[raw_lower]
        elif raw_upper in by_inchikey:
            match = by_inchikey[raw_upper]
        elif raw_str in by_smiles:
            match = by_smiles[raw_str]
            
        if match:
            resolved[raw_id] = {
                "raw_id": raw_id,
                "chembl_id": match["chembl_id"],
                "pref_name": match["pref_name"] or raw_str,
                "inchi_key": match["inchi_key"],
                "smiles": match["smiles"],
                "is_chembl": True,
            }
        else:
            # Guess if raw string is InChIKey or SMILES
            ik_guess = raw_str if (len(raw_str) == 27 and raw_str[14] == '-' and raw_str[25] == '-') else ""
            smi_guess = raw_str if ('=' in raw_str or '#' in raw_str or '(' in raw_str or 'c1' in raw_str) else ""
            resolved[raw_id] = {
                "raw_id": raw_id,
                "chembl_id": "",
                "pref_name": raw_str,
                "inchi_key": ik_guess,
                "smiles": smi_guess,
                "is_chembl": False,
            }
            
    return resolved


def _resolve_targets(target_ids):
    """Resolve target identifiers (ChEMBL IDs, names, gene symbols, accessions) against ChEMBL database."""
    if not target_ids:
        return {}
        
    cleaned_targets = [str(tid).strip() for tid in target_ids if str(tid).strip()]
    unique_targets = list(dict.fromkeys(cleaned_targets))
    if not unique_targets:
        return {}
        
    search_set = set()
    for tid in unique_targets:
        search_set.add(tid)
        search_set.add(tid.upper())
        search_set.add(tid.lower())
    search_list = list(search_set)
    
    db_path = str(DATABASE_DIR / "chembl_36.db")
    rows = []
    try:
        with sqlite3.connect(db_path) as conn:
            chunk_size = 500
            for i in range(0, len(search_list), chunk_size):
                chunk = search_list[i:i + chunk_size]
                ph = ",".join(["?"] * len(chunk))
                query = f"""
                    SELECT DISTINCT td.chembl_id, td.pref_name, 
                           (SELECT csy2.component_synonym 
                            FROM target_components tc2 
                            JOIN component_synonyms csy2 ON tc2.component_id = csy2.component_id 
                            WHERE tc2.tid = td.tid AND csy2.syn_type = 'GENE_SYMBOL' 
                            LIMIT 1) AS gene_symbol,
                           cs.accession, csy.component_synonym, csy.syn_type
                    FROM target_dictionary td
                    LEFT JOIN target_components tc ON td.tid = tc.tid
                    LEFT JOIN component_sequences cs ON tc.component_id = cs.component_id
                    LEFT JOIN component_synonyms csy ON cs.component_id = csy.component_id
                    WHERE (
                        td.chembl_id COLLATE NOCASE IN ({ph}) OR
                        td.pref_name COLLATE NOCASE IN ({ph}) OR
                        cs.accession COLLATE NOCASE IN ({ph}) OR
                        (csy.component_synonym COLLATE NOCASE IN ({ph}) 
                         AND csy.syn_type IN ('GENE_SYMBOL', 'UNIPROT', 'EC_NUMBER'))
                    )
                    AND td.target_type = 'SINGLE PROTEIN'
                    AND td.organism = 'Homo sapiens'
                """
                rows.extend(conn.execute(query, chunk * 4).fetchall())
    except Exception as e:
        print("Warning during target resolution:", e)
        
    resolved = {}
    for target_in in unique_targets:
        target_in_lower = str(target_in).lower()
        found = False
        for r in rows:
            cid, name, gene_sym, acc, syn, syn_type = r
            if (target_in_lower == str(cid).lower() or 
                (name and target_in_lower == str(name).lower()) or
                (gene_sym and target_in_lower == str(gene_sym).lower()) or
                (acc and target_in_lower == str(acc).lower()) or
                (syn and target_in_lower == str(syn).lower())):
                # Preferred canonical display name: Gene Symbol -> Preferred Name -> ChEMBL ID
                canonical = gene_sym if gene_sym else (name if name else cid)
                resolved[target_in] = {
                    "raw_id": target_in,
                    "chembl_id": cid or "",
                    "pref_name": name or "",
                    "gene_symbol": gene_sym or "",
                    "accession": acc or "",
                    "canonical_name": canonical,
                    "is_chembl": True,
                }
                found = True
                break
        if not found:
            resolved[target_in] = {
                "raw_id": target_in,
                "chembl_id": "",
                "pref_name": str(target_in),
                "gene_symbol": "",
                "accession": "",
                "canonical_name": str(target_in),
                "is_chembl": False,
            }
            
    return resolved


def _lookup_custom_price(compound_raw, resolved_info=None):
    """Smart lookup in custom price map across raw ID, ChEMBL ID, InChIKey, SMILES, and pref_name."""
    with _lock:
        price_map = price_upload_state.get("price_map", {})
    if not price_map:
        return None
        
    c_raw = str(compound_raw).strip()
    if c_raw.lower() in price_map:
        return price_map[c_raw.lower()]
    if c_raw.upper() in price_map:
        return price_map[c_raw.upper()]
        
    if resolved_info:
        cid = resolved_info.get("chembl_id")
        if cid and str(cid).strip().upper() in price_map:
            return price_map[str(cid).strip().upper()]
        ik = resolved_info.get("inchi_key")
        if ik and str(ik).strip().upper() in price_map:
            return price_map[str(ik).strip().upper()]
        smi = resolved_info.get("smiles")
        if smi and str(smi).strip() in price_map:
            return price_map[str(smi).strip()]
        name = resolved_info.get("pref_name")
        if name and str(name).strip().lower() in price_map:
            return price_map[str(name).strip().lower()]
            
    return None


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
    chembl_map = {}

    try:
        with sqlite3.connect(db_path) as conn:
            placeholders = ",".join(["?"] * len(input_targets))
            query = f"""
                SELECT DISTINCT td.chembl_id, td.pref_name, 
                       (SELECT csy2.component_synonym 
                        FROM target_components tc2 
                        JOIN component_synonyms csy2 ON tc2.component_id = csy2.component_id 
                        WHERE tc2.tid = td.tid AND csy2.syn_type = 'GENE_SYMBOL' 
                        LIMIT 1) AS gene_symbol,
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
            
            # Map input to canonical ChEMBL IDs and Names with Gene Symbol in brackets
            for target_in in input_targets:
                target_in_lower = str(target_in).lower()
                found = False
                for r in rows:
                    cid, name, gene_sym, acc, syn = r
                    if (target_in_lower == str(cid).lower() or 
                        (name and target_in_lower == str(name).lower()) or
                        (gene_sym and target_in_lower == str(gene_sym).lower()) or
                        (acc and target_in_lower == str(acc).lower()) or
                        (syn and target_in_lower == str(syn).lower())):
                        found = True
                        if cid not in matched_chembl_ids:
                            matched_chembl_ids.add(cid)
                            display_name = name or gene_sym or cid
                            bracket_symbol = gene_sym or cid
                            if bracket_symbol:
                                match_str = f"{target_in} -> {display_name} ({bracket_symbol})"
                            else:
                                match_str = f"{target_in} -> {display_name}"
                            matched.append(match_str)
                            chembl_map[match_str] = cid
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
        "chembl_map": chembl_map,
    })


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Predefined Affinity Data & Custom Price Upload
# ═══════════════════════════════════════════════════════════════

@app.route("/api/upload-affinity", methods=["POST"])
def upload_affinity():
    """Accept CSV/Excel with compound, target, and affinity value."""
    files = request.files.getlist("files[]") or request.files.getlist("file")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "No files uploaded"}), 400
        
    all_dfs = []
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
            
        cmpd_col = None
        tgt_col = None
        aff_col = None
        
        for col in df.columns:
            clean_col = col.strip().lower().replace(" ", "_").replace("-", "_")
            if clean_col in ("compound", "compound_id", "compound_name", "drug", "drug_id", "molecule", "molecule_id", "ligand", "id"):
                if cmpd_col is None:
                    cmpd_col = col
            elif clean_col in ("target", "target_id", "target_name", "protein", "protein_id", "gene", "gene_symbol", "targets", "uniprot", "uniprot_id", "uniprot_accession", "accession", "uniprot_acc", "protein_accession", "target_accession"):
                if tgt_col is None:
                    tgt_col = col
            elif clean_col in ("affinity", "affinity_pkd", "affinity_value", "pkd", "pic50", "pki", "value", "score", "activity", "potency"):
                if aff_col is None:
                    aff_col = col
                    
        # Fallback by column index if 3 columns
        if len(df.columns) >= 3 and (cmpd_col is None or tgt_col is None or aff_col is None):
            cols = list(df.columns)
            if cmpd_col is None: cmpd_col = cols[0]
            if tgt_col is None: tgt_col = cols[1]
            if aff_col is None: aff_col = cols[2]
            
        if cmpd_col is None or tgt_col is None or aff_col is None:
            return jsonify({
                "error": f"Could not identify Compound, Target, and Affinity columns in {file.filename}. "
                         f"Please ensure columns are named 'Compound', 'Target', and 'Affinity'."
            }), 400
            
        # Clean and extract
        sub_df = pd.DataFrame({
            "Compound_Raw": df[cmpd_col].dropna().astype(str).str.strip(),
            "Target_Raw": df[tgt_col].dropna().astype(str).str.strip(),
            "Affinity": pd.to_numeric(df[aff_col], errors="coerce")
        }).dropna()
        
        all_dfs.append(sub_df)
        
    if not all_dfs:
        return jsonify({"error": "No valid affinity data found."}), 400
        
    combined_df = pd.concat(all_dfs, ignore_index=True).drop_duplicates()
    if combined_df.empty:
        return jsonify({"error": "No valid data rows found in uploaded files."}), 400
        
    unique_compounds = combined_df["Compound_Raw"].unique().tolist()
    unique_targets = combined_df["Target_Raw"].unique().tolist()
    
    if len(unique_targets) < 2:
        return jsonify({"error": f"Dataset must contain at least 2 distinct targets (found {len(unique_targets)})."}), 400
        
    # Resolve entities
    resolved_compounds = _resolve_compounds(unique_compounds)
    resolved_targets = _resolve_targets(unique_targets)
    
    formatted_compounds = []
    for c in unique_compounds:
        info = resolved_compounds.get(c, {})
        ik = info.get("inchi_key") or ""
        cid = info.get("chembl_id") or ""
        if ik and cid:
            display_str = f"{c} -> {ik} ({cid})"
        elif ik:
            display_str = f"{c} -> {ik}"
        elif cid:
            display_str = f"{c} -> ({cid})"
        else:
            display_str = f"{c}"
        formatted_compounds.append(display_str)

    formatted_targets = []
    for t in unique_targets:
        info = resolved_targets.get(t, {})
        if info.get("is_chembl"):
            name = info.get("pref_name") or info.get("gene_symbol") or info.get("chembl_id")
            gene_sym = info.get("gene_symbol") or info.get("chembl_id")
            if gene_sym:
                display_str = f"{t} -> {name} ({gene_sym})"
            else:
                display_str = f"{t} -> {name}"
        else:
            display_str = f"{t}"
        formatted_targets.append(display_str)

    with _lock:
        affinity_upload_state["df"] = combined_df
        affinity_upload_state["resolved_compounds"] = resolved_compounds
        affinity_upload_state["resolved_targets"] = resolved_targets
        affinity_upload_state["num_compounds"] = len(unique_compounds)
        affinity_upload_state["num_targets"] = len(unique_targets)
        affinity_upload_state["num_datapoints"] = len(combined_df)
        affinity_upload_state["unique_targets"] = [resolved_targets[t]["canonical_name"] for t in unique_targets]
        affinity_upload_state["formatted_compounds"] = formatted_compounds
        affinity_upload_state["formatted_targets"] = formatted_targets
        
    return jsonify({
        "num_compounds": len(unique_compounds),
        "num_targets": len(unique_targets),
        "num_datapoints": len(combined_df),
        "compounds": formatted_compounds,
        "targets": formatted_targets,
    })


@app.route("/api/upload-prices", methods=["POST"])
def upload_prices():
    """Accept CSV/Excel with compound and price."""
    files = request.files.getlist("files[]") or request.files.getlist("file")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "No price file uploaded"}), 400
        
    file = files[0]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
        
    try:
        filename = file.filename.lower()
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file)
        else:
            return jsonify({"error": "Unsupported file type for price file. Use CSV or Excel (.xlsx)."}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to read price file: {str(e)}"}), 400
        
    cmpd_col = None
    price_col = None
    
    for col in df.columns:
        clean_col = col.strip().lower().replace(" ", "_").replace("-", "_")
        if clean_col in ("compound", "compound_id", "compound_name", "drug", "drug_id", "molecule", "molecule_id", "ligand", "id", "inchikey", "smiles", "name"):
            if cmpd_col is None: cmpd_col = col
        elif clean_col in ("price", "price_usd_per_mg", "price_usd", "price_per_mg", "cost", "cost_usd", "usd_per_mg"):
            if price_col is None: price_col = col
            
    if len(df.columns) >= 2 and (cmpd_col is None or price_col is None):
        cols = list(df.columns)
        if cmpd_col is None: cmpd_col = cols[0]
        if price_col is None: price_col = cols[1]
        
    if cmpd_col is None or price_col is None:
        return jsonify({
            "error": "Could not identify Compound and Price columns. Please use 'Compound' and 'Price'."
        }), 400
        
    clean_df = pd.DataFrame({
        "Compound": df[cmpd_col].dropna().astype(str).str.strip(),
        "Price": pd.to_numeric(df[price_col], errors="coerce")
    }).dropna()
    clean_df = clean_df[clean_df["Price"] > 0]
    
    if clean_df.empty:
        return jsonify({"error": "No valid positive price rows found."}), 400
        
    unique_cmpds = clean_df["Compound"].unique().tolist()
    resolved_cmpds = _resolve_compounds(unique_cmpds)
    
    price_map = {}
    for _, row in clean_df.iterrows():
        raw_c = str(row["Compound"]).strip()
        price_val = float(row["Price"])
        price_map[raw_c.lower()] = price_val
        price_map[raw_c.upper()] = price_val
        
        res = resolved_cmpds.get(raw_c)
        if res:
            if res["chembl_id"]:
                price_map[res["chembl_id"].upper()] = price_val
            if res["inchi_key"]:
                price_map[res["inchi_key"].upper()] = price_val
            if res["smiles"]:
                price_map[res["smiles"].strip()] = price_val
            if res["pref_name"]:
                price_map[res["pref_name"].lower()] = price_val
                
    with _lock:
        price_upload_state["price_map"] = price_map
        price_upload_state["filename"] = file.filename
        price_upload_state["count"] = len(clean_df)
        
    return jsonify({
        "num_prices": len(clean_df),
        "filename": file.filename
    })


@app.route("/api/remove-affinity-target", methods=["POST"])
def remove_affinity_target():
    """Remove a single target from the uploaded affinity dataset."""
    data = request.get_json(force=True) or {}
    target_str = data.get("target", "").strip()
    if not target_str:
        return jsonify({"error": "No target specified"}), 400

    target_raw = target_str.split(" ->")[0].strip().lower()

    with _lock:
        raw_df = affinity_upload_state.get("df")
        if raw_df is None or raw_df.empty:
            return jsonify({"error": "No affinity data found"}), 400

        mask = (
            (raw_df["Target_Raw"].astype(str).str.strip().str.lower() != target_raw) &
            (raw_df["Target_Raw"].astype(str).str.strip() != target_str)
        )
        new_df = raw_df[mask].copy()

        formatted_targets = [
            ft for ft in affinity_upload_state.get("formatted_targets", [])
            if ft != target_str and ft.split(" ->")[0].strip().lower() != target_raw
        ]

        if new_df.empty or not formatted_targets:
            affinity_upload_state["df"] = None
            affinity_upload_state["resolved_compounds"] = {}
            affinity_upload_state["resolved_targets"] = {}
            affinity_upload_state["num_compounds"] = 0
            affinity_upload_state["num_targets"] = 0
            affinity_upload_state["num_datapoints"] = 0
            affinity_upload_state["unique_targets"] = []
            affinity_upload_state["formatted_targets"] = []
            affinity_upload_state["formatted_compounds"] = []
            return jsonify({
                "num_compounds": 0,
                "num_targets": 0,
                "num_datapoints": 0,
                "compounds": [],
                "targets": []
            })

        unique_compounds = new_df["Compound_Raw"].unique().tolist()
        unique_targets = new_df["Target_Raw"].unique().tolist()
        unique_compounds_set = set(c.lower() for c in unique_compounds)

        formatted_compounds = [
            fc for fc in affinity_upload_state.get("formatted_compounds", [])
            if fc.split(" ->")[0].strip().lower() in unique_compounds_set
        ]

        resolved_targets = affinity_upload_state.get("resolved_targets", {})

        if not formatted_compounds:
            affinity_upload_state["df"] = None
            affinity_upload_state["resolved_compounds"] = {}
            affinity_upload_state["resolved_targets"] = {}
            affinity_upload_state["num_compounds"] = 0
            affinity_upload_state["num_targets"] = 0
            affinity_upload_state["num_datapoints"] = 0
            affinity_upload_state["unique_targets"] = []
            affinity_upload_state["formatted_targets"] = []
            affinity_upload_state["formatted_compounds"] = []
            return jsonify({
                "num_compounds": 0,
                "num_targets": 0,
                "num_datapoints": 0,
                "compounds": [],
                "targets": []
            })

        affinity_upload_state["df"] = new_df
        affinity_upload_state["num_compounds"] = len(unique_compounds)
        affinity_upload_state["num_targets"] = len(unique_targets)
        affinity_upload_state["num_datapoints"] = len(new_df)
        affinity_upload_state["unique_targets"] = [
            resolved_targets[t]["canonical_name"] for t in unique_targets if t in resolved_targets
        ]
        affinity_upload_state["formatted_compounds"] = formatted_compounds
        affinity_upload_state["formatted_targets"] = formatted_targets

        return jsonify({
            "num_compounds": len(unique_compounds),
            "num_targets": len(unique_targets),
            "num_datapoints": len(new_df),
            "compounds": formatted_compounds,
            "targets": formatted_targets
        })


@app.route("/api/remove-affinity-compound", methods=["POST"])
def remove_affinity_compound():
    """Remove a single compound from the uploaded affinity dataset."""
    data = request.get_json(force=True) or {}
    compound_str = data.get("compound", "").strip()
    if not compound_str:
        return jsonify({"error": "No compound specified"}), 400

    compound_raw = compound_str.split(" ->")[0].strip().lower()

    with _lock:
        raw_df = affinity_upload_state.get("df")
        if raw_df is None or raw_df.empty:
            return jsonify({"error": "No affinity data found"}), 400

        mask = (
            (raw_df["Compound_Raw"].astype(str).str.strip().str.lower() != compound_raw) &
            (raw_df["Compound_Raw"].astype(str).str.strip() != compound_str)
        )
        new_df = raw_df[mask].copy()

        formatted_compounds = [
            fc for fc in affinity_upload_state.get("formatted_compounds", [])
            if fc != compound_str and fc.split(" ->")[0].strip().lower() != compound_raw
        ]

        if new_df.empty or not formatted_compounds:
            affinity_upload_state["df"] = None
            affinity_upload_state["resolved_compounds"] = {}
            affinity_upload_state["resolved_targets"] = {}
            affinity_upload_state["num_compounds"] = 0
            affinity_upload_state["num_targets"] = 0
            affinity_upload_state["num_datapoints"] = 0
            affinity_upload_state["unique_targets"] = []
            affinity_upload_state["formatted_targets"] = []
            affinity_upload_state["formatted_compounds"] = []
            return jsonify({
                "num_compounds": 0,
                "num_targets": 0,
                "num_datapoints": 0,
                "compounds": [],
                "targets": []
            })

        unique_compounds = new_df["Compound_Raw"].unique().tolist()
        unique_targets = new_df["Target_Raw"].unique().tolist()
        unique_targets_set = set(t.lower() for t in unique_targets)

        formatted_targets = [
            ft for ft in affinity_upload_state.get("formatted_targets", [])
            if ft.split(" ->")[0].strip().lower() in unique_targets_set
        ]

        resolved_targets = affinity_upload_state.get("resolved_targets", {})

        if not formatted_targets:
            affinity_upload_state["df"] = None
            affinity_upload_state["resolved_compounds"] = {}
            affinity_upload_state["resolved_targets"] = {}
            affinity_upload_state["num_compounds"] = 0
            affinity_upload_state["num_targets"] = 0
            affinity_upload_state["num_datapoints"] = 0
            affinity_upload_state["unique_targets"] = []
            affinity_upload_state["formatted_targets"] = []
            affinity_upload_state["formatted_compounds"] = []
            return jsonify({
                "num_compounds": 0,
                "num_targets": 0,
                "num_datapoints": 0,
                "compounds": [],
                "targets": []
            })

        affinity_upload_state["df"] = new_df
        affinity_upload_state["num_compounds"] = len(unique_compounds)
        affinity_upload_state["num_targets"] = len(unique_targets)
        affinity_upload_state["num_datapoints"] = len(new_df)
        affinity_upload_state["unique_targets"] = [
            resolved_targets[t]["canonical_name"] for t in unique_targets if t in resolved_targets
        ]
        affinity_upload_state["formatted_compounds"] = formatted_compounds
        affinity_upload_state["formatted_targets"] = formatted_targets

        return jsonify({
            "num_compounds": len(unique_compounds),
            "num_targets": len(unique_targets),
            "num_datapoints": len(new_df),
            "compounds": formatted_compounds,
            "targets": formatted_targets
        })


@app.route("/api/clear-affinity", methods=["POST"])
def clear_affinity():
    with _lock:
        affinity_upload_state["df"] = None
        affinity_upload_state["resolved_compounds"] = {}
        affinity_upload_state["resolved_targets"] = {}
        affinity_upload_state["num_compounds"] = 0
        affinity_upload_state["num_targets"] = 0
        affinity_upload_state["num_datapoints"] = 0
        affinity_upload_state["unique_targets"] = []
        affinity_upload_state["formatted_targets"] = []
        affinity_upload_state["formatted_compounds"] = []
    return jsonify({"status": "cleared"})


@app.route("/api/clear-prices", methods=["POST"])
def clear_prices():
    with _lock:
        price_upload_state["price_map"] = {}
        price_upload_state["filename"] = ""
        price_upload_state["count"] = 0
    return jsonify({"status": "cleared"})


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


@app.route("/api/build-matrix-from-affinity", methods=["POST"])
def build_matrix_from_affinity():
    """Launch the affinity-based pipeline in a background thread."""
    with _lock:
        if pipeline_state["status"] == "running":
            return jsonify({"error": "Pipeline is already running"}), 409
        if affinity_upload_state["df"] is None or affinity_upload_state["df"].empty:
            return jsonify({"error": "No affinity data uploaded. Please upload an affinity file first."}), 400

    data = request.get_json(force=True) or {}
    selectivity_threshold = float(data.get("selectivity_threshold", 0.5))
    remove_targets = bool(data.get("remove_targets", True))

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
        target=_run_affinity_pipeline,
        args=(selectivity_threshold, remove_targets),
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
    import hashlib

    try:
        output_dir = PROJECT_ROOT / "webapp" / "output"
        output_dir.mkdir(exist_ok=True)
        
        # Generate cache key based on inputs
        cache_str = f"{sorted(chembl_ids)}_{selectivity_threshold}_{remove_targets}_{matched_count}"
        cache_key = hashlib.md5(cache_str.encode('utf-8')).hexdigest()
        matrix_file = str(output_dir / f"selectivity_matrix_{cache_key}.csv")
        
        if os.path.exists(matrix_file):
            _update_pipeline(1, "Loading cached matrix...", "Found a previously computed selectivity matrix for these parameters.")
            final_export_df = pd.read_csv(matrix_file)
            
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
                pipeline_state["current_step"] = 3
                pipeline_state["step_label"] = "Done"
                pipeline_state["detail"] = f"Loaded cached matrix: {dataset['num_drugs']} compounds × {dataset['num_targets']} targets"
            return

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
            SELECT DISTINCT COALESCE(
                (SELECT csy.component_synonym 
                 FROM target_components tc 
                 JOIN component_synonyms csy ON tc.component_id = csy.component_id 
                 WHERE tc.tid = td.tid AND csy.syn_type = 'GENE_SYMBOL' 
                 LIMIT 1),
                td.pref_name, 
                td.chembl_id
            ) AS Target_Name
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
                COALESCE(
                    (SELECT csy.component_synonym 
                     FROM target_components tc 
                     JOIN component_synonyms csy ON tc.component_id = csy.component_id 
                     WHERE tc.tid = td.tid AND csy.syn_type = 'GENE_SYMBOL' 
                     LIMIT 1),
                    td.pref_name,
                    td.chembl_id
                ) AS Target_Name,
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
        dropped_targets = matched_count - final_targets
        if dropped_targets > 0:
            summary_text = (
                f"Found {final_drugs} compounds active against {final_targets} targets. "
                f"{dropped_targets} targets were dropped because they lacked compounds with sufficient affinity or selectivity."
            )
        else:
            summary_text = f"Found {final_drugs} compounds active against {final_targets} targets"

        _update_pipeline(1, "Searching for selective compounds...",
                         f"Pruned {pruned_low_sel} compounds with low selectivity: {final_drugs} compounds remaining",
                         summary=summary_text)

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
        _update_pipeline(2, "Getting price data...", "Querying database for prices")

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

        # Check custom uploaded prices first
        custom_price_dict = {}
        custom_source_dict = {}
        for idx, row in final_export_df.iterrows():
            cmpd_name = row.get("Compound_Name", "")
            res_info = {
                "chembl_id": row.get("Molecule_ChEMBL_ID", ""),
                "inchi_key": row.get("InChIKey", ""),
                "smiles": row.get("SMILES", ""),
                "pref_name": row.get("Compound_Name", "")
            }
            p = _lookup_custom_price(cmpd_name, res_info)
            if p is not None:
                ik = row.get("InChIKey", "")
                if ik:
                    custom_price_dict[ik] = float(p)
                    custom_source_dict[ik] = "Custom Price File"

        custom_price_series = final_export_df["InChIKey"].map(custom_price_dict)
        final_export_df["Molport_Price"] = custom_price_series.combine_first(final_export_df["InChIKey"].map(molport_dict))
        final_export_df["Molport_Source"] = final_export_df["InChIKey"].map(custom_source_dict).combine_first(final_export_df["InChIKey"].map(molport_source_dict))
        
        found_count = final_export_df["Molport_Price"].notna().sum()
        custom_matched_count = custom_price_series.notna().sum()
        molprice_approx_count = (final_export_df["Molport_Source"] == "MolPrice").sum()
        molport_direct_count = found_count - molprice_approx_count - custom_matched_count
        
        _update_pipeline(2, "Getting price data...",
                         f"Found prices for {found_count}/{len(final_export_df)} compounds (Custom: {custom_matched_count}, MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})")

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
                                 f"MolPrice predicted prices for {missing_count} compounds. (Custom: {custom_matched_count}, MolPort: {molport_direct_count}, MolPrice: {molprice_approx_count})",
                                 summary=f"Predicted {missing_count} prices. (Custom: {custom_matched_count}, MolPort: {molport_direct_count})")
            except Exception as e:
                _update_pipeline(2, "Getting price data...",
                                 f"MolPrice prediction failed ({e}), using median fallback.",
                                 summary=f"Prediction failed, used fallback.")
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
                             f"All prices assigned (Custom: {custom_matched_count}, MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})", 
                             summary=f"All prices assigned: {custom_matched_count} from custom file, {molport_direct_count} direct MolPort, {molprice_approx_count} MolPrice.")
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
        _update_pipeline(3, "Saving matrix...", "Saving matrix...")
        
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


def _run_affinity_pipeline(selectivity_threshold=0.5, remove_targets=True):
    """Pipeline for user-uploaded affinity data: calculates selectivity directly and resolves prices."""
    import hashlib
    try:
        output_dir = PROJECT_ROOT / "webapp" / "output"
        output_dir.mkdir(exist_ok=True)

        with _lock:
            raw_df = affinity_upload_state["df"]
            compounds_map = dict(affinity_upload_state["resolved_compounds"])
            targets_map = dict(affinity_upload_state["resolved_targets"])

        if raw_df is None or raw_df.empty:
            raise ValueError("No affinity data uploaded.")

        # ─────────────────────────────────────────────
        # Step 1: Calculating selectivity matrix
        # ─────────────────────────────────────────────
        _update_pipeline(1, "Calculating selectivity matrix...", "Computing blended selectivity scores from uploaded affinity data...")

        work_df = raw_df.copy()
        work_df["Target_Canonical"] = work_df["Target_Raw"].map(lambda t: targets_map.get(t, {}).get("canonical_name", str(t)))

        # Average duplicate measurements
        pivoted_affinities = work_df.pivot_table(
            index="Compound_Raw",
            columns="Target_Canonical",
            values="Affinity",
            aggfunc="mean"
        )

        compounds_list = list(pivoted_affinities.index)
        targets_list = list(pivoted_affinities.columns)

        if len(targets_list) < 2:
            raise ValueError(f"Need at least 2 distinct targets to compute selectivity, but found {len(targets_list)}.")

        # Compute blended selectivity matrix using core/selectivity.py
        affinities_matrix = pivoted_affinities.to_numpy(dtype=float)
        selectivity_matrix = generate_selectivity_matrix(affinities_matrix)

        selectivity_df = pd.DataFrame(selectivity_matrix, index=compounds_list, columns=targets_list)

        init_drugs, init_targets = selectivity_df.shape

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

        final_drugs, final_targets = clean_df.shape
        if final_drugs == 0 or final_targets == 0:
            raise ValueError(f"No compounds or targets survived selectivity pruning at threshold {selectivity_threshold}. Try lowering the threshold.")

        dropped_targets = init_targets - final_targets
        if dropped_targets > 0:
            summary_text = (
                f"Calculated selectivity for {final_drugs} compounds across {final_targets} targets. "
                f"{dropped_targets} targets were dropped due to low selectivity."
            )
        else:
            summary_text = f"Calculated selectivity for {final_drugs} compounds across {final_targets} targets."

        _update_pipeline(1, "Calculating selectivity matrix...",
                         f"Selectivity matrix computed: {final_drugs} compounds × {final_targets} targets",
                         summary=summary_text)

        # ─────────────────────────────────────────────
        # Step 2: Getting price data
        # ─────────────────────────────────────────────
        _update_pipeline(2, "Getting price data...", "Resolving compound prices...")

        final_compounds = list(clean_df.index)

        meta_records = []
        for cmpd in final_compounds:
            res_info = compounds_map.get(cmpd, {})
            chembl_id = res_info.get("chembl_id", "")
            pref_name = res_info.get("pref_name", str(cmpd))
            inchi_key = res_info.get("inchi_key", "")
            smiles = res_info.get("smiles", "")

            # If compound string itself is InChIKey or SMILES
            if not inchi_key and len(str(cmpd)) == 27 and str(cmpd)[14] == '-' and str(cmpd)[25] == '-':
                inchi_key = str(cmpd)
            if not smiles and ('=' in str(cmpd) or '#' in str(cmpd) or '(' in str(cmpd) or 'c1' in str(cmpd)):
                smiles = str(cmpd)

            meta_records.append({
                "Compound_Name": str(cmpd),
                "Molecule_ChEMBL_ID": chembl_id or str(cmpd),
                "InChIKey": inchi_key,
                "SMILES": smiles,
            })

        final_export_df = clean_df.copy().reset_index()
        final_export_df.rename(columns={"index": "Compound_Name"}, inplace=True)
        meta_df = pd.DataFrame(meta_records)
        final_export_df = final_export_df.merge(meta_df, on="Compound_Name", how="left")

        # MolPort lookup cache
        inchikeys = [r["InChIKey"] for r in meta_records if r["InChIKey"]]
        molport_dict = {}
        if inchikeys:
            molport_db = str(DATABASE_DIR / "molport.db")
            try:
                with sqlite3.connect(molport_db) as conn:
                    mp_chunk_size = 30000
                    for i in range(0, len(inchikeys), mp_chunk_size):
                        chunk = inchikeys[i:i + mp_chunk_size]
                        ph = ",".join(["?"] * len(chunk))
                        query = f"SELECT INCHIKEY, PRICE_1MG FROM compounds WHERE INCHIKEY IN ({ph})"
                        for row in conn.execute(query, chunk).fetchall():
                            molport_dict[row[0]] = float(row[1])
            except Exception as e:
                print("MolPort DB lookup warning:", e)

        molprice_model = None
        prices = []
        custom_price_count = 0
        molport_count = 0
        molprice_count = 0
        fallback_count = 0

        for idx, row_meta in enumerate(meta_records):
            cmpd_raw = row_meta["Compound_Name"]
            res_info = compounds_map.get(cmpd_raw, {})

            # Tier 1: Custom Price File
            custom_p = _lookup_custom_price(cmpd_raw, res_info)
            if custom_p is not None:
                prices.append(float(custom_p))
                custom_price_count += 1
                continue

            # Tier 2: MolPort Database
            ik = row_meta["InChIKey"]
            if ik and ik in molport_dict:
                prices.append(float(molport_dict[ik]))
                molport_count += 1
                continue

            # Tier 3: MolPrice ML Model
            smi = row_meta["SMILES"]
            if smi and smi != "Missing_SMILES":
                try:
                    if molprice_model is None:
                        molprice_weights = str(MOLPRICE_DIR / "models" / "Numpy" / "MP_Morgan_hybrid.pkl")
                        molprice_model = NumpyFingerprints(weights_path=molprice_weights)
                    pred = molprice_model.predict_batch_from_smiles([smi])
                    p_val = pred[0][0] if hasattr(pred[0], '__len__') else float(pred[0])
                    prices.append(float(p_val))
                    molprice_count += 1
                    continue
                except Exception:
                    pass

            # Tier 4: Fallback placeholder
            prices.append(np.nan)
            fallback_count += 1

        prices = np.array(prices, dtype=float)
        valid_prices = prices[~np.isnan(prices)]
        fallback_val = float(np.median(valid_prices)) if len(valid_prices) > 0 else 100.0
        prices = np.where(np.isnan(prices), fallback_val, prices)

        final_export_df["Price_USD_per_mg"] = prices

        price_summary_parts = []
        if custom_price_count > 0:
            price_summary_parts.append(f"{custom_price_count} custom file")
        if molport_count > 0:
            price_summary_parts.append(f"{molport_count} MolPort DB")
        if molprice_count > 0:
            price_summary_parts.append(f"{molprice_count} MolPrice predicted")
        if fallback_count > 0:
            price_summary_parts.append(f"{fallback_count} median fallback (${fallback_val:.2f}/mg)")

        price_summary_str = ", ".join(price_summary_parts) or "All prices assigned."
        _update_pipeline(2, "Getting price data...", f"Resolved prices: {price_summary_str}", summary=f"Prices resolved: {price_summary_str}")

        # ─────────────────────────────────────────────
        # Step 3: Saving matrix
        # ─────────────────────────────────────────────
        _update_pipeline(3, "Saving matrix...", "Saving selectivity matrix...")

        target_cols = [c for c in clean_df.columns]
        meta_cols = ["Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"]
        final_export_df = final_export_df[meta_cols + target_cols]

        cache_key = hashlib.md5(f"affinity_{len(final_export_df)}_{selectivity_threshold}_{remove_targets}".encode('utf-8')).hexdigest()
        matrix_file = str(output_dir / f"selectivity_matrix_affinity_{cache_key}.csv")
        final_export_df.to_csv(matrix_file, index=False)

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

    except Exception as e:
        traceback.print_exc()
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
    max_gen = int(data.get("max_gen", 1000))
    ftol = float(data.get("ftol", 0.0025))
    term_period = int(data.get("term_period", 30))
    max_price_raw = data.get("max_price", None)
    max_price = float(max_price_raw) if max_price_raw is not None else None

    # Clamp values
    pop_size = max(pop_size, 5)
    max_gen = max(max_gen, 10)
    ftol = max(ftol, 0.0001)
    term_period = max(term_period, 5)

    with _lock:
        opt_state.update({
            "status": "running",
            "generation": 0,
            "max_gen": max_gen,
            "error": "",
            "stop_requested": False,
            "history": [],
        })

    thread = threading.Thread(
        target=_run_nsga2,
        args=(weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen, ftol, term_period, max_price),
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
            "stop_requested": False,
            "history": [],
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
            "weight_mean": None,
            "weight_min": None,
        })
        affinity_upload_state.update({
            "df": None,
            "resolved_compounds": {},
            "resolved_targets": {},
            "num_compounds": 0,
            "num_targets": 0,
            "num_datapoints": 0,
            "unique_targets": [],
            "formatted_targets": [],
            "formatted_compounds": [],
        })
        price_upload_state.update({
            "price_map": {},
            "filename": "",
            "count": 0,
        })
    # We no longer clear the global cache files on reset, as they are cached by parameters.
    return jsonify({"status": "reset"})


@app.route("/api/reset-opt", methods=["POST"])
def reset_opt_state():
    with _lock:
        opt_state.update({
            "status": "idle",
            "generation": 0,
            "error": "",
            "stop_requested": False,
            "history": [],
        })
    return jsonify({"status": "reset"})

@app.route("/api/stop-opt", methods=["POST"])
def stop_opt_state():
    with _lock:
        if opt_state["status"] == "running":
            opt_state["stop_requested"] = True
    return jsonify({"status": "stop_requested"})


def _run_nsga2(weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen, ftol=0.0025, term_period=30, max_price=None):
    """Run NSGA-II optimization using the loaded dataset."""
    cb = None  # Keep callback accessible for early-stop result extraction
    problem = None
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
        cb = WebappCallback(problem)
        res, elapsed_time = run_optimization(
            problem, X_init,
            pop_size=pop_size, seed=1,
            max_gen=max_gen, ftol=ftol,
            period=term_period,
            mutation_multiplier=mutation_multiplier,
            crossover_type="hux",
            callback=cb
        )
        del X_init  # Free init population memory

        # Select best solution
        best_idx, front = select_best_solution(res, problem)

        # Extract lightweight data from res, then free pymoo's full result
        # (pymoo keeps algorithm state, history, deep copies, etc.)
        res_X = res.X.copy()
        res_F = res.F.copy()
        del res

        _process_and_store_results(res_X, res_F, best_idx, front, problem, max_price=max_price)

        with _lock:
            opt_state["status"] = "complete"

    except StopOptimization:
        # Early stop: extract results from the callback's saved population snapshot
        if cb is not None and cb.last_pop_X is not None and problem is not None:
            try:
                _process_stopped_results(cb, problem, max_price=max_price)
            except Exception as inner_e:
                with _lock:
                    opt_state["status"] = "error"
                    opt_state["error"] = f"Stopped, but failed to process partial results: {inner_e}"
                traceback.print_exc()
        else:
            with _lock:
                opt_state["status"] = "error"
                opt_state["error"] = "Optimization stopped before any generation completed."
    except Exception as e:
        with _lock:
            opt_state["status"] = "error"
            opt_state["error"] = str(e)
        traceback.print_exc()


def _process_stopped_results(cb, problem, max_price=None):
    """Build and store results from the callback's population snapshot after early stop."""
    # Filter to feasible solutions (constraint G <= 0)
    G = cb.last_pop_G
    F = cb.last_pop_F
    X = cb.last_pop_X

    feasible_mask = (G <= 0).all(axis=1) if G.ndim > 1 else (G <= 0).ravel()
    if np.any(feasible_mask):
        res_X = X[feasible_mask]
        res_F = F[feasible_mask]
    else:
        # No feasible solutions — use entire population
        res_X = X
        res_F = F

    # Build a lightweight result and select the best solution
    res_light = _LightResult(res_X, res_F)
    best_idx, front = select_best_solution(res_light, problem)

    _process_and_store_results(res_X, res_F, best_idx, front, problem, max_price=max_price)

    with _lock:
        opt_state["status"] = "complete"


def _find_knee_point(front):
    """Find the knee point (elbow) on a 2D Pareto front using the chord method.

    Uses the Maximum Perpendicular Distance to the Secant Line connecting
    the two extreme points on the front, in normalized space.

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


def _process_and_store_results(res_X, res_F, best_idx, front, problem, max_price=None):
    """Common result processing shared by normal completion and early stop."""

    # ── Post-optimization price filter ──
    # Instead of constraining the optimizer (which impoverishes its gene pool),
    # we filter the full Pareto front to only keep solutions within budget.
    if max_price is not None:
        price_mask = front[:, 1] <= max_price
        if not np.any(price_mask):
            cheapest = front[:, 1].min()
            raise ValueError(
                f"No solutions found within the price limit of ${max_price:,.0f}. "
                f"The cheapest Pareto-optimal solution costs ${cheapest:,.0f}. "
                f"Try increasing the limit."
            )
        res_X = res_X[price_mask]
        res_F = res_F[price_mask]
        front = front[price_mask]
        # Re-select the knee point from the filtered front
        best_idx = _find_knee_point(front)

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
        opt_results["weight_mean"] = round(float(problem.weight_mean), 4) if hasattr(problem, "weight_mean") else 0.5
        opt_results["weight_min"] = round(float(problem.weight_min), 4) if hasattr(problem, "weight_min") else 0.5


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
        problem = opt_results.get("problem")
        weight_mean = opt_results.get("weight_mean")
        if weight_mean is None and problem and hasattr(problem, "weight_mean"):
            weight_mean = round(float(problem.weight_mean), 4)
        elif weight_mean is None:
            weight_mean = 0.5

        weight_min = opt_results.get("weight_min")
        if weight_min is None and problem and hasattr(problem, "weight_min"):
            weight_min = round(float(problem.weight_min), 4)
        elif weight_min is None:
            weight_min = 0.5
    return jsonify({
        "points": front,
        "best_idx": best,
        "selected_idx": selected,
        "weight_mean": weight_mean,
        "weight_min": weight_min,
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


def _get_target_info(target_list):
    """
    Returns (gene_symbols, full_names) for a list of target columns.
    Maps to standard gene symbols (e.g. EGFR) and full preferred names (e.g. Epidermal growth factor receptor).
    """
    if not target_list:
        return target_list, target_list
    try:
        db_path = str(DATABASE_DIR / "chembl_36.db")
        if not os.path.exists(db_path):
            return target_list, target_list
        with sqlite3.connect(db_path) as conn:
            placeholders = ",".join(["?"] * len(target_list))
            query = f"""
                SELECT td.chembl_id, td.pref_name, 
                       (SELECT csy2.component_synonym 
                        FROM target_components tc2 
                        JOIN component_synonyms csy2 ON tc2.component_id = csy2.component_id 
                        WHERE tc2.tid = td.tid AND csy2.syn_type = 'GENE_SYMBOL' 
                        LIMIT 1) AS gene_symbol,
                       cs.accession, csy.component_synonym
                FROM target_dictionary td
                LEFT JOIN target_components tc ON td.tid = tc.tid
                LEFT JOIN component_sequences cs ON tc.component_id = cs.component_id
                LEFT JOIN component_synonyms csy ON cs.component_id = csy.component_id
                WHERE (
                    td.pref_name COLLATE NOCASE IN ({placeholders}) OR
                    td.chembl_id COLLATE NOCASE IN ({placeholders}) OR
                    cs.accession COLLATE NOCASE IN ({placeholders}) OR
                    csy.component_synonym COLLATE NOCASE IN ({placeholders})
                )
                AND td.target_type = 'SINGLE PROTEIN'
                AND td.organism = 'Homo sapiens'
            """
            rows = conn.execute(query, target_list * 4).fetchall()
            
            sym_map = {}
            name_map = {}
            for cid, pref_name, sym, acc, csy_syn in rows:
                p_name = pref_name or sym or cid or acc
                s_name = sym or pref_name or cid or acc
                if cid:
                    sym_map[str(cid).lower()] = s_name
                    name_map[str(cid).lower()] = p_name
                if pref_name:
                    sym_map[str(pref_name).lower()] = s_name
                    name_map[str(pref_name).lower()] = p_name
                if sym:
                    sym_map[str(sym).lower()] = s_name
                    name_map[str(sym).lower()] = p_name
                if acc:
                    sym_map[str(acc).lower()] = s_name
                    name_map[str(acc).lower()] = p_name
                if csy_syn:
                    sym_map[str(csy_syn).lower()] = s_name
                    name_map[str(csy_syn).lower()] = p_name
            
            symbols = [sym_map.get(str(t).lower(), t) for t in target_list]
            names = [name_map.get(str(t).lower(), t) for t in target_list]
            return symbols, names
    except Exception:
        return target_list, target_list


def _map_targets_to_gene_symbols(target_list):
    """Map a list of target names/IDs/pref_names to their gene symbols if available."""
    symbols, _ = _get_target_info(target_list)
    return symbols


def _extract_compound_labels(df):
    """Extract preferred compound labels for heatmap (Molecule_ChEMBL_ID -> InChIKey -> Compound_Name -> Index)."""
    if "Molecule_ChEMBL_ID" in df.columns:
        labels = []
        for i, val in enumerate(df["Molecule_ChEMBL_ID"]):
            val_str = str(val).strip() if pd.notna(val) else ""
            if val_str and val_str not in ("nan", "None", "Unknown"):
                labels.append(val_str)
            elif "InChIKey" in df.columns and pd.notna(df["InChIKey"].iloc[i]) and str(df["InChIKey"].iloc[i]).strip() not in ("nan", "None", "Unknown"):
                labels.append(str(df["InChIKey"].iloc[i]).strip())
            elif "Compound_Name" in df.columns and pd.notna(df["Compound_Name"].iloc[i]) and str(df["Compound_Name"].iloc[i]).strip() not in ("nan", "None", "Unknown"):
                labels.append(str(df["Compound_Name"].iloc[i]).strip())
            else:
                labels.append(f"Compound {i+1}")
        return labels
    elif "InChIKey" in df.columns:
        return [str(x).strip() if pd.notna(x) and str(x).strip() not in ("nan", "None", "Unknown") else f"Compound {i+1}" for i, x in enumerate(df["InChIKey"])]
    elif "Compound_Name" in df.columns:
        return [str(x).strip() if pd.notna(x) and str(x).strip() not in ("nan", "None", "Unknown") else f"Compound {i+1}" for i, x in enumerate(df["Compound_Name"])]
    else:
        return [str(x) for x in df.index.tolist()]


def _build_heatmap_cache(df):
    """Pre-compute the heatmap JSON dict so /api/heatmap-data is instant."""
    sel_cols = [c for c in df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "SMILES", "Price_USD_per_mg", "InChIKey"}]
    target_symbols, target_names = _get_target_info(sel_cols)
    return {
        "matrix": df[sel_cols].astype(object).where(pd.notna(df[sel_cols]), None).values.tolist(),
        "compounds": _extract_compound_labels(df),
        "targets": target_symbols,
        "target_names": target_names,
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
    compounds = _extract_compound_labels(df)
    targets, target_names = _get_target_info(sel_cols)

    return jsonify({
        "matrix": matrix,
        "compounds": compounds,
        "targets": targets,
        "target_names": target_names,
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
    xlsx_path = csv_path.replace(".csv", ".xlsx")

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
