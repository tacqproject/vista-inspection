#!/usr/bin/env python3
"""Persist monitoring/retry state for ONE explicitly authorized RAW inspection.

This process never edits source or deletes data. It can stop only a labelled
RAW worker after 45 minutes with neither CPU/GPU/IO nor log activity. Any
retry uses exactly the saved reconstruction parameters, at most twice.
"""
import argparse
import datetime
import json
import pathlib
import shutil
import sqlite3
import subprocess
import time

ROOT = pathlib.Path('/media/epr/ssd/vista-raw')
REPO = pathlib.Path('/home/epr/git/vista-inspection-raw')
DOCKER = '/usr/bin/docker'


def call(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=45, **kw)


def restart(inspection, params):
    # The token stays in the backend process's memory and is never logged.
    script = '''import asyncio,json,urllib.request,sys
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import User
from app.services.auth import create_token
async def auth():
 async with AsyncSessionLocal() as db:
  user=(await db.execute(select(User).where(User.role=='admin',User.is_active==True))).scalars().first()
  if user is None: raise RuntimeError('No active RAW administrator')
  return create_token(user)
p=json.loads(sys.stdin.readline())
r=urllib.request.Request('http://127.0.0.1:8001/api/inspections/'+p['inspection']+'/reprocess',
 data=json.dumps(p['params']).encode(),headers={'Authorization':'Bearer '+asyncio.run(auth()),'Content-Type':'application/json'})
with urllib.request.urlopen(r,timeout=20) as response:
 print(response.status)
'''
    command = [DOCKER, 'exec', '-i', '-w', str(REPO/'backend'), 'vista-raw-backend',
               str(REPO/'backend/.venv/bin/python'), '-c', script]
    result = call(*command, input=json.dumps({'inspection':inspection,'params':params})+'\n')
    if result.returncode or result.stdout.strip() != '202':
        raise RuntimeError('RAW restart request failed: '+result.stderr[-1200:])


def save(path, data):
    tmp=path.with_suffix('.pending');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--inspection',required=True)
    parser.add_argument('--params',type=pathlib.Path,required=True)
    parser.add_argument('--start',action='store_true')
    args=parser.parse_args()
    import uuid
    uuid.UUID(args.inspection)
    params_path=args.params.resolve(strict=True)
    if not params_path.is_relative_to(ROOT/'operations'):
        raise SystemExit('Parameters must be inside RAW operations')
    params=json.loads(params_path.read_text())
    monitor=ROOT/'operations'/('watch-'+args.inspection)
    monitor.mkdir(exist_ok=True)
    import fcntl
    lock=(monitor/'watch.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path=monitor/'state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'retries':0,'seen_processing':False}
    model=ROOT/'uploads/inspections'/args.inspection/'model'
    last_activity=time.monotonic()
    last_signature=None
    if args.start and not state.get('started'):
        restart(args.inspection,params)
        state['started']=True
        # A fast failure can occur before the first sampling pass. A 202
        # response already confirms that this authorized run was accepted.
        state['seen_processing']=True
        save(state_path,state)
    while True:
        try:
            with sqlite3.connect('file:'+str(ROOT/'state/vista.db')+'?mode=ro',uri=True,timeout=10) as db:
                row=db.execute('select status,progress,error_message,model_path from photo_sets where inspection_id=?',
                               (args.inspection,)).fetchone()
            if not row:
                raise RuntimeError('RAW inspection has no photo set')
            status,progress,error,model_path=row
            jobs=call(DOCKER,'ps','--filter','label=vista.instance=raw','--format','{{.Names}}').stdout.splitlines()
            jobs=[j for j in jobs if j.startswith('vista-raw-job-')]
            stats=[]
            if jobs:
                s=call(DOCKER,'stats','--no-stream','--format','{{json .}}',*jobs)
                stats=[json.loads(line) for line in s.stdout.splitlines() if line.startswith('{')]
            gpu=call('nvidia-smi','--query-gpu=utilization.gpu,memory.used','--format=csv,noheader,nounits').stdout.strip()
            latest=max((p.stat().st_mtime_ns for p in model.glob('*.log') if p.is_file()),default=0)
            # Broker's log changes at each individual stage; CPU/IO guard long
            # bundle adjustments which can legitimately stay at 52% for hours.
            broker=ROOT/'operations/broker.log'
            signature=[latest,broker.stat().st_size,[(s.get('Name'),s.get('BlockIO')) for s in stats]]
            cpu=sum(float(s.get('CPUPerc','0%').rstrip('%')) for s in stats)
            gpu_busy=float(gpu.split(',')[0] or '0') if gpu else 0
            if cpu>1 or gpu_busy>0 or signature!=last_signature:
                last_activity=time.monotonic()
            last_signature=signature
            sample={'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':status,
                    'progress':progress,'error':error,'model':model_path,'jobs':stats,'gpu':gpu,
                    'ssd_free_gb':round(shutil.disk_usage(ROOT).free/1e9,2)}
            with (monitor/'samples.jsonl').open('a') as out:
                out.write(json.dumps(sample,ensure_ascii=False)+'\n')
            state.update(last=sample)
            state.pop('monitor_error',None)
            if status=='processing':
                state.pop('needs_attention',None)
                state['seen_processing']=True
                if jobs and time.monotonic()-last_activity>45*60:
                    for name in jobs:
                        info=call(DOCKER,'inspect','--format','{{index .Config.Labels "vista.instance"}}',name)
                        command=call(DOCKER,'inspect','--format','{{json .Config.Cmd}}',name)
                        if (info.stdout.strip()=='raw' and name.startswith('vista-raw-job-')
                                and args.inspection in command.stdout):
                            call(DOCKER,'kill',name)
                    state['stalled_worker_stopped']=sample['time']
                    last_activity=time.monotonic()
            elif status=='completed' and state.get('seen_processing'):
                if not model_path or not (ROOT/'uploads'/model_path).is_file():
                    raise RuntimeError('Completed status without a model file')
                state['finished']=sample['time'];save(state_path,state)
                print('RAW reconstruction completed',flush=True)
                return
            elif status=='failed' and state.get('seen_processing'):
                transient=any(x in (error or '').lower() for x in
                              ('přerušen','interrupted','disconnected','kód 137','kód 125'))
                if transient and state['retries']<2 and sample['ssd_free_gb']>20:
                    state['retries']+=1;save(state_path,state)
                    print('Retrying interrupted RAW reconstruction',state['retries'],flush=True)
                    restart(args.inspection,params)
                    last_activity=time.monotonic()
                else:
                    state['needs_attention']=True;save(state_path,state)
                    print('RAW reconstruction needs diagnosis:',error,flush=True)
                    return
            save(state_path,state)
        except Exception as exc:
            state['monitor_error']=str(exc);save(state_path,state)
            print('Monitor:',exc,flush=True)
        time.sleep(45)


if __name__=='__main__':
    main()
