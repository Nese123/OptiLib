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
from bisect import bisect_right
from contextlib import contextmanager, closing
from copy import deepcopy
from functools import wraps
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, send_file, session
from pymoo.core.callback import Callback
from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting
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

# Direct script execution also needs the root for package imports (webapp.public).
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "webapp"))
sys.path.insert(0, str(MOLPRICE_DIR))

from bin.numpy_predict import NumpyFingerprints
from core.selectivity import generate_selectivity_matrix, SELECTIVITY_SCORING_VERSION
from core.chembl import get_selectivity_provenance
from core.storage import publish_matrix, ensure_matrix_excel
from core.queries import read_chembl_candidates
from core.state import make_session_state
from core.records import (
    METADATA_COLUMNS, clean_str as _clean_str, format_target_col as _format_target_col,
    format_compound_display as _format_compound_display,
    format_target_display as _format_target_display,
    looks_like_inchikey as _looks_like_inchikey, looks_like_smiles as _looks_like_smiles,
)
from core.resolution import resolve_compounds, resolve_targets, get_target_info
from core.datasets import prepare_dataset
from core.results import (
    build_comparison as _build_comparison, build_heatmap_cache, prepare_selected_library,
)
from core.uploads import read_upload_table, UploadTableError
from core.algorithm import (
    DrugLibraryProblem,
    build_smart_init,
    run_optimization,
    select_best_solution,
    write_library_excel,
    find_knee_point,
    reorder_meta_columns,
)

# Suppress sklearn version mismatch warning from MolPrice's pickled scaler
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

# ═══════════════════════════════════════════════════════════════
#  FLASK APP & SECURITY CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)
# The legacy in-process application remains available for local development
# and numerical regression fixtures. Public deployments use webapp.wsgi:app.
from webapp.public.config import configure_security
configure_security(app)

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
    return {
        "error": "Your session or security token has expired. Please refresh the page.",
        "reason": getattr(e, "description", str(e)),
        "status": 400
    }, 400


@app.errorhandler(429)
def ratelimit_handler(e):
    """Clean JSON response for rate limit violations."""
    return {
        "error": "Rate limit exceeded. Please wait a moment before making more requests.",
        "status": 429
    }, 429


@app.after_request
def set_security_headers(response):
    """Add standard HTTP security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.plot.ly; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    if request.path.startswith('/api/') or request.path == '/optimize':
        response.headers['Cache-Control'] = 'private, no-store'
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


# ═══════════════════════════════════════════════════════════════
#  SESSION-SCOPED STATE & INACTIVITY CLEANER
# ═══════════════════════════════════════════════════════════════

# Inactivity TTL and background cleanup frequency
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 7200))       # 2 hours (7200 seconds)
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 300))  # 5 minutes

# Thread lock for state access
_sessions_lock = threading.Lock()
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
_job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

# In-memory session store: sid -> dict of per-user state dicts
_sessions = {}


def _get_session():
    """Get or create session-scoped state for the current request.

    Uses Flask's signed cookie session to identify the user.
    Must be called from within a Flask request context (i.e. route handlers).
    """
    sid = session.get("sid")
    if sid is None:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    with _sessions_lock:
        if sid not in _sessions:
            _sessions[sid] = make_session_state()
        else:
            _sessions[sid]["last_activity"] = time.time()
        return sid, _sessions[sid]


def _get_session_by_sid(sid):
    """Get session state by ID (for use in background threads where Flask context is unavailable)."""
    with _sessions_lock:
        if sid not in _sessions:
            _sessions[sid] = make_session_state()
        else:
            _sessions[sid]["last_activity"] = time.time()
        return _sessions[sid]


@contextmanager
def _edit_upload_state(s, key):
    """Serialize upload edits while computing a replacement outside state locks."""
    with s.upload_lock:
        with s.lock:
            original = s[key]
            draft = {**original, "files": {
                name: dict(data) for name, data in original["files"].items()
            }}
        yield draft
        with s.lock:
            s[key] = draft


def _number(data, name, default, *, integer=False, ceiling=None):
    value = data.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite {'integer' if integer else 'number'}")
    number = float(value)
    if not np.isfinite(number) or (integer and not number.is_integer()):
        raise ValueError(f"{name} must be a finite {'integer' if integer else 'number'}")
    if ceiling is not None and number > ceiling:
        raise ValueError(f"{name} must not exceed {ceiling}")
    return int(number) if integer else number


def _validated_start(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except (ValueError, TypeError, OverflowError) as exc:
            return {"error": str(exc) or "Invalid computation parameters"}, 400
    return wrapped


def _start_computation(s, target, args, kind, max_gen=0):
    """Reserve process capacity and session state together before starting work."""
    with s.lock:
        pipeline = s["pipeline_state"]
        optimization = s["opt_state"]
        if pipeline["status"] == "running" or optimization["status"] == "running":
            return {"error": "A computation is already running"}, 409
        if kind == "optimization" and not s["dataset"]["ready"]:
            return {"error": "No dataset loaded. Build the matrix first."}, 400
        if kind == "affinity":
            frame = s["affinity_upload_state"]["df"]
            if frame is None or frame.empty:
                return {"error": "No affinity data uploaded. Please upload an affinity file first."}, 400
        slots = _job_slots
        if not slots.acquire(blocking=False):
            return {"error": "The server is busy. Please try again shortly."}, 503

        keys = ("dataset", "opt_results", "pipeline_state", "opt_state")
        previous = {key: dict(s[key]) for key in keys}
        revisions = (s.dataset_revision, s.run_revision, s.selection_revision)
        s.run_revision += 1
        s.selection_revision = 0
        s["opt_results"].clear()
        s["opt_results"].update(make_session_state()["opt_results"])
        if kind == "optimization":
            optimization.update(status="running", generation=0, max_gen=max_gen,
                                error="", stop_requested=False, history=[])
        else:
            s.dataset_revision += 1
            s["dataset"]["ready"] = False
            pipeline.update(status="running", current_step=0, step_label="Starting...",
                            detail="", error="", step_summaries={})
            optimization.update(status="idle", generation=0, error="")

        def work():
            try:
                target(*args)
            except Exception as exc:
                logger.exception("Background computation failed")
                with s.lock:
                    state = optimization if kind == "optimization" else pipeline
                    state.update(status="error", error=str(exc))
            finally:
                slots.release()

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            for key in keys:
                s[key].clear()
                s[key].update(previous[key])
            s.dataset_revision, s.run_revision, s.selection_revision = revisions
            slots.release()
            logger.exception("Could not start background computation")
            return {"error": "Unable to start computation. Please try again."}, 503
    return {"status": "started"}


def _cleanup_stale_sessions():
    """Clean up expired in-memory sessions and all expired output folders/files."""
    now = time.time()
    output_base = PROJECT_ROOT / "webapp" / "output"

    # 1. Identify expired sessions in memory
    expired_sids = []
    with _sessions_lock:
        candidates = list(_sessions.items())
    for sid, s in candidates:
        with s.lock:
            # Do not clean up if an active computation is running
            is_pipeline_running = s.get("pipeline_state", {}).get("status") == "running"
            is_opt_running = s.get("opt_state", {}).get("status") == "running"
            if is_pipeline_running or is_opt_running:
                continue

            with _sessions_lock:
                if (_sessions.get(sid) is s
                        and now - s.get("last_activity", 0) > SESSION_TTL_SECONDS):
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
                if item.is_dir() and (item / ".optilib-session").is_file():
                    dir_sid = item.name
                    with _sessions_lock:
                        is_active_session = dir_sid in _sessions
                    if not is_active_session:
                        try:
                            mtime = item.stat().st_mtime
                            if now - mtime > SESSION_TTL_SECONDS:
                                shutil.rmtree(item, ignore_errors=True)
                        except OSError:
                            pass
        except OSError:
            pass


def _cleanup_worker():
    """Periodic background daemon worker that runs cleanup sweeps."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _cleanup_stale_sessions()
        except Exception as e:
            logger.error(f"Error in output cleanup worker: {e}")


# Cleanup is explicit in development. The public supervisor owns session
# expiry; importing this module never modifies databases or removes exports.


class StopOptimization(Exception):
    pass


class WebappCallback(Callback):
    def __init__(self, problem, session_opt_state, session_lock):
        super().__init__()
        self.problem = problem
        self._opt_state = session_opt_state
        self._session_lock = session_lock
        # Snapshot of the latest algorithm state for early-stop result extraction
        self.last_pop_X = None
        self.last_pop_F = None
        self.last_pop_G = None

    def notify(self, algorithm):

        with self._session_lock:
            self._opt_state["generation"] = algorithm.n_gen
            stopping = self._opt_state.get("stop_requested")

        F = algorithm.pop.get("F")
        G = algorithm.pop.get("G")
        if stopping:
            # Decision vectors can be large. Only collect them at the stop
            # boundary, while the optimizer cannot mutate this population.
            self.last_pop_X = algorithm.pop.get("X").copy()
            self.last_pop_F = F.copy()
            self.last_pop_G = G.copy()
            raise StopOptimization("Optimization stopped by user")

        feasible = G <= 0 if G.ndim == 1 else np.all(G <= 0, axis=1)
        scores = F[feasible] if np.any(feasible) else F
        row = {
            "generation": algorithm.n_gen,
            "best_selectivity": float(-np.min(scores[:, 0]) * self.problem.pool_baseline_score),
            "best_cost": float(np.min(scores[:, 1]) * self.problem.pool_total_cost),
        }
        with self._session_lock:
            self._opt_state["history"].append(row)

class _LightResult:
    """Minimal stand-in for pymoo Result during solution selection."""
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
    return resolve_compounds(compound_ids, get_chembl_db_path())


def _resolve_targets(target_ids):
    return resolve_targets(target_ids, get_chembl_db_path())


def _lookup_custom_price(compound_raw, resolved_info=None, price_state=None):
    """Smart lookup in custom price map across raw ID, ChEMBL ID, InChIKey, SMILES, and pref_name."""
    if price_state is None:
        return None
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
        with closing(sqlite3.connect(molport_db)) as conn:
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


def _resolve_affinity_prices(meta_records, compounds_map, price_state):
    """Resolve ordered prices, predicting missing molecules in bounded batches."""
    prices = np.full(len(meta_records), np.nan, dtype=float)
    counts = {"custom": 0, "molport": 0, "molprice": 0, "fallback": 0}
    pending = []
    for index, row in enumerate(meta_records):
        compound = row["Compound_Name"]
        price = _lookup_custom_price(compound, compounds_map.get(compound, {}), price_state)
        if price is not None and np.isfinite(price):
            prices[index] = price
            counts["custom"] += 1
        else:
            pending.append(index)

    inchikeys = list(dict.fromkeys(meta_records[i]["InChIKey"] for i in pending
                                  if meta_records[i]["InChIKey"]))
    molport, sources = _lookup_molport_prices(inchikeys)
    model_indices, smiles = [], []
    for index in pending:
        row = meta_records[index]
        price = molport.get(row["InChIKey"])
        if price is not None and np.isfinite(price):
            prices[index] = price
            counts["molprice" if sources.get(row["InChIKey"]) == "MolPrice" else "molport"] += 1
        elif row["SMILES"] and row["SMILES"] != "Missing_SMILES":
            model_indices.append(index)
            smiles.append(row["SMILES"])

    if model_indices:
        try:
            model = NumpyFingerprints(weights_path=str(MOLPRICE_DIR / "models/Numpy/MP_Morgan_hybrid.pkl"))
            predicted = np.asarray(model.predict_batch_from_smiles(
                smiles, batch_size=256, errors="coerce"), dtype=float).reshape(-1)
            if len(predicted) != len(model_indices):
                raise ValueError("Unexpected number of MolPrice predictions")
            valid = np.isfinite(predicted)
            prices[np.asarray(model_indices)[valid]] = predicted[valid]
            counts["molprice"] += int(valid.sum())
        except Exception as exc:
            logger.warning("MolPrice predictions unavailable: %s", exc)

    missing = ~np.isfinite(prices)
    counts["fallback"] = int(missing.sum())
    fallback = float(np.median(prices[~missing])) if np.any(~missing) else 100.0
    prices[missing] = fallback
    return prices, counts


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
    return {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": time.time(),
        "databases": {
            "chembl": chembl_exists,
            "chembl_37": chembl_exists,
            "molport": molport_exists,
        }
    }, (200 if is_healthy else 503)


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Target Upload & Validation
# ═══════════════════════════════════════════════════════════════

def _target_upload_summary(input_targets, resolved):
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

    return {
        "total": len(input_targets), "matched": matched, "unmatched": unmatched,
        "chembl_ids": chembl_ids, "chembl_map": chembl_map,
    }


@app.route("/api/upload-targets", methods=["POST"])
@limiter.limit("30 per minute")
def upload_targets():
    """Accept CSV/Excel with target names/IDs, validate against ChEMBL."""

    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]

    files = request.files.getlist("files[]")
    if not files:
        return {"error": "No files uploaded"}, 400

    all_targets = []
    targets_by_file = []
    for file in files:
        if file.filename == "":
            continue

        safe_name = secure_filename(file.filename) or "upload"
        try:
            df = read_upload_table(file, safe_name)
        except UploadTableError as exc:
            return {"error": str(exc)}, 400

        target_col = None
        for col in df.columns:
            if col.strip().lower() in ("target", "target_name", "targets", "target_names"):
                target_col = col
                break

        if target_col is None:
            if len(df.columns) == 1:
                target_col = df.columns[0]
            else:
                return {
                    "error": f"Could not find Target column in {safe_name}."
                }, 400

        file_targets = df[target_col].dropna().astype(str).str.strip().tolist()
        all_targets.extend(file_targets)
        targets_by_file.append((safe_name, file_targets))

    # Get unique targets while preserving order
    input_targets = list(dict.fromkeys(all_targets))
    
    if not input_targets:
        return {"error": "No targets found in the files"}, 400

    resolved = _resolve_targets(input_targets)

    summary = _target_upload_summary(input_targets, resolved)
    with s.lock:
        pipeline_st["matched_targets"] = summary["matched"]
        pipeline_st["unmatched_targets"] = summary["unmatched"]
    return {**summary, "uploaded_files": [
        {"name": name, **_target_upload_summary(list(dict.fromkeys(targets)), resolved)}
        for name, targets in targets_by_file
    ]}


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
        return {"error": "No files uploaded"}, 400

    uploaded_files_summary = []
    uploaded_entries = {}

    for file in files:
        if not file or file.filename == "":
            continue
        safe_name = secure_filename(file.filename) or "upload"
        try:
            df = read_upload_table(file, safe_name)
        except UploadTableError as exc:
            return {"error": str(exc)}, 400

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
            return {
                "error": f"Could not identify Compound, Target, and Affinity columns in {safe_name}. "
                         f"Please ensure columns are named 'Compound', 'Target', and 'Affinity'."
            }, 400

        sub_df = pd.DataFrame({
            "Compound_Raw": df[cmpd_col].dropna().astype(str).str.strip(),
            "Target_Raw": df[tgt_col].dropna().astype(str).str.strip(),
            "Affinity": pd.to_numeric(df[aff_col], errors="coerce")
        }).dropna()
        sub_df = sub_df.drop_duplicates()

        if sub_df.empty:
            return {"error": f"No valid data rows found in {safe_name}."}, 400

        file_compounds = sub_df["Compound_Raw"].unique().tolist()
        file_targets = sub_df["Target_Raw"].unique().tolist()

        res_compounds = _resolve_compounds(file_compounds)
        res_targets = _resolve_targets(file_targets)

        formatted_c = [_format_compound_display(c, res_compounds.get(c)) for c in file_compounds]
        formatted_t = [_format_target_display(t, res_targets.get(t)) for t in file_targets]

        uploaded_entries[safe_name] = {
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
        return {"error": "No valid affinity files processed."}, 400

    with _edit_upload_state(s, "affinity_upload_state") as aff_state:
        aff_state["files"].update(uploaded_entries)
        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(aff_state["files"])

    return {
        "uploaded_files": uploaded_files_summary,
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    }


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
        return {"error": "No price file uploaded"}, 400

    uploaded_files_summary = []
    uploaded_entries = {}

    for file in files:
        if not file or file.filename == "":
            continue

        safe_name = secure_filename(file.filename) or "upload"
        try:
            df = read_upload_table(file, safe_name, label="price file ")
        except UploadTableError as exc:
            return {"error": str(exc)}, 400

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
            return {
                "error": f"Could not identify Compound and Price columns in {safe_name}. Please use 'Compound' and 'Price'."
            }, 400

        clean_df = pd.DataFrame({
            "Compound": df[cmpd_col].dropna().astype(str).str.strip(),
            "Price": pd.to_numeric(df[price_col], errors="coerce")
        }).dropna()
        clean_df = clean_df[clean_df["Price"] > 0]
        clean_df = clean_df.drop_duplicates(subset=["Compound"], keep="last")

        if clean_df.empty:
            return {"error": f"No valid positive price rows found in {safe_name}."}, 400

        unique_cmpds = clean_df["Compound"].unique().tolist()
        resolved_cmpds = _resolve_compounds(unique_cmpds)

        file_formatted = [_format_compound_display(c, resolved_cmpds.get(c)) for c in unique_cmpds]

        uploaded_entries[safe_name] = {
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
        return {"error": "No valid price files uploaded."}, 400

    with _edit_upload_state(s, "price_upload_state") as price_state:
        price_state["files"].update(uploaded_entries)
        _recompute_price_state(price_state)
        all_files_list = _build_price_files_list(price_state["files"])

    return {
        "uploaded_files": uploaded_files_summary,
        "all_files": all_files_list,
        **_build_price_response(price_state),
    }


@app.route("/api/remove-affinity-file", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_file():
    """Remove a specific uploaded affinity file by name."""
    sid, s = _get_session()

    data = request.get_json(force=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return {"error": "No filename specified"}, 400

    with _edit_upload_state(s, "affinity_upload_state") as aff_state:
        files_dict = aff_state.get("files", {})
        if filename in files_dict:
            del files_dict[filename]
        _recompute_affinity_state(aff_state)
        all_files_list = _build_affinity_files_list(files_dict)

    return {
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    }


@app.route("/api/remove-affinity-target", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_target():
    """Remove a single target from the uploaded affinity dataset across all files."""
    sid, s = _get_session()

    data = request.get_json(force=True) or {}
    target_str = data.get("target", "").strip()
    if not target_str:
        return {"error": "No target specified"}, 400

    target_raw = target_str.split(" ->")[0].strip().lower()

    with _edit_upload_state(s, "affinity_upload_state") as aff_state:
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

    return {
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    }


@app.route("/api/remove-affinity-compound", methods=["POST"])
@limiter.limit("60 per minute")
def remove_affinity_compound():
    """Remove a single compound from the uploaded affinity dataset across all files."""
    sid, s = _get_session()

    data = request.get_json(force=True) or {}
    compound_str = data.get("compound", "").strip()
    if not compound_str:
        return {"error": "No compound specified"}, 400

    compound_raw = compound_str.split(" ->")[0].strip().lower()

    with _edit_upload_state(s, "affinity_upload_state") as aff_state:
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

    return {
        "all_files": all_files_list,
        **_build_affinity_response(aff_state),
    }


@app.route("/api/clear-affinity", methods=["POST"])
@limiter.limit("60 per minute")
def clear_affinity():
    sid, s = _get_session()
    with _edit_upload_state(s, "affinity_upload_state") as aff_state:
        aff_state["files"] = {}
        _recompute_affinity_state(aff_state)
    return {"status": "cleared"}


@app.route("/api/remove-price-file", methods=["POST"])
@limiter.limit("60 per minute")
def remove_price_file():
    """Remove a specific uploaded price file by name."""
    sid, s = _get_session()

    data = request.get_json(force=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return {"error": "No filename specified"}, 400

    with _edit_upload_state(s, "price_upload_state") as price_state:
        files_dict = price_state.get("files", {})
        if filename in files_dict:
            del files_dict[filename]
        _recompute_price_state(price_state)
        all_files_list = _build_price_files_list(files_dict)

    return {
        "all_files": all_files_list,
        **_build_price_response(price_state),
    }


@app.route("/api/remove-price-compound", methods=["POST"])
@limiter.limit("60 per minute")
def remove_price_compound():
    """Remove a single compound from the uploaded price dataset."""
    sid, s = _get_session()

    data = request.get_json(force=True) or {}
    compound_str = data.get("compound", "").strip()
    if not compound_str:
        return {"error": "No compound specified"}, 400

    compound_raw = compound_str.split(" ->")[0].strip().lower()

    with _edit_upload_state(s, "price_upload_state") as price_state:
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

    return {
        "all_files": all_files_list,
        **_build_price_response(price_state),
    }


@app.route("/api/clear-prices", methods=["POST"])
@limiter.limit("60 per minute")
def clear_prices():
    sid, s = _get_session()
    with _edit_upload_state(s, "price_upload_state") as price_state:
        price_state["files"] = {}
        _recompute_price_state(price_state)
    return {"status": "cleared"}


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Pipeline (Build Matrix)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/build-matrix", methods=["POST"])
@limiter.limit("20 per minute")
@_validated_start
def build_matrix():
    """Launch a ChEMBL pipeline after atomic admission."""
    sid, s = _get_session()
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    chembl_ids = data.get("chembl_ids", [])
    if not isinstance(chembl_ids, list) or not all(isinstance(cid, str) and cid.strip() for cid in chembl_ids):
        raise ValueError("chembl_ids must be a list of target identifiers")
    if not chembl_ids:
        return {"error": "No matched targets provided"}, 400
    threshold = _number(data, "selectivity_threshold", 0.5)
    matched_count = _number(data, "matched_count", len(chembl_ids), integer=True)
    remove_targets = bool(data.get("remove_targets", True))
    return _start_computation(s, _run_pipeline,
                              (sid, chembl_ids, threshold, remove_targets, matched_count), "chembl")


@app.route("/api/build-matrix-from-affinity", methods=["POST"])
@limiter.limit("20 per minute")
@_validated_start
def build_matrix_from_affinity():
    """Launch an affinity pipeline after atomic admission."""
    sid, s = _get_session()
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    threshold = _number(data, "selectivity_threshold", 0.5)
    remove_targets = bool(data.get("remove_targets", True))
    return _start_computation(s, _run_affinity_pipeline,
                              (sid, threshold, remove_targets), "affinity")


@app.route("/api/pipeline-status")
@limiter.limit("300 per minute")
def pipeline_status():
    sid, s = _get_session()
    pipeline_st = s["pipeline_state"]
    with s.lock:
        return deepcopy(pipeline_st)


def _update_pipeline(sid, step, label, detail="", summary=None):
    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    with s.lock:
        pipeline_st["current_step"] = step
        pipeline_st["step_label"] = label
        pipeline_st["detail"] = detail
        if summary is not None:
            pipeline_st["step_summaries"][step] = summary


def _matrix_cache_key(chembl_ids, threshold, remove_targets, matched_count, price_map, provenance):
    import hashlib
    payload = ["v5", sorted(chembl_ids), threshold, remove_targets, matched_count,
               price_map, provenance["scoring_version"], provenance["build_id"]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _publish_dataset(s, frame, matrix_file, custom, revision, provenance):
    dataset = prepare_dataset(frame, matrix_file, custom, provenance)
    with s.lock:
        if s.dataset_revision != revision:
            return False
        s["dataset"].update(dataset)
        s["pipeline_state"].update(
            status="complete", current_step=3, step_label="Done",
            detail=f"Matrix ready: {dataset['num_drugs']} compounds × {dataset['num_targets']} targets",
        )
    return True


def _run_pipeline(sid, chembl_ids, selectivity_threshold, remove_targets=True, matched_count=0):
    """Full pipeline: ChEMBL → pChEMBL rescue → selectivity → prices → save."""
    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    ds = s["dataset"]
    with s.lock:
        revision = s.dataset_revision
        price_state = {**s["price_upload_state"],
                       "price_map": dict(s["price_upload_state"]["price_map"])}

    try:
        output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Read provenance and scores from one snapshot, including cache hits.
        db_path = get_chembl_db_path().resolve()
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("BEGIN")
            provenance = get_selectivity_provenance(conn)
            if provenance["scoring_version"] != SELECTIVITY_SCORING_VERSION:
                raise ValueError("The ChEMBL selectivity data needs updating before building a matrix.")
            cache_key = _matrix_cache_key(chembl_ids, selectivity_threshold, remove_targets,
                                          matched_count, price_state["price_map"], provenance)
            matrix_file = str(output_dir / f"selectivity_matrix_{cache_key}.csv")
            if os.path.exists(matrix_file):
                _update_pipeline(sid, 1, "Loading cached matrix...", "Found a previously computed selectivity matrix for these parameters.")
                frame = pd.read_csv(matrix_file)
                _publish_dataset(s, frame, matrix_file, False, revision, provenance)
                return

            _update_pipeline(sid, 1, "Searching for selective compounds...",
                             f"Querying database for compounds active against {matched_count} targets...")
            df_raw, active_chembl_ids = read_chembl_candidates(
                conn, chembl_ids, selectivity_threshold,
                progress=lambda count: _update_pipeline(
                    sid, 1, "Searching for selective compounds...", f"Found {count} compounds so far..."),
            )
        dropped_targets_no_pchembl = [cid for cid in chembl_ids if cid.upper() not in active_chembl_ids]
        if dropped_targets_no_pchembl:
            logger.info("Dropped targets lacking high-confidence pChEMBL > 5.0: %s", dropped_targets_no_pchembl)
        if not active_chembl_ids:
            raise ValueError("None of the provided targets have any compounds with pChEMBL > 5.0 in high-confidence human single-protein assays.")

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
        
        matrix_file = publish_matrix(final_export_df, output_dir, filename=Path(matrix_file).name)
        _publish_dataset(s, final_export_df, matrix_file, False, revision, provenance)
        del final_export_df  # Free DataFrame — numpy arrays and CSV are sufficient

    except Exception as e:
        with s.lock:
            if s.dataset_revision != revision:
                return
            pipeline_st["status"] = "error"
            pipeline_st["error"] = str(e)
            pipeline_st["detail"] = ""


def _run_affinity_pipeline(sid, selectivity_threshold=0.5, remove_targets=True):
    """Pipeline for user-uploaded affinity data: calculates selectivity directly and resolves prices."""
    s = _get_session_by_sid(sid)
    pipeline_st = s["pipeline_state"]
    ds = s["dataset"]
    with s.lock:
        aff_state = s["affinity_upload_state"]
        revision = s.dataset_revision
        price_state = {**s["price_upload_state"],
                       "price_map": dict(s["price_upload_state"]["price_map"])}

    try:
        output_dir = PROJECT_ROOT / "webapp" / "output" / sid
        output_dir.mkdir(parents=True, exist_ok=True)

        with s.lock:
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

        prices, price_counts = _resolve_affinity_prices(meta_records, compounds_map, price_state)
        custom_price_count = price_counts["custom"]
        molport_direct_count = price_counts["molport"]
        molprice_count = price_counts["molprice"]
        fallback_count = price_counts["fallback"]

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
        meta_cols = list(METADATA_COLUMNS)
        final_export_df = final_export_df[meta_cols + target_cols]

        matrix_file = publish_matrix(final_export_df, output_dir,
                                     scoring_version=SELECTIVITY_SCORING_VERSION)
        _publish_dataset(s, final_export_df, matrix_file, True, revision,
                         {"scoring_version": SELECTIVITY_SCORING_VERSION, "h": 5})

    except Exception as e:
        traceback.print_exc()
        with s.lock:
            if s.dataset_revision != revision:
                return
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
    with s.lock:
        return {
            "num_drugs": ds["num_drugs"],
            "num_targets": ds["num_targets"],
            "total_cost": round(ds["total_cost"], 2),
            "ready": ds["ready"],
            "has_custom_affinity": ds.get("has_custom_affinity", False),
        }


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Optimization
# ═══════════════════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
@limiter.limit("20 per minute")
@_validated_start
def run_optimization_route():
    """Launch NSGA-II after validating resource bounds and reserving capacity."""
    sid, s = _get_session()
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    weight_mean = _number(data, "weight_mean", 0.5)
    allowed_miss_pct = _number(data, "allowed_miss_pct", 0.04)
    mutation_multiplier = _number(data, "mutation_multiplier", 1.0)
    pop_size = max(5, _number(data, "pop_size", 100, integer=True, ceiling=500))
    max_gen = max(10, _number(data, "max_gen", 1000, integer=True, ceiling=5000))
    ftol = max(0.0001, _number(data, "ftol", 0.0025))
    term_period = max(5, _number(data, "term_period", 30, integer=True, ceiling=500))
    max_price = None if data.get("max_price") is None else _number(data, "max_price", None)
    if not 0 <= weight_mean <= 1 or not 0 <= allowed_miss_pct <= 1:
        raise ValueError("weight_mean and allowed_miss_pct must be between 0 and 1")
    if mutation_multiplier < 0:
        raise ValueError("mutation_multiplier must be non-negative")
    return _start_computation(
        s, _run_nsga2,
        (sid, weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen,
         ftol, term_period, max_price), "optimization", max_gen=max_gen,
    )


@app.route("/api/status")
@limiter.limit("300 per minute")
def optimization_status():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    try:
        since = int(request.args.get("since_generation", -1))
        requested_run = request.args.get("run_revision", type=int)
        if since < -1:
            raise ValueError
    except ValueError:
        return {"error": "since_generation must be an integer greater than or equal to -1"}, 400
    with s.lock:
        history = opt_st["history"]
        if requested_run is not None and requested_run != s.run_revision:
            since = -1
        start = bisect_right(history, since, key=lambda row: row["generation"])
        return {**opt_st, "run_revision": s.run_revision,
                "history": [dict(row) for row in history[start:]]}


@app.route("/api/reset", methods=["POST"])
@limiter.limit("60 per minute")
def reset_state():
    sid, s = _get_session()
    # A new session ID isolates the reset from every in-flight request/job.
    # Let pipeline work finish in the retired session; stop NSGA-II at its
    # next callback. The cleaner removes retired outputs after completion.
    with s.lock:
        s["opt_state"]["stop_requested"] = True
        new_sid = str(uuid.uuid4())
        with _sessions_lock:
            _sessions[new_sid] = make_session_state()
        session["sid"] = new_sid
    return {"status": "reset"}


@app.route("/api/reset-opt", methods=["POST"])
@limiter.limit("60 per minute")
def reset_opt_state():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    with s.lock:
        if opt_st["status"] == "running" or s["pipeline_state"]["status"] == "running":
            return {"error": "Stop the active computation before resetting optimization"}, 409
        s.run_revision += 1
        s.selection_revision = 0
        s["opt_results"].clear()
        s["opt_results"].update(make_session_state()["opt_results"])
        opt_st.update({
            "status": "idle",
            "generation": 0,
            "error": "",
            "stop_requested": False,
            "history": [],
        })
    return {"status": "reset"}

@app.route("/api/stop-opt", methods=["POST"])
@limiter.limit("60 per minute")
def stop_opt_state():
    sid, s = _get_session()
    opt_st = s["opt_state"]
    with s.lock:
        if opt_st["status"] == "running":
            opt_st["stop_requested"] = True
    return {"status": "stop_requested"}


def _run_nsga2(sid, weight_mean, allowed_miss_pct, mutation_multiplier, pop_size, max_gen, ftol=0.0025, term_period=30, max_price=None):
    """Run NSGA-II optimization using the loaded dataset."""
    s = _get_session_by_sid(sid)
    opt_st = s["opt_state"]
    ds = s["dataset"]

    with s.lock:
        revisions = (s.dataset_revision, s.run_revision, s.selection_revision)

    cb = None  # Keep callback accessible for early-stop result extraction
    problem = None
    try:
        with s.lock:
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
        cb = WebappCallback(problem, opt_st, s.lock)
        res, elapsed_time = run_optimization(
            problem, X_init,
            pop_size=pop_size, seed=1,
            max_gen=max_gen, ftol=ftol,
            period=term_period,
            mutation_multiplier=mutation_multiplier,
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

        _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=max_price, revisions=revisions)

        with s.lock:
            if (s.dataset_revision, s.run_revision) == revisions[:2]:
                opt_st["status"] = "complete"

    except StopOptimization:
        # Early stop: extract results from the callback's saved population snapshot
        if cb is not None and cb.last_pop_X is not None and problem is not None:
            try:
                _process_stopped_results(sid, cb, problem, max_price=max_price, revisions=revisions)
            except Exception as inner_e:
                with s.lock:
                    if (s.dataset_revision, s.run_revision) != revisions[:2]:
                        return
                    opt_st["status"] = "error"
                    opt_st["error"] = f"Stopped, but failed to process partial results: {inner_e}"
                traceback.print_exc()
        else:
            with s.lock:
                if (s.dataset_revision, s.run_revision) != revisions[:2]:
                    return
                opt_st["status"] = "error"
                opt_st["error"] = "Optimization stopped before any generation completed."
    except Exception as e:
        with s.lock:
            if (s.dataset_revision, s.run_revision) != revisions[:2]:
                return
            opt_st["status"] = "error"
            opt_st["error"] = str(e)
        traceback.print_exc()
    finally:
        if problem is not None:
            problem._scores = None


def _process_stopped_results(sid, cb, problem, max_price=None, *, revisions=None):
    """Build and store results from the callback's population snapshot after early stop."""
    s = _get_session_by_sid(sid)
    opt_st = s["opt_state"]
    with s.lock:
        if revisions is None:
            revisions = (s.dataset_revision, s.run_revision, s.selection_revision)

    # Filter to feasible solutions (constraint G <= 0)
    G = cb.last_pop_G
    F = cb.last_pop_F
    X = cb.last_pop_X

    feasible_mask = (G <= 0).all(axis=1) if G.ndim > 1 else (G <= 0).ravel()
    if not np.any(feasible_mask):
        raise ValueError("Stopped before any solution met the coverage constraint. Try relaxing the allowed missed targets or running longer.")
    res_X = X[feasible_mask]
    res_F = F[feasible_mask]
    front_indices = NonDominatedSorting().do(res_F, only_non_dominated_front=True)
    res_X = res_X[front_indices]
    res_F = res_F[front_indices]

    # Build a lightweight result and select the best solution
    res_light = _LightResult(res_X, res_F)
    best_idx, front = select_best_solution(res_light, problem)

    _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=max_price, revisions=revisions)

    with s.lock:
        if (s.dataset_revision, s.run_revision) == revisions[:2]:
            opt_st["status"] = "complete"


def _prepare_solution(sid, res_X, res_F, index, matrix_file, problem, custom, revisions):
    s = _get_session_by_sid(sid)
    with s.lock:
        dataset = s["dataset"]
        metadata = dataset.get("matrix_metadata") if dataset["matrix_file"] == matrix_file else None
        scores = dataset["selectivities"]
        targets = dataset.get("target_columns")
    winning = prepare_selected_library(res_X[index], metadata, scores, targets, matrix_file)
    output_dir = PROJECT_ROOT / "webapp" / "output" / sid
    output_dir.mkdir(parents=True, exist_ok=True)
    revision_key = "_".join(str(value) for value in revisions)
    winning_file = str(output_dir / f"optimized_library_{revision_key}.xlsx")
    return {
        "selected_idx": int(index),
        "comparison": _build_comparison(winning, problem, custom),
        "winning_matrix_df": winning, "winning_file": winning_file,
        "heatmap_cache": _build_heatmap_cache(winning),
    }


def _publish_solution(s, revisions, payload):
    with s.lock:
        current = (s.dataset_revision, s.run_revision, s.selection_revision)
        if revisions == current:
            s["opt_results"].update(payload)
            return True
    Path(payload["winning_file"]).unlink(missing_ok=True)
    return False


def _process_and_store_results(sid, res_X, res_F, best_idx, front, problem, max_price=None, *, revisions=None):
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

    with s.lock:
        if revisions is None:
            revisions = (s.dataset_revision, s.run_revision, s.selection_revision)
        if revisions != (s.dataset_revision, s.run_revision, s.selection_revision):
            return False
        matrix_file = ds["matrix_file"]
        custom = ds.get("has_custom_affinity", False)
    payload = _prepare_solution(sid, res_X, res_F, best_idx, matrix_file, problem, custom, revisions)
    payload.update({
        "pareto_front": front.tolist(), "best_idx": int(best_idx),
        "res_X": res_X, "res_F": res_F, "problem": problem,
        "weight_mean": round(float(problem.weight_mean), 4) if hasattr(problem, "weight_mean") else 0.5,
        "weight_min": round(float(problem.weight_min), 4) if hasattr(problem, "weight_min") else 0.5,
    })
    return _publish_solution(s, revisions, payload)


# ═══════════════════════════════════════════════════════════════
#  ROUTES — Results
# ═══════════════════════════════════════════════════════════════

@app.route("/api/results")
@limiter.limit("300 per minute")
def get_results():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with s.lock:
        if opt_res["comparison"] is None:
            return {"error": "No results available yet"}, 404
        return {
            "comparison": opt_res["comparison"],
            "best_idx": opt_res["best_idx"],
        }


@app.route("/api/pareto-data")
@limiter.limit("300 per minute")
def pareto_data():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with s.lock:
        if opt_res["pareto_front"] is None:
            return {"error": "No Pareto data available"}, 404
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
    return {
        "points": front,
        "best_idx": best,
        "selected_idx": selected,
        "weight_mean": weight_mean,
        "weight_min": weight_min,
    }


@app.route("/api/select-solution", methods=["POST"])
@limiter.limit("120 per minute")
def select_solution():
    """Switch results only if this selection still belongs to the current run."""
    sid, s = _get_session()
    data = request.get_json() or {}
    if not isinstance(data, dict) or not isinstance(data.get("index"), int) or isinstance(data.get("index"), bool):
        return {"error": "A valid integer 'index' is required"}, 400
    index = data["index"]
    with s.lock:
        results = s["opt_results"]
        res_X, res_F = results.get("res_X"), results.get("res_F")
        problem = results.get("problem")
        matrix_file = s["dataset"].get("matrix_file")
        if res_X is None or problem is None or matrix_file is None:
            return {"error": "No optimization results available"}, 404
        if index < 0 or index >= res_X.shape[0]:
            return {"error": f"Index {index} out of range (0-{res_X.shape[0] - 1})"}, 400
        s.selection_revision += 1
        revisions = (s.dataset_revision, s.run_revision, s.selection_revision)
        custom = s["dataset"].get("has_custom_affinity", False)
    try:
        payload = _prepare_solution(sid, res_X, res_F, index, matrix_file, problem, custom, revisions)
        if not _publish_solution(s, revisions, payload):
            return {"error": "The results changed while selecting a solution. Please try again."}, 409
        return {"ok": True, "selected_idx": index}
    except Exception as exc:
        logger.exception("Could not select solution")
        return {"error": str(exc)}, 500


def _get_target_info(target_list):
    return get_target_info(target_list, get_chembl_db_path())


def _build_heatmap_cache(df):
    return build_heatmap_cache(df, _get_target_info)


@app.route("/api/heatmap-data")
@limiter.limit("300 per minute")
def heatmap_data():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with s.lock:
        if opt_res["winning_matrix_df"] is None:
            return {"error": "No heatmap data available"}, 404
        cache = opt_res.get("heatmap_cache")
        if cache:
            return cache
        df = opt_res["winning_matrix_df"]

    return _build_heatmap_cache(df)


@app.route("/api/download/library")
@limiter.limit("60 per minute")
def download_library():
    sid, s = _get_session()
    opt_res = s["opt_results"]
    with s.lock:
        path = opt_res.get("winning_file")
        frame = opt_res.get("winning_matrix_df")
    if not path or frame is None:
        return {"error": "No library file available"}, 404
    with s.export_lock:
        if not os.path.isfile(path):
            write_library_excel(frame, path)
    return send_file(path, as_attachment=True, download_name="optimized_library.xlsx")


@app.route("/api/download/matrix")
@limiter.limit("60 per minute")
def download_matrix():
    sid, s = _get_session()
    ds = s["dataset"]
    with s.lock:
        csv_path = ds.get("matrix_file")

    if not (csv_path and os.path.isfile(csv_path)):
        return {"error": "No matrix file available"}, 404

    with s.export_lock:
        xlsx_path = ensure_matrix_excel(csv_path)

    return send_file(xlsx_path, as_attachment=True, download_name="selectivity_matrix.xlsx")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if os.environ.get('OPTILIB_ENV') == 'production':
    from webapp.public.server import create_app
    app = create_app()


if __name__ == "__main__":
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"ChEMBL database: {get_chembl_db_path()}")
    logger.info(f"MolPort database: {DATABASE_DIR / 'molport.db'}")
    app.run(debug=False, host="127.0.0.1", port=5000)
