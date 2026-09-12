"""Column and identifier conventions for matrices, uploads and exports."""

import pandas as pd

METADATA_COLUMNS = ("Compound_Name", "Molecule_ChEMBL_ID", "InChIKey", "SMILES", "Price_USD_per_mg")
METADATA_COLUMN_SET = frozenset(METADATA_COLUMNS)
_SENTINEL_STRINGS = frozenset({"nan", "None", "Unknown", ""})


def target_columns(frame):
    return [column for column in frame.columns if column not in METADATA_COLUMN_SET]


def clean_str(val):
    """Normalise a value to a clean string, returning '' for NaN/None/sentinel values."""
    s = str(val).strip() if pd.notna(val) else ""
    return "" if s in _SENTINEL_STRINGS else s


def format_target_col(pref_name, gene_symbol, fallback=""):
    """Format target column header as 'Target Name (Gene Symbol)' if distinct, otherwise fallback."""
    p_name = clean_str(pref_name)
    g_sym = clean_str(gene_symbol)
    fb = clean_str(fallback)

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


def format_compound_display(raw_id, resolved_info):
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


def format_target_display(raw_id, resolved_info):
    """Format a resolved target identifier for display."""
    info = resolved_info or {}
    if info.get("is_chembl"):
        name = info.get("pref_name") or info.get("gene_symbol") or info.get("chembl_id")
        gene_sym = info.get("gene_symbol") or info.get("chembl_id")
        if gene_sym:
            return f"{raw_id} -> {name} ({gene_sym})"
        return f"{raw_id} -> {name}"
    return str(raw_id)


def looks_like_inchikey(s):
    """Heuristic check whether a string looks like an InChIKey."""
    return len(s) == 27 and s[14] == '-' and s[25] == '-'


def looks_like_smiles(s):
    """Heuristic check whether a string looks like a SMILES string."""
    return bool('=' in s or '#' in s or '(' in s or 'c1' in s)
