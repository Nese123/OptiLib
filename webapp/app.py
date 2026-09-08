"""
Drug Library Optimization — Flask Web Application

Full pipeline: Upload targets → ChEMBL query → selectivity matrix → NSGA-II → results dashboard.
"""

import os
import sys
import json
import uuid
import time
import shutil
import atexit
import sqlite3
import threading
import logging
import warnings
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file, session
from pymoo.core.callback import Callback
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

# ═══════════════════════════════════════════════════════════════
#  PATH SETUP & ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = PROJECT_ROOT / "database"
MOLPRICE_DIR = PROJECT_ROOT / "MolPrice"

# Automatically load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Configure logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("optilib")

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
    save_progress_history_plot,
    find_knee_point,
    reorder_meta_columns,
)

# Suppress sklearn version mismatch warning from MolPrice's pickled scaler
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

# ═══════════════════════════════════════════════════════════════
#  FLASK APP & SECURITY CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Security and session settings
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("true", "1", "yes")
app.config["WTF_CSRF_TIME_LIMIT"] = int(os.environ.get("WTF_CSRF_TIME_LIMIT", 7200))  # 2 hours (7200 seconds)

secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    logger.warning("SECRET_KEY environment variable is not set! Using ephemeral key.")
    secret_key = os.urandom(32).hex()
app.secret_key = secret_key

# CSRF protection (double-submit cookie pattern for AJAX)
csrf = CSRFProtect(app)

# Rate limiter setup
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[os.environ.get("RATE_LIMIT_DEFAULT", "120 per minute")],
    storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URL", "memory://"),
    strategy="fixed-window",
)


@app.errorhandler(CSRFError)
def csrf_error_handler(e):
    """Clean JSON response for CSRF validation and session expiration failures."""
    return jsonify({
        "error": "Your session or security token has expired. Please refresh the page.",
        "reason": getattr(e, "description", str(e)),
        "status": 400
    }), 400


@app.errorhandler(429)
def ratelimit_handler(e):
    """Clean JSON response for rate limit violations."""
    return jsonify({
        "error": "Rate limit exceeded. Please wait a moment before making more requests.",
        "status": 429
    }), 429


@app.after_request
def set_security_headers(response):
    """Add standard HTTP security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.plot.ly; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    return response


def get_chembl_db_path() -> Path:
    """Return path to active ChEMBL database (defaults to chembl_37.db, falls back to chembl_36.db or any chembl_*.db)."""
    custom = os.environ.get("CHEMBL_DB_NAME")
    if custom:
        custom_path = DATABASE_DIR / custom
        if custom_path.exists():
            return custom_path
    for candidate in ["chembl_37.db", "chembl_36.db", "chembl.db"]:
        p = DATABASE_DIR / candidate
        if p.exists():
            return p
    return DATABASE_DIR / "chembl_37.db"


def _init_sqlite_wal():
    """Ensure SQLite databases use WAL mode for non-blocking concurrent reads and writes."""
    for db_name in ["molport.db", "chembl_37.db", "chembl_36.db"]:
        db_file = DATABASE_DIR / db_name
        if db_file.exists():
            try:
                with sqlite3.connect(str(db_file), timeout=10.0) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                logger.info(f"SQLite WAL mode active on {db_name}")
            except Exception as e:
                logger.warning(f"Could not activate WAL mode on {db_name}: {e}")

_init_sqlite_wal()

# ═══════════════════════════════════════════════════════════════
#  SESSION-SCOPED STATE & INACTIVITY CLEANER
# ═══════════════════════════════════════════════════════════════

# Inactivity TTL and background cleanup frequency
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 7200))       # 2 hours (7200 seconds)
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 300))  # 5 minutes

# Thread lock for state access
_lock = threading.Lock()

# In-memory session store: sid -> dict of per-user state dicts
_sessions = {}


def _make_fresh_state():
    """Create a fresh set of state dicts for a new user session."""
    return {
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
            "smiles": None,             # NumPy array (SMILES)
            "num_drugs": 0,
            "num_targets": 0,
            "total_cost": 0.0,
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
    }


def _get_session():
    """Get or create session-scoped state for the current request.

    Uses Flask's signed cookie session to identify the user.
    Must be called from within a Flask request context (i.e. route handlers).
    """
    sid = session.get("sid")
    if sid is None:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    with _lock:
        if sid not in _sessions:
            _sessions[sid] = _make_fresh_state()
        else:
            _sessions[sid]["last_activity"] = time.time()
        return sid, _sessions[sid]


def _get_session_by_sid(sid):
    """Get session state by ID (for use in background threads where Flask context is unavailable)."""
    with _lock:
        if sid not in _sessions:
            _sessions[sid] = _make_fresh_state()
        else:
            _sessions[sid]["last_activity"] = time.time()
        return _sessions[sid]


def _cleanup_stale_sessions():
    """Clean up expired in-memory sessions and all expired output folders/files."""
    now = time.time()
    output_base = PROJECT_ROOT / "webapp" / "output"

    # 1. Identify expired sessions in memory
    expired_sids = []
    with _lock:
        for sid, s in list(_sessions.items()):
            # Do not clean up if an active computation is running
            is_pipeline_running = s.get("pipeline_state", {}).get("status") == "running"
            is_opt_running = s.get("opt_state", {}).get("status") == "running"
            if is_pipeline_running or is_opt_running:
                continue

            last_act = s.get("last_activity", 0)
            if now - last_act > SESSION_TTL_SECONDS:
                expired_sids.append(sid)
                del _sessions[sid]

    # 2. Remove session output folders for expired sessions
    for sid in expired_sids:
        s_dir = output_base / sid
        if s_dir.exists():
            shutil.rmtree(s_dir, ignore_errors=True)

    # 3. Clean up unmanaged/orphan session folders or stale files in webapp/output
    if output_base.exists():
        try:
            for item in output_base.iterdir():
                # Subdirectories (session folders)
                if item.is_dir():
                    dir_sid = item.name
                    with _lock:
                        is_active_session = dir_sid in _sessions
                    if not is_active_session:
                        try:
                            mtime = item.stat().st_mtime
                            if now - mtime > SESSION_TTL_SECONDS:
                                shutil.rmtree(item, ignore_errors=True)
                        except OSError:
                            pass
                # Loose files in output root (e.g. legacy/cached files)
                elif item.is_file():
                    try:
                        mtime = item.stat().st_mtime
                        if now - mtime > SESSION_TTL_SECONDS:
                            item.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError:
            pass


def _wipe_output_dir():
    """Remove all files and directories from the output folder."""
    output_base = PROJECT_ROOT / "webapp" / "output"
    if not output_base.exists():
        return
    try:
        for item in output_base.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Output cleanup error: {e}")


def _cleanup_worker():
    """Periodic background daemon worker that runs cleanup sweeps."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _cleanup_stale_sessions()
        except Exception as e:
            logger.error(f"Error in output cleanup worker: {e}")


# Run startup cleanup of stale generated files
_wipe_output_dir()

# Start background cleanup thread as daemon
_cleanup_thread = threading.Thread(target=_cleanup_worker, name="OutputCleanupWorker", daemon=True)
_cleanup_thread.start()

# Register shutdown hook
atexit.register(_wipe_output_dir)


class StopOptimization(Exception):
    pass


class WebappCallback(Callback):
    def __init__(self, problem, session_opt_state):
        super().__init__()
        self.problem = problem
        self._opt_state = session_opt_state
        # Snapshot of the latest algorithm state for early-stop result extraction
        self.last_pop_X = None
        self.last_pop_F = None
        self.last_pop_G = None

    def notify(self, algorithm):

        # Always snapshot the current population before any stop check,
        # so if we stop we have the latest state available.
        self.last_pop_X = algorithm.pop.get("X").copy()
        self.last_pop_F = algorithm.pop.get("F").copy()
        self.last_pop_G = algorithm.pop.get("G").copy()

        with _lock:
            self._opt_state["generation"] = algorithm.n_gen
            if self._opt_state.get("stop_requested"):
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

            self._opt_state["history"].append({
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


def _recompute_affinity_state(aff_state):
    """Recompute combined dataframe, resolved entities, and stats across all uploaded affinity files."""
    files_dict = aff_state.get("files", {})

    all_dfs = []
    all_resolved_compounds = {}
    all_resolved_targets = {}

    for fname, fdata in list(files_dict.items()):
        fdf = fdata.get("df")
        if fdf is not None and not fdf.empty:
            all_dfs.append(fdf)
            all_resolved_compounds.update(fdata.get("resolved_compounds", {}))
            all_resolved_targets.update(fdata.get("resolved_targets", {}))

    if not all_dfs:
        aff_state["df"] = None
        aff_state["resolved_compounds"] = {}
        aff_state["resolved_targets"] = {}
        aff_state["num_compounds"] = 0
        aff_state["num_targets"] = 0
        aff_state["num_datapoints"] = 0
        aff_state["unique_targets"] = []
        aff_state["formatted_targets"] = []
        aff_state["formatted_compounds"] = []
        return

    combined_df = pd.concat(all_dfs, ignore_index=True).drop_duplicates()
    unique_compounds = combined_df["Compound_Raw"].unique().tolist()
    unique_targets = combined_df["Target_Raw"].unique().tolist()

    missing_compounds = [c for c in unique_compounds if c not in all_resolved_compounds]
    if missing_compounds:
        all_resolved_compounds.update(_resolve_compounds(missing_compounds))

    missing_targets = [t for t in unique_targets if t not in all_resolved_targets]
    if missing_targets:
        all_resolved_targets.update(_resolve_targets(missing_targets))

    formatted_compounds = [_format_compound_display(c, all_resolved_compounds.get(c)) for c in unique_compounds]
    formatted_targets = [_format_target_display(t, all_resolved_targets.get(t)) for t in unique_targets]

    aff_state["df"] = combined_df
    aff_state["resolved_compounds"] = all_resolved_compounds
    aff_state["resolved_targets"] = all_resolved_targets
    aff_state["num_compounds"] = len(unique_compounds)
    aff_state["num_targets"] = len(unique_targets)
    aff_state["num_datapoints"] = len(combined_df)
    aff_state["unique_targets"] = [
        all_resolved_targets[t]["canonical_name"] for t in unique_targets if t in all_resolved_targets
    ]
    aff_state["formatted_compounds"] = formatted_compounds
    aff_state["formatted_targets"] = formatted_targets


def _recompute_price_state(price_state):
    """Recompute combined dataframe, price_map, and formatted_compounds across all uploaded files."""
    files_dict = price_state.get("files", {})

    all_dfs = []
    all_resolved = {}
    filenames = []

    for fname, fdata in list(files_dict.items()):
        fdf = fdata.get("df")
        if fdf is not None and not fdf.empty:
            all_dfs.append(fdf)
            filenames.append(fname)
            all_resolved.update(fdata.get("resolved_compounds", {}))

    if not all_dfs:
        price_state["df"] = None
        price_state["resolved_compounds"] = {}
        price_state["formatted_compounds"] = []
        price_state["price_map"] = {}
        price_state["filename"] = ""
        price_state["count"] = 0
        return

    combined_df = pd.concat(all_dfs, ignore_index=True)
    combined_df = combined_df.drop_duplicates(subset=["Compound"], keep="last")

    unique_cmpds = combined_df["Compound"].unique().tolist()

    price_map = {}
    for _, row in combined_df.iterrows():
        raw_c = str(row["Compound"]).strip()
        price_val = float(row["Price"])
        price_map[raw_c.lower()] = price_val
        price_map[raw_c.upper()] = price_val

        res = all_resolved.get(raw_c)
        if res:
            if res.get("chembl_id"):
                price_map[res["chembl_id"].upper()] = price_val
            if res.get("inchi_key"):
                price_map[res["inchi_key"].upper()] = price_val
            if res.get("smiles"):
                price_map[res["smiles"].strip()] = price_val
            if res.get("pref_name"):
                price_map[res["pref_name"].lower()] = price_val

    formatted_compounds = [_format_compound_display(c, all_resolved.get(c)) for c in unique_cmpds]

    price_state["df"] = combined_df
    price_state["resolved_compounds"] = all_resolved
    price_state["formatted_compounds"] = formatted_compounds
    price_state["price_map"] = price_map
    price_state["filename"] = ", ".join(filenames)
    price_state["count"] = len(combined_df)


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
    
    db_path = str(get_chembl_db_path())
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
        logger.warning(f"Warning during compound resolution: {e}")
        
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
            ik_guess = raw_str if _looks_like_inchikey(raw_str) else ""
            smi_guess = raw_str if _looks_like_smiles(raw_str) else ""
            resolved[raw_id] = {
                "raw_id": raw_id,
                "chembl_id": "",
                "pref_name": raw_str,
                "inchi_key": ik_guess,
                "smiles": smi_guess,
                "is_chembl": False,
            }
            
    return resolved


def _format_target_col(pref_name, gene_symbol, fallback=""):
    """Format target column header as 'Target Name (Gene Symbol)' if distinct, otherwise fallback."""
    p_name = _clean_str(pref_name)
    g_sym = _clean_str(gene_symbol)
    fb = _clean_str(fallback)

    if p_name and g_sym and p_name.lower() != g_sym.lower():
        return f"{p_name} ({g_sym})"
    elif p_name:
        return p_name
    elif g_sym:
        return g_sym
    elif fb:
        return fb
    else:
        return "Unknown"


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
    
    db_path = str(get_chembl_db_path())
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
        logger.warning(f"Warning during target resolution: {e}")
        
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
                # Preferred canonical display name: Target Name (Gene Symbol)
                canonical = _format_target_col(name, gene_sym, cid or target_in)
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


def _lookup_custom_price(compound_raw, resolved_info=None, price_state=None):
    """Smart lookup in custom price map across raw ID, ChEMBL ID, InChIKey, SMILES, and pref_name."""
    if price_state is None:
        return None
    with _lock:
        price_map = price_state.get("price_map", {})
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


# Sentinel strings to treat as empty
_SENTINEL_STRINGS = frozenset({"nan", "None", "Unknown", ""})


def _clean_str(val):
    """Normalise a value to a clean string, returning '' for NaN/None/sentinel values."""
    s = str(val).strip() if pd.notna(val) else ""
    return "" if s in _SENTINEL_STRINGS else s


def _format_compound_display(raw_id, resolved_info):
    """Format a resolved compound identifier for display."""
    ik = (resolved_info or {}).get("inchi_key") or ""
    cid = (resolved_info or {}).get("chembl_id") or ""
    if ik and cid:
        return f"{raw_id} -> {ik} ({cid})"
    elif ik:
        return f"{raw_id} -> {ik}"
    elif cid:
        return f"{raw_id} -> ({cid})"
    return str(raw_id)


def _format_target_display(raw_id, resolved_info):
    """Format a resolved target identifier for display."""
    info = resolved_info or {}
    if info.get("is_chembl"):
        name = info.get("pref_name") or info.get("gene_symbol") or info.get("chembl_id")
        gene_sym = info.get("gene_symbol") or info.get("chembl_id")
        if gene_sym:
            return f"{raw_id} -> {name} ({gene_sym})"
        return f"{raw_id} -> {name}"
    return str(raw_id)


def _looks_like_inchikey(s):
    """Heuristic check whether a string looks like an InChIKey."""
    return len(s) == 27 and s[14] == '-' and s[25] == '-'


def _looks_like_smiles(s):
    """Heuristic check whether a string looks like a SMILES string."""
    return bool('=' in s or '#' in s or '(' in s or 'c1' in s)


def _build_affinity_files_list(files_dict):
    """Build the per-file summary list for affinity API responses."""
    return [
        {
            "name": fname,
            "num_datapoints": len(fdata["df"]),
            "num_compounds": len(fdata["df"]["Compound_Raw"].unique()),
            "num_targets": len(fdata["df"]["Target_Raw"].unique()),
            "compounds": fdata["formatted_compounds"],
            "targets": fdata["formatted_targets"],
        }
        for fname, fdata in files_dict.items()
    ]


def _build_affinity_response(aff_state, extra=None):
    """Build the standard JSON response dict for affinity endpoints."""
    resp = {
        "num_compounds": aff_state["num_compounds"],
        "num_targets": aff_state["num_targets"],
        "num_datapoints": aff_state["num_datapoints"],
        "compounds": aff_state["formatted_compounds"],
        "targets": aff_state["formatted_targets"],
    }
    if extra:
        resp.update(extra)
    return resp


def _build_price_files_list(files_dict):
    """Build the per-file summary list for price API responses."""
    return [
        {
            "name": fname,
            "num_prices": len(fdata["df"]),
            "compounds": fdata["formatted_compounds"],
        }
        for fname, fdata in files_dict.items()
    ]


def _build_price_response(price_state, extra=None):
    """Build the standard JSON response dict for price endpoints."""
    combined_df = price_state.get("df")
    resp = {
        "num_prices": len(combined_df) if combined_df is not None else 0,
        "filename": price_state.get("filename", ""),
        "compounds": price_state.get("formatted_compounds", []),
    }
    if extra:
        resp.update(extra)
    return resp


def _lookup_molport_prices(inchikeys):
    """Look up prices from the MolPort database for a list of InChIKeys.

    Returns:
        (price_dict, source_dict): Mappings from InChIKey to median price and MolPort ID.
    """
    price_dict = {}
    source_dict = {}
    if not inchikeys:
        return price_dict, source_dict
    molport_db = str(DATABASE_DIR / "molport.db")
    try:
        with sqlite3.connect(molport_db) as conn:
            mp_chunk_size = 30000
            all_rows = []
            for i in range(0, len(inchikeys), mp_chunk_size):
                chunk = inchikeys[i:i + mp_chunk_size]
                ph = ",".join(["?"] * len(chunk))
                query = f"SELECT INCHIKEY, PRICE_1MG, MOLPORTID FROM compounds WHERE INCHIKEY IN ({ph})"
                all_rows.extend(conn.execute(query, chunk).fetchall())

            if all_rows:
                mp_df = pd.DataFrame(all_rows, columns=["INCHIKEY", "PRICE_1MG", "MOLPORTID"])
                # Source: first-seen MolPort ID per InChIKey
                source_df = mp_df.drop_duplicates(subset=["INCHIKEY"])
                source_dict = dict(zip(
                    source_df["INCHIKEY"],
                    source_df["MOLPORTID"].apply(lambda x: str(x) if x else ""),
                ))
                # Price: median per InChIKey
                price_dict = mp_df.groupby("INCHIKEY")["PRICE_1MG"].median().to_dict()
    except Exception as e:
        logger.warning(f"MolPort DB lookup warning: {e}")
    return price_dict, source_dict


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Pages & Health Checks
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/optimize")
def tool():
    return render_template("index.html")


@app.route("/favicon.ico")
@limiter.exempt
def favicon():
    return send_file(PROJECT_ROOT / "webapp" / "static" / "favicon.svg", mimetype="image/svg+xml")


@app.route("/health")
@app.route("/api/health")
@limiter.exempt
@csrf.exempt
def health_check():
    """Health check endpoint for Docker, Nginx, and cloud orchestrators."""
    chembl_file = get_chembl_db_path()
    chembl_exists = chembl_file.exists()
    molport_exists = (DATABASE_DIR / "molport.db").exists()
    is_healthy = chembl_exists and molport_exists
    return jsonify({
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": time.time(),
        "databases": {
            "chembl": chembl_exists,
            "chembl_37": chembl_exists,
            "molport": molport_exists,
        }
    }), (200 if is_healthy else 503)


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Target Upload & Validation
# ═══════════════════════════════════════════════════════════════

@app.route("/api/upload-targets", methods=["POST"])
@limiter.limit("30 per minute")
def upload_targets():
    """Accept CSV/Excel with target names/IDs, validate against ChEMBL."""

    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    all_targets = []
    for file in files:
        if file.filename == "":
            continue

        safe_name = secure_filename(file.filename) or "upload"
        try:
            filename = safe_name.lower()
            if filename.endswith(".csv"):
                df = pd.read_csv(file)
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(file)
            else:
                return jsonify({"error": f"Unsupported file type for {safe_name}. Use CSV or Excel (.xlsx)."}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to read {safe_name}: {str(e)}"}), 400

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
                    "error": f"Could not find Target column in {safe_name}."
                }), 400

        file_targets = df[target_col].dropna().astype(str).str.strip().tolist()
        all_targets.extend(file_targets)

    # Get unique targets while preserving order
    input_targets = list(dict.fromkeys(all_targets))
    
    if not input_targets:
        return jsonify({"error": "No targets found in the files"}), 400

    resolved = _resolve_targets(input_targets)

    matched = []
    unmatched = []
    matched_chembl_ids = set()
    chembl_map = {}

    for target_in in input_targets:
        info = resolved.get(target_in, {})
        if info.get("is_chembl"):
            cid = info["chembl_id"]
            if cid and cid not in matched_chembl_ids:
                matched_chembl_ids.add(cid)
                display_name = info.get("pref_name") or info.get("gene_symbol") or cid
                bracket_symbol = info.get("gene_symbol") or cid
                if bracket_symbol:
                    match_str = f"{target_in} -> {display_name} ({bracket_symbol})"
                else:
                    match_str = f"{target_in} -> {display_name}"
                matched.append(match_str)
                chembl_map[match_str] = cid
        else:
            unmatched.append(target_in)

    chembl_ids = list(matched_chembl_ids)

    with _lock:
        pipeline_st["matched_targets"] = matched
        pipeline_st["unmatched_targets"] = unmatched

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
@limiter.limit("30 per minute")
def upload_affinity():
    """Accept CSV/Excel with compound, target, and affinity value (supports incremental multi-file upload)."""
    sid, s = _get_session()
    aff_state = s["affinity_upload_state"]

    files = request.files.getlist("files[]") or request.files.getlist("file")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    uploaded_files_summary = []

    for file in files:
        if not file or file.filename == "":
            continue
        safe_name = secure_filename(file.filename) or "upload"
        try:
            filename = safe_name.lower()
            if filename.endswith(".csv"):
                df = pd.read_csv(file)
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(file)
            else:
                return jsonify({"error": f"Unsupported file type for {safe_name}. Use CSV or Excel (.xlsx)."}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to read {safe_name}: {str(e)}"}), 400

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
                "error": f"Could not identify Compound, Target, and Affinity columns in {safe_name}. "
                         f"Please ensure columns are named 'Compound', 'Target', and 'Affinity'."
            }), 400

        sub_df = pd.DataFrame({
            "Compound_Raw": df[cmpd_col].dropna().astype(str).str.strip(),
            "Target_Raw": df[tgt_col].dropna().astype(str).str.strip(),
            "Affinity": pd.to_numeric(df[aff_col], errors="coerce")
        }).dropna()
        sub_df = sub_df.drop_duplicates()

        if sub_df.empty:
            return jsonify({"error": f"No valid data rows found in {safe_name}."}), 400

        file_compounds = sub_df["Compound_Raw"].unique().tolist()
        file_targets = sub_df["Target_Raw"].unique().tolist()

        res_compounds = _resolve_compounds(file_compounds)
        res_targets = _resolve_targets(file_targets)

        formatted_c = [_format_compound_display(c, res_compounds.get(c)) for c in file_compounds]
        formatted_t = [_format_target_display(t, res_targets.get(t)) for t in file_targets]

        with _lock:
            if "files" not in aff_state:
                aff_state["files"] = {}
            aff_state["files"][safe_name] = {
                "df": sub_df,
                "resolved_compounds": res_compounds,
                "resolved_targets": res_targets,
                "formatted_compounds": formatted_c,
                "formatted_targets": formatted_t,
            }

        uploaded_files_summary.append({
            "name": safe_name,
            "num_datapoints": len(sub_df),
            "num_compounds": len(file_compounds),
            "num_targets": len(file_targets),
            "compounds": formatted_c,
            "targets": formatted_t,
        })

    if not uploaded_files_summary:
        return jsonify({"error": "No valid affinity files processed."}), 400

    with _lock:
        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(aff_state["files"])

    return jsonify({
        "uploaded_files": uploaded_files_summary,
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    })


@app.route("/api/upload-prices", methods=["POST"])
@limiter.limit("30 per minute")
def upload_prices():
    """Accept CSV/Excel with compound and price (supports incremental multi-file upload)."""
    sid, s = _get_session()
    price_state = s["price_upload_state"]

    files = request.files.getlist("files[]") or request.files.getlist("file")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "No price file uploaded"}), 400

    uploaded_files_summary = []

    for file in files:
        if not file or file.filename == "":
            continue

        safe_name = secure_filename(file.filename) or "upload"
        try:
            filename = safe_name.lower()
            if filename.endswith(".csv"):
                df = pd.read_csv(file)
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(file)
            else:
                return jsonify({"error": f"Unsupported file type for {safe_name}. Use CSV or Excel (.xlsx)."}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to read price file {safe_name}: {str(e)}"}), 400

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
                "error": f"Could not identify Compound and Price columns in {safe_name}. Please use 'Compound' and 'Price'."
            }), 400

        clean_df = pd.DataFrame({
            "Compound": df[cmpd_col].dropna().astype(str).str.strip(),
            "Price": pd.to_numeric(df[price_col], errors="coerce")
        }).dropna()
        clean_df = clean_df[clean_df["Price"] > 0]
        clean_df = clean_df.drop_duplicates(subset=["Compound"], keep="last")

        if clean_df.empty:
            return jsonify({"error": f"No valid positive price rows found in {safe_name}."}), 400

        unique_cmpds = clean_df["Compound"].unique().tolist()
        resolved_cmpds = _resolve_compounds(unique_cmpds)

        file_formatted = [_format_compound_display(c, resolved_cmpds.get(c)) for c in unique_cmpds]

        with _lock:
            if "files" not in price_state:
                price_state["files"] = {}
            price_state["files"][safe_name] = {
                "df": clean_df,
                "resolved_compounds": resolved_cmpds,
                "formatted_compounds": file_formatted,
            }

        uploaded_files_summary.append({
            "name": safe_name,
            "num_prices": len(clean_df),
            "compounds": file_formatted
        })

    if not uploaded_files_summary:
        return jsonify({"error": "No valid price files uploaded."}), 400

    with _lock:
        _recompute_price_state(price_state)
        all_files_list = _build_price_files_list(price_state["files"])

    return jsonify({
        "uploaded_files": uploaded_files_summary,
        "all_files": all_files_list,
        **_build_price_response(price_state),
    })


@app.route("/api/remove-affinity-file", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_file():
    """Remove a specific uploaded affinity file by name."""
    sid, s = _get_session()
    aff_state = s["affinity_upload_state"]

    data = request.get_json(force=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "No filename specified"}), 400

    with _lock:
        files_dict = aff_state.get("files", {})
        if filename in files_dict:
            del files_dict[filename]
        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(files_dict)

    return jsonify({
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    })


@app.route("/api/remove-affinity-target", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_target():
    """Remove a single target from the uploaded affinity dataset across all files."""
    sid, s = _get_session()
    aff_state = s["affinity_upload_state"]

    data = request.get_json(force=True) or {}
    target_str = data.get("target", "").strip()
    if not target_str:
        return jsonify({"error": "No target specified"}), 400

    target_raw = target_str.split(" ->")[0].strip().lower()

    with _lock:
        files_dict = aff_state.get("files", {})
        for fname, fdata in list(files_dict.items()):
            fdf = fdata.get("df")
            if fdf is not None and not fdf.empty:
                mask = (
                    (fdf["Target_Raw"].astype(str).str.strip().str.lower() != target_raw) &
                    (fdf["Target_Raw"].astype(str).str.strip() != target_str)
                )
                filtered_df = fdf[mask].copy()
                if filtered_df.empty:
                    del files_dict[fname]
                else:
                    fdata["df"] = filtered_df
                    fdata["formatted_targets"] = [
                        ft for ft in fdata.get("formatted_targets", [])
                        if ft != target_str and ft.split(" ->")[0].strip().lower() != target_raw
                    ]
                    file_cmpds_set = set(filtered_df["Compound_Raw"].unique())
                    fdata["formatted_compounds"] = [
                        fc for fc in fdata.get("formatted_compounds", [])
                        if fc.split(" ->")[0].strip() in file_cmpds_set
                    ]

        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(files_dict)

    return jsonify({
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    })


@app.route("/api/remove-affinity-compound", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_compound():
    """Remove a single compound from the uploaded affinity dataset across all files."""
    sid, s = _get_session()
    aff_state = s["affinity_upload_state"]

    data = request.get_json(force=True) or {}
    compound_str = data.get("compound", "").strip()
    if not compound_str:
        return jsonify({"error": "No compound specified"}), 400

    compound_raw = compound_str.split(" ->")[0].strip().lower()

    with _lock:
        files_dict = aff_state.get("files", {})
        for fname, fdata in list(files_dict.items()):
            fdf = fdata.get("df")
            if fdf is not None and not fdf.empty:
                mask = (
                    (fdf["Compound_Raw"].astype(str).str.strip().str.lower() != compound_raw) &
                    (fdf["Compound_Raw"].astype(str).str.strip() != compound_str)
                )
                filtered_df = fdf[mask].copy()
                if filtered_df.empty:
                    del files_dict[fname]
                else:
                    fdata["df"] = filtered_df
                    fdata["formatted_compounds"] = [
                        fc for fc in fdata.get("formatted_compounds", [])
                        if fc != compound_str and fc.split(" ->")[0].strip().lower() != compound_raw
                    ]
                    file_tgts_set = set(filtered_df["Target_Raw"].unique())
                    fdata["formatted_targets"] = [
                        ft for ft in fdata.get("formatted_targets", [])
                        if ft.split(" ->")[0].strip() in file_tgts_set
                    ]

        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(files_dict)

    return jsonify({
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    })


@app.route("/api/clear-affinity", methods=["POST"])
@limiter.limit("60 per minute")
def clear_affinity():
    sid, s = _get_session()
    aff_state = s["affinity_upload_state"]
    with _lock:
        aff_state["files"] = {}
        _recompute_affinity_state(aff_state)
    return jsonify({"status": "cleared"})


@app.route("/api/remove-price-file", methods=["POST"])
@limiter.limit("60 per minute")
def remove_price_file():
    """Remove a specific uploaded price file by name."""
    sid, s = _get_session()
    price_state = s["price_upload_state"]

    data = request.get_json(force=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"error": "No filename specified"}), 400

    with _lock:
        files_dict = price_state.get("files", {})
        if filename in files_dict:
            del files_dict[filename]
        _recompute_price_state(price_state)
        all_files_list = _build_price_files_list(files_dict)

    return jsonify({
        "all_files": all_files_list,
        **_build_price_response(price_state),
    })


@app.route("/api/remove-price-compound", methods=["POST"])
@limiter.limit("60 per minute")
def remove_price_compound():
    """Remove a single compound from the uploaded price dataset."""
    sid, s = _get_session()
    price_state = s["price_upload_state"]

    data = request.get_json(force=True) or {}
    compound_str = data.get("compound", "").strip()
    if not compound_str:
        return jsonify({"error": "No compound specified"}), 400

    compound_raw = compound_str.split(" ->")[0].strip().lower()

    with _lock:
        files_dict = price_state.get("files", {})
        for fname, fdata in list(files_dict.items()):
            fdf = fdata.get("df")
            if fdf is not None and not fdf.empty:
                mask = (
                    (fdf["Compound"].astype(str).str.strip().str.lower() != compound_raw) &
                    (fdf["Compound"].astype(str).str.strip() != compound_str)
                )
                new_fdf = fdf[mask].copy()
                fdata["df"] = new_fdf
                fdata["formatted_compounds"] = [
                    fc for fc in fdata.get("formatted_compounds", [])
                    if fc != compound_str and fc.split(" ->")[0].strip().lower() != compound_raw
                ]

        _recompute_price_state(price_state)
        all_files_list = _build_price_files_list(files_dict)

    return jsonify({
        "all_files": all_files_list,
        **_build_price_response(price_state),
    })


@app.route("/api/clear-prices", methods=["POST"])
@limiter.limit("60 per minute")
def clear_prices():
    sid, s = _get_session()
    price_state = s["price_upload_state"]
    with _lock:
        price_state["files"] = {}
        price_state["df"] = None
        price_state["resolved_compounds"] = {}
        price_state["formatted_compounds"] = []
        price_state["price_map"] = {}
        price_state["filename"] = ""
        price_state["count"] = 0
    return jsonify({"status": "cleared"})


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Pipeline (Build Matrix)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/build-matrix", methods=["POST"])
@limiter.limit("20 per minute")
def build_matrix():
    """Launch the full pipeline in a background thread."""
    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]
    opt_st = s["opt_state"]

    with _lock:
        if pipeline_st["status"] == "running":
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
        pipeline_st.update({
            "status": "running",
            "current_step": 0,
            "step_label": "Starting...",
            "detail": "",
            "error": "",
            "step_summaries": {},
        })
        opt_st.update({"status": "idle", "generation": 0, "error": ""})

    thread = threading.Thread(
        target=_run_pipeline,
        args=(sid, chembl_ids, selectivity_threshold, remove_targets, matched_count),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/build-matrix-from-affinity", methods=["POST"])
@limiter.limit("20 per minute")
def build_matrix_from_affinity():
    """Launch the affinity-based pipeline in a background thread."""
    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]
    opt_st = s["opt_state"]
    aff_state = s["affinity_upload_state"]

    with _lock:
        if pipeline_st["status"] == "running":
            return jsonify({"error": "Pipeline is already running"}), 409
        if aff_state["df"] is None or aff_state["df"].empty:
            return jsonify({"error": "No affinity data uploaded. Please upload an affinity file first."}), 400

    data = request.get_json(force=True) or {}
    selectivity_threshold = float(data.get("selectivity_threshold", 0.5))
    remove_targets = bool(data.get("remove_targets", True))

    # Reset states
    with _lock:
        pipeline_st.update({
            "status": "running",
            "current_step": 0,
            "step_label": "Starting...",
            "detail": "",
            "error": "",
            "step_summaries": {},
        })
        opt_st.update({"status": "idle", "generation": 0, "error": ""})

    thread = threading.Thread(
        target=_run_affinity_pipeline,
        args=(sid, selectivity_threshold, remove_targets),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/pipeline-status")
@limiter.limit("300 per minute")
def pipeline_status():
    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]
    with _lock:
        return jsonify({**pipeline_st})


def _update_pipeline(sid, step, label, detail="", summary=None):
    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    with _lock:
        pipeline_st["current_step"] = step
        pipeline_st["step_label"] = label
        pipeline_st["detail"] = detail
        if summary is not None:
            pipeline_st["step_summaries"][step] = summary


def _run_pipeline(sid, chembl_ids, selectivity_threshold, remove_targets=True, matched_count=0):
    """Full pipeline: ChEMBL → pChEMBL rescue → selectivity → prices → save."""
    import hashlib

    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    ds = s["dataset"]
    price_state = s["price_upload_state"]

    try:
        output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate cache key based on inputs
        cache_str = f"v3_{sorted(chembl_ids)}_{selectivity_threshold}_{remove_targets}_{matched_count}"
        cache_key = hashlib.md5(cache_str.encode('utf-8')).hexdigest()
        matrix_file = str(output_dir / f"selectivity_matrix_{cache_key}.csv")
        
        if os.path.exists(matrix_file):
            _update_pipeline(sid, 1, "Loading cached matrix...", "Found a previously computed selectivity matrix for these parameters.")
            final_export_df = pd.read_csv(matrix_file)
            
            target_cols = [c for c in final_export_df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"}]
            with _lock:
                ds["selectivities"] = final_export_df[target_cols].to_numpy(dtype=float)
                ds["prices"] = final_export_df["Price_USD_per_mg"].to_numpy(dtype=float)
                ds["smiles"] = final_export_df["SMILES"].to_numpy()
                ds["num_drugs"] = len(final_export_df)
                ds["num_targets"] = len(target_cols)
                ds["total_cost"] = float(np.sum(ds["prices"]))
                ds["matrix_file"] = matrix_file
                ds["ready"] = True
                ds["has_custom_affinity"] = False
    
                pipeline_st["status"] = "complete"
                pipeline_st["current_step"] = 3
                pipeline_st["step_label"] = "Done"
                pipeline_st["detail"] = f"Loaded cached matrix: {ds['num_drugs']} compounds × {ds['num_targets']} targets"
            return

        # ─────────────────────────────────────────────
        # Step 1: Searching for selective compounds
        # ─────────────────────────────────────────────
        _update_pipeline(sid, 1, "Searching for selective compounds...", f"Querying database for compounds active against {matched_count} targets...")

        db_path = str(get_chembl_db_path())

        # Build WHERE clause
        id_ph = ",".join(["?"] * len(chembl_ids))
        where_targets = f"td.chembl_id IN ({id_ph})"
        params = [cid.upper() for cid in chembl_ids]

        # Filter: Drop targets that do not have any compound with pChEMBL > 5.0
        query_active_targets = f"""
            SELECT DISTINCT td.chembl_id
            FROM target_dictionary td
            JOIN assays ass ON td.tid = ass.tid
            JOIN activities act ON act.assay_id = ass.assay_id
            WHERE ({where_targets})
              AND td.target_type = 'SINGLE PROTEIN'
              AND td.organism = 'Homo sapiens'
              AND ass.confidence_score IN (8, 9)
              AND act.pchembl_value > 5.0;
        """
        with sqlite3.connect(db_path) as conn:
            active_targets_df = pd.read_sql_query(query_active_targets, conn, params=params)

        active_chembl_ids = set(active_targets_df["chembl_id"].str.upper()) if not active_targets_df.empty else set()
        dropped_targets_no_pchembl = [cid for cid in chembl_ids if cid.upper() not in active_chembl_ids]

        if dropped_targets_no_pchembl:
            logger.info(
                f"Dropping {len(dropped_targets_no_pchembl)} target(s) with no compound having pChEMBL > 5.0: "
                f"{dropped_targets_no_pchembl}"
            )
            _update_pipeline(
                sid, 1, "Searching for selective compounds...",
                f"Dropped {len(dropped_targets_no_pchembl)} target(s) lacking compounds with pChEMBL > 5.0..."
            )

        if not active_chembl_ids:
            raise ValueError("None of the provided targets have any compounds with pChEMBL > 5.0 in high-confidence human single-protein assays.")

        # Retain only qualifying targets for downstream query and matrix building
        chembl_ids = [cid for cid in chembl_ids if cid.upper() in active_chembl_ids]
        id_ph = ",".join(["?"] * len(chembl_ids))
        where_targets = f"td.chembl_id IN ({id_ph})"
        params = [cid.upper() for cid in chembl_ids]

        # Get the actual names of the uploaded targets
        query0 = f"""
            SELECT td.chembl_id, td.pref_name,
                   (SELECT csy.component_synonym 
                    FROM target_components tc 
                    JOIN component_synonyms csy ON tc.component_id = csy.component_id 
                    WHERE tc.tid = td.tid AND csy.syn_type = 'GENE_SYMBOL' 
                    LIMIT 1) AS gene_symbol
            FROM target_dictionary td
            WHERE ({where_targets})
        """
        with sqlite3.connect(db_path) as conn:
            uploaded_targets_df = pd.read_sql_query(query0, conn, params=params)
        uploaded_target_names = [
            _format_target_col(r["pref_name"], r["gene_symbol"], r["chembl_id"])
            for _, r in uploaded_targets_df.iterrows()
        ]

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
                td.pref_name AS Target_Pref_Name,
                (SELECT csy.component_synonym 
                 FROM target_components tc 
                 JOIN component_synonyms csy ON tc.component_id = csy.component_id 
                 WHERE tc.tid = td.tid AND csy.syn_type = 'GENE_SYMBOL' 
                 LIMIT 1) AS Target_Gene_Symbol,
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
                      AND act.pchembl_value > 5.0
              );
        """
        
        with sqlite3.connect(db_path) as conn:
            # Supply params twice: once for outer WHERE, once for subquery WHERE
            chunks = []
            compounds_so_far = set()
            for chunk in pd.read_sql_query(query1, conn, params=params + params, chunksize=1000):
                chunks.append(chunk)
                compounds_so_far.update(chunk['Clean_Molregno'])
                _update_pipeline(sid, 1, "Searching for selective compounds...",
                                 f"Found {len(compounds_so_far)} compounds so far...")
            
            if chunks:
                df_raw = pd.concat(chunks, ignore_index=True)
            else:
                df_raw = pd.DataFrame()

        compounds_found_initial = df_raw['Clean_Molregno'].nunique() if not df_raw.empty else 0
        if compounds_found_initial == 0:
            raise ValueError("No highly active and selective compounds found for the provided targets.")
        df_raw["Target_Name"] = [
            _format_target_col(p, g, c)
            for p, g, c in zip(df_raw["Target_Pref_Name"], df_raw["Target_Gene_Symbol"], df_raw["Target_ChEMBL_ID"])
        ]
        _update_pipeline(sid, 1, "Searching for selective compounds...",
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

        _update_pipeline(sid, 1, "Searching for selective compounds...",
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
        _update_pipeline(sid, 2, "Getting price data...", "Querying database for prices")

        inchikeys = final_export_df["InChIKey"].dropna().unique().tolist()
        molport_dict, molport_source_dict = _lookup_molport_prices(inchikeys)

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
            p = _lookup_custom_price(cmpd_name, res_info, price_state)
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
        has_custom_price = bool(price_state.get("files")) or bool(price_state.get("price_map")) or (price_state.get("count", 0) > 0) or (custom_matched_count > 0)
        custom_part_summary = f"{custom_matched_count} prices assigned from custom price file, " if has_custom_price else ""
        custom_part_detail = f"Custom: {custom_matched_count}, " if has_custom_price else ""
        
        _update_pipeline(sid, 2, "Getting price data...",
                         f"Found prices for {found_count}/{len(final_export_df)} compounds ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})")

        # ─────────────────────────────────────────────
        # Handle missing prices
        # ─────────────────────────────────────────────
        missing_price_mask = final_export_df["Molport_Price"].isna()
        missing_count = int(missing_price_mask.sum())

        if missing_count > 0:
            _update_pipeline(sid, 2, "Getting price data...",
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
                molprice_approx_count += missing_count

                _update_pipeline(sid, 2, "Getting price data...",
                                 f"All prices assigned ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})",
                                 summary=f"All prices assigned. {custom_part_summary}{molport_direct_count} prices found from the MolPort database, {molprice_approx_count} prices approximated using MolPrice.")
            except Exception as e:
                fallback = final_export_df["Molport_Price"].median()
                if pd.isna(fallback):
                    fallback = 100.0
                final_prices = np.where(
                    missing_price_mask,
                    fallback,
                    final_export_df["Molport_Price"]
                )
                _update_pipeline(sid, 2, "Getting price data...",
                                 f"All prices assigned ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count}, Fallback: {missing_count})",
                                 summary=f"All prices assigned. {custom_part_summary}{molport_direct_count} prices found from the MolPort database, {molprice_approx_count} prices approximated using MolPrice, {missing_count} median fallback.")
        else:
            _update_pipeline(sid, 2, "Getting price data...", 
                             f"All prices assigned ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_approx_count})", 
                             summary=f"All prices assigned. {custom_part_summary}{molport_direct_count} prices found from the MolPort database, {molprice_approx_count} prices approximated using MolPrice.")
            final_prices = final_export_df["Molport_Price"].values
        final_export_df["Price_USD_per_mg"] = final_prices
        final_export_df.drop(columns=["MW", "Molport_Price", "Molport_Source"], inplace=True, errors="ignore")

        # Drop rows with NaN prices
        final_export_df = final_export_df.dropna(subset=["Price_USD_per_mg"])

        final_export_df = reorder_meta_columns(final_export_df)

        # Save matrix as CSV (fast) — Excel generated lazily on download
        _update_pipeline(sid, 3, "Saving matrix...", "Saving matrix...")
        
        final_export_df.to_csv(matrix_file, index=False)

        # Store in session dataset
        target_cols = [c for c in final_export_df.columns if c not in {"Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"}]
        with _lock:
            ds["selectivities"] = final_export_df[target_cols].to_numpy(dtype=float)
            ds["prices"] = final_export_df["Price_USD_per_mg"].to_numpy(dtype=float)
            ds["smiles"] = final_export_df["SMILES"].to_numpy()
            ds["num_drugs"] = len(final_export_df)
            ds["num_targets"] = len(target_cols)
            ds["total_cost"] = float(np.sum(ds["prices"]))
            ds["matrix_file"] = matrix_file
            ds["ready"] = True
            ds["has_custom_affinity"] = False

            pipeline_st["status"] = "complete"
            pipeline_st["detail"] = f"Matrix ready: {ds['num_drugs']} compounds × {ds['num_targets']} targets"
        del final_export_df  # Free DataFrame — numpy arrays and CSV are sufficient

    except Exception as e:
        with _lock:
            pipeline_st["status"] = "error"
            pipeline_st["error"] = str(e)
            pipeline_st["detail"] = ""


def _run_affinity_pipeline(sid, selectivity_threshold=0.5, remove_targets=True):
    """Pipeline for user-uploaded affinity data: calculates selectivity directly and resolves prices."""
    import hashlib

    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    ds = s["dataset"]
    aff_state = s["affinity_upload_state"]
    price_state = s["price_upload_state"]

    try:
        output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        output_dir.mkdir(parents=True, exist_ok=True)

        with _lock:
            raw_df = aff_state["df"]
            compounds_map = dict(aff_state["resolved_compounds"])
            targets_map = dict(aff_state["resolved_targets"])

        if raw_df is None or raw_df.empty:
            raise ValueError("No affinity data uploaded.")

        # ─────────────────────────────────────────────
        # Step 1: Calculating selectivity matrix
        # ─────────────────────────────────────────────
        _update_pipeline(sid, 1, "Calculating selectivity matrix...", "Computing blended selectivity scores from uploaded affinity data...")

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

        _update_pipeline(sid, 1, "Calculating selectivity matrix...",
                         f"Selectivity matrix computed: {final_drugs} compounds × {final_targets} targets",
                         summary=summary_text)

        # ─────────────────────────────────────────────
        # Step 2: Getting price data
        # ─────────────────────────────────────────────
        _update_pipeline(sid, 2, "Getting price data...", "Resolving compound prices...")

        final_compounds = list(clean_df.index)

        meta_records = []
        for cmpd in final_compounds:
            res_info = compounds_map.get(cmpd, {})
            chembl_id = res_info.get("chembl_id", "")
            pref_name = res_info.get("pref_name", str(cmpd))
            inchi_key = res_info.get("inchi_key", "")
            smiles = res_info.get("smiles", "")

            # If compound string itself is InChIKey or SMILES
            if not inchi_key and _looks_like_inchikey(str(cmpd)):
                inchi_key = str(cmpd)
            if not smiles and _looks_like_smiles(str(cmpd)):
                smiles = str(cmpd)

            meta_records.append({
                "Compound_Name": str(cmpd),
                "Molecule_ChEMBL_ID": chembl_id or "",
                "InChIKey": inchi_key,
                "SMILES": smiles,
            })

        final_export_df = clean_df.copy().reset_index()
        final_export_df.rename(columns={"index": "Compound_Name"}, inplace=True)
        meta_df = pd.DataFrame(meta_records)
        final_export_df = final_export_df.merge(meta_df, on="Compound_Name", how="left")

        # MolPort lookup cache
        inchikeys = [r["InChIKey"] for r in meta_records if r["InChIKey"]]
        molport_dict, molport_source_dict = _lookup_molport_prices(inchikeys)

        molprice_model = None
        prices = []
        custom_price_count = 0
        molport_direct_count = 0
        molprice_count = 0
        fallback_count = 0

        for idx, row_meta in enumerate(meta_records):
            cmpd_raw = row_meta["Compound_Name"]
            res_info = compounds_map.get(cmpd_raw, {})

            # Tier 1: Custom Price File
            custom_p = _lookup_custom_price(cmpd_raw, res_info, price_state)
            if custom_p is not None:
                prices.append(float(custom_p))
                custom_price_count += 1
                continue

            # Tier 2: MolPort Database (checking if genuine MolPort or DB pre-computed MolPrice)
            ik = row_meta["InChIKey"]
            if ik and ik in molport_dict:
                prices.append(float(molport_dict[ik]))
                src = molport_source_dict.get(ik, "")
                if src == "MolPrice":
                    molprice_count += 1
                else:
                    molport_direct_count += 1
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

        has_custom_price = bool(price_state.get("files")) or bool(price_state.get("price_map")) or (price_state.get("count", 0) > 0) or (custom_price_count > 0)
        custom_part_summary = f"{custom_price_count} prices assigned from custom price file, " if has_custom_price else ""
        custom_part_detail = f"Custom: {custom_price_count}, " if has_custom_price else ""

        if fallback_count > 0:
            summary_msg = f"All prices assigned. {custom_part_summary}{molport_direct_count} prices found from the MolPort database, {molprice_count} prices approximated using MolPrice, {fallback_count} median fallback."
            detail_msg = f"All prices assigned ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_count}, Fallback: {fallback_count})"
        else:
            summary_msg = f"All prices assigned. {custom_part_summary}{molport_direct_count} prices found from the MolPort database, {molprice_count} prices approximated using MolPrice."
            detail_msg = f"All prices assigned ({custom_part_detail}MolPort: {molport_direct_count}, MolPrice approx: {molprice_count})"

        _update_pipeline(sid, 2, "Getting price data...", detail_msg, summary=summary_msg)

        # ─────────────────────────────────────────────
        # Step 3: Saving matrix
        # ─────────────────────────────────────────────
        _update_pipeline(sid, 3, "Saving matrix...", "Saving selectivity matrix...")

        target_cols = [c for c in clean_df.columns]
        meta_cols = ["Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg"]
        final_export_df = final_export_df[meta_cols + target_cols]

        cache_key = hashlib.md5(f"v2_affinity_{len(final_export_df)}_{selectivity_threshold}_{remove_targets}".encode('utf-8')).hexdigest()
        matrix_file = str(output_dir / f"selectivity_matrix_affinity_{cache_key}.csv")
        final_export_df.to_csv(matrix_file, index=False)

        with _lock:
            ds["selectivities"] = final_export_df[target_cols].to_numpy(dtype=float)
            ds["prices"] = final_export_df["Price_USD_per_mg"].to_numpy(dtype=float)
            ds["smiles"] = final_export_df["SMILES"].to_numpy()
            ds["num_drugs"] = len(final_export_df)
            ds["num_targets"] = len(target_cols)
            ds["total_cost"] = float(np.sum(ds["prices"]))
            ds["matrix_file"] = matrix_file
            ds["ready"] = True
            ds["has_custom_affinity"] = True

            pipeline_st["status"] = "complete"
            pipeline_st["detail"] = f"Matrix ready: {ds['num_drugs']} compounds × {ds['num_targets']} targets"

    except Exception as e:
        traceback.print_exc()
        with _lock:
            pipeline_st["status"] = "error"
            pipeline_st["error"] = str(e)
            pipeline_st["detail"] = ""


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Dataset Info
# ═══════════════════════════════════════════════════════════════

@app.route("/api/dataset-info")
@limiter.limit("300 per minute")
def dataset_info():
    sid, s = _get_session()
    ds = s["dataset"]
    with _lock:
        return jsonify({
            "num_drugs": ds["num_drugs"],
            "num_targets": ds["num_targets"],
            "total_cost": round(ds["total_cost"], 2),
            "ready": ds["ready"],
            "has_custom_affinity": ds.get("has_custom_affinity", False),
        })


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Optimization
# ═══════════════════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
@limiter.limit("20 per minute")
def run_optimization_route():
    """Launch NSGA-II optimization in a background thread."""
    sid, s = _get_session()
    opt_st = s["opt_state"]
    ds = s["dataset"]

    with _lock:
        if opt_st["status"] == "running":
            return jsonify({"error": "Optimization is already running"}), 409
        if not ds["ready"]:
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
        opt_st.update({
            "status": "running",
            "generation": 0,
            "max_gen": max_gen,
            "error": "",
            "stop_requested": False,
            "history": [],
        })

    thread = threading.Thread(
        target=_run_nsga2,
        args=(sid, weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen, ftol, term_period, max_price),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/status")
@limiter.limit("300 per minute")
def optimization_status():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    with _lock:
        return jsonify({**opt_st})


@app.route("/api/reset", methods=["POST"])
@limiter.limit("60 per minute")
def reset_state():
    sid, s = _get_session()
    with _lock:
        s["pipeline_state"].update({
            "status": "idle",
            "current_step": 0,
            "step_label": "",
            "detail": "",
            "error": "",
            "step_summaries": {},
            "matched_targets": [],
            "unmatched_targets": [],
        })
        s["dataset"].update({
            "selectivities": None,
            "prices": None,
            "smiles": None,
            "num_drugs": 0,
            "num_targets": 0,
            "total_cost": 0.0,
            "matrix_file": None,
            "ready": False,
        })
        s["opt_state"].update({
            "status": "idle",
            "generation": 0,
            "max_gen": 0,
            "error": "",
            "stop_requested": False,
            "history": [],
        })
        s["opt_results"].update({
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
        s["affinity_upload_state"].update({
            "files": {},
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
        s["price_upload_state"].update({
            "files": {},
            "df": None,
            "resolved_compounds": {},
            "formatted_compounds": [],
            "price_map": {},
            "filename": "",
            "count": 0,
        })
    # Clean up per-session output directory on reset
    session_output_dir = PROJECT_ROOT / "webapp" / "output" / sid
    if session_output_dir.exists():
        shutil.rmtree(session_output_dir, ignore_errors=True)
    return jsonify({"status": "reset"})


@app.route("/api/reset-opt", methods=["POST"])
@limiter.limit("60 per minute")
def reset_opt_state():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    with _lock:
        opt_st.update({
            "status": "idle",
            "generation": 0,
            "error": "",
            "stop_requested": False,
            "history": [],
        })
    return jsonify({"status": "reset"})

@app.route("/api/stop-opt", methods=["POST"])
@limiter.limit("60 per minute")
def stop_opt_state():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    with _lock:
        if opt_st["status"] == "running":
            opt_st["stop_requested"] = True
    return jsonify({"status": "stop_requested"})


def _run_nsga2(sid, weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen, ftol=0.0025, term_period=30, max_price=None):
    """Run NSGA-II optimization using the loaded dataset."""
    s = _get_session_by_sid(sid)
    opt_st = s["opt_state"]
    ds = s["dataset"]

    cb = None  # Keep callback accessible for early-stop result extraction
    problem = None
    try:
        with _lock:
            # Use direct references — these arrays are read-only during optimization
            selectivities = ds["selectivities"]
            prices = ds["prices"]

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
        cb = WebappCallback(problem, opt_st)
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

        _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=max_price)

        # Save a clean matplotlib progress history plot (white background PNG)
        with _lock:
            history_snapshot = list(opt_st["history"])
        session_output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        save_progress_history_plot(history_snapshot, output_dir=session_output_dir)

        with _lock:
            opt_st["status"] = "complete"

    except StopOptimization:
        # Early stop: extract results from the callback's saved population snapshot
        if cb is not None and cb.last_pop_X is not None and problem is not None:
            try:
                _process_stopped_results(sid, cb, problem, max_price=max_price)
                # Save progress history plot even on early stop
                with _lock:
                    history_snapshot = list(opt_st["history"])
                session_output_dir = PROJECT_ROOT / "webapp" / "output" / sid
                save_progress_history_plot(history_snapshot, output_dir=session_output_dir)
            except Exception as inner_e:
                with _lock:
                    opt_st["status"] = "error"
                    opt_st["error"] = f"Stopped, but failed to process partial results: {inner_e}"
                traceback.print_exc()
        else:
            with _lock:
                opt_st["status"] = "error"
                opt_st["error"] = "Optimization stopped before any generation completed."
    except Exception as e:
        with _lock:
            opt_st["status"] = "error"
            opt_st["error"] = str(e)
        traceback.print_exc()


def _process_stopped_results(sid, cb, problem, max_price=None):
    """Build and store results from the callback's population snapshot after early stop."""
    s = _get_session_by_sid(sid)
    opt_st = s["opt_state"]

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

    _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=max_price)

    with _lock:
        opt_st["status"] = "complete"




def _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=None):
    """Common result processing shared by normal completion and early stop."""
    s = _get_session_by_sid(sid)
    ds = s["dataset"]
    opt_res = s["opt_results"]

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
        best_idx = find_knee_point(front)

    # Load matrix from CSV on demand (avoids keeping large DataFrame resident)
    with _lock:
        matrix_file = ds["matrix_file"]
    matrix_df_indexed = pd.read_csv(matrix_file).set_index("SMILES")

    # Wrap in lightweight result for save_results compatibility
    res_light = _LightResult(res_X, res_F)

    # Save results to per-session output directory
    output_dir = PROJECT_ROOT / "webapp" / "output" / sid
    output_dir.mkdir(parents=True, exist_ok=True)
    winning_file = str(output_dir / "optimized_library.xlsx")

    winning_smiles, selected_drug_indices, winning_matrix_df = save_results(
        res_light, best_idx, matrix_df_indexed,
        output_file=winning_file,
    )
    del matrix_df_indexed  # Free immediately

    # Calculate comparison metrics
    has_custom_affinity = ds.get("has_custom_affinity", False)
    comparison = _build_comparison(winning_matrix_df, problem, has_custom_affinity)

    # Store results
    with _lock:
        opt_res["pareto_front"] = front.tolist()
        opt_res["best_idx"] = int(best_idx)
        opt_res["selected_idx"] = int(best_idx)
        opt_res["comparison"] = comparison
        opt_res["winning_matrix_df"] = winning_matrix_df
        opt_res["winning_file"] = winning_file
        opt_res["res_X"] = res_X
        opt_res["res_F"] = res_F
        opt_res["problem"] = problem
        opt_res["heatmap_cache"] = _build_heatmap_cache(winning_matrix_df)
        opt_res["weight_mean"] = round(float(problem.weight_mean), 4) if hasattr(problem, "weight_mean") else 0.5
        opt_res["weight_min"] = round(float(problem.weight_min), 4) if hasattr(problem, "weight_min") else 0.5


def _build_comparison(winning_matrix_df, problem, has_custom_affinity=False):
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
        name_str = _clean_str(row.get("Compound_Name", ""))
        inchikey_str = _clean_str(row.get("InChIKey", ""))
        chembl_str = _clean_str(row.get("Molecule_ChEMBL_ID", ""))
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


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Results
# ═══════════════════════════════════════════════════════════════

@app.route("/api/results")
@limiter.limit("300 per minute")
def get_results():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with _lock:
        if opt_res["comparison"] is None:
            return jsonify({"error": "No results available yet"}), 404
        return jsonify({
            "comparison": opt_res["comparison"],
            "best_idx": opt_res["best_idx"],
        })


@app.route("/api/pareto-data")
@limiter.limit("300 per minute")
def pareto_data():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with _lock:
        if opt_res["pareto_front"] is None:
            return jsonify({"error": "No Pareto data available"}), 404
        front = opt_res["pareto_front"]
        best = opt_res["best_idx"]
        selected = opt_res.get("selected_idx", best)
        problem = opt_res.get("problem")
        weight_mean = opt_res.get("weight_mean")
        if weight_mean is None and problem and hasattr(problem, "weight_mean"):
            weight_mean = round(float(problem.weight_mean), 4)
        elif weight_mean is None:
            weight_mean = 0.5

        weight_min = opt_res.get("weight_min")
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
@limiter.limit("120 per minute")
def select_solution():
    """Switch the active solution to a different Pareto front point."""
    sid, s = _get_session()
    opt_res = s["opt_results"]
    ds = s["dataset"]

    data = request.get_json()
    idx = data.get("index")
    if idx is None:
        return jsonify({"error": "Missing 'index' parameter"}), 400

    with _lock:
        res_X = opt_res.get("res_X")
        problem = opt_res.get("problem")
        matrix_file = ds.get("matrix_file")

    if res_X is None or problem is None or matrix_file is None:
        return jsonify({"error": "No optimization results available"}), 404

    num_solutions = res_X.shape[0]
    if idx < 0 or idx >= num_solutions:
        return jsonify({"error": f"Index {idx} out of range (0-{num_solutions - 1})"}), 400

    try:
        # Load matrix from CSV on demand (avoids keeping large DataFrame resident)
        matrix_df_indexed = pd.read_csv(matrix_file).set_index("SMILES")
        res_light = _LightResult(res_X, opt_res.get("res_F"))

        # Save to per-session output directory
        output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        output_dir.mkdir(parents=True, exist_ok=True)
        winning_file = str(output_dir / "optimized_library.xlsx")

        winning_smiles, selected_drug_indices, winning_matrix_df = save_results(
            res_light, idx, matrix_df_indexed,
            output_file=winning_file,
        )
        del matrix_df_indexed  # Free immediately

        has_custom_affinity = ds.get("has_custom_affinity", False)
        comparison = _build_comparison(winning_matrix_df, problem, has_custom_affinity)

        with _lock:
            opt_res["selected_idx"] = int(idx)
            opt_res["comparison"] = comparison
            opt_res["winning_matrix_df"] = winning_matrix_df
            opt_res["winning_file"] = winning_file
            opt_res["heatmap_cache"] = _build_heatmap_cache(winning_matrix_df)

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

    import re
    parsed_symbols = {}
    parsed_names = {}
    to_lookup = set()

    for t in target_list:
        t_str = str(t).strip()
        m = re.match(r"^(.*?)\s*\(([^()]+)\)$", t_str)
        if m:
            p_name = m.group(1).strip()
            g_sym = m.group(2).strip()
            parsed_symbols[t] = g_sym
            parsed_names[t] = p_name
            to_lookup.add(p_name)
            to_lookup.add(g_sym)
            to_lookup.add(t_str)
        else:
            to_lookup.add(t_str)

    try:
        db_path = str(get_chembl_db_path())
        if os.path.exists(db_path) and to_lookup:
            lookup_list = list(to_lookup)
            with sqlite3.connect(db_path) as conn:
                placeholders = ",".join(["?"] * len(lookup_list))
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
                rows = conn.execute(query, lookup_list * 4).fetchall()
                
                sym_map = {}
                name_map = {}
                for cid, pref_name, sym, acc, csy_syn in rows:
                    p_name = pref_name or sym or cid or acc
                    s_name = sym or pref_name or cid or acc
                    for key in (cid, pref_name, sym, acc, csy_syn):
                        if key:
                            sym_map[str(key).lower()] = s_name
                            name_map[str(key).lower()] = p_name

                symbols = []
                names = []
                for t in target_list:
                    if t in parsed_symbols:
                        g_sym = parsed_symbols[t]
                        p_name = parsed_names[t]
                        db_sym = sym_map.get(g_sym.lower()) or sym_map.get(p_name.lower()) or sym_map.get(str(t).lower()) or g_sym
                        db_name = name_map.get(p_name.lower()) or name_map.get(g_sym.lower()) or name_map.get(str(t).lower()) or p_name
                        symbols.append(db_sym)
                        names.append(db_name)
                    else:
                        t_lower = str(t).lower()
                        symbols.append(sym_map.get(t_lower, str(t)))
                        names.append(name_map.get(t_lower, str(t)))
                return symbols, names
    except Exception:
        pass

    symbols = []
    names = []
    for t in target_list:
        if t in parsed_symbols:
            symbols.append(parsed_symbols[t])
            names.append(parsed_names[t])
        else:
            symbols.append(str(t))
            names.append(str(t))
    return symbols, names



def _extract_compound_labels(df):
    """Extract preferred compound labels for heatmap (Molecule_ChEMBL_ID -> InChIKey -> Compound_Name -> Index)."""
    if "Molecule_ChEMBL_ID" in df.columns:
        labels = []
        for i, val in enumerate(df["Molecule_ChEMBL_ID"]):
            val_str = _clean_str(val)
            if val_str:
                labels.append(val_str)
            elif "InChIKey" in df.columns and _clean_str(df["InChIKey"].iloc[i]):
                labels.append(_clean_str(df["InChIKey"].iloc[i]))
            elif "Compound_Name" in df.columns and _clean_str(df["Compound_Name"].iloc[i]):
                labels.append(_clean_str(df["Compound_Name"].iloc[i]))
            else:
                labels.append(f"Compound {i+1}")
        return labels
    elif "InChIKey" in df.columns:
        return [_clean_str(x) or f"Compound {i+1}" for i, x in enumerate(df["InChIKey"])]
    elif "Compound_Name" in df.columns:
        return [_clean_str(x) or f"Compound {i+1}" for i, x in enumerate(df["Compound_Name"])]
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
@limiter.limit("300 per minute")
def heatmap_data():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with _lock:
        if opt_res["winning_matrix_df"] is None:
            return jsonify({"error": "No heatmap data available"}), 404
        cache = opt_res.get("heatmap_cache")
        if cache:
            return jsonify(cache)
        df = opt_res["winning_matrix_df"]

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
@limiter.limit("60 per minute")
def download_library():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with _lock:
        path = opt_res.get("winning_file")
    if path and os.path.isfile(path):
        return send_file(path, as_attachment=True, download_name="optimized_library.xlsx")
    return jsonify({"error": "No library file available"}), 404


@app.route("/api/download/matrix")
@limiter.limit("60 per minute")
def download_matrix():
    sid, s = _get_session()
    ds = s["dataset"]
    with _lock:
        csv_path = ds.get("matrix_file")

    if not (csv_path and os.path.isfile(csv_path)):
        return jsonify({"error": "No matrix file available"}), 404

    # Generate Excel lazily from CSV (cached after first call)
    xlsx_path = csv_path.replace(".csv", ".xlsx")

    if not os.path.isfile(xlsx_path):
        pd.read_csv(csv_path).to_excel(xlsx_path, index=False, engine='xlsxwriter')

    return send_file(xlsx_path, as_attachment=True, download_name="selectivity_matrix.xlsx")


@app.route("/api/download/progress-plot")
@limiter.limit("60 per minute")
def download_progress_plot():
    sid, s = _get_session()
    session_output_dir = PROJECT_ROOT / "webapp" / "output" / sid
    plot_path = session_output_dir / "optimization_progress_history.png"
    if not plot_path.exists():
        plot_path = PROJECT_ROOT / "webapp" / "output" / "optimization_progress_history.png"
    if plot_path.exists():
        return send_file(plot_path, as_attachment=True, download_name="optimization_progress_history.png")
    return jsonify({"error": "No progress history plot available"}), 404


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"ChEMBL database: {get_chembl_db_path()}")
    logger.info(f"MolPort database: {DATABASE_DIR / 'molport.db'}")
    app.run(debug=False, host="0.0.0.0", port=5000)
