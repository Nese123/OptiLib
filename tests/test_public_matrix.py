"""Compare disk-backed construction with the existing ChEMBL query semantics."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import numpy as np

import test_queries_storage as query_fixtures
from webapp.public.config import Policy
from webapp.public.matrices import build,open_dataset


class DiskMatrixTests(unittest.TestCase):
    def test_chembl_scores_metadata_and_missing_smiles_match(self):
        query_fixtures.CandidateQueryTests.setUp(self)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE optilib_selectivity_metadata(key TEXT,value TEXT)')
            db.execute('INSERT INTO optilib_selectivity_metadata VALUES (?,?)',('provenance',json.dumps({'scoring_version':'blended_boundary_average_v2','build_id':'fixture','h':5,'storage_decimals':4})))
            db.commit()
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(tmp);policy.chembl=self.database
            out=Path(tmp)/'build';out.mkdir()
            def prices(records,*args,**kwargs):return np.arange(1,len(records)+1,dtype=float),{}
            with patch('webapp.core.pricing._resolve_affinity_prices',side_effect=prices):
                description=build(out,'chembl',{'chembl_ids':['CHEMBL_T1','CHEMBL_T2'],'selectivity_threshold':.1,'remove_targets':False},None,policy,lambda:None,lambda value:None)
            matrix,prices,description=open_dataset(out)
            # Parent compound eligibility, alphabetical SMILES order and max
            # aggregation are unchanged; the missing-SMILES compound is omitted.
            np.testing.assert_array_equal(matrix,[[.2,.4],[.6,.1],[.5,.49]])
            self.assertEqual(description['targets'],['Target 1 (GENE1)','Target 2'])
            with closing(sqlite3.connect(out/'metadata.sqlite')) as db:
                self.assertEqual([r[0] for r in db.execute('SELECT chembl_id FROM compounds ORDER BY i')],['CHEMBL_M100','CHEMBL_M200','CHEMBL_M300'])
            self.assertIsInstance(matrix,np.memmap)
