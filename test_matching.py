import sqlite3
import pandas as pd
df = pd.read_excel("./webapp/static/example_targets.xlsx")
input_targets = df["Target"].dropna().astype(str).str.strip().tolist()

# Get the rows from the DB query just like app.py does
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
rows = conn.execute(query, params).fetchall()

matched_chembl_ids = set()
matched = []
unmatched = []

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
            matched_chembl_ids.add(cid)
            matched.append(f"{target_in} -> {cid} ({name})")
            break
    if not found:
        unmatched.append(target_in)

print(f"Matched count: {len(matched)}")
print(f"Unmatched count: {len(unmatched)}")
if len(unmatched) > 0:
    print("Unmatched:", unmatched[:5])
