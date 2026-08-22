import requests
import json

# Let's query targets with pref_name__icontains=kinase, single protein, Homo sapiens
r = requests.get('https://www.ebi.ac.uk/chembl/api/data/target.json?target_organism=Homo+sapiens&target_type=SINGLE+PROTEIN&pref_name__icontains=kinase&limit=1000')
data = r.json()['targets']

print(f"Total targets returned by name substring search: {len(data)}")

# Unique pref_names
unique_names = {}
for t in data:
    name = t.get('pref_name')
    chembl_id = t.get('target_chembl_id')
    if name not in unique_names:
        unique_names[name] = chembl_id

print(f"Unique target names: {len(unique_names)}")

# Categorize them:
mutants = [n for n in unique_names if any(w in n.lower() for w in ['mutant', 'domain', 'fragment', 'phosphorylated', 'inactive', 'catalytic', 'truncated'])]
lipids = [n for n in unique_names if any(w in n.lower() for w in ['phosphatidylinositol', 'lipid', 'sphingosine', 'diacylglycerol', 'ceramide', 'pi3k', 'pi4k', 'phosphoinositide'])]
metabolic = [n for n in unique_names if any(w in n.lower() for w in ['hexokinase', 'glucokinase', 'phosphofructokinase', 'pyruvate kinase', 'galactokinase', 'fructokinase', 'ribokinase', 'ketohexokinase', 'phosphoglycerate kinase', 'creatine kinase', 'glycerol kinase', 'phosphoglucomutase', 'mevalonate kinase'])]
nucleotide = [n for n in unique_names if any(w in n.lower() for w in ['adenylate kinase', 'thymidine kinase', 'nucleoside', 'nucleotide', 'guanylate kinase', 'uridine', 'cytidine', 'deoxycytidine', 'thymidylate kinase', 'uridine-cytidine kinase'])]

print(f"\nBreakdown of entries in the 663 unique targets:")
print(f"1. Mutants / Domain constructs / Fragments: {len(mutants)}")
print(f"2. Lipid kinases (PI3K, Sphingosine kinase, etc.): {len(lipids)}")
print(f"3. Carbohydrate / Metabolic kinases (Hexokinase, Pyruvate kinase, etc.): {len(metabolic)}")
print(f"4. Nucleotide / Nucleoside kinases (Adenylate kinase, Thymidine kinase, etc.): {len(nucleotide)}")

non_canonical = set(mutants + lipids + metabolic + nucleotide)
print(f"Total non-canonical / non-protein / variant kinase targets: {len(non_canonical)}")
print(f"Remaining (canonical protein kinase names): ~{len(unique_names) - len(non_canonical)}")
