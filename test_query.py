import sqlite3
import pandas as pd
df = pd.read_excel("./webapp/static/example_targets.xlsx")
input_targets = df["Target"].dropna().astype(str).str.strip().tolist()
db_path = "./database/chembl_36.db"
conn = sqlite3.connect(db_path)
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
         AND csy.syn_type IN ("GENE_SYMBOL", "UNIPROT", "EC_NUMBER"))
    )
    AND td.target_type = 'SINGLE PROTEIN'
    AND td.organism = 'Homo sapiens'
"""
params = input_targets * 4
print("Running query...")
rows = conn.execute(query, params).fetchall()
print("Found rows:", len(rows))
for r in rows:
    print(r)
