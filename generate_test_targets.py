import sqlite3
import random
import os
import csv

db_path = "database/chembl_36.db"

# We want roughly 10 of each type
with sqlite3.connect(db_path) as conn:
    # 10 ChEMBL IDs
    chembl_ids = [row[0] for row in conn.execute("SELECT chembl_id FROM target_dictionary WHERE target_type='SINGLE PROTEIN' AND organism='Homo sapiens' ORDER BY RANDOM() LIMIT 10").fetchall()]
    
    # 10 Target Names
    target_names = [row[0] for row in conn.execute("SELECT pref_name FROM target_dictionary WHERE target_type='SINGLE PROTEIN' AND organism='Homo sapiens' AND pref_name IS NOT NULL ORDER BY RANDOM() LIMIT 10").fetchall()]
    
    # 10 UniProt Accessions
    uniprot_ids = [row[0] for row in conn.execute("""
        SELECT cs.accession FROM component_sequences cs
        JOIN target_components tc ON cs.component_id = tc.component_id
        JOIN target_dictionary td ON tc.tid = td.tid
        WHERE td.target_type='SINGLE PROTEIN' AND td.organism='Homo sapiens' AND cs.accession IS NOT NULL
        ORDER BY RANDOM() LIMIT 10
    """).fetchall()]
    
    # 10 Gene Symbols
    gene_symbols = [row[0] for row in conn.execute("""
        SELECT csy.component_synonym FROM component_synonyms csy
        JOIN target_components tc ON csy.component_id = tc.component_id
        JOIN target_dictionary td ON tc.tid = td.tid
        WHERE td.target_type='SINGLE PROTEIN' AND td.organism='Homo sapiens' AND csy.syn_type = 'GENE_SYMBOL'
        ORDER BY RANDOM() LIMIT 10
    """).fetchall()]

fake_targets = [
    "FAKE_CHEMBL999",
    "FAKE_CHEMBL888",
    "Fake Growth Factor Receptor",
    "Fake Kinase 1",
    "P99999",
    "Q88888",
    "FAKEGENE1",
    "FAKEGENE2",
    "10.9.9.9",
    "XYZ123_FAKE"
]

all_targets = chembl_ids + target_names + uniprot_ids + gene_symbols + fake_targets

# Shuffle the list
random.seed(42)
random.shuffle(all_targets)

# Save to CSV
output_path = os.path.join(os.getcwd(), "test_targets.csv")
with open(output_path, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["Target"])
    for t in all_targets:
        writer.writerow([t])
        
print(f"Saved 50 test targets to {output_path}")
