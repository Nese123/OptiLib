"""Public HTTP application: bounded responses and artifact references only."""
import json
import logging
import math
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from flask import Flask, g, request, session, render_template, send_file, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError, validate_csrf
from wtforms.validators import ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from .config import ROOT, Policy, MiB, configure_security
from .runtime import Runtime, Rejected

logger = logging.getLogger('optilib.http')


def integer(value, default, low, high):
    if value is None:
        return default
    if isinstance(value,bool):
        raise ValueError('Expected an integer.')
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or not low <= number <= high:
        raise ValueError(f'Expected an integer between {low} and {high}.')
    return int(number)


def number(body,key,default,low=None,high=None):
    value = body.get(key,default)
    if isinstance(value,bool):
        raise ValueError(f'{key} must be a finite number.')
    value = float(value)
    if not math.isfinite(value) or (low is not None and value<low) or (high is not None and value>high):
        raise ValueError(f'{key} is outside the allowed range.')
    return value


def create_app(policy=None):
    policy = policy or Policy()
    app = Flask(__name__,template_folder=str(ROOT/'webapp/templates'),static_folder=str(ROOT/'webapp/static'))
    configure_security(app)
    csrf = CSRFProtect(app)
    limiter = Limiter(get_remote_address,app=app,default_limits=[os.environ.get('RATE_LIMIT_DEFAULT','120 per minute')],storage_uri=os.environ.get('RATE_LIMIT_STORAGE_URL','memory://'))
    runtime = Runtime(policy)
    epoch = runtime.start_epoch()
    app.extensions.update(runtime=runtime,policy=policy,public_limiter=limiter)

    def current():
        if not hasattr(g,'optilib_session'):
            g.optilib_session = runtime.session(session.get('sid'),epoch)
            session['sid'] = g.optilib_session['sid']
        return g.optilib_session

    def state():
        return current()['state']

    def body():
        data = request.get_json(silent=False)
        if not isinstance(data,dict):
            raise ValueError('Expected a JSON object.')
        return data

    def require(key):
        value = state().get(key)
        if not value:
            raise Rejected('Build a dataset and run optimization first.',409)
        return runtime.artifact(current()['sid'],value)

    def metadata(directory,name):
        return json.loads((directory/name).read_text())

    def enqueue(kind,payload,*,ready=True,reserve=None):
        info = state().get('dataset_info',{})
        cells = info.get('num_drugs',0)*info.get('num_targets',0)
        job = runtime.admit(current()['sid'],request.remote_addr or '',kind,payload,cells=cells,reserve=reserve)
        if ready:
            (runtime.directory(current()['sid'],job)/'.ready').touch()
        return job

    def accepted(job):
        return {'status':'started','job_id':job,'status_url':f'/api/jobs/{job}'},202

    @app.before_request
    def request_id():
        g.request_id = uuid.uuid4().hex

    @app.after_request
    def headers(response):
        response.headers.update({'X-Content-Type-Options':'nosniff','X-Frame-Options':'SAMEORIGIN',
                                 'Referrer-Policy':'strict-origin-when-cross-origin','X-Request-ID':g.get('request_id',''),
                                 'Content-Security-Policy':"default-src 'self'; script-src 'self' https://cdn.plot.ly; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"})
        if request.path.startswith('/api/') or request.path=='/optimize':
            response.headers['Cache-Control']='private, no-store'
            response.vary.add('Cookie')
        if hasattr(g,'optilib_session'):
            response.headers['X-Optilib-Session']=g.optilib_session['sid']
        if response.status_code in (429,503):
            response.headers['Retry-After']='30'
        return response

    @app.errorhandler(Rejected)
    def rejection(exc):
        return {'error':str(exc),'status':exc.status},exc.status

    @app.errorhandler(CSRFError)
    def csrf_error(exc):
        return {'error':'Your security token expired. Refresh the page.','status':400},400

    @app.errorhandler(ValueError)
    def invalid(exc):
        return {'error':str(exc),'status':400},400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages={413:'Upload exceeds the 16 MiB request limit.',429:'Rate limit exceeded. Please wait before trying again.',400:'Invalid request.',404:'Not found.'}
        return {'error':messages.get(exc.code,exc.name),'status':exc.code},exc.code

    @app.errorhandler(Exception)
    def unexpected(exc):
        logger.exception('Request %s failed',g.get('request_id'))
        return {'error':'The request could not be completed. Please try again.','request_id':g.get('request_id'),'status':500},500

    @app.get('/')
    def home():
        return render_template('home.html')

    @app.get('/optimize')
    def optimize_page():
        return render_template('index.html',public_mode=True,session_id=current()['sid'])

    @app.get('/favicon.ico')
    @limiter.exempt
    def favicon():
        return send_file(ROOT/'webapp/static/favicon.svg')

    @app.get('/live')
    @limiter.exempt
    def live():
        return {'status':'alive'}

    readiness_cache = {'at':0,'checks':{}}

    @app.get('/health')
    @app.get('/api/health')
    @limiter.exempt
    def health():
        now = time.time()
        if now-readiness_cache['at']>15:
            checks={}
            for name,path,queries in (
                ('chembl',policy.chembl,[
                    'SELECT molregno,tid,selectivity_score FROM compound_target_selectivity LIMIT 0',
                    'SELECT assay_id,molregno,pchembl_value FROM activities LIMIT 0',
                    'SELECT assay_id,tid,confidence_score FROM assays LIMIT 0',
                    'SELECT molregno,chembl_id,pref_name FROM molecule_dictionary LIMIT 0',
                    'SELECT molregno,canonical_smiles,standard_inchi_key FROM compound_structures LIMIT 0',
                    'SELECT tid,chembl_id,pref_name,target_type,organism FROM target_dictionary LIMIT 0',
                    'SELECT component_id,tid FROM target_components LIMIT 0',
                    'SELECT component_id,component_synonym,syn_type FROM component_synonyms LIMIT 0',
                    'SELECT component_id,accession FROM component_sequences LIMIT 0',
                    'SELECT molregno,full_mwt FROM compound_properties LIMIT 0',
                    'SELECT molregno,parent_molregno FROM molecule_hierarchy LIMIT 0']),
                ('molport',policy.database/'molport.db',['SELECT INCHIKEY,PRICE_1MG,MOLPORTID FROM compounds LIMIT 0'])):
                try:
                    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                        for query in queries:
                            db.execute(query)
                        if name=='chembl':
                            row=db.execute("SELECT value FROM optilib_selectivity_metadata WHERE key='provenance'").fetchone()
                            provenance=json.loads(row[0])
                            if provenance.get('scoring_version')!='blended_boundary_average_v2' or not provenance.get('build_id'):
                                raise ValueError('Invalid provenance')
                    checks[name]=True
                except Exception:
                    checks[name]=False
            try:
                with tempfile.TemporaryFile(dir=runtime.root) as probe:
                    probe.write(b'ready');probe.flush()
                checks['storage']=True
            except OSError:
                checks['storage']=False
            readiness_cache.update(at=now,checks=checks)
        checks=dict(readiness_cache['checks'])
        with runtime.connect() as db:
            worker=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('worker_heartbeat','model_ok')"))
        checks['worker']=now-float(worker.get('worker_heartbeat','0'))<10
        checks['model']=worker.get('model_ok')=='1'
        healthy=all(checks.values())
        return {'status':'healthy' if healthy else 'degraded','timestamp':now,'checks':checks,'databases':{'chembl':checks.get('chembl',False),'molport':checks.get('molport',False)}},200 if healthy else 503

    @app.get('/api/jobs/<job_id>')
    @limiter.limit('300 per minute')
    def job_status(job_id):
        job=runtime.job(job_id,current()['sid'])
        return {key:job[key] for key in ('id','kind','status','progress','result','error')}

    @app.post('/api/upload-<kind>')
    @limiter.limit('30 per minute')
    def upload(kind):
        if kind not in ('targets','affinity','prices'):
            raise Rejected('Not found',404)
        files=request.files.getlist('files[]') or request.files.getlist('file')
        if not files or len(files)>20:
            raise ValueError('Upload between one and 20 files per request.')
        entries=[]
        for i,file in enumerate(files):
            name=secure_filename(file.filename or '')
            if len(name)>255:
                raise ValueError('Filenames must contain at most 255 characters.')
            if not name.lower().endswith(('.csv','.xlsx')):
                raise ValueError('Use CSV or Excel (.xlsx). Legacy .xls is not supported.')
            entries.append({'name':name,'stored':f'upload-{i}','size':0})
        job=enqueue('upload',{'kind':kind,'files':entries},ready=False,reserve=512*MiB)
        try:
            directory=runtime.directory(current()['sid'],job)
            total=0
            for file,item in zip(files,entries):
                with (directory/item['stored']).open('wb') as out:
                    while chunk:=file.stream.read(MiB):
                        total+=len(chunk);item['size']+=len(chunk)
                        if total>16*MiB:
                            raise Rejected('Upload exceeds the request limit.',413)
                        out.write(chunk)
            with runtime.connect() as db:
                spec=runtime.job(job)['spec'];spec['payload']['files']=entries
                db.execute('UPDATE jobs SET spec=? WHERE id=?',(json.dumps(spec),job))
            (directory/'.ready').touch()
        except Exception:
            runtime.fail(job,'Upload was not completed.')
            raise
        return accepted(job)

    @app.post('/api/clear-<kind>')
    @app.post('/api/remove-<kind>-<operation>')
    def edit_upload(kind,operation='clear'):
        kind={'price':'prices'}.get(kind,kind)
        if kind not in ('affinity','prices') or operation not in ('clear','file','compound','target') or (kind=='prices' and operation=='target'):
            raise Rejected('Not found',404)
        data=body() if operation!='clear' else {}
        value=data.get('filename',data.get('name',data.get(operation,'')))
        if not isinstance(value,str) or len(value)>4096:
            raise ValueError('Invalid identifier.')
        return accepted(enqueue('edit',{'kind':kind,'operation':operation,'value':value},reserve=512*MiB))

    @app.get('/api/uploads/<kind>')
    def uploads_page(kind):
        if kind not in ('affinity','prices'):
            raise Rejected('Not found',404)
        from .ingestion import summary
        limit=integer(request.args.get('limit'),100,1,500)
        offset=integer(request.args.get('offset'),0,0,policy.compounds)
        path=require('uploads')
        return summary(path,kind,offset,limit)

    @app.post('/api/build-matrix')
    @app.post('/api/build-matrix-from-affinity')
    def build():
        data=body()
        kind='affinity' if request.path.endswith('-from-affinity') else 'chembl'
        payload={'selectivity_threshold':number(data,'selectivity_threshold',.5),'remove_targets':data.get('remove_targets',True)}
        if not isinstance(payload['remove_targets'],bool):
            raise ValueError('remove_targets must be true or false.')
        if kind=='chembl':
            ids=data.get('chembl_ids',[])
            if not isinstance(ids,list) or not ids or not all(isinstance(v,str) and 0<len(v)<=64 for v in ids):
                raise ValueError('chembl_ids must be a list of target identifiers.')
            payload['chembl_ids']=list(dict.fromkeys(ids))
            policy.dimensions(0,len(payload['chembl_ids']))
        else:
            require('uploads')
        return accepted(enqueue(kind,payload))

    @app.post('/api/run')
    def run():
        require('dataset')
        data=body()
        params={key:number(data,key,default,low,high) for key,default,low,high in (
            ('weight_mean',.5,0,1),('allowed_miss_pct',.04,0,1),('mutation_multiplier',1.,0,None),('ftol',.0025,.0001,None))}
        params.update({key:integer(data.get(key),default,low,high) for key,default,low,high in (
            ('pop_size',100,5,500),('max_gen',1000,10,5000),('term_period',30,5,500))})
        params['max_price']=None if data.get('max_price') is None else number(data,'max_price',None,0)
        return accepted(enqueue('optimization',params))

    def latest(kinds):
        with runtime.connect() as db:
            row=db.execute('SELECT id FROM jobs WHERE sid=? AND kind IN ('+','.join('?' for _ in kinds)+') ORDER BY created DESC LIMIT 1',(current()['sid'],*kinds)).fetchone()
        return runtime.job(row['id']) if row else None

    @app.get('/api/pipeline-status')
    @limiter.limit('300 per minute')
    def pipeline_status():
        job=latest(('chembl','affinity'))
        data={'status':'idle','current_step':0,'total_steps':3,'step_label':'','detail':'','error':'','step_summaries':{}}
        if job:
            data.update(job['progress'] or {})
            data['status']='running' if job['status'] in ('reserved','running','stopping') else 'complete' if job['status']=='complete' else 'error'
            data['error']=job['error'] or ''
            if job['status']=='complete':
                info=state().get('dataset_info',{})
                data.update(current_step=3,detail=f"{info.get('num_drugs',0):,} compounds × {info.get('num_targets',0):,} targets")
        return data

    @app.get('/api/status')
    @limiter.limit('300 per minute')
    def status():
        job=latest(('optimization',))
        data={'status':'idle','generation':0,'max_gen':0,'history':[],'error':'','run_revision':0,'stop_requested':False}
        if job and job['created']>=state().get('opt_reset_at',0):
            data['max_gen']=job['spec']['payload'].get('max_gen',0)
            data.update(job['progress'] or {})
            revision=int(job['created']*1000000)
            since=integer(request.args.get('since_generation'),-1,-1,5000)
            if request.args.get('run_revision')!=str(revision):
                since=-1
            data['history']=[row for row in data['history'] if row['generation']>since]
            data.update(status='running' if job['status'] in ('reserved','running','stopping') else 'complete' if job['status']=='complete' else 'error',run_revision=revision,error=job['error'] or '',stop_requested=job['status']=='stopping')
        return data

    @app.get('/api/dataset-info')
    @limiter.limit('300 per minute')
    def dataset_info():
        return state().get('dataset_info',{'ready':False})

    @app.post('/api/stop-opt')
    def stop():
        runtime.stop(current()['sid'])
        return {'status':'stop_requested'}

    @app.post('/api/reset')
    def reset():
        runtime.retire(current()['sid'])
        g.pop('optilib_session',None);session.pop('sid',None)
        current()
        return {'status':'reset'}

    @app.post('/api/reset-opt')
    def reset_opt():
        sid=current()['sid']
        with runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE sid=? AND status IN ('reserved','running','stopping')",(sid,)).fetchone():
                raise Rejected('Stop the active computation before resetting.',409)
            value=dict(state());value.update(solutions=None,selection=None,exports={},opt_reset_at=time.time())
            db.execute('UPDATE sessions SET state=?,revision=revision+1 WHERE sid=?',(json.dumps(value),sid))
        return {'status':'reset'}

    @app.get('/api/pareto-data')
    def pareto():
        data=metadata(require('solutions'),'solutions.json')
        data.pop('parameters',None)
        data['selected_idx']=metadata(require('selection'),'selection.json')['selected_idx']
        return data

    @app.post('/api/select-solution')
    def select():
        solutions=metadata(require('solutions'),'solutions.json')
        index=integer(body().get('index'),0,0,len(solutions['points'])-1)
        return accepted(enqueue('selection',{'index':index}))

    def selected():
        import numpy as np
        directory=require('selection')
        return (np.load(directory/'rows.npy',mmap_mode='r',allow_pickle=False),np.load(directory/'columns.npy',mmap_mode='r',allow_pickle=False),metadata(directory,'selection.json'))

    def compound_page(rows,offset,limit):
        with closing(sqlite3.connect((require('dataset')/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            output=[]
            for index in rows[offset:offset+limit]:
                row=db.execute('SELECT name,chembl_id,inchikey,price FROM compounds WHERE i=?',(int(index),)).fetchone()
                output.append(dict(zip(('name','chembl_id','inchikey','price'),row)))
            return output

    @app.get('/api/results')
    def results():
        rows,cols,data=selected()
        offset=integer(request.args.get('offset'),0,0,policy.compounds)
        limit=integer(request.args.get('limit'),100,1,500)
        data['comparison']['library']['compounds']=compound_page(rows,offset,limit)
        return {'comparison':data['comparison'],'selected_idx':data['selected_idx'],'offset':offset,'limit':limit,'total':len(rows),'paged':True,'revision':state()['selection']}

    @app.get('/api/heatmap-data')
    def heatmap():
        import numpy as np
        rows,cols,selection=selected()
        ro=integer(request.args.get('row_offset'),0,0,len(rows))
        co=integer(request.args.get('column_offset'),0,0,len(cols))
        nr=integer(request.args.get('row_count'),20,1,100)
        nc=integer(request.args.get('column_count'),40,1,100)
        directory=require('dataset')
        description=metadata(directory,'dataset.json')
        matrix=np.load(directory/'matrix.npy',mmap_mode='r',allow_pickle=False)
        values=matrix[np.ix_(rows[ro:ro+nr],cols[co:co+nc])]
        labels=compound_page(rows,ro,nr)
        return {'paged':True,'row_offset':ro,'column_offset':co,'total_rows':len(rows),'total_columns':len(cols),
                'matrix':[[float(v) if math.isfinite(v) else None for v in row] for row in values],
                'targets':[description['targets'][i] for i in cols[co:co+nc]],
                'compounds':[r['chembl_id'] or r['inchikey'] or r['name'] for r in labels],
                'zmin':selection['zmin'],'zmax':selection['zmax'],'revision':state()['selection'],
                'distribution':selection['distribution']}

    @app.get('/api/download/<which>')
    def download(which):
        if which not in ('library','matrix'):
            raise Rejected('Not found',404)
        require('dataset')
        if which=='library':
            require('selection')
        fmt=request.args.get('format','xlsx')
        if fmt not in ('xlsx','csv'):
            raise ValueError('Choose XLSX or CSV.')
        revision=state().get('selection') if which=='library' else state()['dataset']
        key=f'{which}:{fmt}:{revision}'
        reference=state().get('exports',{}).get(key)
        if reference:
            path=runtime.artifact(current()['sid'],reference)
            if path.is_file():
                if os.environ.get('OPTILIB_ACCEL_REDIRECT','false').lower() in ('true','1','yes'):
                    # The Nginx location is internal; access was checked above.
                    response=Response(mimetype='text/csv' if fmt=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    response.headers['X-Accel-Redirect']=f"/_optilib_artifacts/{current()['sid']}/{reference}"
                    response.headers['Content-Disposition']=f'attachment; filename="{which}.{fmt}"'
                    return response
                return send_file(path,as_attachment=True,download_name=f'{which}.{fmt}')
        job=latest(('export',))
        if job and job['status'] in ('reserved','running','stopping') and job['spec']['payload']['key']==key:
            return accepted(job['id'])
        # Preparing a download consumes computation capacity, even though the
        # completed artifact itself remains an ordinary GET download.
        if app.config.get('WTF_CSRF_ENABLED', True):
            try:
                validate_csrf(request.headers.get('X-CSRFToken'))
            except ValidationError:
                raise CSRFError('A security token is required to prepare this download.') from None
        return accepted(enqueue('export',{'which':which,'format':fmt,'key':key}))

    return app
