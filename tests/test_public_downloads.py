"""Download caching, preparation metadata and atomic export regressions."""
import io
import json
import os
import re
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np
from openpyxl import load_workbook

import test_public as fixtures
from webapp.core.records import METADATA_COLUMNS
from webapp.public import matrices, tasks


class DownloadTests(unittest.TestCase):
    setUp = fixtures.PublicTests.setUp
    perform = fixtures.PublicTests.perform
    upload = fixtures.PublicTests.upload
    dataset = fixtures.PublicTests.dataset

    def optimize(self):
        return self.perform(self.client.post('/api/run', json={
            'pop_size': 5, 'max_gen': 10, 'allowed_miss_pct': 0,
        }))

    def export_jobs(self):
        with self.runtime.connect() as db:
            return db.execute("SELECT count(*) FROM jobs WHERE sid=? AND kind='export'",
                              (self.sid,)).fetchone()[0]

    def assert_workbook_matches(self, response, which):
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        state = self.runtime.state(self.sid)
        directory = self.runtime.artifact(self.sid, state['dataset'])
        matrix, prices, description = matrices.open_dataset(directory)
        if which == 'library':
            selected = self.runtime.artifact(self.sid, state['selection'])
            rows = np.load(selected / 'rows.npy', allow_pickle=False)
            columns = np.load(selected / 'columns.npy', allow_pickle=False)
        else:
            rows, columns = np.arange(matrix.shape[0]), np.arange(matrix.shape[1])
        book = load_workbook(io.BytesIO(response.data), read_only=True)
        self.addCleanup(book.close)
        self.addCleanup(response.close)
        actual = list(book.active.values)
        self.assertEqual(actual[0], tuple(METADATA_COLUMNS) + tuple(
            description['targets'][int(column)] for column in columns))
        self.assertEqual(len(actual) - 1, len(rows))
        with sqlite3.connect(directory / 'metadata.sqlite') as db:
            for row, index in zip(actual[1:], rows):
                metadata = db.execute(
                    'SELECT name,chembl_id,inchikey,smiles,price FROM compounds WHERE i=?',
                    (int(index),)).fetchone()
                self.assertEqual(row[:len(METADATA_COLUMNS)], tuple(
                    value if value != '' else None for value in metadata))
                np.testing.assert_allclose(row[len(METADATA_COLUMNS):],
                                           matrix[int(index), columns])

    def assert_prepared(self, response, which, fmt='xlsx'):
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json, {
            'status': 'complete',
            'download_url': f'/api/download/{which}?format={fmt}',
        })
        self.assertEqual(response.mimetype, 'application/json')
        self.assertNotIn('Content-Disposition', response.headers)
        self.assertNotIn('X-Accel-Redirect', response.headers)

    def test_library_export_is_ready_for_optimized_and_newly_selected_rows(self):
        self.dataset()
        self.optimize()
        original = self.runtime.state(self.sid)
        old_key = f"library:xlsx:{original['selection']}"
        self.assertIn(old_key, original['exports'])
        self.assertEqual(self.export_jobs(), 0)
        self.assert_workbook_matches(self.client.get('/api/download/library'), 'library')

        # Add a known alternative to the short real optimization's solutions.
        # The download must use this choice rather than the former cached rows.
        solutions = self.runtime.artifact(self.sid, original['solutions'])
        choices = np.load(solutions / 'choices.npy', allow_pickle=False)
        alternative = np.zeros(choices.shape[1], dtype=bool)
        alternative[0] = True
        np.save(solutions / 'choices.npy', np.vstack((choices, alternative)), allow_pickle=False)
        path = solutions / 'solutions.json'
        description = json.loads(path.read_text())
        index = len(description['points'])
        description['points'].append([0, 10])
        path.write_text(json.dumps(description))
        self.perform(self.client.post('/api/select-solution', json={'index': index}))

        current = self.runtime.state(self.sid)
        self.assertNotEqual(current['selection'], original['selection'])
        self.assertNotIn(old_key, current['exports'])
        self.assertIn(f"library:xlsx:{current['selection']}", current['exports'])
        self.assertEqual(self.client.get('/api/results').json['total'], 1)
        self.assert_workbook_matches(self.client.get('/api/download/library'), 'library')
        self.assertEqual(self.export_jobs(), 0)

    def test_cached_preparation_returns_metadata_without_transferring_the_file(self):
        self.dataset()
        self.optimize()
        for accelerated in ('false', 'true'):
            with self.subTest(accelerated=accelerated), patch.dict(
                    os.environ, {'OPTILIB_ACCEL_REDIRECT': accelerated}):
                self.assert_prepared(self.client.get('/api/download/library?prepare=1'), 'library')
        self.assertEqual(self.export_jobs(), 0)

    def test_matrix_export_deduplicates_and_survives_selection_run_and_reset(self):
        self.dataset()
        self.optimize()
        response = self.client.get('/api/download/matrix?prepare=1')
        self.assertEqual(response.status_code, 202, response.json)
        duplicate = self.client.get('/api/download/matrix?prepare=1')
        self.assertEqual(response.json['job_id'], duplicate.json['job_id'])
        self.assertEqual(self.export_jobs(), 1)
        self.perform(response)
        self.assert_prepared(self.client.get('/api/download/matrix?prepare=1'), 'matrix')
        self.assert_workbook_matches(self.client.get('/api/download/matrix'), 'matrix')
        original = self.runtime.state(self.sid)
        matrix_key = f"matrix:xlsx:{original['dataset']}"
        reference = original['exports'][matrix_key]
        path = self.runtime.artifact(self.sid, reference)

        for transition in ('selection', 'optimization', 'reset'):
            with self.subTest(transition=transition):
                old_libraries = {key for key in self.runtime.state(self.sid)['exports']
                                 if key.startswith('library:')}
                if transition == 'selection':
                    self.perform(self.client.post('/api/select-solution', json={'index': 0}))
                elif transition == 'optimization':
                    self.optimize()
                else:
                    self.assertEqual(self.client.post('/api/reset-opt').status_code, 200)
                exports = self.runtime.state(self.sid)['exports']
                self.assertEqual(exports[matrix_key], reference)
                self.assertTrue(path.is_file())
                self.assertTrue(old_libraries.isdisjoint(exports))
                self.assert_prepared(self.client.get('/api/download/matrix?prepare=1'), 'matrix')
                self.assertEqual(self.export_jobs(), 1)

        self.assertEqual(self.client.get('/api/download/library?prepare=1').status_code, 409)
        self.upload('affinity', 'Compound,Target,Affinity\nA,T1,9\nA,T2,1\nB,T1,1\nB,T2,9\n')
        self.perform(self.client.post('/api/build-matrix-from-affinity', json={}))
        current = self.runtime.state(self.sid)
        self.assertNotEqual(current['dataset'], original['dataset'])
        self.assertEqual(current['exports'], {})
        self.assertEqual(self.client.get('/api/download/matrix?prepare=1').status_code, 202)
        self.assertEqual(self.export_jobs(), 2)

    def test_reset_preserves_matrix_export_completed_after_request_snapshot(self):
        self.dataset()
        self.optimize()
        response = self.client.get('/api/download/matrix?prepare=1')
        self.assertEqual(response.status_code, 202, response.json)
        job_id = response.json['job_id']
        with self.runtime.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        original_session = self.runtime.session

        def complete_export_after_snapshot(sid, epoch):
            snapshot = original_session(sid, epoch)
            tasks.execute(self.runtime, self.runtime.job(job_id))
            return snapshot

        with patch.object(self.runtime, 'session', side_effect=complete_export_after_snapshot):
            response = self.client.post('/api/reset-opt')
        self.assertEqual(response.status_code, 200, response.json)
        state = self.runtime.state(self.sid)
        key = f"matrix:xlsx:{state['dataset']}"
        self.assertEqual(state['exports'], {key: f'{job_id}/matrix.xlsx'})
        self.assertIsNone(state['selection'])
        self.assertIsNone(state['solutions'])
        self.assert_prepared(self.client.get('/api/download/matrix?prepare=1'), 'matrix')

    def test_library_above_eager_limit_uses_deduplicated_async_export(self):
        self.dataset()
        with patch.object(tasks, 'EAGER_LIBRARY_EXPORT_CELLS', 1):
            self.optimize()
        state = self.runtime.state(self.sid)
        self.assertEqual(state['exports'], {})
        self.assertFalse((self.runtime.artifact(self.sid, state['selection']) / 'library.xlsx').exists())
        response = self.client.get('/api/download/library?prepare=1')
        self.assertEqual(response.status_code, 202, response.json)
        duplicate = self.client.get('/api/download/library?prepare=1')
        self.assertEqual(response.json['job_id'], duplicate.json['job_id'])
        self.perform(response)
        self.assert_prepared(self.client.get('/api/download/library?prepare=1'), 'library')
        self.assert_workbook_matches(self.client.get('/api/download/library'), 'library')
        self.assertEqual(self.export_jobs(), 1)

    def test_preparation_preserves_session_isolation_and_csrf_admission(self):
        self.dataset()
        self.optimize()
        other = self.app.test_client()
        self.assertEqual(other.get('/api/download/library?prepare=1').status_code, 409)
        self.assertEqual(other.get('/api/download/matrix?prepare=1').status_code, 409)
        self.assertEqual(self.export_jobs(), 0)

        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.get('/api/download/matrix?prepare=1').status_code, 400)
        self.assertEqual(self.export_jobs(), 0)
        page = self.client.get('/optimize')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        response = self.client.get('/api/download/matrix?prepare=1', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 202, response.json)
        self.perform(response)
        self.assert_prepared(self.client.get('/api/download/matrix?prepare=1'), 'matrix')
        self.assertEqual(other.get('/api/download/matrix?prepare=1').status_code, 409)

    def test_export_writer_failure_cleans_temporary_file_and_preserves_destination(self):
        self.dataset()
        job_id = self.runtime.admit(self.sid, 'ip', 'export', {}, reserve=1)
        context = tasks.Context(self.runtime, self.runtime.job(job_id))
        destination = context.directory / 'matrix.xlsx'
        destination.write_bytes(b'previous complete file')

        def fail_writer(headers, rows, temporary):
            temporary.write_bytes(b'incomplete file')
            raise OSError('disk write failed')

        dataset = context.artifact(self.runtime.state(self.sid)['dataset'])
        with patch.object(tasks, 'write_rows_excel', side_effect=fail_writer):
            with self.assertRaisesRegex(OSError, 'disk write failed'):
                tasks.write_export(context, dataset, 'matrix', 'xlsx')
        self.assertEqual(destination.read_bytes(), b'previous complete file')
        self.assertFalse((context.directory / 'matrix.xlsx.tmp').exists())
        self.assertEqual(self.runtime.state(self.sid)['exports'], {})

    def test_stop_after_spreadsheet_close_prevents_atomic_publication(self):
        self.dataset()
        job_id = self.runtime.admit(self.sid, 'ip', 'export', {}, reserve=1)
        context = tasks.Context(self.runtime, self.runtime.job(job_id))
        writer = tasks.write_rows_excel

        def stop_after_close(headers, rows, temporary):
            writer(headers, rows, temporary)
            self.runtime.stop(self.sid)

        dataset = context.artifact(self.runtime.state(self.sid)['dataset'])
        with patch.object(tasks, 'write_rows_excel', side_effect=stop_after_close):
            with self.assertRaises(tasks.Cancelled):
                tasks.write_export(context, dataset, 'matrix', 'xlsx')
        self.assertFalse((context.directory / 'matrix.xlsx').exists())
        self.assertFalse((context.directory / 'matrix.xlsx.tmp').exists())
        self.assertEqual(self.runtime.state(self.sid)['exports'], {})


if __name__ == '__main__':
    unittest.main()
