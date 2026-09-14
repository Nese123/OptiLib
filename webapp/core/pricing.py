"""Price resolution shared with isolated computation jobs."""
import logging
import os
import sqlite3
import sys
from pathlib import Path
from contextlib import closing
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]
DATABASE_DIR = Path(os.environ.get("OPTILIB_DATABASE", ROOT / "database"))
MOLPRICE_DIR = ROOT / "MolPrice"
sys.path.insert(0, str(MOLPRICE_DIR))
from bin import numpy_predict
numpy_predict.RAY_AVAILABLE = False
NumpyFingerprints = numpy_predict.NumpyFingerprints
logger = logging.getLogger("optilib")
_model = None
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
        with closing(sqlite3.connect(Path(molport_db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
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

def _resolve_affinity_prices(meta_records, compounds_map, price_state, *, fallback=True):
    """Resolve ordered prices, predicting missing molecules in bounded batches."""
    prices = np.full(len(meta_records), np.nan, dtype=float)
    counts = {"custom": 0, "molport": 0, "molprice": 0, "fallback": 0}
    pending = []
    for index, row in enumerate(meta_records):
        compound = row["Compound_Name"]
        info = {**compounds_map.get(compound, {}),
                'chembl_id': row.get('Molecule_ChEMBL_ID', ''),
                'inchi_key': row.get('InChIKey', ''), 'smiles': row.get('SMILES', ''),
                'pref_name': compound}
        price = _lookup_custom_price(compound, info, price_state)
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
            global _model
            if _model is None:
                _model = NumpyFingerprints(weights_path=str(MOLPRICE_DIR / "models/Numpy/MP_Morgan_hybrid.pkl"))
            model = _model
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
    if fallback:
        prices[missing] = float(np.median(prices[~missing])) if np.any(~missing) else 100.0
    return prices, counts
