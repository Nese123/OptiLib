"""Shared upload parsing preserves formats and route error responses."""

import io
import unittest

import pandas as pd

import test_app
from test_app import app_module
from webapp.core.uploads import read_upload_table


class UploadParsingTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp

    def test_csv_and_excel_keep_column_names_values_and_missing_data(self):
        expected = pd.DataFrame({'Compound': ['A', 'B'], 'Price': [2., float('nan')]})
        csv = io.BytesIO(expected.to_csv(index=False).encode())
        excel = io.BytesIO()
        expected.to_excel(excel, index=False)
        excel.seek(0)
        for source, name in ((csv, 'prices.CSV'), (excel, 'prices.XLSX')):
            with self.subTest(name=name):
                pd.testing.assert_frame_equal(read_upload_table(source, name), expected)

    def test_all_upload_routes_preserve_unsupported_and_read_error_responses(self):
        for endpoint in ('upload-targets', 'upload-affinity', 'upload-prices'):
            for name, content in (('bad.txt', b'unknown'), ('empty.csv', b'')):
                with self.subTest(endpoint=endpoint, name=name):
                    response = self.client.post('/api/' + endpoint, data={
                        'files[]': (io.BytesIO(content), name),
                    })
                    self.assertEqual(response.status_code, 400)
                    if name.endswith('.txt'):
                        self.assertEqual(response.json['error'],
                                         'Unsupported file type for bad.txt. Use CSV or Excel (.xlsx).')
                    else:
                        label = 'price file ' if endpoint == 'upload-prices' else ''
                        self.assertTrue(response.json['error'].startswith(f'Failed to read {label}{name}:'))
                    self.assertEqual(self.state['affinity_upload_state']['files'], {})
                    self.assertEqual(self.state['price_upload_state']['files'], {})


if __name__ == '__main__':
    unittest.main()
