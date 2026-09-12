"""Identifier precedence and deterministic database resource cleanup."""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from test_app import app_module


class ResolutionTests(unittest.TestCase):
    def test_target_aliases_preserve_first_match_and_input_order(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            ('CHEMBL1', 'First target', 'GENE', 'ACC1', 'alias', 'GENE_SYMBOL'),
            ('CHEMBL2', 'Second target', 'GENE', 'ACC2', 'alias', 'GENE_SYMBOL'),
        ]
        inputs = ['gene', 'ALIAS', 'ACC2', 'chembl1', 'First target', 'unknown']
        with patch.object(app_module.sqlite3, 'connect', return_value=connection):
            result = app_module._resolve_targets(inputs)
        self.assertEqual(list(result), inputs)
        for identifier in ('gene', 'ALIAS', 'chembl1', 'First target'):
            self.assertEqual(result[identifier]['chembl_id'], 'CHEMBL1')
        self.assertEqual(result['ACC2']['chembl_id'], 'CHEMBL2')
        self.assertFalse(result['unknown']['is_chembl'])
        self.assertEqual(result['unknown']['canonical_name'], 'unknown')
        connection.close.assert_called_once_with()

    def test_failed_lookups_close_connections_and_preserve_fallbacks(self):
        for lookup in (app_module._resolve_targets, app_module._resolve_compounds,
                       app_module._lookup_molport_prices):
            with self.subTest(lookup=lookup.__name__):
                connection = sqlite3.connect(':memory:')
                with patch.object(app_module.sqlite3, 'connect', return_value=connection):
                    with self.assertLogs('optilib', level='WARNING'):
                        result = lookup(['unknown'])
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
                if isinstance(result, dict):
                    self.assertFalse(result['unknown']['is_chembl'])
                else:
                    self.assertEqual(result, ({}, {}))


if __name__ == '__main__':
    unittest.main()
