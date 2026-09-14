"""ChEMBL compound and target identifier resolution without Flask state."""

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .records import format_target_col, looks_like_inchikey, looks_like_smiles

logger = logging.getLogger("optilib")


def resolve_compounds(compound_ids, db_path):
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

    db_path = str(db_path)
    raw_matches = []
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
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
            ik_guess = raw_str if looks_like_inchikey(raw_str) else ""
            smi_guess = raw_str if looks_like_smiles(raw_str) else ""
            resolved[raw_id] = {
                "raw_id": raw_id,
                "chembl_id": "",
                "pref_name": raw_str,
                "inchi_key": ik_guess,
                "smiles": smi_guess,
                "is_chembl": False,
            }

    return resolved


def resolve_targets(target_ids, db_path):
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

    db_path = str(db_path)
    rows = []
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
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

    # Keep the first matching database row, as the previous nested scan did.
    # Building this index once avoids rescanning every row for every input.
    by_identifier = {}
    for row in rows:
        cid, name, gene_sym, acc, syn, _ = row
        for identifier in (cid, name, gene_sym, acc, syn):
            if identifier:
                by_identifier.setdefault(str(identifier).lower(), row)

    resolved = {}
    for target_in in unique_targets:
        match = by_identifier.get(target_in.lower())
        if match is not None:
            cid, name, gene_sym, acc, _, _ = match
            resolved[target_in] = {
                "raw_id": target_in,
                "chembl_id": cid or "",
                "pref_name": name or "",
                "gene_symbol": gene_sym or "",
                "accession": acc or "",
                "canonical_name": format_target_col(name, gene_sym, cid or target_in),
                "is_chembl": True,
            }
        else:
            resolved[target_in] = {
                "raw_id": target_in,
                "chembl_id": "",
                "pref_name": target_in,
                "gene_symbol": "",
                "accession": "",
                "canonical_name": target_in,
                "is_chembl": False,
            }

    return resolved


def get_target_info(target_list, db_path):
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
        db_path = str(db_path)
        if os.path.exists(db_path) and to_lookup:
            lookup_list = list(to_lookup)
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
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
