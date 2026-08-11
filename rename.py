import os

def replace_in_file(filepath, replacements):
    with open(filepath, 'r') as f:
        content = f.read()
    
    for old, new in replacements:
        content = content.replace(old, new)
        
    with open(filepath, 'w') as f:
        f.write(content)

replacements = [
    ('OptiDrug', 'OptiLib'),
    ('text-white">Drug</span>', 'text-white">DrugLib</span>')
]

replace_in_file('/home/nese/OptiDrug/webapp/templates/home.html', replacements)
replace_in_file('/home/nese/OptiDrug/webapp/app.py', replacements)
replace_in_file('/home/nese/OptiDrug/webapp/static/style.css', replacements)
