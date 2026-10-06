"""Bounded public heatmap tiles retain local symbol and hover labels."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from webapp.core.resolution import get_target_info
from webapp.public.config import Policy
from webapp.public.server import create_app


class PublicHeatmapTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            'OPTILIB_ENV': 'development',
            'SESSION_COOKIE_SECURE': 'false',
            'TRUSTED_HOSTS': '',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Policy(self.tmp.name)
        self.policy.chembl = Path(self.tmp.name) / 'chembl.db'
        self.app = create_app(self.policy)
        self.app.config.update(TESTING=True)
        self.app.extensions['public_limiter'].enabled = False
        self.runtime = self.app.extensions['runtime']
        self.client = self.app.test_client()
        page = self.client.get('/optimize')
        self.sid = page.headers['X-Optilib-Session']

        self.targets = [f'Target {i}' for i in range(48)]
        self.targets[40:44] = [
            'ATP-dependent RNA helicase family member 1 (DDX1)',
            'CHEMBL2',
            'Unrecognized custom target',
            'Aldehyde dehydrogenase family member A1 (ALDH1A1)',
        ]
        self.matrix = np.arange(24 * 48, dtype=float).reshape(24, 48)
        self.matrix[1, 42] = np.nan
        directory = self.runtime.artifact(self.sid, 'fixture')
        directory.mkdir(parents=True)
        self.directory = directory
        np.save(directory / 'matrix.npy', self.matrix, allow_pickle=False)
        np.save(directory / 'rows.npy', np.arange(24), allow_pickle=False)
        np.save(directory / 'columns.npy', np.arange(48), allow_pickle=False)
        (directory / 'dataset.json').write_text(json.dumps({'targets': self.targets}))
        (directory / 'selection.json').write_text(json.dumps({
            'zmin': -3, 'zmax': 7, 'distribution': [],
        }))
        with sqlite3.connect(directory / 'metadata.sqlite') as db:
            db.execute('CREATE TABLE compounds(i INTEGER PRIMARY KEY,name TEXT,chembl_id TEXT,inchikey TEXT,price REAL)')
            db.executemany('INSERT INTO compounds VALUES (?,?,?,?,?)', [
                (i, f'Compound {i}', f'CHEMBL{i}', '', 1.) for i in range(24)
            ])
        with self.runtime.connect() as db:
            db.execute('UPDATE sessions SET state=? WHERE sid=?',
                       (json.dumps({'dataset': 'fixture', 'selection': 'fixture'}), self.sid))
        with sqlite3.connect(self.policy.chembl) as db:
            db.executescript('''
                CREATE TABLE target_dictionary(tid INTEGER,chembl_id TEXT,pref_name TEXT,target_type TEXT,organism TEXT);
                CREATE TABLE target_components(tid INTEGER,component_id INTEGER);
                CREATE TABLE component_sequences(component_id INTEGER,accession TEXT);
                CREATE TABLE component_synonyms(component_id INTEGER,component_synonym TEXT,syn_type TEXT);
                INSERT INTO target_dictionary VALUES(2,'CHEMBL2','Epidermal growth factor receptor','SINGLE PROTEIN','Homo sapiens');
                INSERT INTO target_components VALUES(2,2);
                INSERT INTO component_sequences VALUES(2,'P00533');
                INSERT INTO component_synonyms VALUES(2,'EGFR','GENE_SYMBOL');
            ''')

    def test_offset_tile_uses_symbols_and_preserves_names_and_missing_values(self):
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            response = self.client.get('/api/heatmap-data?row_offset=1&column_offset=40&row_count=2&column_count=4')
        self.assertEqual(response.status_code, 200, response.json)
        tile = response.json
        resolve.assert_called_once_with(self.targets, self.policy.chembl)
        self.assertEqual(tile['targets'], ['DDX1', 'EGFR', 'Unrecognized custom target', 'ALDH1A1'])
        self.assertEqual(tile['target_names'], [
            'ATP-dependent RNA helicase family member 1',
            'Epidermal growth factor receptor',
            'Unrecognized custom target',
            'Aldehyde dehydrogenase family member A1',
        ])
        self.assertEqual(tile['compounds'], ['CHEMBL1', 'CHEMBL2'])
        self.assertEqual(tile['matrix'], [[88., 89., None, 91.], [136., 137., 138., 139.]])
        self.assertEqual((tile['row_offset'], tile['column_offset']), (1, 40))
        self.assertEqual((tile['total_rows'], tile['total_columns']), (24, 48))
        self.assertEqual((tile['zmin'], tile['zmax']), (-3, 7))
        self.assertEqual(tile['revision'], 'fixture')
        self.assertTrue(tile['paged'])

    def test_pages_remain_bounded_and_unknown_labels_survive_missing_database(self):
        self.policy.chembl = Path(self.tmp.name) / 'missing.db'
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            first = self.client.get('/api/heatmap-data').json
        resolve.assert_called_once_with(self.targets, self.policy.chembl)
        self.assertEqual((len(first['matrix']), len(first['matrix'][0])), (20, 40))
        self.assertEqual(first['targets'], self.targets[:40])
        self.assertEqual(first['target_names'], self.targets[:40])
        last = self.client.get('/api/heatmap-data?row_offset=22&column_offset=46').json
        self.assertEqual(last['compounds'], ['CHEMBL22', 'CHEMBL23'])
        self.assertEqual(last['targets'], self.targets[46:])
        self.assertEqual(last['matrix'], self.matrix[22:, 46:].tolist())
        for query in ('row_count=101', 'column_count=101', 'row_offset=-1',
                      'column_offset=-1', 'row_offset=25', 'column_offset=49'):
            with self.subTest(query=query):
                response = self.client.get('/api/heatmap-data?' + query)
                self.assertEqual(response.status_code, 400, response.json)

    def test_distribution_resolves_all_target_labels_with_cached_metadata_beyond_first_tile(self):
        stats = [{'target': raw, 'min': i - 5., 'median': i - .5, 'max': i + 2.}
                 for i, raw in enumerate(self.targets)]
        legacy = {'zmin': -3, 'zmax': 7, 'distribution': stats}
        (self.directory / 'selection.json').write_text(json.dumps(legacy))
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            first = self.client.get('/api/heatmap-data').json
            later = self.client.get('/api/heatmap-data?row_offset=4&column_offset=40').json
        resolve.assert_called_once_with(self.targets, self.policy.chembl)
        self.assertEqual(len(first['targets']), 40)
        self.assertEqual(len(first['distribution']), 48)
        self.assertEqual(first['distribution'], later['distribution'])
        self.assertEqual([item['target'] for item in first['distribution'][40:44]],
                         ['DDX1', 'EGFR', 'Unrecognized custom target', 'ALDH1A1'])
        self.assertEqual([item['target_name'] for item in first['distribution'][40:44]], [
            'ATP-dependent RNA helicase family member 1',
            'Epidermal growth factor receptor',
            'Unrecognized custom target',
            'Aldehyde dehydrogenase family member A1',
        ])
        self.assertEqual([tuple(item[key] for key in ('min', 'median', 'max'))
                          for item in first['distribution']],
                         [tuple(item[key] for key in ('min', 'median', 'max')) for item in stats])
        self.assertEqual(json.loads((self.directory / 'selection.json').read_text()), legacy)

    def test_distribution_preserves_legacy_labels_above_metadata_cache_limit(self):
        targets = self.targets + [f'Additional target {i}' for i in range(953)]
        (self.directory / 'dataset.json').write_text(json.dumps({'targets': targets}))
        np.save(self.directory / 'matrix.npy',
                np.pad(self.matrix, ((0, 0), (0, 953)), constant_values=np.nan), allow_pickle=False)
        stats = [{'target': self.targets[40], 'min': -1., 'median': 2., 'max': 4.}]
        (self.directory / 'selection.json').write_text(json.dumps({
            'zmin': -3, 'zmax': 7, 'distribution': stats,
        }))
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            response = self.client.get('/api/heatmap-data')
        self.assertEqual(response.status_code, 200, response.json)
        resolve.assert_called_once_with(targets[:40], self.policy.chembl)
        self.assertEqual(response.json['distribution'], stats)

    def test_target_metadata_is_cached_across_pages_and_invalidated_for_updated_artifacts(self):
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            first = self.client.get('/api/heatmap-data?column_offset=40').json
            later = self.client.get('/api/heatmap-data?row_offset=4&column_offset=41').json
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(first['targets'][0], 'DDX1')
            self.assertEqual(later['targets'][0], 'EGFR')

            self.targets[40] = 'Updated target name (NEWGENE)'
            description = self.directory / 'dataset.json'
            previous = description.stat()
            description.write_text(json.dumps({'targets': self.targets}))
            os.utime(description, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
            updated = self.client.get('/api/heatmap-data?column_offset=40').json
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(updated['targets'][0], 'NEWGENE')
            self.assertEqual(updated['target_names'][0], 'Updated target name')

    def test_target_metadata_cache_is_isolated_by_session_artifact(self):
        selection = {'zmin': -3, 'zmax': 7, 'distribution': [
            {'target': self.targets[40], 'min': -1., 'median': 2., 'max': 4.},
        ]}
        (self.directory / 'selection.json').write_text(json.dumps(selection))
        other = self.app.test_client()
        other_sid = other.get('/optimize').headers['X-Optilib-Session']
        directory = self.runtime.artifact(other_sid, 'fixture')
        shutil.copytree(self.directory, directory)
        targets = list(self.targets)
        targets[40] = 'Other session target (OTHERGENE)'
        (directory / 'dataset.json').write_text(json.dumps({'targets': targets}))
        selection['distribution'][0]['target'] = targets[40]
        (directory / 'selection.json').write_text(json.dumps(selection))
        with self.runtime.connect() as db:
            db.execute('UPDATE sessions SET state=? WHERE sid=?',
                       (json.dumps({'dataset': 'fixture', 'selection': 'fixture'}), other_sid))
        with patch('webapp.core.resolution.get_target_info', wraps=get_target_info) as resolve:
            first = self.client.get('/api/heatmap-data?column_offset=40').json
            second = other.get('/api/heatmap-data?column_offset=40').json
            again = self.client.get('/api/heatmap-data?column_offset=40').json
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(first['targets'][0], 'DDX1')
        self.assertEqual(second['targets'][0], 'OTHERGENE')
        self.assertEqual(again['targets'], first['targets'])
        self.assertEqual(first['distribution'][0]['target'], 'DDX1')
        self.assertEqual(second['distribution'][0]['target'], 'OTHERGENE')
        self.assertEqual(again['distribution'], first['distribution'])

    def test_small_library_preload_returns_full_data_without_changing_tile_limits(self):
        response = self.client.get('/api/heatmap-data?preload=1')
        self.assertEqual(response.status_code, 200, response.json)
        data = response.json
        self.assertFalse(data['paged'])
        self.assertEqual((data['row_offset'], data['column_offset']), (0, 0))
        self.assertEqual((len(data['matrix']), len(data['matrix'][0])), (24, 48))
        self.assertEqual(data['matrix'][1][42], None)
        self.assertEqual(data['compounds'], [f'CHEMBL{i}' for i in range(24)])
        self.assertEqual(data['targets'][40:44], ['DDX1', 'EGFR', 'Unrecognized custom target', 'ALDH1A1'])
        self.assertEqual((data['zmin'], data['zmax']), (-3, 7))
        self.assertEqual(data['revision'], 'fixture')
        normal = self.client.get('/api/heatmap-data').json
        self.assertTrue(normal['paged'])
        self.assertEqual((len(normal['matrix']), len(normal['matrix'][0])), (20, 40))
        for query in ('preload=2', 'preload=1&row_count=101', 'preload=1&column_count=101'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/heatmap-data?' + query).status_code, 400)

    def test_preload_rejects_large_library_while_default_tiles_stay_bounded(self):
        # A valid selection can include many rows without loading them all.
        np.save(self.directory / 'matrix.npy', np.tile(self.matrix, (44, 1)), allow_pickle=False)
        np.save(self.directory / 'rows.npy', np.arange(1056), allow_pickle=False)
        with sqlite3.connect(self.directory / 'metadata.sqlite') as db:
            db.executemany('INSERT INTO compounds VALUES (?,?,?,?,?)', [
                (i, f'Compound {i}', f'CHEMBL{i}', '', 1.) for i in range(24, 1056)
            ])
        response = self.client.get('/api/heatmap-data?preload=1')
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('50,000 cells', response.json['error'])
        tile = self.client.get('/api/heatmap-data').json
        self.assertTrue(tile['paged'])
        self.assertEqual((len(tile['matrix']), len(tile['matrix'][0])), (20, 40))
        self.assertEqual(tile['total_rows'], 1056)

    def test_preload_bounds_label_counts_for_thin_tall_libraries(self):
        np.save(self.directory / 'matrix.npy', np.tile(self.matrix, (42, 1))[:1001], allow_pickle=False)
        np.save(self.directory / 'rows.npy', np.arange(1001), allow_pickle=False)
        np.save(self.directory / 'columns.npy', np.array([0]), allow_pickle=False)
        with sqlite3.connect(self.directory / 'metadata.sqlite') as db:
            db.executemany('INSERT INTO compounds VALUES (?,?,?,?,?)', [
                (i, f'Compound {i}', f'CHEMBL{i}', '', 1.) for i in range(24, 1001)
            ])
        response = self.client.get('/api/heatmap-data?preload=1')
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('1,000 compounds and targets', response.json['error'])
        tile = self.client.get('/api/heatmap-data').json
        self.assertTrue(tile['paged'])
        self.assertEqual((len(tile['matrix']), len(tile['matrix'][0])), (20, 1))
        self.assertEqual((tile['total_rows'], tile['total_columns']), (1001, 1))


if __name__ == '__main__':
    unittest.main()
