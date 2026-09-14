"""Import-safe worker tasks. All large values remain inside the child process."""
import csv
import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from pymoo.core.callback import Callback
from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting

from webapp.core.algorithm import (DrugLibraryProblem, build_smart_init, run_optimization,
                                   select_best_solution, find_knee_point, column_maximum)
from webapp.core.records import METADATA_COLUMNS
from webapp.core.resolution import resolve_targets
from webapp.core.storage import write_rows_excel
from . import ingestion, matrices

logger = logging.getLogger('optilib.jobs')


class Cancelled(Exception):
    pass


class PartialStop(Exception):
    pass


class Context:
    def __init__(self, runtime, job):
        self.runtime = runtime
        self.job = job
        self.directory = runtime.directory(job['sid'],job['id'])
        self.directory.mkdir(parents=True,exist_ok=True)
        self.last_check = 0
        self.stopping = False

    def checkpoint(self, allow_stop=False):
        if time.monotonic() - self.last_check >= .2:
            self.last_check = time.monotonic()
            status = self.runtime.job(self.job['id'])['status']
            self.stopping = status not in ('running','reserved')
        if self.stopping and not allow_stop:
            raise Cancelled()
        return False

    def progress(self, values):
        self.runtime.progress(self.job['id'],values)

    def reference(self, name=''):
        return str(Path(self.job['id'])/name)

    def artifact(self, reference):
        return self.runtime.artifact(self.job['sid'],reference)


class Progress(Callback):
    def __init__(self, context, problem, max_gen):
        super().__init__()
        self.context, self.problem, self.max_gen = context, problem, max_gen
        self.history = []
        self.partial = None

    def notify(self, algorithm):
        F, G = algorithm.pop.get('F'), algorithm.pop.get('G')
        feasible = G <= 0 if G.ndim == 1 else np.all(G <= 0,axis=1)
        values = F[feasible] if feasible.any() else F
        self.history.append({'generation':int(algorithm.n_gen),
                             'best_selectivity':float(-values[:,0].min()*self.problem.pool_baseline_score),
                             'best_cost':float(values[:,1].min()*self.problem.pool_total_cost)})
        self.context.progress({'generation':int(algorithm.n_gen),'max_gen':self.max_gen,'history':self.history})
        self.context.checkpoint(allow_stop=True)
        if self.context.stopping:
            if feasible.any():
                front = NonDominatedSorting().do(F[feasible],only_non_dominated_front=True)
                self.partial = SimpleNamespace(X=algorithm.pop.get('X')[feasible][front].astype(bool),F=F[feasible][front].copy())
            raise PartialStop()


def selection(context, dataset_dir, solutions_dir, index, problem=None):
    matrix, prices, description = matrices.open_dataset(dataset_dir)
    choices = np.load(solutions_dir/'choices.npy',mmap_mode='r',allow_pickle=False)
    if not 0 <= index < len(choices):
        raise ValueError('Invalid solution index.')
    chosen = np.asarray(choices[index],dtype=bool)
    indices = np.flatnonzero(chosen)
    maxima = column_maximum(matrix,chosen)
    columns = np.flatnonzero(maxima>0)
    if problem is None:
        params = json.loads((solutions_dir/'solutions.json').read_text())['parameters']
        problem = DrugLibraryProblem(matrix,prices,weight_mean=params['weight_mean'],allowed_miss_pct=params['allowed_miss_pct'],prepared_score_budget=0)
    np.save(context.directory/'rows.npy',indices,allow_pickle=False)
    np.save(context.directory/'columns.npy',columns,allow_pickle=False)
    stats = []
    low, high = None, None
    # One column at a time: at most 100,000 numeric values are copied.
    for column in columns:
        context.checkpoint(allow_stop=context.job['kind']=='optimization')
        values = matrix[indices,column]
        values = values[np.isfinite(values)]
        if len(values):
            mn,mx,median = float(values.min()),float(values.max()),float(np.median(values))
            stats.append({'target':description['targets'][column],'min':mn,'median':median,'max':mx})
            low = mn if low is None else min(low,mn)
            high = mx if high is None else max(high,mx)
    def metrics(cost, mean, minimum, rows, cols):
        return {'total_cost':int(round(cost)),'mean_selectivity':round(mean,2),'min_selectivity':round(minimum,2),'num_drugs':int(rows),'num_targets':int(cols)}
    pool = metrics(problem.pool_total_cost,problem.pool_mean_sel,problem.pool_min_sel,problem.num_drugs,problem.num_targets)
    best = maxima[columns]
    library = metrics(float(prices[chosen].sum()),float(best.mean()) if len(best) else 0.,float(best.min()) if len(best) else 0.,len(indices),len(columns))
    percentages = {key:round(library[field]/pool[field]*100,1) if pool[field] else 0 for key,field in (
        ('cost','total_cost'),('mean_selectivity','mean_selectivity'),('min_selectivity','min_selectivity'),('targets','num_targets'),('drugs','num_drugs'))}
    comparison = {'pool':pool,'library':library,'percentages':percentages,'has_custom_affinity':description['custom']}
    result = {'comparison':comparison,'selected_idx':int(index),'shape':[len(indices),len(columns)],'zmin':low or 0.,'zmax':high if high is not None else 1.,'distribution':stats}
    (context.directory/'selection.json').write_text(json.dumps(result,allow_nan=False))
    return result


def optimize(context, payload, snapshot):
    dataset_dir = context.artifact(snapshot['dataset'])
    matrix,prices,description = matrices.open_dataset(dataset_dir)
    params = dict(payload)
    problem = DrugLibraryProblem(matrix,prices,weight_mean=params['weight_mean'],allowed_miss_pct=params['allowed_miss_pct'],prepared_score_budget=0)
    context.progress({'generation':0,'max_gen':params['max_gen'],'history':[]})
    seeds = build_smart_init(matrix,prices,pop_size=params['pop_size'],seed=1)
    cb = Progress(context,problem,params['max_gen'])
    partial = False
    try:
        result,_ = run_optimization(problem,seeds,pop_size=params['pop_size'],seed=1,max_gen=params['max_gen'],ftol=params['ftol'],period=params['term_period'],mutation_multiplier=params['mutation_multiplier'],callback=cb)
    except PartialStop:
        if cb.partial is None:
            raise ValueError('Stopped before a feasible library was found. Relax the coverage constraint and try again.') from None
        result,partial = cb.partial,True
    best, front = select_best_solution(result,problem)
    choices = np.asarray(result.X,dtype=bool)
    if params.get('max_price') is not None:
        keep = front[:,1] <= params['max_price']
        if not keep.any():
            raise ValueError('No feasible library fits the price limit.')
        front,choices = front[keep],choices[keep]
        best = find_knee_point(front)
    np.save(context.directory/'choices.npy',choices,allow_pickle=False)
    solutions = {'points':front.tolist(),'best_idx':int(best),'weight_mean':params['weight_mean'],'weight_min':1-params['weight_mean'],'parameters':params,'partial':partial}
    (context.directory/'solutions.json').write_text(json.dumps(solutions,allow_nan=False))
    del result,choices,seeds
    chosen = selection(context,dataset_dir,context.directory,best,problem)
    return {'solutions':context.reference(),'selection':context.reference(),'exports':{}}, {'status':'complete','partial':partial,'selected_idx':chosen['selected_idx']}


def export(context, payload, snapshot):
    dataset_dir = context.artifact(snapshot['dataset'])
    matrix,prices,description = matrices.open_dataset(dataset_dir)
    if payload['which'] == 'library':
        selected = context.artifact(snapshot['selection'])
        rows = np.load(selected/'rows.npy',mmap_mode='r',allow_pickle=False)
        columns = np.load(selected/'columns.npy',mmap_mode='r',allow_pickle=False)
    else:
        rows,columns = range(matrix.shape[0]),range(matrix.shape[1])
    filename = payload['which'] + '.' + payload['format']
    headers = list(METADATA_COLUMNS)+[description['targets'][i] for i in columns]
    with closing(sqlite3.connect((dataset_dir/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        def values():
            for i in rows:
                context.checkpoint()
                name,cid,ik,smiles,price = db.execute('SELECT name,chembl_id,inchikey,smiles,price FROM compounds WHERE i=?',(int(i),)).fetchone()
                yield [name,cid,ik,smiles,price]+matrix[int(i),columns].tolist()
        temporary = context.directory/(filename+'.tmp')
        if payload['format'] == 'xlsx':
            write_rows_excel(headers,values(),temporary)
        else:
            with temporary.open('w',newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for v in headers])
                for row in values():
                    # Spreadsheet applications must not execute uploaded text.
                    writer.writerow([("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else '' if isinstance(v,float) and not np.isfinite(v) else v) for v in row])
        temporary.replace(context.directory/filename)
    exports = dict(snapshot.get('exports',{}))
    exports[payload['key']] = context.reference(filename)
    return {'exports':exports}, {'status':'complete','download_url':f"/api/download/{payload['which']}?format={payload['format']}"}


def execute(runtime, job):
    context = Context(runtime,job)
    payload,snapshot = job['spec']['payload'],job['spec']['snapshot']
    kind = job['kind']
    context.checkpoint()
    if kind in ('upload','edit'):
        previous = context.artifact(snapshot['uploads']) if snapshot.get('uploads') else None
        destination = context.directory/'uploads.sqlite'
        ingestion.initialize(destination,previous)
        upload_kind = payload['kind']
        if kind == 'upload':
            staged = [{**item,'path':context.directory/item['stored']} for item in payload['files']]
            ingestion.ingest(destination,staged,upload_kind,runtime.policy,context.checkpoint)
        else:
            ingestion.edit(destination,upload_kind,payload['operation'],payload.get('value',''))
        response = ingestion.summary(destination,upload_kind)
        if upload_kind == 'targets':
            resolved = resolve_targets(response,runtime.policy.chembl)
            def target_summary(targets):
                ids = list(dict.fromkeys(resolved[raw]['chembl_id'] for raw in targets if resolved[raw]['is_chembl']))
                matched = [raw for raw in targets if resolved[raw]['is_chembl']]
                unmatched = [raw for raw in targets if not resolved[raw]['is_chembl']]
                return {'total':len(targets),'matched':matched,'unmatched':unmatched,
                        'chembl_ids':ids,'chembl_map':{raw:resolved[raw]['chembl_id'] for raw in matched}}
            response = target_summary(response)
            response['uploaded_files'] = []
            with closing(sqlite3.connect(destination)) as db:
                for item in payload.get('files',[]):
                    targets = [row[0] for row in db.execute('SELECT target FROM targets WHERE file=? ORDER BY rowid',(item['name'],))]
                    response['uploaded_files'].append({'name':item['name'],**target_summary(targets)})
        else:
            uploaded = {item['name'] for item in payload.get('files',[])}
            response['uploaded_files'] = [item for item in response['all_files'] if item['name'] in uploaded]
        summaries = dict(snapshot.get('upload_summaries',{})); summaries[upload_kind] = response
        changes = {'uploads':context.reference('uploads.sqlite'),'upload_summaries':summaries}
        # Delete raw uploads only after the immutable SQLite snapshot is complete.
        for item in payload.get('files',[]):
            (context.directory/item['stored']).unlink(missing_ok=True)
    elif kind in ('chembl','affinity'):
        upload = context.artifact(snapshot['uploads']) if snapshot.get('uploads') else None
        description = matrices.build(context.directory,kind,payload,upload,runtime.policy,context.checkpoint,context.progress)
        changes = {'dataset':context.reference(),'dataset_info':{'ready':True,'num_drugs':description['shape'][0],'num_targets':description['shape'][1],'total_cost':description['total_cost'],'has_custom_affinity':description['custom']},'solutions':None,'selection':None,'exports':{}}
        response = {'status':'complete',**changes['dataset_info']}
    elif kind == 'optimization':
        changes,response = optimize(context,payload,snapshot)
    elif kind == 'selection':
        selected = selection(context,context.artifact(snapshot['dataset']),context.artifact(snapshot['solutions']),payload['index'])
        changes,response = {'selection':context.reference(),'exports':{}},{'status':'complete','selected_idx':selected['selected_idx']}
    elif kind == 'export':
        changes,response = export(context,payload,snapshot)
    else:
        raise ValueError('Unknown job kind.')
    runtime.complete(job['id'],changes,response)
