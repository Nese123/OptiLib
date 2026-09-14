"""Target-scoped ChEMBL queries, run inside the caller's read transaction."""

import pandas as pd


def read_chembl_candidates(conn, chembl_ids, threshold, progress=None, *, sink=None):
    conn.execute("CREATE TEMP TABLE requested_ids (chembl_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO requested_ids VALUES (?)",
                     ((cid.upper(),) for cid in chembl_ids))
    conn.execute("""
        CREATE TEMP TABLE requested_targets AS
        SELECT td.tid, td.chembl_id, td.pref_name,
               (SELECT sy.component_synonym FROM target_components tc
                JOIN component_synonyms sy ON sy.component_id = tc.component_id
                WHERE tc.tid = td.tid AND sy.syn_type = 'GENE_SYMBOL'
                LIMIT 1) AS gene_symbol
        FROM requested_ids ids JOIN target_dictionary td ON td.chembl_id = ids.chembl_id
        WHERE td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens'
    """)
    conn.execute("CREATE UNIQUE INDEX requested_targets_tid ON requested_targets(tid)")
    conn.execute("""
        CREATE TEMP TABLE qualifying_activity (
            tid INTEGER NOT NULL, molregno INTEGER, UNIQUE(tid, molregno)
        )
    """)
    # Fix join order to start from the small requested-target set. Maintenance
    # creates covering indexes for the two following probes.
    conn.execute("""
        INSERT OR IGNORE INTO qualifying_activity
        SELECT rt.tid, act.molregno
        FROM requested_targets rt CROSS JOIN assays ass CROSS JOIN activities act
        WHERE ass.tid = rt.tid AND act.assay_id = ass.assay_id
          AND ass.confidence_score IN (8, 9) AND act.pchembl_value > 5.0
    """)
    conn.execute("DELETE FROM requested_targets WHERE tid NOT IN (SELECT tid FROM qualifying_activity)")
    active_ids = {row[0] for row in conn.execute("SELECT chembl_id FROM requested_targets")}
    if not active_ids:
        return pd.DataFrame(), active_ids
    conn.execute("CREATE TEMP TABLE potent_compounds (molregno INTEGER PRIMARY KEY)")
    conn.execute("""
        INSERT OR IGNORE INTO potent_compounds
        SELECT COALESCE(mh.parent_molregno, md.molregno)
        FROM qualifying_activity qa JOIN molecule_dictionary md ON md.molregno = qa.molregno
        LEFT JOIN molecule_hierarchy mh ON mh.molregno = md.molregno
    """)
    query = """
        SELECT cts.molregno AS Clean_Molregno,
               md.chembl_id AS Molecule_ChEMBL_ID, md.pref_name AS Compound_Name,
               cs.canonical_smiles AS SMILES, cs.standard_inchi_key AS InChIKey,
               cp.full_mwt AS MW, td.chembl_id AS Target_ChEMBL_ID,
               td.pref_name AS Target_Pref_Name, td.gene_symbol AS Target_Gene_Symbol,
               cts.selectivity_score AS Selectivity_Score
        FROM requested_targets td CROSS JOIN compound_target_selectivity cts
        JOIN potent_compounds pc ON pc.molregno = cts.molregno
        JOIN molecule_dictionary md ON md.molregno = cts.molregno
        LEFT JOIN compound_structures cs ON cs.molregno = cts.molregno
        LEFT JOIN compound_properties cp ON cp.molregno = cts.molregno
        WHERE cts.tid = td.tid
          AND EXISTS (
              SELECT 1 FROM compound_target_selectivity scores
              WHERE scores.molregno = cts.molregno AND scores.selectivity_score > ?
          )
    """
    chunks = []
    compounds = set()
    for chunk in pd.read_sql_query(query, conn, params=(threshold,), chunksize=1000):
        if sink is None:
            chunks.append(chunk)
        else:
            sink(chunk)
        if progress is not None:
            compounds.update(chunk["Clean_Molregno"])
            progress(len(compounds))
    return (pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()), active_ids
