#!/usr/bin/env python3
"""Compare client headroom with a fixed one-core memcached server."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import time
import uuid
import run as launcher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-cores',type=int,default=1)
    parser.add_argument('--client-cores',type=int,default=34)
    parser.add_argument('--rates',type=int,nargs='+',default=[640000,960000,1280000])
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    c=launcher.load_config(launcher.TEMPLATE)
    c['allocation'].update(use_smt=False,memcached_cores=args.server_cores,memcached_guaranteed=args.server_cores,client_cores=args.client_cores)
    c['suite'].update(warmup_seconds=10,measurement_seconds=30,repetitions=1)
    c['logging'].update(run_with_logging=False,run_without_logging=True)
    c['conditions']={name:name=='memcached_alone' for name in launcher.CONDITIONS}
    c['calibration']['enabled']=False
    if any(c['interventions'].values()):raise ValueError('Interventions must be off')
    if any(r<=0 for r in args.rates):raise ValueError('Rates must be positive')
    launcher.validate_config(c)
    if not args.run:
        print(json.dumps(dict(server_physical_cores=args.server_cores,client_physical_cores=args.client_cores,rates=args.rates)))
        return
    root=launcher.RESULTS/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-client-headroom-'+uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    launcher.say('Results: '+str(root))
    launcher.save(root/'config.json',c)
    shutil.copy2(__file__,root/'client_headroom.py')
    shutil.copy2(launcher.__file__,root/'launcher.py')
    shutil.copy2(launcher.REPO/'apps/loadgen/src/main.rs',root/'loadgen-main.rs')
    rows=[]
    for rate in args.rates:
        run=dict(id=f'{rate}rps',condition='memcached_alone',rps=rate,logging=False,fraction=1.0,repetition=1)
        launcher.say(f'Testing {rate} RPS: server {args.server_cores} cores, client {args.client_cores} cores')
        try:
            result=launcher.run_one(copy.deepcopy(c),root,run,10,30)
        except RuntimeError:
            path=root/run['id']/'latest.json'
            result=json.loads(path.read_text()) if path.exists() else {}
            if not result.get('summary') or not result.get('validation_issues') or any(
                    not e.startswith('Validation:') for e in result.get('errors',[])):
                raise
        row=dict(offered_rps=rate,state=result['state'],summary=result['summary'],
                 validation_issues=result.get('validation_issues',[]))
        rows.append(row)
        launcher.save(root/'summary.json',rows)
        launcher.say(json.dumps(row))
    launcher.say('Complete: '+str(root/'summary.json'))


if __name__=='__main__':main()
