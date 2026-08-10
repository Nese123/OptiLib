import sqlite3
import time

db_path = "database/chembl_36.db"
input_targets = ["EGFR", "BRAF", "P00533", "CHEMBL210", "XYZ123"]
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
         AND csy.syn_type IN ('GENE_SYMBOL', 'UNIPROT'))
    )
    AND td.target_type = 'SINGLE PROTEIN'
    AND td.organism = 'Homo sapiens'
"""
params = input_targets * 4

t0 = time.time()
with sqlite3.connect(db_path) as conn:
    rows = conn.execute(query, params).fetchall()
print(f"Query took {time.time()-t0:.4f} seconds")
print(f"Returned {len(rows)} rows")
for r in rows:
    print(r)
