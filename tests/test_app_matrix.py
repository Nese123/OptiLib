"""Matrix cache and read-snapshot regressions using isolated session outputs."""

import importlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

with patch('sqlite3.connect'), patch('shutil.rmtree'), patch.object(Path, 'unlink'), \
        patch('threading.Thread.start'), patch('atexit.register'):
    app_module = importlib.import_module('webapp.app')


class MatrixCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, value in (('PROJECT_ROOT', Path(self.tmp.name)), ('_sessions', {})):
            replacement = patch.object(app_module, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.limiter.enabled = False
        self.client = app_module.app.test_client()
        self.client.get('/api/status')
        with self.client.session_transaction() as session:
            self.sid = session['sid']
        self.state = app_module._sessions[self.sid]
        self.database = Path(self.tmp.name) / 'chembl.db'
        with sqlite3.connect(self.database) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE probe (score REAL)')
            conn.execute('INSERT INTO probe VALUES (1)')
            conn.execute('CREATE TABLE optilib_selectivity_metadata (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute("INSERT INTO optilib_selectivity_metadata VALUES ('provenance', ?)",
                         (json.dumps(self.provenance('first')),))
        replacement = patch.object(app_module, 'get_chembl_db_path', return_value=self.database)
        replacement.start()
        self.addCleanup(replacement.stop)

    def provenance(self, build):
        return {'scoring_version': app_module.SELECTIVITY_SCORING_VERSION,
                'build_id': build, 'h': 5, 'storage_decimals': 4}

    def test_affinity_rebuild_never_serves_old_excel_for_same_row_count(self):
        affinity = self.state['affinity_upload_state']
        affinity['df'] = pd.DataFrame({
            'Compound_Raw': ['A', 'A', 'B', 'B'],
            'Target_Raw': ['T1', 'T2', 'T1', 'T2'],
            'Affinity': [8., 6., 6., 8.],
        })
        paths = []
        with patch.object(app_module, '_lookup_molport_prices', return_value=({}, {})):
            for price, affinity_value in ((10., 8.), (99., 8.), (99., 9.)):
                self.state['price_upload_state']['price_map'] = {'a': price, 'b': 20.}
                affinity['df'].loc[0, 'Affinity'] = affinity_value
                app_module._run_affinity_pipeline(self.sid, .5, True)
                self.assertEqual(self.state['pipeline_state']['status'], 'complete')
                paths.append(self.state['dataset']['matrix_file'])
                response = self.client.get('/api/download/matrix')
                self.assertEqual(response.status_code, 200)
                exported = pd.read_excel(io.BytesIO(response.data))
                response.close()
                self.assertEqual(exported.loc[exported['Compound_Name'] == 'A', 'Price_USD_per_mg'].iloc[0], price)
                self.assertEqual(exported.loc[exported['Compound_Name'] == 'A', 'T1'].iloc[0], affinity_value - 6.)
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(Path(path).with_suffix('.xlsx').is_file() for path in paths))

    def test_legacy_provenance_is_rejected_before_cache_or_query(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute('DROP TABLE optilib_selectivity_metadata')
        with patch.object(app_module, '_matrix_cache_key') as key, \
                patch.object(app_module, 'read_chembl_candidates') as query:
            app_module._run_pipeline(self.sid, ['CHEMBL1'], .5, True, 1)
        self.assertEqual(self.state['pipeline_state']['status'], 'error')
        self.assertIn('needs updating', self.state['pipeline_state']['error'])
        key.assert_not_called()
        query.assert_not_called()

    def test_provenance_and_scores_use_one_read_snapshot(self):
        def read_candidates(conn, *args, **kwargs):
            self.assertTrue(conn.in_transaction)
            with sqlite3.connect(self.database) as writer:
                writer.execute('UPDATE probe SET score=2')
                writer.execute("UPDATE optilib_selectivity_metadata SET value=? WHERE key='provenance'",
                               (json.dumps(self.provenance('second')),))
            score = conn.execute('SELECT score FROM probe').fetchone()[0]
            return pd.DataFrame({
                'Clean_Molregno': [1], 'Molecule_ChEMBL_ID': ['CHEMBL2'],
                'Compound_Name': ['A'], 'SMILES': ['CC'], 'InChIKey': ['IK'],
                'MW': [30.], 'Target_ChEMBL_ID': ['CHEMBL1'],
                'Target_Pref_Name': ['Target'], 'Target_Gene_Symbol': ['T'],
                'Selectivity_Score': [score],
            }), {'CHEMBL1'}

        with patch.object(app_module, 'read_chembl_candidates', side_effect=read_candidates), \
                patch.object(app_module, '_lookup_molport_prices', return_value=({'IK': 10.}, {'IK': 'MP'})):
            app_module._run_pipeline(self.sid, ['CHEMBL1'], .5, True, 1)
        self.assertEqual(self.state['pipeline_state']['status'], 'complete')
        self.assertEqual(self.state['dataset']['selectivities'].tolist(), [[1.]])
        self.assertEqual(self.state['dataset']['provenance']['build_id'], 'first')
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(app_module.get_selectivity_provenance(conn)['build_id'], 'second')

    def test_chembl_prices_coerce_invalid_smiles_and_keep_every_compound(self):
        frame = pd.DataFrame({
            'Clean_Molregno': [1, 2, 3], 'Molecule_ChEMBL_ID': ['CHEMBL10', 'CHEMBL20', 'CHEMBL30'],
            'Compound_Name': ['A', None, 'C'], 'SMILES': ['CC', 'bad', 'CCC'],
            'InChIKey': ['IKA', 'IKB', 'IKC'], 'MW': [30., 40., 50.],
            'Target_ChEMBL_ID': ['CHEMBL1'] * 3, 'Target_Pref_Name': ['Target'] * 3,
            'Target_Gene_Symbol': ['T'] * 3, 'Selectivity_Score': [1., 2., 3.],
        })
        self.state['price_upload_state']['price_map'] = {'CHEMBL10': 5.}
        with patch.object(app_module, 'read_chembl_candidates', return_value=(frame, {'CHEMBL1'})), \
                patch.object(app_module, '_lookup_molport_prices', return_value=({}, {})), \
                patch.object(app_module, 'NumpyFingerprints') as constructor:
            # One invalid SMILES must not discard the other prediction.
            constructor.return_value.predict_batch_from_smiles.return_value = [[7.], [np.nan]]
            app_module._run_pipeline(self.sid, ['CHEMBL1'], .5, True, 1)
        self.assertEqual(self.state['pipeline_state']['status'], 'complete',
                         self.state['pipeline_state']['error'])
        constructor.return_value.predict_batch_from_smiles.assert_called_once_with(
            ['CCC', 'bad'], batch_size=256, errors='coerce')
        self.assertEqual(self.state['dataset']['num_drugs'], 3)
        self.assertEqual(sorted(self.state['dataset']['prices'].tolist()), [5., 6., 7.])
        self.assertIn('1 median fallback', self.state['pipeline_state']['step_summaries'][2])

    def test_numeric_excel_headers_and_string_flags_are_client_errors(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['Compound', 'Target', 'Affinity', 2024])
        book.active.append(['A', 'T1', 8., 1])
        content = io.BytesIO()
        book.save(content)
        content.seek(0)
        with patch.object(app_module, '_resolve_compounds', return_value={}), \
                patch.object(app_module, '_resolve_targets', return_value={}):
            response = self.client.post('/api/upload-affinity',
                                        data={'files[]': (content, 'numeric.xlsx')})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['num_datapoints'], 1)
        response = self.client.post('/api/build-matrix-from-affinity',
                                    json={'remove_targets': 'false'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state['pipeline_state']['status'], 'idle')


if __name__ == '__main__':
    unittest.main()
