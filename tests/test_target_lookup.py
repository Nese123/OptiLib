"""Indexed target matching parity, read-only fallback and offline preparation."""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from webapp.core.resolution import resolve_targets
from webapp.core.target_lookup import lookup_target_rows, rebuild_target_lookup


class TargetLookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chembl.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript("""
                CREATE TABLE target_dictionary (
                    tid INTEGER PRIMARY KEY, chembl_id TEXT, pref_name TEXT,
                    target_type TEXT, organism TEXT
                );
                CREATE INDEX target_organism ON target_dictionary (organism);
                CREATE TABLE target_components (
                    tid INTEGER, component_id INTEGER, PRIMARY KEY (tid, component_id)
                );
                CREATE TABLE component_sequences (
                    component_id INTEGER PRIMARY KEY, accession TEXT
                );
                CREATE TABLE component_synonyms (
                    component_id INTEGER, component_synonym TEXT, syn_type TEXT,
                    UNIQUE (component_id, component_synonym, syn_type)
                );
                INSERT INTO target_dictionary VALUES
                    (1, 'CHEMBL1', 'First kinase', 'SINGLE PROTEIN', 'Homo sapiens'),
                    (2, 'CHEMBL2', 'Second kinase', 'SINGLE PROTEIN', 'Homo sapiens'),
                    (3, 'CHEMBL3', 'Mouse kinase', 'SINGLE PROTEIN', 'Mus musculus'),
                    (4, 'CHEMBL4', 'Complex', 'PROTEIN COMPLEX', 'Homo sapiens'),
                    (5, 'CHEMBL5', 'No components', 'SINGLE PROTEIN', 'Homo sapiens'),
                    (6, 'CHEMBL6', NULL, 'SINGLE PROTEIN', 'Homo sapiens'),
                    (7, 'CHEMBL7', 'É kiñase', 'SINGLE PROTEIN', 'Homo sapiens');
                INSERT INTO target_components VALUES (1,11), (1,12), (2,21), (3,31), (4,41), (6,61);
                INSERT INTO component_sequences VALUES
                    (11,'ACC1'), (12,'ACC12'), (21,'ACC2'), (31,'MOUSE'), (41,'COMPLEX'), (61,'ACC6');
                INSERT INTO component_synonyms VALUES
                    (11,'GENE','GENE_SYMBOL'), (11,'alias','UNIPROT'), (11,'1.2.3.4','EC_NUMBER'),
                    (11,'OTHER_ALIAS','OTHER'), (12,'GENE12','GENE_SYMBOL'),
                    (21,'GENE','GENE_SYMBOL'), (21,'alias','UNIPROT'),
                    (31,'MOUSE_GENE','GENE_SYMBOL'), (41,'COMPLEX_GENE','GENE_SYMBOL'),
                    (61,'GENE6','GENE_SYMBOL');
            """)
            conn.commit()

    def prepare(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return rebuild_target_lookup(conn)

    def traced_resolution(self, inputs):
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch("webapp.core.resolution.sqlite3.connect", side_effect=traced_connect):
            result = resolve_targets(inputs, self.database)
        return result, statements

    def test_cached_and_original_results_match_for_identifiers_and_ambiguities(self):
        cases = [
            ['gene', 'ALIAS', 'acc2', 'chembl1', 'First kinase', 'unknown'],
            ['GENE12'], ['GENE12', 'CHEMBL1'],
            ['OTHER_ALIAS'], ['OTHER_ALIAS', 'CHEMBL1'],
            ['ACC12', 'GENE', '1.2.3.4', 'No components', 'CHEMBL6', 'GENE6'],
            ['CHEMBL3', 'MOUSE_GENE', 'CHEMBL4', 'COMPLEX_GENE'],
            [' ', ' First kinase ', 'First kinase', 'ACC1', 'acc1'],
            ['é kiñase'], ['É KIñASE'], ['é kiñase', 'CHEMBL7'],
        ]
        expected = [resolve_targets(inputs, self.database) for inputs in cases]
        self.assertGreater(self.prepare(), 0)
        for inputs, original in zip(cases, expected):
            with self.subTest(inputs=inputs):
                actual, statements = self.traced_resolution(inputs)
                self.assertEqual(actual, original)
                self.assertEqual(list(actual), list(original))
                self.assertFalse(any('FROM target_dictionary' in sql for sql in statements))
                self.assertTrue(any('FROM optilib_target_lookup AS lookup' in sql for sql in statements))

    def test_lookup_is_read_only_and_uses_identifier_index(self):
        self.prepare()
        with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)) as conn:
            matches = lookup_target_rows(conn, ['ACC1', 'unknown'])
            self.assertEqual(matches['acc1'][0], 'CHEMBL1')
            self.assertNotIn('unknown', matches)
            plan = conn.execute("""
                EXPLAIN QUERY PLAN SELECT row_number FROM optilib_target_lookup
                WHERE identifier IN (?, ?)
            """, ('acc1', 'unknown')).fetchall()
            self.assertTrue(any('SEARCH' in row[3] and 'identifier=?' in row[3] for row in plan), plan)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                conn.execute('DELETE FROM optilib_target_lookup')

    def test_refresh_replaces_removed_aliases_and_changed_names(self):
        self.prepare()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE target_dictionary SET pref_name='Renamed kinase' WHERE tid=1")
            conn.execute("DELETE FROM component_synonyms WHERE component_synonym='alias'")
            conn.execute("INSERT INTO component_synonyms VALUES (11,'NEW_ALIAS','UNIPROT')")
        self.prepare()
        result = resolve_targets(['CHEMBL1', 'alias', 'NEW_ALIAS', 'First kinase'], self.database)
        self.assertEqual(result['CHEMBL1']['pref_name'], 'Renamed kinase')
        self.assertTrue(result['NEW_ALIAS']['is_chembl'])
        self.assertFalse(result['alias']['is_chembl'])
        self.assertFalse(result['First kinase']['is_chembl'])

    def test_failed_refresh_preserves_previous_index(self):
        self.prepare()
        previous = resolve_targets(['CHEMBL1', 'ACC1'], self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('DROP TABLE component_sequences')
            conn.commit()
            with self.assertRaises(sqlite3.OperationalError):
                rebuild_target_lookup(conn)
            self.assertIsNone(conn.execute("""
                SELECT name FROM sqlite_master WHERE name='optilib_target_lookup_build'
            """).fetchone())
        self.assertEqual(resolve_targets(['CHEMBL1', 'ACC1'], self.database), previous)

    def test_missing_incompatible_and_partial_indexes_use_original_query(self):
        inputs = ['CHEMBL1', 'ACC2', 'unknown']
        expected = resolve_targets(inputs, self.database)
        self.prepare()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE optilib_target_lookup_metadata SET value='incompatible'")
        actual, statements = self.traced_resolution(inputs)
        self.assertEqual(actual, expected)
        self.assertTrue(any('FROM target_dictionary' in sql for sql in statements))
        self.prepare()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute('DROP TABLE optilib_target_lookup_rows')
        actual, statements = self.traced_resolution(inputs)
        self.assertEqual(actual, expected)
        self.assertTrue(any('FROM target_dictionary' in sql for sql in statements))

    def test_large_requests_are_batched_without_changing_input_order(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executemany('INSERT INTO target_dictionary VALUES (?,?,?,?,?)', [
                (i, f'CHEMBL{i}', f'Target {i}', 'SINGLE PROTEIN', 'Homo sapiens')
                for i in range(10, 620)
            ])
        self.prepare()
        inputs = [f'chembl{i}' for i in reversed(range(10, 620))]
        result, statements = self.traced_resolution(inputs)
        self.assertEqual(list(result), inputs)
        self.assertTrue(all(info['is_chembl'] for info in result.values()))
        self.assertEqual(sum('FROM optilib_target_lookup AS lookup' in sql for sql in statements), 3)

    def test_offline_preparation_builds_chembl_and_skips_other_databases(self):
        other = self.database.parent / 'molport.db'
        with closing(sqlite3.connect(other)) as conn:
            conn.execute('CREATE TABLE compounds (compound TEXT)')
            conn.commit()
        script = Path(__file__).resolve().parents[1] / 'scripts/prepare_readonly_databases.py'
        completed = subprocess.run([sys.executable, str(script), '--directory', str(self.database.parent)],
                                   capture_output=True, text=True, check=True)
        self.assertIn('target identifiers', completed.stdout)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertIsNotNone(lookup_target_rows(conn, ['CHEMBL1']))
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        with closing(sqlite3.connect(other)) as conn:
            self.assertIsNone(lookup_target_rows(conn, ['CHEMBL1']))


if __name__ == '__main__':
    unittest.main()
