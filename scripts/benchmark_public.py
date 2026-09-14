"""Standalone sizing harness; generated matrices are temporary, never source data.

Run in the computation container for meaningful deployment resource measurements:
python scripts/benchmark_public.py --rows 100000 --columns 1000 --density .05
Repeat with --density 1. Use --soak-seconds 14400 for a sustained optimizer run.
Results report observed timings and RSS, not a convergence guarantee.
"""
import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[key]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from pymoo.core.callback import Callback
from webapp.core.algorithm import DrugLibraryProblem,build_smart_init,run_optimization


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows',type=int,default=100000)
    parser.add_argument('--columns',type=int,default=1000)
    parser.add_argument('--density',type=float,default=.05)
    parser.add_argument('--generations',type=int,default=10)
    parser.add_argument('--population',type=int,default=20)
    parser.add_argument('--soak-seconds',type=int,default=0)
    parser.add_argument('--directory',default=None)
    args=parser.parse_args()
    if not 0<args.density<=1 or not 1<=args.rows<=100000 or not 2<=args.columns<=1000:
        parser.error('Dimensions or density exceed the public limits.')
    start=time.monotonic()
    with tempfile.TemporaryDirectory(dir=args.directory) as temp:
        matrix=np.lib.format.open_memmap(Path(temp)/'matrix.npy',mode='w+',dtype='float64',shape=(args.rows,args.columns))
        rng=np.random.default_rng(42)
        for i in range(0,args.rows,256):
            block=rng.uniform(-1,5,(min(256,args.rows-i),args.columns))
            if args.density<1:
                block[rng.random(block.shape)>args.density]=np.nan
            matrix[i:i+len(block)]=block
        matrix.flush()
        del matrix
        matrix=np.load(Path(temp)/'matrix.npy',mmap_mode='r')
        prices=rng.uniform(1,100,args.rows)
        generated=time.monotonic()
        problem=DrugLibraryProblem(matrix,prices,allowed_miss_pct=.04,prepared_score_budget=0)
        seeds=build_smart_init(matrix,prices,args.population)
        initialized=time.monotonic()
        class Progress(Callback):
            def notify(self,algorithm):
                if algorithm.n_gen==1 or algorithm.n_gen%10==0:
                    print(json.dumps({'generation':int(algorithm.n_gen),'elapsed_seconds':round(time.monotonic()-start,2)}),flush=True)
        runs=0
        while True:
            result,elapsed=run_optimization(problem,seeds,pop_size=args.population,max_gen=args.generations,callback=Progress())
            if result.F is None:
                raise RuntimeError('Fixture optimization did not produce feasible solutions')
            runs+=1
            if not args.soak_seconds or time.monotonic()-initialized>=args.soak_seconds:
                break
        print(json.dumps({'rows':args.rows,'columns':args.columns,'density':args.density,'population':args.population,
                          'generations_per_run':args.generations,'runs':runs,'matrix_bytes':matrix.nbytes,
                          'generation_seconds':round(generated-start,3),'initialization_seconds':round(initialized-generated,3),
                          'optimization_seconds':round(time.monotonic()-initialized,3),
                          'peak_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2)},indent=2),flush=True)


if __name__=='__main__':main()
