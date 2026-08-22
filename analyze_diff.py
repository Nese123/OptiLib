import pandas as pd

df = pd.read_excel('/home/nese/OptiLib/chembl_human_kinases.xlsx')
print(f'Total unique targets in previous excel: {len(df)}')

# Check lipid kinases
lipid = df[df['Target'].str.contains('lipid|phosphatidylinositol|sphingosine|diacylglycerol|ceramide|PI3K|PI4K', case=False, na=False)]
print(f'Lipid kinases count: {len(lipid)}')

# Check carbohydrate/metabolic/small molecule kinases
metab = df[df['Target'].str.contains('hexokinase|glucokinase|phosphofructokinase|pyruvate kinase|galactokinase|fructokinase|ribokinase|ketohexokinase|phosphoglycerate kinase|creatine kinase|glycerol kinase', case=False, na=False)]
print(f'Carbohydrate/metabolic kinases count: {len(metab)}')

# Check nucleotide/nucleoside kinases
nucleo = df[df['Target'].str.contains('adenylate kinase|thymidine kinase|nucleoside|nucleotide|guanylate kinase|uridine|cytidine|deoxycytidine|ribonucleoside|thymidylate kinase', case=False, na=False)]
print(f'Nucleotide/nucleoside kinases count: {len(nucleo)}')

# Check mutant/fragment/domain targets
mutants = df[df['Target'].str.contains('mutant|domain|fragment|isoform|phosphorylated|inactive', case=False, na=False)]
print(f'Mutant/fragment/domain targets count: {len(mutants)}')

# Non-protein kinases total
non_pk = df[df['Target'].str.contains('lipid|phosphatidylinositol|sphingosine|diacylglycerol|ceramide|PI3K|PI4K|hexokinase|glucokinase|phosphofructokinase|pyruvate kinase|galactokinase|fructokinase|ribokinase|ketohexokinase|phosphoglycerate kinase|creatine kinase|glycerol kinase|adenylate kinase|thymidine kinase|nucleoside|nucleotide|guanylate kinase|uridine|cytidine|deoxycytidine|ribonucleoside|thymidylate kinase|mutant|domain|fragment|isoform', case=False, na=False)]
print(f'Total non-protein or mutant/fragment kinase entries matching substrings: {len(non_pk)}')
print('Examples of non-canonical protein kinase targets in the 663:')
print(non_pk[['Target', 'ChEMBL_ID']].head(15))
