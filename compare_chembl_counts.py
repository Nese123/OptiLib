import requests
import json

# 1. Fetch all protein classifications under Kinase (protein_class_id=6) and Protein Kinase (protein_class_id=1100)
def get_all_protein_classes():
    all_pc = []
    offset = 0
    while True:
        r = requests.get(f'https://www.ebi.ac.uk/chembl/api/data/protein_classification.json?limit=1000&offset={offset}').json()
        all_pc.extend(r['protein_classifications'])
        if not r['page_meta']['next']:
            break
        offset += 1000
    return all_pc

all_pc = get_all_protein_classes()
pc_by_parent = {}
for pc in all_pc:
    pid = pc['parent_id']
    cid = pc['protein_class_id']
    pc_by_parent.setdefault(pid, []).append(cid)

def get_subtree_ids(root_id):
    res = {root_id}
    q = [root_id]
    while q:
        curr = q.pop(0)
        for child in pc_by_parent.get(curr, []):
            if child not in res:
                res.add(child)
                q.append(child)
    return res

# Protein Kinases (1100) vs All Kinases (6)
protein_kinase_ids = get_subtree_ids(1100)
all_kinase_ids = get_subtree_ids(6)

print(f"Total Protein Kinase class IDs (subtree of 1100): {len(protein_kinase_ids)}")
print(f"Total All Kinase class IDs (subtree of 6): {len(all_kinase_ids)}")

# Let's check target_component mapping
# We can fetch target components for Homo sapiens
print("Fetching target components for Homo sapiens...")
tc_list = []
offset = 0
while True:
    r = requests.get(f'https://www.ebi.ac.uk/chembl/api/data/target_component.json?tax_id=9606&limit=1000&offset={offset}').json()
    tcs = r.get('target_components', [])
    tc_list.extend(tcs)
    print(f"Fetched {len(tc_list)} target components...")
    if not r['page_meta']['next']:
        break
    offset += 1000

print(f"Total human target components: {len(tc_list)}")

# Map target components to protein kinase vs other kinases
pk_targets = set()
all_k_targets = set()
for tc in tc_list:
    p_classes = tc.get('protein_classifications') or []
    is_pk = False
    is_k = False
    for pc in p_classes:
        pcid = pc.get('protein_classification_id')
        if pcid in protein_kinase_ids:
            is_pk = True
        if pcid in all_kinase_ids:
            is_k = True
    
    for t in tc.get('targets', []):
        if t.get('target_type') == 'SINGLE PROTEIN':
            if is_pk:
                pk_targets.add((t['target_chembl_id'], t.get('pref_name')))
            if is_k:
                all_k_targets.add((t['target_chembl_id'], t.get('pref_name')))

print(f"\nResults based on ChEMBL Target Classification:")
print(f"Targets classified under 'Protein Kinase' (ID 1100 subtree): {len(pk_targets)}")
print(f"Targets classified under 'Kinase' (ID 6 subtree): {len(all_k_targets)}")

# Print sample of the Protein Kinase targets
print(f"\nSample of Protein Kinase targets (first 10):")
for tid, name in list(pk_targets)[:10]:
    print(f"  {tid}: {name}")
