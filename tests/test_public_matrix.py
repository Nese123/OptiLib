"""Compare disk-backed construction with the existing ChEMBL query semantics."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import numpy as np

import test_queries_storage as query_fixtures
from webapp.public.config import Policy
from webapp.public.matrices import build,open_dataset


class DiskMatrixTests(unittest.TestCase):
    def prepare_chembl(self):
        query_fixtures.CandidateQueryTests.setUp(self)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE optilib_selectivity_metadata(key TEXT,value TEXT)')
            db.execute('INSERT INTO optilib_selectivity_metadata VALUES (?,?)',('provenance',json.dumps({'scoring_version':'blended_boundary_average_v2','build_id':'fixture','h':5,'storage_decimals':4})))
            db.commit()

    def test_chembl_scores_metadata_and_missing_smiles_match(self):
        self.prepare_chembl()
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(tmp);policy.chembl=self.database
            out=Path(tmp)/'build';out.mkdir()
            progress = []
            def prices(records,*args,**kwargs):
                return np.arange(1,len(records)+1,dtype=float), {'custom':0,'molport':len(records),'molprice':0,'fallback':0}
            with patch('webapp.core.pricing._resolve_affinity_prices',side_effect=prices):
                description=build(out,'chembl',{'chembl_ids':['CHEMBL_T1','CHEMBL_T2'],'selectivity_threshold':.1,'remove_targets':False},None,policy,lambda:None,lambda value:progress.append(deepcopy(value)))
            matrix,prices,description=open_dataset(out)
            # Parent compound eligibility, alphabetical SMILES order and max
            # aggregation are unchanged; the missing-SMILES compound is omitted.
            np.testing.assert_array_equal(matrix,[[.2,.4],[.6,.1],[.5,.49]])
            self.assertEqual(description['targets'],['Target 1 (GENE1)','Target 2'])
            with closing(sqlite3.connect(out/'metadata.sqlite')) as db:
                self.assertEqual([r[0] for r in db.execute('SELECT chembl_id FROM compounds ORDER BY i')],['CHEMBL_M100','CHEMBL_M200','CHEMBL_M300'])
            self.assertIsInstance(matrix,np.memmap)
            summaries = {
                1: 'Found 3 candidate compounds for 2 targets.',
                2: 'All prices assigned. 3 prices found from the MolPort database, 0 prices approximated using MolPrice.',
            }
            details = [update['detail'] for update in progress]
            self.assertIn('Found 4 compounds so far...', details)
            self.assertTrue(any('4 candidate compounds before filtering' in detail for detail in details))
            self.assertEqual(progress[-1], {
                'current_step': 3, 'step_label': 'Saving matrix...',
                'detail': 'Saving matrix...', 'step_summaries': summaries,
            })
            for update in progress:
                self.assertTrue({'current_step', 'step_label', 'detail', 'step_summaries'} <= update.keys())
                if update['current_step'] == 2:
                    self.assertEqual(update['step_summaries'][1], summaries[1])

    def test_chembl_summary_counts_requested_targets_and_kept_compounds(self):
        self.prepare_chembl()
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(tmp)
            policy.chembl = self.database
            for matched_count, dropped in ((None, 2), (4, 3)):
                with self.subTest(matched_count=matched_count):
                    out = Path(tmp) / str(matched_count)
                    out.mkdir()
                    payload = {'chembl_ids': ['CHEMBL_T1', 'CHEMBL_T2', 'CHEMBL_T3'],
                               'selectivity_threshold': .5, 'remove_targets': True}
                    if matched_count is not None:
                        payload['matched_count'] = matched_count
                    progress = []
                    with patch('webapp.core.pricing._resolve_affinity_prices', return_value=(
                            np.array([10.]), {'custom': 0, 'molport': 1, 'molprice': 0, 'fallback': 0})):
                        description = build(out, 'chembl', payload, None, policy, lambda: None,
                                            lambda value: progress.append(deepcopy(value)))
                    self.assertEqual(description['shape'], [1, 1])
                    self.assertIn('Found 3 compounds so far...',
                                  [update['detail'] for update in progress])
                    self.assertEqual(progress[-1]['step_summaries'][1],
                                     'Found 1 candidate compound for 1 target. '
                                     f'{dropped} targets were dropped because they lacked compounds with sufficient affinity or selectivity.')

    def test_affinity_progress_summarizes_filtered_matrix_and_all_price_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Policy(tmp)
            upload = Path(tmp) / 'uploads.sqlite'
            with closing(sqlite3.connect(upload)) as db:
                db.executescript('''
                    CREATE TABLE affinity(compound TEXT,target TEXT,value REAL);
                    CREATE TABLE prices(compound TEXT,value REAL,file TEXT);
                    CREATE TABLE files(kind TEXT,name TEXT);
                ''')
                observations = [(f'A{i:03}', target, 9. if j == i % 2 else 1.)
                                for i in range(258) for j, target in enumerate(('T1', 'T2', 'T3'))]
                observations.extend(('Filtered', target, 5.) for target in ('T1', 'T2', 'T3'))
                db.executemany('INSERT INTO affinity VALUES (?,?,?)', observations)
                db.execute("INSERT INTO files VALUES ('prices','prices.csv')")
                db.executemany('INSERT INTO prices VALUES (?,?,?)',
                               [(f'A{i:03}', 10., 'prices.csv') for i in range(0, 258, 4)])
                db.commit()
            out = Path(tmp) / 'build'
            out.mkdir()
            progress = []
            price_batch_sizes = []
            def resolve_compounds(compounds, database):
                return {raw: {'chembl_id': '', 'inchi_key': '', 'smiles': ''} for raw in compounds}
            def prices(records, *args, **kwargs):
                price_batch_sizes.append(len(records))
                counts = dict.fromkeys(('custom', 'molport', 'molprice', 'fallback'), 0)
                result = []
                for record in records:
                    source = int(record['Compound_Name'][1:]) % 4
                    counts[('custom', 'molport', 'molprice', 'fallback')[source]] += 1
                    result.append((10., 20., 30., np.nan)[source])
                return np.array(result), counts
            with patch('webapp.public.matrices.resolve_targets', return_value={
                    target: {'canonical_name': target} for target in ('T1', 'T2', 'T3')}), \
                    patch('webapp.public.matrices.resolve_compounds', side_effect=resolve_compounds), \
                    patch('webapp.core.pricing._resolve_affinity_prices', side_effect=prices) as price_lookup:
                build(out, 'affinity', {'selectivity_threshold': .5}, upload, policy,
                      lambda: None, lambda value: progress.append(deepcopy(value)))
            matrix, assigned_prices, description = open_dataset(out)
            self.assertEqual(description['shape'], [258, 2])
            self.assertEqual(price_batch_sizes, [256, 2])
            self.assertEqual(price_lookup.call_count, 2)
            self.assertTrue(all(call.kwargs == {'fallback': False} for call in price_lookup.call_args_list))
            np.testing.assert_array_equal(assigned_prices[3::4], np.full(64, 20.))
            self.assertEqual(progress[-1], {
                'current_step': 3, 'step_label': 'Saving matrix...',
                'detail': 'Saving selectivity matrix...', 'step_summaries': {
                    1: ('Found 258 candidate compounds for 2 targets. '
                        '1 target was dropped due to low selectivity.'),
                    2: 'All prices assigned. 65 prices assigned from custom price file, 65 prices found from the MolPort database, 64 prices approximated using MolPrice, 64 median fallback.',
                },
            })
