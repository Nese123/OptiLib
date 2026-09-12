"""Downloads must keep seeing the previous workbook until its replacement is ready."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from webapp.core.algorithm import save_results


class AtomicExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'library.xlsx'
        self.previous = b'previous complete workbook'
        self.output.write_bytes(self.previous)
        self.frame = pd.DataFrame({
            'SMILES': ['CC', 'CCC'],
            'Compound_Name': ['A', 'B'],
            'Price_USD_per_mg': [10., 20.],
            'Target': [2., 1.],
        }).set_index('SMILES')
        self.result = SimpleNamespace(X=np.array([[True, False]]))

    def test_publishes_only_after_workbook_is_complete(self):
        replace = os.replace

        def check_and_replace(source, destination):
            self.assertEqual(Path(source).parent, self.output.parent)
            self.assertEqual(self.output.read_bytes(), self.previous)
            self.assertEqual(pd.read_excel(source)['Compound_Name'].tolist(), ['A'])
            replace(source, destination)

        with patch('webapp.core.algorithm.os.replace', side_effect=check_and_replace):
            save_results(self.result, 0, self.frame, self.output)
        self.assertEqual(pd.read_excel(self.output)['Compound_Name'].tolist(), ['A'])
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_writer_failure_preserves_previous_export_and_removes_partial_file(self):
        def fail_after_partial_write(path, **kwargs):
            Path(path).write_bytes(b'partial workbook')
            raise OSError('write failed')

        with patch.object(pd.DataFrame, 'to_excel', side_effect=fail_after_partial_write):
            with self.assertRaisesRegex(OSError, 'write failed'):
                save_results(self.result, 0, self.frame, self.output)
        self.assertEqual(self.output.read_bytes(), self.previous)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_publication_failure_preserves_previous_export_and_cleans_temporary_file(self):
        with patch('webapp.core.algorithm.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                save_results(self.result, 0, self.frame, self.output)
        self.assertEqual(self.output.read_bytes(), self.previous)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])


if __name__ == '__main__':
    unittest.main()
