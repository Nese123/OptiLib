import sqlite3
import pandas as pd
from pathlib import Path

db_path = "/home/nese/OptiDrug/database/chembl_36.db"
conn = sqlite3.connect(db_path)

# ChEMBL IDs
query_chembl = "SELECT chembl_id FROM target_dictionary WHERE target_type = 'SINGLE PROTEIN' AND organism = 'Homo sapiens' LIMIT 10"
chembl_ids = pd.read_sql_query(query_chembl, conn)['chembl_id'].tolist()

# Target Names
query_name = "SELECT pref_name FROM target_dictionary WHERE pref_name IS NOT NULL AND target_type = 'SINGLE PROTEIN' AND organism = 'Homo sapiens' LIMIT 10"
names = pd.read_sql_query(query_name, conn)['pref_name'].tolist()

# UniProt Accessions
query_acc = """SELECT cs.accession FROM target_dictionary td 
JOIN target_components tc ON td.tid = tc.tid 
JOIN component_sequences cs ON tc.component_id = cs.component_id
WHERE cs.accession IS NOT NULL AND td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens' LIMIT 10"""
accessions = pd.read_sql_query(query_acc, conn)['accession'].tolist()

# Gene Symbols
query_gene = """SELECT csy.component_synonym FROM target_dictionary td 
JOIN target_components tc ON td.tid = tc.tid 
JOIN component_synonyms csy ON tc.component_id = csy.component_id
WHERE csy.syn_type = 'GENE_SYMBOL' AND td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens' LIMIT 10"""
genes = pd.read_sql_query(query_gene, conn)['component_synonym'].tolist()

targets = chembl_ids + names + accessions + genes

df = pd.DataFrame({'Target': targets})
# Don't shuffle to keep it predictable, or shuffle it? Let's not shuffle, just order it by type.
df.to_excel("/home/nese/OptiDrug/webapp/static/example_targets.xlsx", index=False)
