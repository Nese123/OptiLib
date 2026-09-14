"""Disk-backed construction, metadata and selected-library summaries."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np

from webapp.core.algorithm import matrix_blocks, column_maximum
from webapp.core.chembl import get_selectivity_provenance
from webapp.core.queries import read_chembl_candidates
from webapp.core.records import format_target_col, clean_str
from webapp.core.resolution import resolve_compounds, resolve_targets
from webapp.core.selectivity import SELECTIVITY_SCORING_VERSION, score_measured_affinities


def connect(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA temp_store=FILE')
    db.execute('PRAGMA cache_size=-16384')
    return db


def price_map(upload, chembl):
    if upload is None:
        return {}
    result = {}
    with closing(connect(upload)) as db:
        # File insertion order and the last measurement within a file preserve
        # the existing incremental custom-price precedence.
        cursor = db.execute("SELECT p.compound,p.value FROM prices p JOIN files f ON f.kind='prices' AND f.name=p.file ORDER BY f.rowid,p.rowid")
        while batch := cursor.fetchmany(256):
            resolved = resolve_compounds([row[0] for row in batch],chembl)
            for compound,price in batch:
                info = resolved[compound]
                for key in (compound.lower(),compound.upper(),info.get('chembl_id','').upper(),
                            info.get('inchi_key','').upper(),info.get('smiles',''),info.get('pref_name','').lower()):
                    if key:
                        result[key] = float(price)
    return result


def build(directory, kind, payload, upload, policy, checkpoint, progress):
    directory = Path(directory)
    scratch = directory / 'build.sqlite'
    with closing(connect(scratch)) as db:
        db.executescript('''
            CREATE TABLE observations(compound TEXT,target TEXT,value REAL);
            CREATE TABLE compounds(compound TEXT PRIMARY KEY,record TEXT);
            CREATE TABLE target_map(raw TEXT PRIMARY KEY,canonical TEXT);
        ''')
        provenance = {'scoring_version':SELECTIVITY_SCORING_VERSION,'build_id':directory.name}
        if kind == 'chembl':
            ids = payload['chembl_ids']
            policy.dimensions(0, len(ids))
            with closing(sqlite3.connect(policy.chembl.as_uri()+'?mode=ro', uri=True)) as source:
                source.set_progress_handler(lambda: 1 if checkpoint() else 0, 10000)
                source.execute('BEGIN')
                provenance = get_selectivity_provenance(source)
                if provenance['scoring_version'] != SELECTIVITY_SCORING_VERSION:
                    raise ValueError('ChEMBL selectivity data needs updating before building a matrix.')
                def sink(frame):
                    checkpoint()
                    observations, compounds = [], []
                    for row in frame.itertuples(index=False):
                        smiles = clean_str(row.SMILES) or 'Missing_SMILES'
                        target = format_target_col(row.Target_Pref_Name,row.Target_Gene_Symbol,row.Target_ChEMBL_ID)
                        record = {'Compound_Name':clean_str(row.Compound_Name), 'Molecule_ChEMBL_ID':clean_str(row.Molecule_ChEMBL_ID), 'InChIKey':clean_str(row.InChIKey), 'SMILES':smiles}
                        observations.append((smiles,target,float(row.Selectivity_Score)))
                        compounds.append((smiles,json.dumps(record)))
                    db.executemany('INSERT INTO observations VALUES (?,?,?)',observations)
                    db.executemany('INSERT OR IGNORE INTO compounds VALUES (?,?)',compounds)
                    db.commit()
                    policy.dimensions(db.execute('SELECT count(*) FROM compounds').fetchone()[0],len(ids))
                _, active = read_chembl_candidates(source,ids,payload.get('selectivity_threshold',.5),sink=sink)
                if not active:
                    raise ValueError('No targets have qualifying ChEMBL activity.')
        else:
            if upload is None:
                raise ValueError('Upload affinity data first.')
            db.execute('ATTACH DATABASE ? AS uploads', (str(upload),))
            targets = [r[0] for r in db.execute('SELECT DISTINCT target FROM uploads.affinity')]
            if not targets:
                raise ValueError('Upload affinity data first.')
            resolved = resolve_targets(targets, policy.chembl)
            db.executemany('INSERT INTO target_map VALUES (?,?)', [(t,resolved[t]['canonical_name']) for t in targets])
            # De-duplicate original measurements before canonical target grouping.
            db.execute('INSERT INTO observations SELECT a.compound,m.canonical,avg(a.value) FROM (SELECT DISTINCT compound,target,value FROM uploads.affinity) a JOIN target_map m ON m.raw=a.target GROUP BY a.compound,m.canonical')
            cursor = db.execute('SELECT DISTINCT compound FROM observations ORDER BY compound')
            while rows := cursor.fetchmany(256):
                checkpoint()
                resolved = resolve_compounds([r[0] for r in rows],policy.chembl)
                db.executemany('INSERT INTO compounds VALUES (?,?)', [(raw,json.dumps({
                    'Compound_Name':raw,'Molecule_ChEMBL_ID':resolved[raw]['chembl_id'],
                    'InChIKey':resolved[raw]['inchi_key'],'SMILES':resolved[raw]['smiles'] or 'Missing_SMILES',
                })) for (raw,) in rows])
            db.commit()
        checkpoint()
        db.execute('CREATE INDEX observation_lookup ON observations(compound,target)')
        db.execute('CREATE INDEX observation_target ON observations(target)')
        targets = [r[0] for r in db.execute('SELECT DISTINCT target FROM observations ORDER BY target')]
        compounds_n = db.execute('SELECT count(*) FROM compounds').fetchone()[0]
        policy.dimensions(compounds_n,len(targets))
        if not compounds_n or len(targets) < 2:
            raise ValueError('At least one compound and two targets are required.')
        target_index = {t:i for i,t in enumerate(targets)}
        matrix = np.lib.format.open_memmap(directory/'raw.npy',mode='w+',dtype='float64',shape=(compounds_n,len(targets)))
        # Initialize per row: touching the entire mapping at once is unnecessary.
        for i,(compound,) in enumerate(db.execute('SELECT compound FROM compounds ORDER BY compound')):
            if i % 256 == 0:
                checkpoint()
                progress({'current_step':1,'step_label':'Building matrix','detail':f'{i:,} of {compounds_n:,} compounds'})
            row = np.full(len(targets),np.nan)
            for target,value in db.execute('SELECT target,max(value) FROM observations WHERE compound=? GROUP BY target',(compound,)):
                row[target_index[target]] = value
            if kind == 'affinity':
                measured = np.flatnonzero(~np.isnan(row))
                row[measured] = score_measured_affinities(row[measured])
            matrix[i] = row
        threshold = payload.get('selectivity_threshold', .5)
        rows_keep = np.zeros(compounds_n,dtype=bool)
        for start,block in matrix_blocks(matrix):
            rows_keep[start:start+len(block)] = np.nan_to_num(np.fmax.reduce(block,axis=1),nan=-np.inf) >= threshold
        cols_keep = column_maximum(matrix) >= threshold if payload.get('remove_targets',True) else np.ones(len(targets),dtype=bool)
        if kind == 'chembl':
            for i,(compound,) in enumerate(db.execute('SELECT compound FROM compounds ORDER BY compound')):
                if compound == 'Missing_SMILES':
                    rows_keep[i] = False
        indices, columns = np.flatnonzero(rows_keep), np.flatnonzero(cols_keep)
        if not len(indices) or not len(columns):
            raise ValueError('No compounds or targets remain at this selectivity threshold.')
        final = np.lib.format.open_memmap(directory/'matrix.npy',mode='w+',dtype='float64',shape=(len(indices),len(columns)))
        offset = 0
        for start,block in matrix_blocks(matrix):
            checkpoint()
            selected = block[rows_keep[start:start+len(block)]][:,columns]
            final[offset:offset+len(selected)] = selected
            offset += len(selected)
        final.flush()
        matrix.flush()
        del final,matrix
        (directory/'raw.npy').unlink()
        progress({'current_step':2,'step_label':'Resolving prices','detail':'Looking up and predicting compound prices'})
        custom_prices = price_map(upload, policy.chembl)
        prices = np.full(len(indices),np.nan)
        metadata = directory/'metadata.sqlite'
        from webapp.core.pricing import _resolve_affinity_prices
        with closing(connect(metadata)) as meta:
            meta.execute('CREATE TABLE compounds(i INTEGER PRIMARY KEY,name TEXT,chembl_id TEXT,inchikey TEXT,smiles TEXT,price REAL)')
            kept = set(indices.tolist())
            records, output_index = [], 0
            def flush():
                nonlocal output_index
                if not records:
                    return
                checkpoint()
                mapping = {r['Compound_Name']:{'chembl_id':r['Molecule_ChEMBL_ID'],'inchi_key':r['InChIKey'],'smiles':r['SMILES'],'pref_name':r['Compound_Name']} for r in records}
                # Resolve finite prices without applying a per-batch fallback.
                resolved_prices, counts = _resolve_affinity_prices(records,mapping,{'price_map':custom_prices}, fallback=False)
                for record,price in zip(records,resolved_prices):
                    prices[output_index] = price
                    meta.execute('INSERT INTO compounds VALUES (?,?,?,?,?,?)',(output_index,record['Compound_Name'],record['Molecule_ChEMBL_ID'],record['InChIKey'],record['SMILES'],float(price) if np.isfinite(price) else None))
                    output_index += 1
                records.clear()
            for i,(_,record) in enumerate(db.execute('SELECT compound,record FROM compounds ORDER BY compound')):
                if i in kept:
                    records.append(json.loads(record))
                    if len(records) >= 256:
                        flush()
            flush()
            missing = ~np.isfinite(prices)
            fallback = float(np.median(prices[~missing])) if (~missing).any() else 100.
            prices[missing] = fallback
            meta.execute('UPDATE compounds SET price=? WHERE price IS NULL',(fallback,))
            meta.commit()
        np.save(directory/'prices.npy',prices,allow_pickle=False)
        description = {'shape':[len(indices),len(columns)],'targets':[targets[i] for i in columns],
                       'provenance':provenance,'custom':kind=='affinity','total_cost':float(prices.sum())}
        (directory/'dataset.json').write_text(json.dumps(description,allow_nan=False))
    scratch.unlink(missing_ok=True)
    return description


def open_dataset(directory):
    directory = Path(directory)
    description = json.loads((directory/'dataset.json').read_text())
    return (np.load(directory/'matrix.npy',mmap_mode='r',allow_pickle=False),
            np.load(directory/'prices.npy',mmap_mode='r',allow_pickle=False),description)
